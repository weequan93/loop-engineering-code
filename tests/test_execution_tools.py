"""Frozen configuration and injected HTTP responses, with zero network calls."""

from copy import deepcopy
import unittest
from urllib.error import HTTPError

from loop_engineering.contracts import ContractError, load, ROOT, validate
from loop_engineering.execution_tools import http_load, NoRedirects, origin
from loop_engineering.external_execution import inspect_report
from reference.core import canonical_digest
import test_scenarios as scenario_fixture


def load_plan():
    return {"kind": "http_load", "check_id": "load", "timeout_seconds": 2,
        "url": "http://fixture.invalid/items", "allowed_origins": ["http://fixture.invalid"],
        "requests": 3, "concurrency": 2, "requests_per_second": 100,
        "request_timeout_ms": 100, "max_response_bytes": 16, "expected_status": 200,
        "thresholds": {"max_p95_ms": 100, "max_errors": 0, "min_successes": 3}}


def browser_plan():
    return {"kind": "browser", "check_id": "ui", "timeout_seconds": 2,
        "node": "/missing/node", "playwright_module": "/missing/playwright", "browser_executable": "/missing/browser",
        "document": "ui.html", "url": None, "allowed_origins": [],
        "steps": [{"action": "assert_text", "selector": "#status", "value": "done"}],
        "viewport": {"width": 800, "height": 600}}


class Response:
    def __init__(self, url, status=200, body=b"ok"):
        self.url, self.status, self.body = url, status, body
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def geturl(self): return self.url
    def read(self, limit): return self.body[:limit]


class Opener:
    def __init__(self, status=200, body=b"ok", redirect=None, error=None):
        self.status, self.body, self.redirect, self.error = status, body, redirect, error
        self.calls = []
    def open(self, request, timeout):
        self.calls.append((request.full_url, request.method, timeout))
        if self.error: raise self.error
        return Response(self.redirect or request.full_url, self.status, self.body)


class ExecutionToolTests(unittest.TestCase):
    def config(self, plan):
        result = load(ROOT / "templates/scenarios/development/execution-tools.json")
        result["executors"] = [plan]
        return result

    def test_passing_workload_retains_samples_units_and_recomputable_thresholds(self):
        plan = load_plan(); opener = Opener()
        result = http_load(plan, opener=opener)
        self.assertEqual(result["result"], "pass")
        self.assertEqual(result["metrics"]["requests"], 3)
        self.assertEqual(len(opener.calls), 3)
        self.assertTrue(all(method == "GET" and 0 < timeout <= 0.1 for _, method, timeout in opener.calls))
        report = {**result, "bindings": {"id": "fixture"}, "plan_digest": canonical_digest(plan)}
        self.assertEqual(inspect_report(report, plan, report["bindings"]), "pass")

    def test_status_body_redirect_and_transport_errors_fail_thresholds(self):
        for opener in (Opener(status=503), Opener(body=b"x" * 20),
                       Opener(redirect="http://outside.invalid/items"), Opener(error=TimeoutError())):
            with self.subTest(opener=opener):
                report = http_load(load_plan(), opener=opener)
                self.assertEqual(report["result"], "fail")
                self.assertEqual(report["metrics"]["errors"], 3)

    def test_urllib_never_follows_redirects(self):
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, "Found", {}, "http://outside.invalid"))

    def test_no_implicit_targets_or_increased_workload_bounds(self):
        for mutation in ({"url": "http://outside.invalid"}, {"url": "http://user:secret@fixture.invalid"},
                {"requests": 1001}, {"concurrency": 9}, {"requests_per_second": 101},
                {"allowed_origins": ["http://fixture.invalid/path"]}, {"timeout_seconds": 121}):
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                validate("execution-tools", self.config({**load_plan(), **mutation}))

    def test_origin_canonicalization_has_no_credentials_fragments_or_default_port(self):
        self.assertEqual(origin("https://EXAMPLE.COM:443/path?x=1"), "https://example.com")
        self.assertEqual(origin("http://[::1]:8080/path"), "http://[::1]:8080")
        for url in ("file:///tmp/x", "https://example.com/#x", "http://example.com:bad", "http://u:p@example.com"):
            with self.subTest(url=url), self.assertRaises(ContractError): origin(url)

    def test_report_cannot_change_plan_candidate_metrics_or_declare_false_pass(self):
        plan = load_plan(); actual = http_load(plan, opener=Opener())
        report = {**actual, "bindings": {"candidate": "fixture"}, "plan_digest": canonical_digest(plan)}
        mutations = [lambda r: r.update(bindings={"candidate": "another"}),
            lambda r: r.update(plan_digest="sha256:" + "0" * 64),
            lambda r: r["metrics"].update(p95_us=0),
            lambda r: r["samples"][0].update(status=500),
            lambda r: r.update(thresholds={**plan["thresholds"], "max_errors": 100})]
        for mutate in mutations:
            wrong = deepcopy(report); mutate(wrong)
            with self.assertRaises(ContractError): inspect_report(wrong, plan, report["bindings"])

    def test_browser_needs_assertions_explicit_runtime_and_exact_action_values(self):
        validate("execution-tools", self.config(browser_plan()))
        mutations = [{"browser_executable": None}, {"url": "http://fixture.invalid"},
                     {"steps": [{"action": "click", "selector": "#button", "value": None}]},
                     {"steps": [{"action": "assert_count", "selector": "li", "value": "1.5"}]},
                     {"document": "../outside"}, {"node": "node"}]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                validate("execution-tools", self.config({**browser_plan(), **mutation}))

    def test_installer_output_inputs_and_control_paths_are_separate(self):
        cfg = self.config(load_plan())
        cfg["provision"].update(inputs=[".git/config"], output_dirs=["node_modules"],
            steps=[{"id": "install", "argv": ["npm", "ci"], "cwd": ".", "timeout_seconds": 30}])
        with self.assertRaises(ContractError): validate("execution-tools", cfg)

        cfg["provision"]["inputs"] = ["node_modules/package.json"]
        with self.assertRaises(ContractError): validate("execution-tools", cfg)

    def test_changed_tool_configuration_requires_a_new_team_selection(self):
        fixture = scenario_fixture.ScenarioTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.initialize(); fixture.prepare_task()
        selected = (fixture.root / ".loop/team.json").read_bytes()
        config = load(fixture.root / ".loop/execution-tools.json")
        config["provision"]["max_output_files"] += 1
        fixture.write(".loop/execution-tools.json", config)
        from loop_engineering.scenarios import status
        self.assertEqual(status(fixture.root)["team"]["state"], "stale")
        self.assertEqual((fixture.root / ".loop/team.json").read_bytes(), selected)
