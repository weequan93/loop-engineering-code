// Private Playwright worker. Assertions and authorized targets are fixed by the
// accepted execution plan; this worker receives no evaluator authority keys.
'use strict';
const fs = require('fs');
const path = require('path');

async function main() {
  const raw = fs.readFileSync(0);
  if (raw.length > 2097152) throw new Error('Executor input exceeds 2 MiB');
  const job = JSON.parse(raw.toString('utf8'));
  const plan = job.plan;
  const {chromium} = require(plan.playwright_module);
  const started = Date.now();
  const deadline = started + Math.min(plan.timeout_seconds * 1000, job.remaining_ms);
  let browser, context, page, tracing = false;
  const report = {bindings: job.request, plan_digest: job.plan_digest,
    result: 'inconclusive', steps: [], blocked_requests: [], artifacts: [],
    runtime: {node: process.version, playwright: require(path.join(plan.playwright_module, 'package.json')).version}};
  const left = () => { const value = deadline - Date.now(); if (value <= 0) throw new Error('Deadline exceeded'); return value; };
  const block = value => { if (report.blocked_requests.length < 64) report.blocked_requests.push(String(value).slice(0, 2000)); };
  const authorized = value => {
    try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password && plan.allowed_origins.includes(url.origin); }
    catch { return false; }
  };
  try {
    browser = await chromium.launch({headless: true, executablePath: plan.browser_executable, timeout: left(),
      args: ['--no-proxy-server', '--disable-background-networking']});
    context = await browser.newContext({viewport: plan.viewport, serviceWorkers: 'block', acceptDownloads: false});
    if (typeof context.routeWebSocket !== 'function') throw new Error('Playwright routeWebSocket capability unavailable');
    await context.routeWebSocket(/.*/, async socket => { block(socket.url()); await socket.close(); });
    await context.route(/.*/, async route => {
      if (authorized(route.request().url())) await route.continue();
      else { block(route.request().url()); await route.abort(); }
    });
    await context.tracing.start({screenshots: true, snapshots: true, sources: false});
    tracing = true;
    page = await context.newPage();
    page.on('dialog', dialog => dialog.dismiss());
    context.on('page', other => { if (other !== page) { block('unexpected popup'); other.close(); } });
    if (plan.document) await page.setContent(job.html, {waitUntil: 'load', timeout: left()});
    else await page.goto(plan.url, {waitUntil: 'load', timeout: left()});
    for (const step of plan.steps) {
      const row = {...step, status: 'running'};
      report.steps.push(row);
      const locator = page.locator(step.selector);
      try {
        if (step.action === 'click') await locator.click({timeout: left()});
        else if (step.action === 'fill') await locator.fill(step.value, {timeout: left()});
        else if (step.action === 'press') await locator.press(step.value, {timeout: left()});
        else if (step.action === 'assert_visible') await locator.waitFor({state: 'visible', timeout: left()});
        else if (step.action === 'assert_text' || step.action === 'assert_count') {
          while (true) {
            left();
            row.observed = step.action === 'assert_count' ? await locator.count() : await locator.first().textContent({timeout: left()});
            if (step.action === 'assert_count' ? row.observed === Number(step.value) : row.observed === step.value) break;
            await new Promise(resolve => setTimeout(resolve, Math.min(50, left())));
          }
        } else throw new Error('Unsupported browser action');
        row.status = 'pass';
      } catch (error) {
        row.status = 'fail'; row.error = String(error.message).slice(0, 2000);
        report.result = 'fail'; break;
      }
    }
    if (report.result !== 'fail') report.result = report.blocked_requests.length ? 'fail' : 'pass';
  } catch (error) {
    report.error = String(error.message).slice(0, 2000);
    report.result = 'inconclusive';
  } finally {
    fs.mkdirSync(job.artifacts, {recursive: true});
    if (page && !page.isClosed()) {
      try { await page.screenshot({path: path.join(job.artifacts, 'screenshot.png'), timeout: Math.max(1, Math.min(2000, deadline - Date.now()))}); report.artifacts.push('screenshot.png'); }
      catch (error) { report.capture_error = String(error.message).slice(0, 1000); }
    }
    if (context && tracing) {
      try { await context.tracing.stop({path: path.join(job.artifacts, 'trace.zip')}); report.artifacts.push('trace.zip'); }
      catch (error) { report.trace_error = String(error.message).slice(0, 1000); }
    }
    if (browser) await browser.close();
    if (report.blocked_requests.length && report.result === 'pass') report.result = 'fail';
    report.elapsed_ms = Math.max(1, Date.now() - started);
    if (report.result === 'pass' && (!report.artifacts.includes('screenshot.png') || !report.artifacts.includes('trace.zip'))) report.result = 'inconclusive';
    fs.writeFileSync(path.join(job.artifacts, 'report.json'), JSON.stringify(report));
    process.stdout.write(JSON.stringify({result: report.result, artifacts: ['report.json', ...report.artifacts]}));
  }
}
main().catch(error => { process.stderr.write(String(error.message)); process.exitCode = 2; });
