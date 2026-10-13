#!/usr/bin/env python3
"""PermissionRequest hook: forwards a permission prompt to the Agent Lab web app and
waits for you to click Allow or Deny there (you also get an ntfy push).

Claude Code runs this hook outside the sandbox whenever a tool call would otherwise
need someone to approve it. Jobs have no terminal to approve in, so without this
hook those calls would simply be refused.

usage: permission_hook.py <job_id> <token> <port> <timeout_seconds>
"""
import json
import sys
import time
import urllib.request


def call(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-Agent-Lab": "hook"},
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def answer(behavior, message=None):
    d = {"behavior": behavior}
    if message:
        d["message"] = message
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": d}}))
    sys.exit(0)


def main():
    jid, token, port, timeout = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    try:
        data = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        answer("deny", "Agent Lab could not read this permission request.")
    base = f"http://127.0.0.1:{port}/api/jobs/{jid}/permissions"
    try:
        req = call(f"{base}?token={token}", {"tool_name": data.get("tool_name"), "tool_input": data.get("tool_input"),
                                             "agent": data.get("agent_type") or ("subagent" if data.get("agent_id") else "orchestrator"),
                                             "suggestions": data.get("permission_suggestions")})
    except Exception as e:  # noqa: BLE001
        answer("deny", f"Agent Lab could not ask the user ({e}). Treat this as denied; don't retry it.")
    if req.get("status") == "allowed":  # auto-allow is on in Agent Lab
        answer("allow")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        try:
            st = call(f"{base}/{req['id']}?token={token}")
        except Exception:  # noqa: BLE001
            continue
        if st.get("status") == "allowed":
            answer("allow")
        if st.get("status") == "denied":
            msg = "The user denied this request."
            if st.get("message"):
                msg += f" Their note: {st['message']}"
            answer("deny", msg + " Don't retry it. Work around it, or stop if you can't.")
    try:
        call(f"{base}/{req['id']}/expire?token={token}", {})
    except Exception:  # noqa: BLE001
        pass
    answer("deny", f"The user didn't answer within {timeout // 60} minutes, so this was denied. "
                   "Don't retry it. Work around it, or stop and report the blocker.")


if __name__ == "__main__":
    main()
