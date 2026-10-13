"""Default lab instructions and topology presets."""
from __future__ import annotations

import copy

ALL_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebFetch", "WebSearch"]
MODELS = ["fable", "opus", "sonnet", "haiku", "inherit"]

BASE_PROMPT = """# Agent Lab: shared instructions

You are working inside Agent Lab. It is a local sandbox for prototyping ML experiments at toy scale on a Mac, before they are scaled up on a Slurm cluster with NVIDIA GPUs.

## Environment
- Your working directory is this job's workspace. The experiment spec is in `./spec.*`. The Read tool reads PDFs directly.
- A shared cache that persists across jobs is at `$LAB_SHARED`. Put datasets in `$LAB_SHARED/datasets/<name>/`, and reuse a dataset if it is already there. Hugging Face, torch and pip caches already point there.
- You cannot read or write anything outside the workspace and `$LAB_SHARED`. Do not try to.
- You have internet access for datasets, packages and documentation.
- Python: never use the system or conda Python. Create the env with `uv venv --python 3.12 .venv`, then install with `uv pip install --python .venv/bin/python <pkgs>` and run with `.venv/bin/python`. uv caches downloads, so reinstalling is cheap.
- `notify "<message>" "<title>"` sends a phone push notification to the user. Use it only for things the user should know about while you keep working.

## Code requirements
- Code must be device-agnostic: use CUDA if available, then Apple MPS, then CPU. Never hard-code a device.
- Local runs are toy-scale. Subsample the data and use few steps, so the toy run finishes in minutes. Expose the scale through config/CLI flags (for example `--scale toy|full`), so the full run on the cluster needs no code changes.
- Deliverables in the workspace:
  - the code in `src/`
  - `README.md` with how to run it
  - `run_local.sh`
  - `slurm/job.sbatch`, a template only: never submit it, and leave partition/account as clearly marked placeholders
  - pytest tests in `tests/` that pass
  - `RESULTS.md` with the toy-run numbers

## Working rules
- If the spec is ambiguous, contradictory or infeasible: write the concern to `SPEC_CONCERNS.md`, run `notify "<one-line concern>" "Spec concern"`, pick the most reasonable interpretation, and continue. Do not stop.
- Prefer small, verifiable steps. When a command fails, read the error before retrying. Never retry the same failing command unchanged.
- Keep long-running commands bounded (timeouts, small subsets).
"""

ORCH_PROMPT = """You are the orchestrator for this experiment.
1. Read the spec. Write a short plan to `PLAN.md`: goal, dataset(s), model, metrics, toy-scale settings, and which team member does what.
2. Delegate along your topology edges. Give each delegate a self-contained task: the goal, input file paths, the expected output files, and done criteria. Do not do heavy implementation yourself when a team member covers it.
3. Route each handoff: pass one member's output files to the next member, as the topology says.
4. After testing, decide whether another fix round is needed. Do at most 3 rounds.
5. Finish with a final summary: what was built, test status, toy-run results, how to run it locally and on the cluster, and any open spec concerns."""


def _node(id, role, model, description, prompt, tools, x, y):
    return {"id": id, "name": id, "role": role, "model": model, "description": description,
            "prompt": prompt, "tools": list(tools), "x": x, "y": y}


RESEARCHER = ("Finds and downloads datasets, reference implementations and hyperparameters the experiment needs.",
              """You are the researcher. Find and fetch what the experiment needs:
- Datasets: download to `$LAB_SHARED/datasets/<name>/`, and reuse one if it is already there. For toy runs, prefer small subsets or splits.
- Reference implementations, papers, library versions, and the key hyperparameters from the spec or paper.
Write your findings to your handoff folder as RESEARCH.md: dataset paths and format, a loading snippet, hyperparameters, and pitfalls. Be concise.""")
CODER = ("Implements the experiment code, scripts and Slurm template from the plan and research notes.",
         """You are the coder. Implement the experiment in `src/`, following PLAN.md and any research notes you are given.
- Use config via CLI flags or YAML, with a toy/full scale switch. The code must be device-agnostic.
- Write `run_local.sh`, `slurm/job.sbatch` (template only) and `README.md`.
- Keep the code minimal and clean. Before handing off, run a quick smoke test yourself.""")
TESTER = ("Writes and runs tests and the toy run, and reports failures precisely.",
          """You are the tester. Write pytest tests in `tests/`: data loading, tensor shapes, a 1-2 step training smoke test, and seeded determinism. Run them, then run the toy run via `run_local.sh`.
Write REPORT.md to your handoff folder with: pass/fail, exact error excerpts, the suspected cause, and the toy-run metrics. Small fixes are fine. Report larger problems back instead of rewriting `src/`.
If everything passes, write `RESULTS.md`.""")


