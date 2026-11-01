"""Exploration-only receiver: records every request verbatim, answers 200."""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OUT = sys.argv[1] if len(sys.argv) > 1 else "/out/probe.jsonl"
lock = threading.Lock()


class H(BaseHTTPRequestHandler):
    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        rec = {
            "t": time.time(),
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers),
            "len": len(body),
            "body": body.decode("utf-8", "replace"),
        }
        with lock, open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    do_POST = do_PUT = do_GET = _handle

    def log_message(self, *a):
        pass


ThreadingHTTPServer(("0.0.0.0", 8000), H).serve_forever()
