# Agent Lab

A small localhost web app for macOS that runs [Claude Code](https://code.claude.com) on ML experiment specs. Each experiment is handled by a team of subagents whose structure you draw. Agent Lab sandboxes every job, sends phone notifications, and improves its own prompts from each run, with your review before anything changes.

It's meant for prototyping an experiment at toy scale on a Mac before scaling it up on a Slurm cluster.

_Note: The app was built by Claude, using co-authored (human and AI) specifications. Although it's designed to be safe, at your own risk._

## Features

- **Specs as text or files.** Paste a spec, or attach files of any type: a PDF, Markdown, Python pseudocode, a .zip of data. Zips are unpacked for the agents. Every job gets its own workspace folder.
- **Access to your own folders (optional).** When you start a job, you can list folders on your Mac that the agents may read and change, for example your Desktop or one project folder, so a job can work on an existing project. Agent Lab's own files stay protected inside those folders: its code, settings, other jobs and the app icon. The self-test checks this.
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
- **Ask: a follow-up chat on each finished job.** One assistant works in that job's folder, in the same sandbox, with the agents' activity log as context. Use it for questions about the run and for small fixes; it can read and edit files, run tests and use the GPU. The conversation is held only in memory, never written to disk: it's gone when you start a new chat or stop Agent Lab. File changes it makes are real.
- **Modify app: Agent Lab changes itself.** Describe a change to the app in the **Modify app** tab. One assistant edits a copy of the source code. Agent Lab then checks the result: it must compile, start, answer its API and keep the sandbox settings. If it passes, Agent Lab applies it (keeping a backup), restarts once no job is running, and pushes it to your GitHub repository if you want. Changes to the sandbox files always wait for your review. **Undo** puts back the latest change, and if a change ever stops the app from starting, double-click `restore-app.command`. Before pushing, Agent Lab syncs with changes made on GitHub, and it refuses to publish your name, username, home folder, tokens, ntfy topic or cluster name.
- **Storage watch.** Agent Lab measures its folder every 15 minutes and after each job. Above a limit you set (15 GB by default), you get a notification and a warning at the top of the page. **Settings → Storage** shows what's using space: jobs, their virtual environments, downloaded models, and package caches. It can delete finished jobs' virtual environments, single models, or unused packages. If free disk space drops below a minimum (3 GB by default), running jobs are paused.
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
3. In **Settings**, set an ntfy topic, then run the **sandbox self-test**. It checks that everything outside the lab is blocked, that internet access works, and that the GPU is visible through `lab-gpu`. If the lab folder is inside a folder you can grant (like the Desktop), it also checks that Agent Lab's own files stay protected when that folder is granted.
4. Optional: to have the **Modify app** tab publish changes, set your GitHub repository (`owner/name`) in **Settings**. Pushing uses git on your Mac (Apple's command line tools). If git isn't signed in to GitHub, add a fine-grained token with *Contents: read and write* on that repository.

Stop Agent Lab from **Settings → Stop Agent Lab**. Its output goes to `runtime/server.log`.

## Isolation

Every job runs `claude -p` with settings written by the app (`jobs/<id>/settings.json`):

- **The OS sandbox** (Seatbelt) is on. `failIfUnavailable` is set, unsandboxed retries are not allowed, and bypass and auto permission modes are disabled.
- **`blockReadsOutsideWorkingDirectories`:** file tools and shell commands can read only the job's workspace and `shared/`. Writes are limited to the same folders. Credential folders (`~/.ssh`, `~/.aws` and so on) are also denied explicitly.
- **Nothing from your own setup is loaded:** no user settings, plugins, hooks, MCP servers or auto memory. `ANTHROPIC_API_KEY` is removed from the environment, so jobs use your Claude subscription.
- **`lab-gpu`** is excluded from Claude Code's sandbox and applies its own profile (`app/sandbox/gpu.sb`) instead. That profile allows reading and writing only the job's workspace and `shared/`, and blocks reading the rest of `/Users`, localhost (the app's own API), Apple Events, and launching other apps. The launcher and profile live in `app/`, which agents can't write to.
- **Granted folders** are added as working directories, so agents can read and write them. Agent Lab's code, settings, runtime files, other jobs, the job's own records and the app icon stay off-limits through deny rules for the sandbox and the file tools. `lab-gpu` applies the same rules.
- **Modify app** runs one session in a copy of the source under `runtime/`, in the same sandbox. It can't reach the running app's files. Agent Lab itself applies the change, after its checks.
- **The Ask chat** uses the same sandbox and permission handling. Its settings and context are passed on the command line rather than written to disk, and it runs with `--no-session-persistence`.
- **The web app only accepts requests from its own page.** It requires a custom header that other websites can't send, and it only answers requests addressed to `127.0.0.1` or `localhost`, which blocks DNS-rebinding tricks. Tokens in Settings are never sent back to the page. Uploads are streamed to disk, not held in memory.

System folders (`/usr`, `/opt`, …) stay readable, because programs need them. Claude Code's sandbox reduces risk but isn't a VM. Run the self-test after Claude Code updates.

## Layout

```
Agent Lab.app      Desktop launcher
agent-lab/
  app/             the server (standard-library Python), web UI, hooks, sandbox profile
  config/          topology versions and settings            (created at runtime; not in git)
  jobs/<id>/       one folder per job; work/ is the agent's workspace   (not in git)
  shared/          caches shared by jobs: datasets, Hugging Face, uv, Python   (not in git)
  runtime/         logs, setup markers, and Modify app changes with their backups   (not in git)
  restore-app.command   puts back the code from before the last Modify app change
```

## Notes

- The default topology uses Claude Code model aliases (`fable` for the orchestrator, `opus` for workers). Change them in the editor to match your plan.
- The estimated $ shown per job is Claude Code's API-price estimate, not a bill.
