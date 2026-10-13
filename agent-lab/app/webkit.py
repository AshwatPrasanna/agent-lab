"""A tiny standard-library web framework (no pip dependencies), just enough for Agent Lab."""
from __future__ import annotations

import inspect
import json
import mimetypes
import re
import traceback
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


class HTTPException(Exception):
    def __init__(self, status: int, detail=None):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class FileResp:
    def __init__(self, path: Path):
        self.path = Path(path)


def _parse_multipart(body: bytes, content_type: str) -> dict:
    msg = BytesParser(policy=email_policy).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    out = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is not None:
            out[name] = {"filename": filename, "data": payload}
        else:
            out[name] = payload.decode("utf-8", "replace")
    return out


class App:
    def __init__(self, static_dir: Path):
        self.static_dir = Path(static_dir)
        self.routes: list[tuple[str, re.Pattern, callable]] = []

    def _route(self, method, path):
        pattern = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", path) + "$")

        def deco(fn):
            self.routes.append((method, pattern, fn))
            return fn
        return deco

    def get(self, path):
        return self._route("GET", path)

    def post(self, path):
        return self._route("POST", path)

    # ------------------------------------------------------------------
    def handle(self, h: BaseHTTPRequestHandler, method: str):
        url = urlparse(h.path)
        if method == "POST" and not h.headers.get("X-Agent-Lab"):
            # Browsers can't add custom headers to cross-site requests without a CORS preflight,
            # which this server never approves, so other websites can't drive Agent Lab.
            return self._send_json(h, 403, {"detail": "missing X-Agent-Lab header"})
        for m, pat, fn in self.routes:
            if m != method:
                continue
            mt = pat.match(url.path)
            if not mt:
                continue
            try:
                result = self._call(fn, mt.groupdict(), parse_qs(url.query), h)
                if isinstance(result, FileResp):
                    return self._send_file(h, result.path)
                return self._send_json(h, 200, result)
            except HTTPException as e:
                return self._send_json(h, e.status, {"detail": e.detail})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return self._send_json(h, 500, {"detail": f"{type(e).__name__}: {e}"})
        if method == "GET" and not url.path.startswith("/api/"):
            rel = url.path.lstrip("/") or "index.html"
            p = (self.static_dir / rel).resolve()
            if self.static_dir.resolve() in p.parents and p.is_file():
                return self._send_file(h, p)
        return self._send_json(h, 404, {"detail": "not found"})

    def _call(self, fn, path_params, query, h):
        sig = inspect.signature(fn)
        kwargs = {}
        body_bytes = None
        length = int(h.headers.get("Content-Length") or 0)
        if length:
            body_bytes = h.rfile.read(length)
        for name, prm in sig.parameters.items():
            ann = prm.annotation if isinstance(prm.annotation, type) else {"int": int, "bool": bool}.get(str(prm.annotation), str)
            if name in path_params:
                raw = path_params[name]
            elif name == "body":
                try:
                    kwargs[name] = json.loads(body_bytes or b"{}")
                except json.JSONDecodeError:
                    raise HTTPException(400, "invalid JSON body")
                continue
            elif name == "form":
                ct = h.headers.get("Content-Type", "")
                if "multipart/form-data" not in ct:
                    raise HTTPException(400, "expected multipart/form-data")
                kwargs[name] = _parse_multipart(body_bytes or b"", ct)
                continue
            elif name in query:
                raw = query[name][0]
            else:
                continue
            if ann is bool:
                kwargs[name] = raw.lower() in ("1", "true", "yes")
            elif ann is int:
                try:
                    kwargs[name] = int(raw)
                except ValueError:
                    raise HTTPException(400, f"{name} must be an integer")
            else:
                kwargs[name] = raw
        return fn(**kwargs)

    @staticmethod
    def _send_json(h, status, obj):
        data = json.dumps(obj).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        h.wfile.write(data)

    @staticmethod
    def _send_file(h, p: Path):
        data = p.read_bytes()
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        h.send_response(200)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        h.wfile.write(data)

    def serve(self, host="127.0.0.1", port=8765):
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802
                app.handle(self, "GET")

            def do_POST(self):  # noqa: N802
                app.handle(self, "POST")

            def log_message(self, fmt, *args):
                if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
                    super().log_message(fmt, *args)

        srv = ThreadingHTTPServer((host, port), Handler)
        srv.daemon_threads = True
        print(f"Agent Lab running at http://{host}:{port}  (Ctrl+C to stop)", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
