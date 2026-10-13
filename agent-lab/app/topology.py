"""Validate a topology graph and compile it into Claude Code flags."""
from __future__ import annotations

import re

from presets import ALL_TOOLS, MODELS

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
RESERVED = {"general-purpose", "explore", "plan", "statusline-setup", "claude-code-guide", "fork"}
MAX_DEPTH = 3  # Claude Code's default nesting limit below the main session


def validate(cfg: dict) -> list[str]:
    errs: list[str] = []
    nodes = cfg.get("nodes") or []
    edges = cfg.get("edges") or []
    if not isinstance(cfg.get("base_prompt", ""), str):
        errs.append("base_prompt must be text")
    ids = [n.get("id") for n in nodes]
    if len(set(ids)) != len(ids):
        errs.append("node ids must be unique")
    names = [n.get("name", "") for n in nodes]
    if len(set(names)) != len(names):
        errs.append("node names must be unique")
    for n in nodes:
        nm = n.get("name", "")
        if not NAME_RE.match(nm):
            errs.append(f"'{nm}': names must be lowercase letters, digits or dashes, starting with a letter")
        if nm.lower() in RESERVED:
            errs.append(f"'{nm}' is a reserved Claude Code agent name")
        if n.get("model") not in MODELS and not str(n.get("model", "")).startswith("claude-"):
            errs.append(f"'{nm}': unknown model '{n.get('model')}'")
        if n.get("role") == "worker" and not (n.get("description") or "").strip():
            errs.append(f"'{nm}': a worker needs a description (Claude uses it to decide when to delegate)")
        bad = [t for t in n.get("tools", []) if t not in ALL_TOOLS]
        if bad:
            errs.append(f"'{nm}': unknown tools {bad}")
    orchs = [n for n in nodes if n.get("role") == "orchestrator"]
    if len(orchs) != 1:
        errs.append("there must be exactly one orchestrator node")
        return errs
    idset = set(ids)
    for e in edges:
        if e.get("from") not in idset or e.get("to") not in idset:
            errs.append("an edge points at a missing node")
        elif e.get("from") == e.get("to"):
            errs.append("self-loops are not allowed")
        elif e.get("type") not in ("delegate", "handoff"):
            errs.append("edge type must be delegate or handoff")
        elif e.get("type") == "delegate" and e.get("to") == orchs[0]["id"]:
            errs.append("nobody can delegate to the orchestrator")
    if cfg.get("require_delegation") and len(nodes) < 2:
        errs.append("'require delegation' needs at least one worker to delegate to")
    if errs:
        return errs
    # delegate graph: acyclic, every worker reachable, depth limit
    kids = children_map(cfg)
    root = orchs[0]["id"]
    depth = {root: 0}
    stack = [(root, frozenset([root]))]
    while stack:
        cur, path = stack.pop()
        for k in kids.get(cur, []):
            if k in path:
                errs.append("delegate edges form a cycle")
                return errs
            d = len(path)
            depth[k] = max(depth.get(k, 0), d)
            stack.append((k, path | {k}))
    for n in nodes:
        if n["id"] not in depth:
            errs.append(f"'{n['name']}' can't be reached: add a delegate edge to it")
    if depth and max(depth.values()) > MAX_DEPTH:
        errs.append(f"delegation is nested deeper than {MAX_DEPTH} levels (Claude Code's limit)")
    return errs


def children_map(cfg):
    kids: dict[str, list[str]] = {}
    for e in cfg.get("edges", []):
        if e.get("type") == "delegate":
            kids.setdefault(e["from"], []).append(e["to"])
    return kids


def _name(cfg, nid):
    return next(n["name"] for n in cfg["nodes"] if n["id"] == nid)


