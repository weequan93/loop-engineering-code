"""HTTP boundary fixtures. urllib is mocked before any request can be sent."""

from contextlib import redirect_stdout
import io
import json
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from scripts import http_worker


class HTTPWorkerTests(TestCase):
    def run_worker(self, request, *, raw=b'{"input_tokens":100}', failure=None, credential=True):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = raw
        opener = Mock()
        opener.open.return_value = response
        opener.open.side_effect = failure
        output = io.StringIO()
        with patch.object(http_worker.sys, "stdin", io.StringIO(json.dumps(request))), \
                patch.dict(http_worker.os.environ, {"LOOP_FIXTURE_KEY": "fixture-credential"} if credential else {}, clear=True), \
                patch.object(http_worker, "build_opener", return_value=opener) as factory, redirect_stdout(output):
            http_worker.main()
        text = output.getvalue()
        self.assertNotIn("fixture-credential", text)
        return json.loads(text), opener, response, factory

    def request(self, provider="openai", operation="count"):
        return {"provider": provider, "operation": operation, "api_key_env": "LOOP_FIXTURE_KEY",
                "payload": {"model": "fixture-only"}, "timeout_seconds": 2, "max_bytes": 128}

    def test_only_fixed_provider_endpoints_and_bounded_reads_are_used(self):
        for provider, root in (("openai", "https://api.openai.com/v1/responses"),
                               ("anthropic", "https://api.anthropic.com/v1/messages")):
            for operation in ("count", "respond"):
                with self.subTest(provider=provider, operation=operation):
                    result, opener, response, factory = self.run_worker(self.request(provider, operation))
                    request = opener.open.call_args.args[0]
                    suffix = "/input_tokens" if provider == "openai" else "/count_tokens"
                    self.assertEqual(request.full_url, root + (suffix if operation == "count" else ""))
                    self.assertEqual(opener.open.call_args.kwargs["timeout"], 2)
                    response.read.assert_called_once_with(129)
                    self.assertEqual(result["body"], {"input_tokens": 100})
                    self.assertTrue(any(isinstance(handler, http_worker.NoRedirect) for handler in factory.call_args.args))
                    self.assertTrue(any(isinstance(handler, http_worker.ProxyHandler) and handler.proxies == {}
                                        for handler in factory.call_args.args))

    def test_missing_credentials_never_build_or_dispatch_transport(self):
        result, opener, _, factory = self.run_worker(self.request(), credential=False)
        self.assertEqual(result["category"], "authentication")
        factory.assert_not_called(); opener.open.assert_not_called()

    def test_oversized_body_is_not_parsed_or_returned(self):
        result, _, _, _ = self.run_worker(self.request(), raw=b"x" * 129)
        self.assertEqual(result, {"ok": False, "category": "invalid_output", "status": None})

    def test_invalid_json_body_is_an_output_failure(self):
        result, _, _, _ = self.run_worker(self.request(), raw=b"invalid JSON")
        self.assertEqual(result, {"ok": False, "category": "invalid_output", "status": None})

    def test_http_errors_are_classified_without_reading_error_bodies(self):
        for status, category in ((401, "authentication"), (403, "permission"), (429, "transient"), (400, "invalid_output")):
            body = Mock()
            error = HTTPError("https://fixture.invalid", status, "fixture", {}, body)
            result, _, _, _ = self.run_worker(self.request(), failure=error)
            self.assertEqual(result, {"ok": False, "category": category, "status": status})
            body.read.assert_not_called()

    def test_network_disconnect_is_ambiguous_and_redirects_are_refused(self):
        result, _, _, _ = self.run_worker(self.request(), failure=URLError("fixture disconnect"))
        self.assertEqual(result["category"], "transient")
        self.assertIsNone(http_worker.NoRedirect().redirect_request(None, None, 302, "fixture", {}, "https://fixture.invalid"))
