"""Ephemeral loopback HTML viewer. No control endpoints or filesystem routing."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
import threading


class ProgressView:
    def __init__(self):
        self.server = self.thread = None
        self.problem = None
        self.paths, self.tokens = {}, {}
        self.lock = threading.Lock()

    def register(self, team_id, path):
        with self.lock:
            if self.server is None and self.problem is None:
                owner = self
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *_):
                        pass  # MCP stdout is JSON-RPC only.

                    def do_GET(self):
                        host = self.headers.get("Host", "")
                        port = self.server.server_address[1]
                        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                            self.send_error(403)
                            return
                        with owner.lock:
                            target = owner.paths.get(self.path)
                        if target is None:
                            self.send_error(404)
                            return
                        try:
                            if target.stat().st_size > 2097152:
                                self.send_error(413)
                                return
                            content = target.read_bytes()
                        except OSError:
                            self.send_error(404)
                            return
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(content)))
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
                        self.end_headers()
                        self.wfile.write(content)
                try:
                    self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                    self.server.daemon_threads = True
                    self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.1), daemon=True)
                    self.thread.start()
                except OSError as exc:
                    self.problem = str(exc)
            if self.server is not None:
                token = self.tokens.setdefault(team_id, secrets.token_urlsafe(32))
                self.paths["/" + token] = path

    def url(self, team_id):
        with self.lock:
            if self.server is None or team_id not in self.tokens:
                return None
            return f"http://127.0.0.1:{self.server.server_address[1]}/{self.tokens[team_id]}"

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
