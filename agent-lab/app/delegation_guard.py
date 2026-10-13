#!/usr/bin/env python3
"""PreToolUse hook installed on every Agent Lab job.

1. Shell commands are approved here. Every one of them runs inside the OS sandbox
   (or, for lab-gpu, inside Agent Lab's own GPU sandbox profile), so there's nothing
   for a person to approve. This stops harmless forms like `FOO=bar cmd` from being
   refused for lack of someone to approve them.
2. With --delegation, calls from the main session (the orchestrator) are limited to
   coordination: reading, delegating, writing the plan/notes, and `notify`.
   Claude Code passes `agent_id` only for calls made inside a subagent.

Every decision is appended to the audit log given with --log.
"""
import argparse
import json
import re
import sys
import time

READ_ONLY = {"Read", "Glob", "Grep", "LS", "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet"}
DELEGATION = {"Agent", "Task", "SendMessage"}
WRITABLE = re.compile(r"(^|/)(PLAN\.md|SPEC_CONCERNS\.md|SUMMARY\.md)$|(^|/)handoff/orchestrator/")
NOTIFY = re.compile(r"^\s*notify(\s|$)")


def decide(data: dict, delegation: bool = True):
    """Return (decision, reason). decision is 'allow', 'deny' or None (= no opinion)."""
    tool = data.get("tool_name", "")
    inp = data.get("tool_input") or {}
    is_sub = bool(data.get("agent_id"))
    if delegation and not is_sub:
        if tool in READ_ONLY or tool in DELEGATION:
            return None, None
        if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            path = str(inp.get("file_path") or inp.get("notebook_path") or "")
            if WRITABLE.search(path):
                return None, None
            return "deny", (f"Delegation is required in this lab topology. The orchestrator may only write PLAN.md, "
                            f"SPEC_CONCERNS.md, SUMMARY.md or files in handoff/orchestrator/, not '{path}'. "
                            "Delegate this work to the right team member with the Agent tool.")
        if tool == "Bash":
            cmd = str(inp.get("command", ""))
            outside_quotes = re.sub(r'"[^"`$\\]*"|\'[^\']*\'', "", cmd)
            if NOTIFY.match(cmd) and not re.search(r"[;&|`$<>()\n\"'\\]", outside_quotes):
                return "allow", "notify"
            return "deny", ("Delegation is required in this lab topology: the orchestrator does not run shell commands. "
                            "Use Read/Glob/Grep to inspect files, and delegate running, installing, downloading "
                            "and testing to a team member with the Agent tool.")
        return "deny", (f"Delegation is required in this lab topology: the orchestrator does not use {tool}. "
                        "Delegate this to a team member with the Agent tool.")
    if tool == "Bash":
        return "allow", "shell commands run inside the OS sandbox"
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log")
    ap.add_argument("--delegation", action="store_true")
    ap.add_argument("legacy_log", nargs="?")  # older jobs passed the log path positionally
    a = ap.parse_args()
    try:
        data = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        sys.exit(0)
    if a.legacy_log and not a.log:
        a.log, a.delegation = a.legacy_log, True
    decision, reason = decide(data, a.delegation)
    if a.log:
        try:
            inp = data.get("tool_input") or {}
            with open(a.log, "a") as f:
                f.write(json.dumps({"t": time.time(), "tool": data.get("tool_name"),
                                    "agent": data.get("agent_type") or ("subagent" if data.get("agent_id") else "orchestrator"),
                                    "decision": decision or "pass", "reason": reason,
                                    "input": str(inp.get("command") or inp.get("file_path") or "")[:200]}) + "\n")
        except OSError:
            pass
    if decision:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": decision,
                                                 "permissionDecisionReason": reason}}))
    sys.exit(0)


if __name__ == "__main__":
    main()
