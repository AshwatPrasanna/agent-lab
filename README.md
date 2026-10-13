# Agent Lab

A small localhost web app for macOS that runs [Claude Code](https://code.claude.com) on ML experiment specs. Each experiment is handled by a team of subagents whose structure you draw. Agent Lab sandboxes every job, sends phone notifications, and improves its own prompts from each run, with your review before anything changes.

It's meant for prototyping an experiment at toy scale on a Mac before scaling it up on a Slurm cluster.

_Note: The app was built by Claude, using co-authored (human and AI) specifications. Although it's designed to be safe, at your own risk._

## Features

- **Specs as text or files.** Paste a spec, or attach a PDF, Markdown, text or LaTeX file. Every job gets its own workspace folder.
- **A topology you draw.** A graph editor sets up the orchestrator and its subagents:
  - **Delegate** edges: who may spawn whom, nested up to 3 levels.
  - **Handoff** edges: whose output feeds whom.
  - **Require delegation** stops the orchestrator from doing the work itself. A hook enforces it.
  - Every save is a new version, and you can restore any earlier one.
- **Sandboxed jobs with internet access.** Jobs run in Claude Code's built-in macOS sandbox. See [Isolation](#isolation).
- **GPU access.** The sandbox hides the GPU, so `lab-gpu` runs GPU commands (MLX, PyTorch MPS) under a separate, stricter sandbox profile.
- **Permission requests in the browser.** When a job needs approval, it waits, and the request appears in the web app with Allow and Deny buttons and a push notification. An **auto-allow** switch is available and off by default.
- **Self-improvement.** After every run, Claude analyzes what slowed it down or made it fail and drafts an improvement request. You edit it and submit it, or leave it. The prompts are then rewritten as a new config version that you can undo. Fixed runtime rules, such as the sandbox facts and "stop when stuck", can't be edited away.
- **Job control.**
  - Pause and resume freeze the whole process tree, including training runs.
  - Cancel stops everything a job started.
  - Each job has a time limit (paused time doesn't count).
  - Agents are told to stop and report rather than loop when they're stuck.
- **Building on earlier jobs.** A new job can get copies of earlier jobs' workspaces in `./prior/<job-id>/`. These are APFS clones, so they're instant and free on disk. **Make a Slurm version** turns a working local prototype into a cluster job: an sbatch script, the config for the full run, and instructions. It never submits anything.
- **Notifications** through [ntfy](https://ntfy.sh) when a job finishes, fails, times out, or needs permission.

## Requirements

- macOS on Apple Silicon. Linux would need a different sandbox profile for `lab-gpu`.
- [Claude Code](https://code.claude.com), installed and signed in. Run `claude` once.
- `python3` 3.9 or later to run the app. The app uses only the standard library.

## Getting started

1. Put the `agent-lab` folder on your Desktop, with `Agent Lab.app` next to it. Or keep it anywhere and use `start.command`.
2. Double-click **Agent Lab.app**, or `agent-lab/start.command`.
   - The first time, setup installs [uv](https://docs.astral.sh/uv/) and a Python 3.12 *inside the folder*. Your shell profile isn't touched.
   - Agent Lab starts in the background and opens at http://127.0.0.1:8765, and the Terminal window closes itself.
3. In **Settings**, set an ntfy topic, then run the **sandbox self-test**. It checks that everything outside the lab is blocked, that internet access works, and that the GPU is visible through `lab-gpu`.

Stop Agent Lab from **Settings → Stop Agent Lab**. Its output goes to `runtime/server.log`.

## Isolation

Every job runs `claude -p` with settings written by the app (`jobs/<id>/settings.json`):

- **The OS sandbox** (Seatbelt) is on. `failIfUnavailable` is set, unsandboxed retries are not allowed, and bypass and auto permission modes are disabled.
- **`blockReadsOutsideWorkingDirectories`:** file tools and shell commands can read only the job's workspace and `shared/`. Writes are limited to the same folders. Credential folders (`~/.ssh`, `~/.aws` and so on) are also denied explicitly.
- **Nothing from your own setup is loaded:** no user settings, plugins, hooks, MCP servers or auto memory. `ANTHROPIC_API_KEY` is removed from the environment, so jobs use your Claude subscription.
- **`lab-gpu`** is excluded from Claude Code's sandbox and applies its own profile (`app/sandbox/gpu.sb`) instead. That profile allows reading and writing only the job's workspace and `shared/`, and blocks reading the rest of `/Users`, localhost (the app's own API), Apple Events, and launching other apps. The launcher and profile live in `app/`, which agents can't write to.
- **The web app only accepts requests from its own page.** It requires a custom header that other websites can't send.

System folders (`/usr`, `/opt`, …) stay readable, because programs need them. Claude Code's sandbox reduces risk but isn't a VM. Run the self-test after Claude Code updates.

## Layout

```
Agent Lab.app      Desktop launcher
agent-lab/
  app/             the server (standard-library Python), web UI, hooks, sandbox profile
  config/          topology versions and settings            (created at runtime; not in git)
  jobs/<id>/       one folder per job; work/ is the agent's workspace   (not in git)
  shared/          caches shared by jobs: datasets, Hugging Face, uv, Python   (not in git)
  runtime/         logs and setup markers                    (not in git)
```

## Notes

- The default topology uses Claude Code model aliases (`fable` for the orchestrator, `opus` for workers). Change them in the editor to match your plan.
- The estimated $ shown per job is Claude Code's API-price estimate, not a bill.