def node_context(cfg, node) -> str:
    nid = node["id"]
    kids = [_name(cfg, e["to"]) for e in cfg["edges"] if e["type"] == "delegate" and e["from"] == nid]
    feeds = [_name(cfg, e["to"]) for e in cfg["edges"] if e["type"] == "handoff" and e["from"] == nid]
    gets = [_name(cfg, e["from"]) for e in cfg["edges"] if e["type"] == "handoff" and e["to"] == nid]
    lines = [f"## Your place in the team (you are `{node['name']}`)"]
    if kids:
        lines.append("- You may delegate (with the Agent tool) ONLY to: " + ", ".join(f"`{k}`" for k in kids) + ".")
    else:
        lines.append("- You do not delegate. Do the work yourself.")
    if gets:
        lines.append("- Your inputs come from: " + ", ".join(f"`{g}`" for g in gets) + ". Their outputs are in `handoff/<name>/`.")
    if feeds:
        lines.append("- Your output feeds: " + ", ".join(f"`{f}`" for f in feeds) + ".")
    if node["role"] != "orchestrator":
        lines.append(f"- Write your notes and reports to `handoff/{node['name']}/`. Code goes in the normal project layout.")
        lines.append("- End with a concise summary: what you did, the files you created or changed, and any problems. Your caller only sees that summary.")
    return "\n".join(lines)


def topology_text(cfg) -> str:
    lines = ["## Team topology"]
    for n in cfg["nodes"]:
        if n["role"] == "orchestrator":
            continue
        lines.append(f"- `{n['name']}` ({n['model']}): {n['description']}")
    dl = [f"{_name(cfg, e['from'])} → {_name(cfg, e['to'])}" for e in cfg["edges"] if e["type"] == "delegate"]
    hl = [f"{_name(cfg, e['from'])} ⇢ {_name(cfg, e['to'])}" for e in cfg["edges"] if e["type"] == "handoff"]
    if dl:
        lines.append("\nDelegation edges (who may spawn whom): " + "; ".join(dl))
    if hl:
        lines.append("Handoff edges (output of A becomes input of B). Whoever spawned both members routes the handoff: give B the paths to A's outputs. A loop such as tester ⇢ coder means iterate until the work passes, up to 3 rounds: " + "; ".join(hl))
    if len(cfg["nodes"]) == 1:
        lines.append("You have no team members. Do all of the work yourself.")
    return "\n".join(lines)


DELEGATION_RULES = """## Delegation is REQUIRED (enforced)
You coordinate. You do not do the work yourself, however small the task is.
- You may: read files (Read/Glob/Grep), write PLAN.md, SPEC_CONCERNS.md, SUMMARY.md and files in handoff/orchestrator/, use `notify`, and delegate with the Agent tool.
- You may not: run shell commands, write or edit code, install packages, download data, or run experiments or tests. A hook blocks these calls for you, so attempting them only wastes turns.
- Every piece of research, implementation, execution and testing goes to a team member. If no member is an exact fit, pick the closest one and say so in the task.
- Delegated tasks must be self-contained: goal, input paths, expected outputs, done criteria."""


def compile_config(cfg: dict) -> dict:
    """Return the pieces needed to launch claude for this topology."""
    orch = next(n for n in cfg["nodes"] if n["role"] == "orchestrator")
    kids = children_map(cfg)
    agents = {}
    for n in cfg["nodes"]:
        if n["role"] == "orchestrator":
            continue
        tools = list(n.get("tools") or [])
        if kids.get(n["id"]):
            tools.append("Agent(" + ", ".join(_name(cfg, k) for k in kids[n["id"]]) + ")")
        a = {"description": n["description"], "prompt": (n.get("prompt") or "") + "\n\n" + node_context(cfg, n), "tools": tools}
        if n.get("model") and n["model"] != "inherit":
            a["model"] = n["model"]
        agents[n["name"]] = a
    orch_tools = list(orch.get("tools") or [])
    if kids.get(orch["id"]):
        orch_tools.append("Agent")
    require = bool(cfg.get("require_delegation")) and bool(agents)
    parts = [cfg.get("base_prompt", ""), "## Your role\n" + (orch.get("prompt") or ""),
             topology_text(cfg), node_context(cfg, orch)]
    if require:
        parts.append(DELEGATION_RULES)
    system = "\n\n".join(parts)
    return {"model": orch.get("model", "fable"), "agents": agents, "system_append": system,
            "require_delegation": require,
            "subagent_append": cfg.get("base_prompt", ""), "orch_tools": orch_tools}
