"""Turn Claude Code stream-json events into a readable activity log and efficiency metrics."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path


def _short_input(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return str(inp)[:160]
    for key in ("command", "file_path", "pattern", "url", "query", "path"):
        if key in inp:
            return str(inp[key])[:200]
    if name in ("Agent", "Task"):
        return f"{inp.get('subagent_type', '?')}: {inp.get('description') or str(inp.get('prompt', ''))[:120]}"
    if name == "TodoWrite":
        todos = inp.get("todos") or []
        return f"{len(todos)} todo items"
    return json.dumps(inp)[:160]


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                out.append(c.get("text", ""))
        return "\n".join(out)
    return str(content)


class Digest:
    """Incrementally parses events.jsonl lines ({"ts":..,"ev":{..}})."""

    def __init__(self):
        self.items: list[dict] = []
        self.agent_of_tool: dict[str, str] = {}   # Agent tool_use id -> subagent name
        self.pending: dict[str, dict] = {}        # tool_use id -> call info
        self.calls: list[dict] = []
        self.result: dict | None = None
        self.init: dict | None = None
        self.t0: float | None = None

    def label(self, ev) -> str:
        p = ev.get("parent_tool_use_id")
        return self.agent_of_tool.get(p, "subagent") if p else "orchestrator"

    def feed(self, rec: dict):
        ts = rec.get("ts", 0.0)
        if self.t0 is None:
            self.t0 = ts
        ev = rec.get("ev")
        rel = round(ts - (self.t0 or ts), 1)
        if not isinstance(ev, dict):
            self.items.append({"t": rel, "kind": "raw", "agent": "", "text": str(rec.get("raw", ""))[:500]})
            return
        typ = ev.get("type")
        if typ == "system" and ev.get("subtype") == "init":
            self.init = ev
            self.items.append({"t": rel, "kind": "system", "agent": "",
                               "text": f"Session started · model {ev.get('model')} · cwd {ev.get('cwd')}"})
        elif typ == "assistant":
            who = self.label(ev)
            for c in (ev.get("message") or {}).get("content") or []:
                if c.get("type") == "text" and c.get("text", "").strip():
                    self.items.append({"t": rel, "kind": "text", "agent": who, "text": c["text"]})
                elif c.get("type") == "tool_use":
                    name, inp = c.get("name"), c.get("input") or {}
                    if name in ("Agent", "Task"):
                        self.agent_of_tool[c.get("id")] = inp.get("subagent_type") or inp.get("name") or "subagent"
                    info = {"id": c.get("id"), "name": name, "agent": who, "short": _short_input(name, inp),
                            "start": ts, "end": None, "error": False}
                    self.pending[c.get("id")] = info
                    self.calls.append(info)
                    self.items.append({"t": rel, "kind": "delegate" if name in ("Agent", "Task") else "tool",
                                       "agent": who, "text": f"{name} · {info['short']}"})
        elif typ == "user":
            content = (ev.get("message") or {}).get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        info = self.pending.pop(c.get("tool_use_id"), None)
                        if info:
                            info["end"] = ts
                            info["error"] = bool(c.get("is_error"))
                        if c.get("is_error"):
                            self.items.append({"t": rel, "kind": "error", "agent": self.label(ev),
                                               "text": _result_text(c.get("content"))[:600]})
                            if info:
                                info["error_text"] = _result_text(c.get("content"))[:400]
        elif typ == "result":
            self.result = ev
            txt = ev.get("result") or ev.get("subtype") or ""
            self.items.append({"t": rel, "kind": "result", "agent": "orchestrator", "text": str(txt)})

    def metrics(self, end_ts: float | None = None) -> dict:
        r = self.result or {}
        usage = r.get("usage") or {}
        tool_counts = Counter(c["name"] for c in self.calls)
        per_agent = defaultdict(Counter)
        for c in self.calls:
            per_agent[c["agent"]][c["name"]] += 1
        durs = [dict(name=c["name"], agent=c["agent"], short=c["short"],
                     seconds=round(c["end"] - c["start"], 1)) for c in self.calls if c["end"]]
        slow = sorted(durs, key=lambda d: -d["seconds"])[:8]
        bash = Counter(c["short"] for c in self.calls if c["name"] == "Bash")
        repeated = [{"command": k, "times": v} for k, v in bash.most_common() if v > 1][:10]
        errors = [{"agent": c["agent"], "tool": c["name"], "input": c["short"], "error": c.get("error_text", "")}
                  for c in self.calls if c["error"]]
        sandbox_hits = [e for e in errors if any(s in e["error"] for s in
                        ("Operation not permitted", "blocked", "not allowed", "denied", "outside"))]
        wall = None
        if self.t0 is not None and end_ts:
            wall = round(end_ts - self.t0, 1)
        return {
            "status": r.get("subtype"),
            "is_error": r.get("is_error"),
            "duration_s": round((r.get("duration_ms") or 0) / 1000, 1) or wall,
            "num_turns": r.get("num_turns"),
            "est_cost_usd": r.get("total_cost_usd"),
            "tokens": {k: usage.get(k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                 "cache_creation_input_tokens") if usage.get(k) is not None},
            "tool_calls": sum(tool_counts.values()),
            "tool_counts": dict(tool_counts),
            "per_agent": {k: dict(v) for k, v in per_agent.items()},
            "delegations": [c["short"] for c in self.calls if c["name"] in ("Agent", "Task")],
            "errors": len(errors),
            "error_samples": errors[:15],
            "sandbox_denials": sandbox_hits[:10],
            "slowest_calls": slow,
            "repeated_commands": repeated,
        }


def load_digest(path: Path) -> Digest:
    d = Digest()
    if path.exists():
        with path.open() as f:
            for line in f:
                try:
                    d.feed(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return d


def transcript_markdown(d: Digest, limit_chars: int = 60000) -> str:
    rows = []
    for it in d.items:
        txt = it["text"].replace("\n", " ")
        if it["kind"] == "text":
            txt = txt[:400]
        rows.append(f"[{it['t']:>7}s] {it['agent'] or '-':<14} {it['kind']:<8} {txt[:600]}")
    body = "\n".join(rows)
    if len(body) > limit_chars:
        half = limit_chars // 2
        body = body[:half] + "\n\n... [middle of transcript omitted] ...\n\n" + body[-half:]
    return body


# --------------------------------------------------------------------------- keeping logs small
EVENT_TEXT_LIMIT = 20_000  # characters kept of any one string in a saved event (tool output, file contents…)
RESULT_TEXT_LIMIT = 200_000  # the final message is kept nearly whole


def shrink(obj, limit: int = EVENT_TEXT_LIMIT):
    """A copy of a Claude Code event with long strings cut down and images left out, for saving.
    Tool results can be whole files or base64 images; the activity log only needs a few hundred
    characters of them."""
    if isinstance(obj, str):
        if len(obj) <= limit:
            return obj
        keep = limit // 2
        return obj[:keep] + f"\n… [{len(obj) - 2 * keep:,} characters left out of the saved log] …\n" + obj[-keep:]
    if isinstance(obj, list):
        return [shrink(x, limit) for x in obj]
    if isinstance(obj, dict):
        if obj.get("type") == "image" and isinstance(obj.get("source"), dict):
            return {"type": "image", "source": {"type": "omitted", "note": "image left out of the saved log"}}
        return {k: shrink(v, limit) for k, v in obj.items()}
    return obj


def shrink_event(ev: dict) -> dict:
    return shrink(ev, RESULT_TEXT_LIMIT if ev.get("type") == "result" else EVENT_TEXT_LIMIT)


class DigestCache:
    """Digests of events.jsonl files, updated by reading only the new lines. The job page polls every
    1.5 s while a job runs, and re-reading the whole log each time made that cost grow with the
    square of the job's length."""

    def __init__(self, size: int = 8):
        import threading
        from collections import OrderedDict
        self.size, self.lock, self.entries = size, threading.Lock(), OrderedDict()

    def get(self, path: Path) -> Digest:
        key = str(path)
        with self.lock:
            try:
                st = path.stat()
            except OSError:
                self.entries.pop(key, None)
                return Digest()
            ent = self.entries.get(key)
            if not ent or ent["ino"] != st.st_ino or st.st_size < ent["offset"]:
                ent = {"digest": Digest(), "offset": 0, "ino": st.st_ino, "rest": b""}
            if st.st_size > ent["offset"]:
                with path.open("rb") as f:
                    f.seek(ent["offset"])
                    data = ent["rest"] + f.read(st.st_size - ent["offset"])
                ent["offset"] = st.st_size
                lines = data.split(b"\n")
                ent["rest"] = lines.pop()  # a line still being written
                for line in lines:
                    try:
                        ent["digest"].feed(json.loads(line))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
            self.entries[key] = ent
            self.entries.move_to_end(key)
            while len(self.entries) > self.size:
                self.entries.popitem(last=False)
            return ent["digest"]