def preset_orchestrator_workers():
    return {
        "base_prompt": BASE_PROMPT,
        "nodes": [
            _node("orchestrator", "orchestrator", "fable", "Plans the experiment and coordinates the team.", ORCH_PROMPT, ALL_TOOLS, 90, 230),
            _node("researcher", "worker", "opus", *RESEARCHER, ALL_TOOLS, 420, 80),
            _node("coder", "worker", "opus", *CODER, ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebFetch"], 420, 230),
            _node("tester", "worker", "opus", *TESTER, ["Read", "Write", "Edit", "Bash", "Glob", "Grep"], 420, 380),
        ],
        "edges": [
            {"from": "orchestrator", "to": "researcher", "type": "delegate"},
            {"from": "orchestrator", "to": "coder", "type": "delegate"},
            {"from": "orchestrator", "to": "tester", "type": "delegate"},
            {"from": "researcher", "to": "coder", "type": "handoff"},
            {"from": "coder", "to": "tester", "type": "handoff"},
            {"from": "tester", "to": "coder", "type": "handoff"},
        ],
    }


def preset_pipeline():
    cfg = preset_orchestrator_workers()
    pos = {"orchestrator": (60, 230), "researcher": (300, 230), "coder": (540, 230), "tester": (780, 230)}
    for n in cfg["nodes"]:
        n["x"], n["y"] = pos[n["id"]]
    cfg["edges"] = [
        {"from": "orchestrator", "to": "researcher", "type": "delegate"},
        {"from": "orchestrator", "to": "coder", "type": "delegate"},
        {"from": "orchestrator", "to": "tester", "type": "delegate"},
        {"from": "researcher", "to": "coder", "type": "handoff"},
        {"from": "coder", "to": "tester", "type": "handoff"},
    ]
    return cfg


def preset_hierarchical():
    """Coder owns its own tester (nested subagent)."""
    cfg = preset_orchestrator_workers()
    pos = {"orchestrator": (80, 230), "researcher": (360, 110), "coder": (360, 330), "tester": (640, 330)}
    for n in cfg["nodes"]:
        n["x"], n["y"] = pos[n["id"]]
    cfg["edges"] = [
        {"from": "orchestrator", "to": "researcher", "type": "delegate"},
        {"from": "orchestrator", "to": "coder", "type": "delegate"},
        {"from": "coder", "to": "tester", "type": "delegate"},
        {"from": "researcher", "to": "coder", "type": "handoff"},
    ]
    return cfg


def preset_solo():
    return {
        "base_prompt": BASE_PROMPT,
        "nodes": [_node("orchestrator", "orchestrator", "fable", "Does everything itself.",
                        "You work alone: plan, research, implement, test, and report.", ALL_TOOLS, 200, 230)],
        "edges": [],
    }


PRESETS = {
    "orchestrator-workers": ("Orchestrator + workers (with feedback loop)", preset_orchestrator_workers),
    "pipeline": ("Pipeline: research → code → test", preset_pipeline),
    "hierarchical": ("Hierarchical: coder owns tester (nested)", preset_hierarchical),
    "solo": ("Solo agent (no subagents)", preset_solo),
}


def get_preset(key: str) -> dict:
    return copy.deepcopy(PRESETS[key][1]())
