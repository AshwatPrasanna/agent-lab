"""A tiny standard-library web framework (no pip dependencies), just enough for Agent Lab."""
from __future__ import annotations

import inspect
import json
import mimetypes
import os
import re
import shutil
import tempfile
import traceback
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


MAX_BODY = 2**30  # 1 GB: spec uploads can include zipped data (streamed to disk, never held in memory)
MAX_JSON_BODY = 20 * 2**20  # other requests are small
MAX_TEXT_FIELD = 20 * 2**20


class HTTPException(Exception):
    def __init__(self, status: int, detail=None):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class FileResp:
    def __init__(self, path: Path):
        self.path = Path(path)


def _stream_multipart(rfile, length: int, content_type: str, dest_dir: Path, made: list) -> dict:
    """Parse a multipart request body straight from the connection, 1 MB at a time. File parts are
    written to their own temporary files as they arrive ({"filename", "path", "size"}; the caller moves
    them, and lists their paths in `made`), so an upload is never held in memory. Text fields come
    back as strings."""
    hdr = BytesParser(policy=email_policy).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n")
    boundary = hdr.get_param("boundary")
    if not boundary:
        raise HTTPException(400, "multipart body without a boundary")
    delim = b"\r\n--" + boundary.encode("latin-1")
    left = length
    buf = b"\r\n"  # so the first boundary, at the very start, matches delim too

    def fill() -> bool:
        nonlocal buf, left
        if left <= 0:
            return False
        chunk = rfile.read(min(1 << 20, left))
        if not chunk:
            raise HTTPException(400, "upload was cut short")
        left -= len(chunk)
        buf += chunk
        return True

    out, files = {}, {}
    while (i := buf.find(delim)) == -1:  # the preamble
        buf = buf[-len(delim):]
        if not fill():
            raise HTTPException(400, "malformed multipart body")
    buf = buf[i + len(delim):]
    while True:
        while len(buf) < 2:
            if not fill():
                raise HTTPException(400, "malformed multipart body")
        if buf[:2] == b"--":
            break  # the closing boundary
        while (j := buf.find(b"\r\n\r\n")) == -1:
            if len(buf) > 64 * 1024 or not fill():
                raise HTTPException(400, "malformed multipart headers")
        headers = BytesParser(policy=email_policy).parsebytes(buf[:j].lstrip(b"\r\n") + b"\r\n\r\n")
        buf = buf[j + 4:]
        name = headers.get_param("name", header="content-disposition")
        filename = headers.get_filename()
        sink, text, size = None, bytearray(), 0
        if name and filename is not None:
            fd, fpath = tempfile.mkstemp(dir=str(dest_dir), prefix=".part-", suffix=".tmp")
            made.append(fpath)
            sink = os.fdopen(fd, "wb")
        try:
            while True:
                k = buf.find(delim)
                piece, done = (buf[:k], True) if k != -1 else (buf[:max(0, len(buf) - len(delim) + 1)], False)
                buf = buf[k + len(delim):] if done else buf[len(piece):]
                size += len(piece)
                if sink:
                    sink.write(piece)
                else:
                    text += piece
                    if len(text) > MAX_TEXT_FIELD:
                        raise HTTPException(413, f"the {name} field is too long")
                if done:
                    break
                if not fill():
                    raise HTTPException(400, "malformed multipart body")
        finally:
            if sink:
                sink.close()
        if sink:
            info = {"filename": filename, "path": fpath, "size": size}
            out[name] = info  # the last file under this name
            files.setdefault(name, []).append(info)  # all of them (multiple uploads)
        elif name and name != "_files":  # that key is reserved for the uploaded files
            out[name] = bytes(text).decode("utf-8", "replace")
    while left > 0:  # read past the epilogue, so the connection stays usable
        left -= len(rfile.read(min(1 << 20, left)) or b"x" * left)
    out["_files"] = files
    return out


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
            f = {"filename": filename, "data": payload}
            out[name] = f  # the last file under this name
            out.setdefault("_files", {}).setdefault(name, []).append(f)  # all of them (multiple uploads)
        elif name != "_files":  # that key is reserved for the uploaded files
            out[name] = payload.decode("utf-8", "replace")
    return out


class App:
    def __init__(self, static_dir: Path, upload_dir: Path | None = None):
        self.static_dir = Path(static_dir)
        self.upload_dir = Path(upload_dir) if upload_dir else None
        self.port = None
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
        host = (h.headers.get("Host") or "").lower()
        if self.port and host not in {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}:
            # Only requests addressed to this computer by name. This blocks "DNS rebinding", where a website
            # points its own domain name at 127.0.0.1 to get past the browser's same-origin protection.
            h.close_connection = True
            return self._send_json(h, 403, {"detail": "unexpected Host header"})
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
        wants_form = "form" in sig.parameters
        if length > (MAX_BODY if wants_form else MAX_JSON_BODY):
            h.close_connection = True
            raise HTTPException(413, f"request too large (limit {(MAX_BODY if wants_form else MAX_JSON_BODY) // 2**20} MB)")
        streamed, parts = None, []
        if length and wants_form and self.upload_dir is not None:
            ct = h.headers.get("Content-Type", "")
            if "multipart/form-data" not in ct:
                raise HTTPException(400, "expected multipart/form-data")
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            try:
                streamed = _stream_multipart(h.rfile, length, ct, self.upload_dir, parts)
            except BaseException:
                h.close_connection = True
                for p in parts:
                    if os.path.exists(p):
                        os.unlink(p)
                raise
        elif length:
            body_bytes = h.rfile.read(length)
        try:
            return self._call_with(fn, sig, kwargs, body_bytes, streamed, path_params, query, h)
        finally:
            for p in parts:  # whatever the handler didn't move into place
                if os.path.exists(p):
                    os.unlink(p)

    def _call_with(self, fn, sig, kwargs, body_bytes, streamed, path_params, query, h):
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
                kwargs[name] = streamed if streamed is not None else _parse_multipart(body_bytes or b"", ct)
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
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(p, "rb") as f:  # sent in pieces, so a large file isn't loaded into memory
            size = os.fstat(f.fileno()).st_size
            h.send_response(200)
            h.send_header("Content-Type", ctype)
            h.send_header("Content-Length", str(size))
            h.send_header("Cache-Control", "no-store")
            h.end_headers()
            shutil.copyfileobj(f, h.wfile, 1 << 20)

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

        class Server(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                import sys
                if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)):
                    return  # the browser closed a connection: normal, not worth a traceback in the log
                super().handle_error(request, client_address)

        self.port = port
        srv = Server((host, port), Handler)
        srv.daemon_threads = True
        print(f"Agent Lab running at http://{host}:{port}  (Ctrl+C to stop)", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
