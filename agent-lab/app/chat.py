"""Throwaway Q&A chat about a finished job: one single-agent Claude Code session per job.

Nothing here is written to disk. The conversation lives in this process's memory and in the
claude process, which runs with --no-session-persistence. Closing the chat, or restarting or
stopping Agent Lab, ends it for good. File changes the assistant makes in the workspace are real.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import threading
import time
import uuid

import procs

MAX_ITEMS = 3000
TOOL_KEYS = ("command", "file_path", "pattern", "url", "query", "path")


def _short(name: str, inp) -> str:
    if isinstance(inp, dict):
        for k in TOOL_KEYS:
            if k in inp:
                return str(inp[k])[:300]
        return json.dumps(inp)[:200]
    return str(inp)[:200]


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return str(content)


class ChatSession:
    """One live `claude -p --input-format stream-json` process and the messages shown in the page."""

    def __init__(self, jid: str, model: str, carry: list | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.jid, self.model = jid, model
        self.token = secrets.token_hex(16)  # for the permission hook; only ever held in memory
        self.created = time.time()
        self.items: list[dict] = list(carry or [])
        self._n = (self.items[-1]["n"] + 1) if self.items else 0  # running message number
        self.perms: list[dict] = []
        self.busy = self.ended = self.closing = self.interrupted = False
        self.draft = ""  # text of the reply being streamed
        self.cost = 0.0
        self.proc: subprocess.Popen | None = None
        self.cwd = ""
        self._stderr: list[str] = []
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ process
    def start(self, cmd: list[str], cwd: str, env: dict):
        self.cwd = cwd.rstrip("/") + "/"
        self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stderr(self):
        for line in self.proc.stderr:
            self._stderr = (self._stderr + [line.rstrip()])[-20:]

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                self._on_event(ev)
            except Exception:  # noqa: BLE001  (a malformed event must not kill the reader)
                pass
        rc = self.proc.wait()
        with self.lock:
            self.busy, self.draft, self.ended = False, "", True
            if not self.closing:
                tail = " ".join(self._stderr[-3:])[-400:]
                self._add("note", f"The assistant's session ended (exit code {rc}){': ' + tail if tail else ''}. "
                                  "Send a message to start a new session. It won't remember the messages above.")
            for r in self.perms:
                if r["status"] == "pending":
                    r["status"] = "expired"

    def _on_event(self, ev: dict):
        typ = ev.get("type")
        with self.lock:
            if typ == "stream_event" and not ev.get("parent_tool_use_id"):
                e = ev.get("event") or {}
                if e.get("type") == "message_start":
                    self.draft = ""
                elif e.get("type") == "content_block_delta" and (e.get("delta") or {}).get("type") == "text_delta":
                    self.draft += e["delta"].get("text", "")
            elif typ == "assistant":
                for c in (ev.get("message") or {}).get("content") or []:
                    if c.get("type") == "text" and c.get("text", "").strip():
                        self._add("assistant", c["text"])
                        self.draft = ""
                    elif c.get("type") == "tool_use":
                        short = _short(c.get("name"), c.get("input"))
                        self._add("tool", f"{c.get('name')} · {short.replace(self.cwd, '') if self.cwd else short}")
            elif typ == "user":
                content = (ev.get("message") or {}).get("content")
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"):
                            self._add("error", _text(c.get("content"))[:800])
            elif typ == "result":
                self.busy, self.draft = False, ""
                self.cost += float(ev.get("total_cost_usd") or 0)
                if self.interrupted:
                    self._add("note", "Stopped.")
                elif ev.get("is_error"):
                    self._add("error", f"The turn ended with an error: {ev.get('subtype')} {str(ev.get('result') or '')[:400]}".strip())
                self.interrupted = False

    def _add(self, role: str, text: str):
        self.items.append({"n": self._n, "t": time.time(), "role": role, "text": text})
        self._n += 1
        if len(self.items) > MAX_ITEMS:
            self.items = self.items[-MAX_ITEMS:]

    def _write(self, obj: dict):
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            self.ended = True
            raise RuntimeError("the assistant's session has ended; send your message again to start a new one")

    # ------------------------------------------------------------------ actions
    def send(self, text: str):
        with self.lock:
            if self.ended:
                raise RuntimeError("this chat session has ended")
            if self.busy:
                raise RuntimeError("the assistant is still answering")
            self._add("user", text)
            self.busy, self.draft = True, ""
            self._write({"type": "user", "message": {"role": "user", "content": text}})

    def interrupt(self):
        with self.lock:
            if not self.busy or self.ended:
                return
            self.interrupted = True
            self._write({"type": "control_request", "request_id": "stop-" + uuid.uuid4().hex[:8],
                         "request": {"subtype": "interrupt"}})

    def close(self, wait: bool = False):
        """End the session and everything it started. The conversation is gone afterwards."""
        with self.lock:
            self.closing = True
            proc = self.proc
        if not proc or proc.poll() is not None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        if wait:
            procs.stop(proc.pid, grace=2.0)
        else:
            threading.Thread(target=procs.stop, args=(proc.pid, 2.0), daemon=True).start()

    # ------------------------------------------------------------------ permissions (memory only)
    def add_perm(self, rec: dict) -> dict:
        with self.lock:
            self.perms.append(rec)
            self._add("note", f"✋ Asked for permission: {rec['tool_name']} · {rec['summary'][:200]}"
                      if rec["status"] == "pending" else f"Auto-allowed: {rec['tool_name']} · {rec['summary'][:200]}")
        return rec

    def get_perm(self, rid: str) -> dict | None:
        return next((r for r in self.perms if r["id"] == rid), None)

    def view(self, since: int = 0) -> dict:
        with self.lock:
            start = max(0, since - (self.items[0]["n"] if self.items else 0))
            return {"id": self.id, "model": self.model, "created": self.created, "busy": self.busy,
                    "ended": self.ended, "cost": round(self.cost, 4), "draft": self.draft,
                    "items": self.items[start:], "total": self._n,
                    "perms": [{k: v for k, v in r.items() if k != "tool_input"} for r in self.perms]}
