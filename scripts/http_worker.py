"""Trusted HTTP-only worker; no model tools, redirects, automatic retries, or SDK."""

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main() -> None:
    request = json.load(sys.stdin)
    provider, operation = request["provider"], request["operation"]
    if provider not in {"openai", "anthropic"} or operation not in {"count", "respond"}:
        raise ValueError("Unsupported provider operation")
    key = os.environ.get(request["api_key_env"])
    if not key:
        print(json.dumps({"ok": False, "category": "authentication", "status": None}))
        return
    if provider == "openai":
        url = "https://api.openai.com/v1/responses" + ("/input_tokens" if operation == "count" else "")
        headers = {"Authorization": "Bearer " + key}
    else:
        url = "https://api.anthropic.com/v1/messages" + ("/count_tokens" if operation == "count" else "")
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    headers["Content-Type"] = "application/json"
    body = json.dumps(request["payload"], ensure_ascii=False).encode()
    # Disable ambient proxy forwarding so credentials only reach the fixed API
    # host over the standard verified TLS connection.
    opener = build_opener(NoRedirect(), ProxyHandler({}))
    try:
        with opener.open(Request(url, data=body, headers=headers), timeout=request["timeout_seconds"]) as response:
            raw = response.read(request["max_bytes"] + 1)
        if len(raw) > request["max_bytes"]:
            print(json.dumps({"ok": False, "category": "invalid_output", "status": None}))
            return
        parsed = json.loads(raw)
        print(json.dumps({"ok": True, "body": parsed}, ensure_ascii=False))
    except HTTPError as exc:
        category = "authentication" if exc.code == 401 else "permission" if exc.code == 403 else "transient" if exc.code in {408, 429, 500, 502, 503, 504} else "invalid_output"
        print(json.dumps({"ok": False, "category": category, "status": exc.code}))
    except (ValueError, UnicodeError):
        print(json.dumps({"ok": False, "category": "invalid_output", "status": None}))
    except (URLError, TimeoutError, OSError):
        print(json.dumps({"ok": False, "category": "transient", "status": None}))


if __name__ == "__main__":
    main()
