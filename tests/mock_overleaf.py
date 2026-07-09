"""Mock Overleaf for the read-link flow: interstitial -> grant -> zip.

Mirrors link-sharing v2 the way overleaf.com behaved on 2026-07-09: the read
link serves a grant interstitial (csrf meta, no project id); the grant POST
must carry the session cookie, the csrf header, and the link's #fragment as
tokenHashPrefix, and answers {"redirect": "/project/<id>"}; the zip download
needs the granted session.

    python3 tests/mock_overleaf.py <port> [document text]
"""
import io
import json
import sys
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

PID = "0123456789abcdef01234567"
CSRF = "csrf-123"
FRAG = "#ccbb9b"
DOC = sys.argv[2] if len(sys.argv) > 2 else "version A"


class H(BaseHTTPRequestHandler):
    granted = False

    def log_message(self, *a):  # quiet
        pass

    def _has_session(self) -> bool:
        return "sess=anon" in (self.headers.get("Cookie") or "")

    def do_GET(self):
        if self.path.startswith("/read/"):
            body = (f'<html><head><meta name="ol-csrfToken" '
                    f'content="{CSRF}"></head>'
                    "<body>join this project?</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Set-Cookie", "sess=anon; Path=/")
            self.end_headers()
            self.wfile.write(body)
        elif (self.path == f"/project/{PID}/download/zip"
              and H.granted and self._has_session()):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("main.tex",
                            "\\documentclass{article}\\begin{document}"
                            + DOC + "\\end{document}\n")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.end_headers()
            self.wfile.write(buf.getvalue())
        else:
            self.send_response(403)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        if (self.path.startswith("/read/") and self.path.endswith("/grant")
                and self.headers.get("X-Csrf-Token") == CSRF
                and body.get("tokenHashPrefix") == FRAG
                and self._has_session()):
            H.granted = True
            payload = json.dumps({"redirect": f"/project/{PID}"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(403)
            self.end_headers()


HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
