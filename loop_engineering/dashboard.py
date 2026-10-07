"""Read-only local dashboard: ``loop dashboard`` serves 127.0.0.1 and polls the files.

Shows the goal summary plus the activity behind it: every agent turn (the prompt the
controller sent, the agent's messages, commands with output, tool calls, file edits and
token usage), the runner log and each check's output. Nothing here can change state.
"""

from __future__ import annotations

import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import engine, status, transcript
from .store import Project
from .util import LoopError, parse_time, read_tail

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Loop status</title>
<style>
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e4e4df;--soft:#f1f1ec;--ok:#1f7a4d;--bad:#b3261e;--warn:#9a6700;--run:#2457c5}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe6;--muted:#a3a29b;--card:#1f1f1d;--line:#33332f;--soft:#262624;--ok:#4cc38a;--bad:#ff6b5e;--warn:#e3b341;--run:#7aa2ff}}
*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,system-ui,Segoe UI,sans-serif}
main{max-width:1280px;margin:0 auto;padding:20px 16px}h1{font-size:20px;margin:0 0 4px}h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 8px}
.row{display:flex;gap:12px;flex-wrap:wrap}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;flex:1 1 280px;min-width:0}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-weight:600;font-size:12px;border:1px solid currentColor}
.done,.pass,.idle,.ok{color:var(--ok)}.blocked,.fail,.failed,.error,.timeout,.limit{color:var(--bad)}.waiting,.paused,.cancelled,.interrupted{color:var(--warn)}.running,.active,.ready{color:var(--run)}
ul{margin:0;padding-left:18px}li{margin:2px 0}code{font-size:12px}.muted{color:var(--muted)}
pre{white-space:pre-wrap;word-break:break-word;overflow:auto;max-height:480px;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;margin:0}
select,button{font:inherit;padding:4px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
.bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden}.bar>i{display:block;height:100%;background:var(--ok)}
.tabs{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}.tabs button.on{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.split{display:flex;gap:12px;min-height:420px}.list{flex:0 0 280px;max-height:70vh;overflow:auto;border-right:1px solid var(--line);padding-right:8px}
.list div{padding:6px 8px;border-radius:6px;cursor:pointer;margin-bottom:2px}.list div:hover{background:var(--soft)}.list div.sel{background:var(--soft);outline:1px solid var(--line)}
.view{flex:1;min-width:0;max-height:70vh;overflow:auto}
.ev{border:1px solid var(--line);border-radius:8px;margin:0 0 6px;background:var(--card)}.ev>summary{padding:6px 10px;cursor:pointer;list-style:none;display:flex;gap:8px;align-items:baseline}
.ev>summary::-webkit-details-marker{display:none}.ev pre{padding:8px 10px;border-top:1px solid var(--line);background:var(--soft)}
.k{font-size:11px;font-weight:700;text-transform:uppercase;min-width:64px}.t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.prompt{border-left:3px solid var(--warn)}.instruction{border-left:3px solid var(--warn)}.reasoning{border-left:3px solid var(--muted)}.sub{margin:10px 0 0;padding:8px;border:1px dashed var(--line);border-radius:8px}.sub>summary{cursor:pointer;font-weight:600}
.message{border-left:3px solid var(--run)}.message pre{background:var(--card);font:14px/1.5 -apple-system,system-ui,sans-serif}
@media (max-width:760px){.split{flex-direction:column}.list{flex:none;max-height:200px;border-right:0;border-bottom:1px solid var(--line)}}
</style></head><body><main>
<div class="row" style="align-items:center;justify-content:space-between;margin-bottom:12px"><div><h1 id="title">Loop</h1><div id="reason" class="muted"></div></div>
<div><select id="goal"></select> <span id="beat" class="muted"></span></div></div>
<div class="row"><div class="card"><h2>Status</h2><div id="status"></div></div><div class="card"><h2>Next</h2><div id="next"></div></div><div class="card"><h2>Budget</h2><div id="budget"></div></div></div>
<div class="card" id="pipecard" style="margin-top:12px" hidden><h2>Pipeline</h2><div id="pipeline" class="row" style="gap:6px"></div></div>
<div class="row" style="margin-top:12px"><div class="card"><h2>Tasks</h2><div id="tasks"></div></div><div class="card"><h2>Goal queue</h2><ul id="queue"></ul></div><div class="card"><h2>Acceptance &amp; reviews</h2><ul id="checks"></ul><h2 style="margin-top:12px">Questions</h2><ul id="questions"></ul></div></div>
<div class="card" style="margin-top:12px"><div class="tabs"><button data-tab="turns" class="on">Agent turns</button><button data-tab="runner">Runner log</button><button data-tab="checkl">Check logs</button><button data-tab="md">STATUS.md</button><span style="flex:1"></span><button id="latest" title="Jump to the newest entry">↓ 最新 latest</button></div>
<div id="pane-turns" class="split"><div class="list" id="turnlist"></div><div class="view" id="turnview"><p class="muted">Select a turn.</p></div></div>
<div id="pane-runner" hidden><pre id="runnerlog"></pre></div>
<div id="pane-checkl" hidden class="split"><div class="list" id="checklist"></div><div class="view"><pre id="checkview" class="muted">Select a check.</pre></div></div>
<div id="pane-md" hidden><pre id="md"></pre></div></div>
</main><script>
const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let goal=new URLSearchParams(location.search).get('goal')||'',tab='turns',selTurn=null,selCheck=null,lastTurnKey='',jump=true,lastRunner='',lastTurnCount=0,followLatest=true;
const nearEnd=el=>el.scrollTop+el.clientHeight>=el.scrollHeight-60;const toEnd=el=>{el.scrollTop=el.scrollHeight};
const q=()=>goal?'goal='+encodeURIComponent(goal):'';
async function get(p){const r=await fetch(p);return r.json()}
async function tick(){try{const d=await get('/api/status?'+q());
if(d.error){$('title').textContent='Loop';$('reason').textContent=d.error;return}
const s=d.summary;goal=s.goal;$('title').textContent=s.title;$('reason').textContent=s.reason;
$('goal').innerHTML=d.goals.map(g=>`<option ${g===s.goal?'selected':''}>${esc(g)}</option>`).join('');
$('status').innerHTML=`<span class="pill ${esc(s.status)}">${esc(s.status)}</span> <span class="muted">${esc(s.kind)} · runner ${esc(s.runner.state)}</span>`+(s.blocker?`<p class="blocked">Blocked (${esc(s.blocker.category)}): ${esc(s.blocker.reason)}</p>`:'');
$('next').innerHTML=`<b>${esc(s.next.kind)}</b> <span class="muted">(${esc(s.next.actor)})</span><div>${esc(s.next.summary)}</div>`;
const t=s.tasks,pct=t.total?Math.round(100*t.done/t.total):0;
$('budget').innerHTML=`<div>turn ${s.iteration} / ${s.max_iterations}</div><div>agent time ${(s.usage.agent_seconds/60).toFixed(1)} min · tokens in ${(s.usage.input_tokens||0).toLocaleString()} · out ${(s.usage.output_tokens||0).toLocaleString()}</div><div>cost $${(s.usage.cost_usd||0).toFixed(2)}${s.usage.cost_complete?'':' (partial)'}</div><div class="muted">updated ${esc(s.updated_at)}</div>`;
$('tasks').innerHTML=`<div class="bar"><i style="width:${pct}%"></i></div><div class="muted">${t.done}/${t.total} done · ${t.active} active · ${t.blocked} blocked</div>${t.total&&t.done+t.dropped===t.total&&!['done','failed','stopped'].includes(s.status)?`<div class="running">All tasks finished — now in the ${({verify:'acceptance check',review:'independent review',repair:'repair'})[s.next.kind]||esc(s.next.kind)} phase: ${esc(s.next.summary)}</div>`:''}<ul>`+d.tasks.map(x=>`<li><span class="${esc(x.status)}">${esc(x.status)}</span> <code>${esc(x.id)}</code> ${esc(x.title)} <span class="muted">${esc(x.team||x.role||'')}</span></li>`).join('')+'</ul>';
$('checks').innerHTML=(Object.entries(s.acceptance).map(([k,v])=>`<li><span class="${esc(v)}">${esc(v)}</span> <code>${esc(k)}</code></li>`).join('')+(d.reviews||[]).map(r=>`<li><span class="${esc(r.verdict)}">${esc(r.verdict)}</span> <code>review.${esc(r.id)}</code> <span class="muted">(${esc(r.by)})</span></li>`).join(''))||'<li class="muted">none</li>';
$('pipecard').hidden=!s.pipeline;if(s.pipeline)$('pipeline').innerHTML=s.pipeline.map(p=>`<div style="flex:1 1 120px;border:1px solid var(--line);border-radius:8px;padding:6px 8px;${p.state==='current'?'outline:2px solid var(--run)':''}"><div class="${p.state==='done'?'ok':p.state==='current'?'running':'muted'}"><b>${p.state==='done'?'✓':p.state==='current'?'◐':'○'} ${esc(p.stage)}</b></div><div class="muted" style="font-size:12px">${esc(p.detail)}</div></div>`).join('');
$('queue').innerHTML=(d.queue||[]).map(r=>`<li class="${r.id===s.goal?'running':''}">${r.position}. <a href="?goal=${encodeURIComponent(r.id)}"><code>${esc(r.id)}</code></a> <span class="${esc(r.status)}">${esc(r.status)}</span>${r.status!=='done'&&!r.approved?' <span class="waiting">needs approval</span>':''}<div class="muted">${esc(r.title)}</div></li>`).join('')||'<li class="muted">empty — <code>loop goal … --enqueue</code></li>';
$('questions').innerHTML=s.open_questions.map(q=>`<li><code>${esc(q.id)}</code> ${esc(q.text)}<div class="muted"><code>loop answer ${esc(q.id)} "…"</code></div></li>`).join('')||'<li class="muted">none</li>';
$('md').textContent=d.markdown;$('beat').textContent=s.runner.heartbeat_age_s!=null?`heartbeat ${s.runner.heartbeat_age_s}s ago`:'';
await activity();}catch(e){$('reason').textContent='dashboard: '+e}}
async function activity(){
 if(tab==='turns'){const d=await get('/api/turns?'+q());
  const list=d.turns.slice().reverse();  // oldest at top, newest at the bottom
  const newest=list.length?list[list.length-1].n:null;
  // Follow the newest turn while you are viewing the newest one; a click on an older turn stops following.
  if(newest!==null&&(selTurn===null||(followLatest&&newest!==selTurn))){selTurn=newest;lastTurnKey='';jump=true}
  followLatest=selTurn===newest;
  const tl=$('turnlist'),listAtEnd=nearEnd(tl)||list.length!==lastTurnCount;lastTurnCount=list.length;
  tl.innerHTML=list.map(x=>`<div class="${x.n===selTurn?'sel':''}" data-n="${x.n}"><b>#${x.n}</b> ${esc(x.action)} <span class="${esc(x.outcome)}">${esc(x.outcome)}</span>${x.model||x.adapter?`<div class="muted" style="font-size:11px">${esc(x.adapter||'')}${x.model?' / '+esc(x.model):''}${x.effort?' @'+esc(x.effort):''}${x.role?' · '+esc(x.role):''}</div>`:''}<div class="muted">${x.seconds!=null?Math.round(x.seconds/60)+' min · ':''}${x.progress===false?'no progress · ':''}${esc((x.summary||'').slice(0,90))}</div></div>`).join('')||'<p class="muted">No turns yet.</p>';
  if(listAtEnd)toEnd(tl);
  if(selTurn!==null){const v=await get(`/api/turn?${q()}&n=${selTurn}`);const key=selTurn+':'+v.entries.length+':'+JSON.stringify(v.entries.slice(-1))+':'+(v.subagents||[]).map(a=>a.entries.length).join(',');
   if(key!==lastTurnKey){const view=$('turnview'),atEnd=jump||nearEnd(view);const open=new Set([...view.querySelectorAll('details[open]')].map(e=>e.dataset.i));
    const ev=(e,i,p)=>`<details class="ev ${esc(e.kind)}" data-i="${p}${i}" ${e.kind==='message'||open.has(p+i)?'open':''}><summary><span class="k ${esc(e.status||'')}">${esc(e.kind)}</span><span class="t">${esc(e.title)}</span><span class="${esc(e.status||'')}">${esc(e.status||'')}</span></summary>${e.body?`<pre>${esc(e.body)}</pre>`:''}</details>`;
    const subs=(v.subagents||[]).map((a,j)=>`<details class="sub" data-i="s${j}" ${open.has('s'+j)?'open':''}><summary>🧩 Sub-agent ${esc(a.name||a.id)} <span class="muted">${esc(a.agent_path||'')} · ${a.entries.length} events</span></summary>${a.entries.map((e,i)=>ev(e,i,'s'+j+'-')).join('')||'<p class="muted">No readable events.</p>'}</details>`).join('');
    view.innerHTML=`<details class="ev prompt" data-i="p" ${open.has('p')?'open':''}><summary><span class="k">→ prompt</span><span class="t">Instruction the controller sent to the model this turn (${v.prompt.length.toLocaleString()} chars)</span></summary><pre>${esc(v.prompt)}</pre></details>`+v.entries.map((e,i)=>ev(e,i,'')).join('')+(subs?`<h2 style="margin-top:14px">Sub-agents (${v.subagents.length})</h2>`+subs:'')+(v.running?'<p class="running">… turn in progress (auto-refreshing)</p>':'');
    if(atEnd)toEnd(view);jump=false;lastTurnKey=key}}}
 if(tab==='runner'){const v=await get('/api/log?'+q()),el=$('runnerlog');if(v.text!==lastRunner){const end=jump||nearEnd(el);el.textContent=v.text||'(empty)';lastRunner=v.text;if(end)toEnd(el)}jump=false}
 if(tab==='checkl'){const d=await get('/api/checks?'+q());const cl=$('checklist');cl.innerHTML=d.checks.slice().reverse().map(c=>`<div class="${c.id===selCheck?'sel':''}" data-c="${esc(c.id)}"><span class="${esc(c.status)}">${esc(c.status)}</span> <code>${esc(c.id)}</code><div class="muted">${esc(c.finished_at||'')} · exit ${esc(c.exit_code)}</div></div>`).join('')||'<p class="muted">No checks have run.</p>';
  if(jump)toEnd(cl);
  if(selCheck){const v=await get(`/api/check?${q()}&id=${encodeURIComponent(selCheck)}`),el=$('checkview'),end=jump||nearEnd(el.parentElement);el.textContent=v.text;if(end)toEnd(el.parentElement)}jump=false}}
document.querySelector('.tabs').onclick=e=>{const b=e.target.closest('button');if(!b)return;tab=b.dataset.tab;document.querySelectorAll('.tabs button').forEach(x=>x.classList.toggle('on',x===b));
 for(const p of ['turns','runner','checkl','md'])$('pane-'+p).hidden=p!==tab;lastTurnKey='';lastRunner='';jump=true;activity()};
$('latest').onclick=()=>{jump=true;lastTurnKey='';lastRunner='';if(tab==='turns'){selTurn=null;followLatest=true}activity();if(tab==='md')toEnd(document.scrollingElement)};
$('turnlist').onclick=e=>{const d=e.target.closest('[data-n]');if(d){const all=[...$('turnlist').querySelectorAll('[data-n]')];selTurn=+d.dataset.n;followLatest=d===all[all.length-1];lastTurnKey='';jump=true;activity()}};
$('checklist').onclick=e=>{const d=e.target.closest('[data-c]');if(d){selCheck=d.dataset.c;jump=true;activity()}};
$('goal').onchange=e=>{goal=e.target.value;selTurn=null;history.replaceState(null,'','?goal='+encodeURIComponent(goal));tick()};
tick();setInterval(tick,3000);
</script></body></html>"""


def payload(project: Project, goal_id: str | None) -> dict:
    try:
        store = project.goal(goal_id or None)
    except Exception as exc:
        return {"error": str(exc), "goals": project.goal_ids()}
    state, goal = store.state(), store.goal()
    from . import queue as queue_mod
    return {"summary": status.summary(store), "goals": project.goal_ids(), "markdown": status.markdown(store),
            "queue": queue_mod.view(project),
            "tasks": [{**{k: t.get(k) for k in ("id", "title", "status", "role")},
                       "team": " + ".join(f"{s['count']}×{s['role']}" for s in t.get("team", []))}
                      for t in state["tasks"]],
            "reviews": [{"id": g["id"], "by": g["by"], "verdict": (state["reviews"].get(g["id"]) or {}).get("verdict",
                                                                                                        "pending")}
                        for g in engine.review_gates(goal)]}


def turns(store) -> dict:
    state = store.state()
    rows = {t["n"]: {k: t.get(k) for k in ("n", "action", "outcome", "seconds", "progress", "summary", "adapter",
                                           "model", "effort", "role")}
            for t in state["turns"]}
    runner = store.runner() or {}
    current = runner.get("turn") if not runner.get("exited_at") else None
    if current:
        rows[current["n"]] = {"n": current["n"], "action": current["action"], "outcome": "running",
                              "seconds": None, "progress": None, "summary": "in progress"}
    for directory in store.runs.glob("turn-*"):  # turns recorded only on disk (e.g. interrupted)
        try:
            n = int(directory.name.split("-")[1])
        except (IndexError, ValueError):
            continue
        rows.setdefault(n, {"n": n, "action": "?", "outcome": "unrecorded", "seconds": None, "progress": None,
                            "summary": ""})
    return {"turns": sorted(rows.values(), key=lambda r: r["n"], reverse=True)}


_SUBAGENT_CACHE: dict = {}


def turn(store, n: int) -> dict:
    directory = store.runs / f"turn-{n:04d}"
    if not directory.is_dir():
        raise LoopError(f"No turn {n}")
    runner = store.runner() or {}
    running = bool((runner.get("turn") or {}).get("n") == n and not runner.get("exited_at"))
    prompt = (directory / "prompt.md").read_text(encoding="utf-8", errors="replace") if (
        directory / "prompt.md").is_file() else ""
    record = next((t for t in store.state()["turns"] if t["n"] == n), None)
    started = parse_time((record or {}).get("started_at") or (runner.get("turn") or {}).get("started_at"))
    key = (str(directory), n)
    if running or key not in _SUBAGENT_CACHE:
        try:
            _SUBAGENT_CACHE[key] = transcript.subagents(directory, started)
        except OSError:
            _SUBAGENT_CACHE[key] = []
    return {"n": n, "running": running, "prompt": prompt[:40000],
            "entries": transcript.parse(directory / "stdout.log", directory / "stderr.log"),
            "subagents": _SUBAGENT_CACHE[key]}


def check_rows(store) -> dict:
    state = store.state()
    rows = [{"id": k, **{x: v.get(x) for x in ("status", "exit_code", "finished_at", "duration_ms")}}
            for k, v in state["checks"].items()]
    return {"checks": sorted(rows, key=lambda r: r.get("finished_at") or "", reverse=True)}


def check_log(store, check_id: str) -> dict:
    result = store.state()["checks"].get(check_id)
    if not result:
        raise LoopError(f"Unknown check {check_id}")
    base = (store.project.root / result["log"]).resolve()
    if store.runs.resolve() not in base.parents:  # only ever read inside this goal's runs/
        raise LoopError("Check log is outside the goal's run directory")
    parts = []
    for name in ("stdout.log", "stderr.log"):
        text = read_tail(base / name, 60000)
        if text.strip():
            parts.append(f"===== {name} =====\n{text}")
    header = (f"{check_id}: {result['status']} · exit {result.get('exit_code')} · {result.get('duration_ms')} ms · "
              f"finished {result.get('finished_at')}\nlog: {result['log']}\n\n")
    return {"text": header + ("\n\n".join(parts) or "(no output)")}


def serve(project: Project, port: int = 8765, host: str = "127.0.0.1") -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            goal = (query.get("goal") or [None])[0] or None
            try:
                if url.path in {"/", "/index.html"}:
                    return self._send(PAGE.encode(), "text/html; charset=utf-8")
                if url.path == "/api/status":
                    data = payload(project, goal)
                else:
                    store = project.goal(goal)
                    if url.path == "/api/turns":
                        data = turns(store)
                    elif url.path == "/api/turn":
                        data = turn(store, int((query.get("n") or ["0"])[0]))
                    elif url.path == "/api/log":
                        data = {"text": read_tail(store.runs / "runner.log", 60000)}
                    elif url.path == "/api/checks":
                        data = check_rows(store)
                    elif url.path == "/api/check":
                        data = check_log(store, (query.get("id") or [""])[0])
                    else:
                        return self.send_error(404)
            except (LoopError, ValueError, OSError) as exc:
                data = {"error": str(exc), "entries": [], "turns": [], "checks": [], "text": str(exc), "prompt": ""}
            self._send(json.dumps(data, default=str, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _send(self, body: bytes, kind: str):
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass
    server = None
    for candidate in range(port, port + 30):  # one dashboard per project; skip ports already in use
        try:
            server = ThreadingHTTPServer((host, candidate), Handler)
            break
        except OSError as exc:
            if exc.errno not in {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)}:
                raise
    if server is None:
        raise LoopError(f"Ports {port}-{port + 29} are all in use; pass --port")
    actual = server.server_address[1]
    note = f" (port {port} was busy)" if actual != port else ""
    print(f"Loop dashboard for {project.root}: http://{host}:{actual}/{note}  (Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


__all__ = ["serve", "payload", "turns", "turn", "check_rows", "check_log", "Path"]
