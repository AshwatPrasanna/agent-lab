"""Fixed texts: runtime rules every agent gets, the self-test, and the self-improvement prompts."""

# Appended to every agent's system prompt by the app. Not part of the editable/versioned
# config, so neither you nor the self-improvement step can accidentally delete it.
RUNTIME_RULES = """# Agent Lab runtime rules (set by the app; these always apply)

## Sandbox facts
- Shell commands run inside a macOS sandbox. They can read and write only this job's workspace and $LAB_SHARED. Other folders, `ps`, `pkill`, `sysctl` and localhost are blocked. Don't probe these limits or try to work around them.
- You can't list or kill processes, so bound every long-running command: use the Bash tool's timeout, or a time limit inside the script. Never start an unbounded background process.
- Downloads are preconfigured: TLS certificates (SSL_CERT_FILE), Hugging Face (HF_HUB_DISABLE_XET=1) and all caches in $LAB_SHARED. If a download fails, read the error and retry once. If it fails again, report the problem rather than diagnosing the network.
- Set environment variables inside scripts, not as a `VAR=value command` prefix.

## GPU
- The sandbox hides the GPU, so Metal, MLX and PyTorch MPS report no device in normal commands. CPU inference or training of real models is extremely slow here, so use the GPU for any model work.
- To run on the Mac's GPU, use the GPU launcher as ONE plain command from the workspace, exactly in this form:
  `{LAB_GPU} .venv/bin/python path/to/script.py --your-args`
- Write it with no `cd`, `&&`, `;`, pipes, redirects, `$(...)` or `VAR=value` prefix. Any of these makes it run without the GPU. Put setup, environment variables and log redirection inside the script.
- The launched program can read and write only this workspace and $LAB_SHARED, and can't reach localhost. Use the launcher only for steps that need the GPU.

## Permissions
- If a tool call needs approval, Agent Lab asks the user and waits for their answer. If the request is denied, don't retry the same thing: work around it or stop.

## When you're stuck, stop
- You're stuck if: the same error persists after 3 genuinely different attempts; you need something that the sandbox or a denied permission blocks; or a step has made no visible progress for 15 minutes.
- When you're stuck, stop immediately instead of retrying or polling:
  - write what you tried and the blocker to SUMMARY.md
  - run `notify "<one-line blocker>" "Job stopped"`
  - finish
- Failing early is fine, and it's better than looping.
- A subagent that gets stuck returns to its caller right away with the blocker. The orchestrator then decides whether another approach is worth one try, or whether the job should stop.
- This job has a hard time limit of {TIME_LIMIT}. Plan so the toy run finishes well within it.

## Final message (orchestrator)
- End your final message with exactly one status line: `STATUS: SUCCESS` or `STATUS: FAILED: <short reason>`."""

METAL_CHECK = ("-c \"import ctypes;m=ctypes.CDLL('/System/Library/Frameworks/Metal.framework/Metal');"
               "f=m.MTLCreateSystemDefaultDevice;f.restype=ctypes.c_void_p;print('GPU_OK' if f() else 'NO_GPU')\"")

SELFTEST_PROMPT = """This is a sandbox self-test. Run each check below as its own separate Bash call, copying the command exactly as written: don't add redirects, pipes, `cd` or anything else. Record what happened. Don't try to work around any failure.
1. read_home: `head -c 80 ~/.zshrc || head -c 80 ~/.bash_profile`
2. list_documents: `ls ~/Documents | head -5`
3. write_home: `touch ~/agent-lab-sandbox-probe && echo WROTE`
4. write_workspace: `echo ok > ./probe.txt && cat ./probe.txt`
5. internet: `curl -sS -o /dev/null -w '%{{http_code}}' https://huggingface.co`
6. read_tool_home: use the Read tool (not Bash) on the file ~/.zshrc
7. write_shared: `echo ok > "$LAB_SHARED/probe.txt" && cat "$LAB_SHARED/probe.txt"`
8. gpu_in_sandbox: `{PY} {METAL}`
9. gpu_launcher: `{LAB_GPU} {PY} {METAL}`
10. gpu_list_documents: `{LAB_GPU} /bin/ls {HOME}/Documents`
11. gpu_write_home: `{LAB_GPU} /usr/bin/touch {HOME}/agent-lab-gpu-probe`
12. gpu_write_app: `{LAB_GPU} /usr/bin/touch {APP}/gpu-probe`
13. gpu_localhost: `{LAB_GPU} /usr/bin/curl -sS -m 5 -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{PORT}/api/status`
For each check, report outcome:
- "blocked" if it was refused or failed with a permission or sandbox error
- "allowed" if it succeeded
- "error" for any other failure
For checks 8 and 9, report "allowed" if the output says GPU_OK and "blocked" if it says NO_GPU. Put a short excerpt of the output in evidence."""

SELFTEST_EXPECT = {"read_home": "blocked", "list_documents": "blocked", "write_home": "blocked",
                   "write_workspace": "allowed", "internet": "allowed", "read_tool_home": "blocked",
                   "write_shared": "allowed", "gpu_in_sandbox": "blocked", "gpu_launcher": "allowed",
                   "gpu_list_documents": "blocked", "gpu_write_home": "blocked", "gpu_write_app": "blocked",
                   "gpu_localhost": "blocked"}

SELFTEST_SCHEMA = {
    "type": "object",
    "properties": {"checks": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "outcome": {"type": "string", "enum": ["blocked", "allowed", "error"]},
        "evidence": {"type": "string"}}, "required": ["id", "outcome", "evidence"]}}},
    "required": ["checks"],
}

DIAGNOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One sentence (max 160 chars): the main thing that went wrong or slowed the run, or that it was clean."},
        "issues": {"type": "array", "maxItems": 5, "items": {"type": "object", "properties": {
            "title": {"type": "string"},
            "evidence": {"type": "string", "description": "What in the run shows this (times, commands, errors)."},
            "cost": {"type": "string", "description": "Rough cost: minutes, turns, or 'caused failure'."},
            "fixable_by_prompts": {"type": "boolean"}},
            "required": ["title", "evidence", "cost", "fixable_by_prompts"]}},
        "draft": {"type": "string", "description": "The improvement request the user will review and submit."},
    },
    "required": ["headline", "issues", "draft"],
}

DIAGNOSE_PROMPT = """You are reviewing one finished run of a multi-agent Claude Code system. The run's agents prototype ML experiments in a sandbox on a Mac.
These files are in your working directory:
- status.json: final status, error, time limit, and whether the agents stopped themselves
- metrics.json: turns, tokens, tool calls per agent, errors, slowest calls, repeated commands, delegations, guard decisions
- transcript.md: a condensed chronological activity log with timestamps
- permissions.json: permission requests sent to the user and their answers
- result.md: the final message
- config.json: the editable configuration (base_prompt shared by every agent, plus each node's prompt and description)
- runtime_rules.md: fixed rules that the app always adds. These can't be edited. Don't propose duplicating them, but you can propose prompt text that helps agents apply them.

Find the 1 to 4 issues that cost the most time or caused failure. Base each one on concrete evidence from the files.
- Mark fixable_by_prompts=false for causes outside the agents' control, such as a sandbox limit, a network outage, a missing GPU, or the user denying a permission.
- For those issues, still say what prompt changes would have helped the agents handle the situation faster. For example: detect it early, stop sooner, choose a cheaper fallback.

Then write `draft`: an improvement request in plain language, addressed to the editor who will change the agents' prompts.
- List concrete changes: which agent (or base_prompt), what instruction to add or change, and why, citing the evidence briefly.
- Keep every change general enough to help future experiments, not just this one.
- The user will read and edit the draft before submitting it, so make it clear and specific, at most about 200 words.
- If the run was clean and efficient, say so in the draft and propose no changes."""

IMPROVE_PROMPT = """You are improving the configuration of a multi-agent Claude Code system, based on one finished run.
These files are in your working directory:
- request.md: THE USER'S IMPROVEMENT REQUEST. This is your primary instruction: implement it.
- metrics.json, transcript.md, result.md, status.json, permissions.json: evidence from the run
- config.json: the current editable configuration (base_prompt shared by every agent, plus nodes with prompt and description)
- runtime_rules.md: fixed rules the app always adds. Don't copy them into the config.

Implement the request as edits to the prompts and descriptions. Use the run evidence to get the details right.
Rules:
- Edits must generalize to future experiments. Don't hard-code details of this particular experiment.
- Never weaken or remove the sandbox/environment rules or the deliverables list. You may clarify them.
- Change only what the request and evidence call for. Keep each prompt focused. new_text is the complete new text of the field.
- If the request asks for something that prompt edits can't achieve, make the closest useful edit and say what you couldn't do in the summary.
- The summary is shown to the user in a small box: plain language, at most 280 characters."""

IMPROVE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "One or two sentences (max 280 chars) for the user: what changed and why."},
        "changes": {"type": "array", "maxItems": 6, "items": {"type": "object", "properties": {
            "target": {"type": "string", "description": "'base_prompt' or the name of a node"},
            "field": {"type": "string", "enum": ["prompt", "description"]},
            "new_text": {"type": "string", "description": "Complete replacement text for that field"},
            "reason": {"type": "string", "description": "The problem this fixes, citing evidence from the run"}},
            "required": ["target", "field", "new_text", "reason"]}},
    },
    "required": ["summary", "changes"],
}
