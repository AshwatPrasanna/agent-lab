"""Agent Lab: a localhost front end for running sandboxed Claude Code experiment jobs."""
from __future__ import annotations

import copy
import hmac
import json
import secrets
import os
import re
import shlex
import shutil
import sys
import signal
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

from webkit import App, FileResp, HTTPException

import chat as chatmod
import presets
import prior
import procs
import rules
import selfdev
import storage
import topology
from events import DigestCache, load_digest, shrink_event, transcript_markdown

LAB = Path(__file__).resolve().parent.parent
APP = LAB / "app"
CONFIG = LAB / "config"
VERSIONS = CONFIG / "versions"
JOBS = LAB / "jobs"
SHARED = LAB / "shared"
SETTINGS_FILE = CONFIG / "settings.json"
for d in (VERSIONS, JOBS, SHARED / "datasets", SHARED / "bin", SHARED / "cache"):
    d.mkdir(parents=True, exist_ok=True)

NOTIFY_SCRIPT = r"""#!/bin/sh
# usage: notify "message" ["title"]  -> push notification to the user's phone via ntfy
if [ -z "$LAB_NTFY_URL" ]; then echo "notify: no ntfy topic configured (message was: $1)"; exit 0; fi
if [ -n "$LAB_NTFY_TOKEN" ]; then AUTH="Authorization: Bearer $LAB_NTFY_TOKEN"; else AUTH="X-Agent-Lab: 1"; fi
curl -sS -m 15 -H "Title: ${2:-Agent Lab}" -H "Tags: warning" -H "$AUTH" -d "$1" "$LAB_NTFY_URL" >/dev/null && echo "notified"
"""
_ns = SHARED / "bin" / "notify"
_ns.write_text(NOTIFY_SCRIPT)
_ns.chmod(0o755)

LOCK = threading.RLock()
PROCS: dict[str, subprocess.Popen] = {}
SERVER_PORT = int(os.environ.get("AGENT_LAB_PORT", 8765))
LAB_GPU = str(APP / "bin" / "lab-gpu")
CERT_FILE = "/private/etc/ssl/cert.pem" if Path("/private/etc/ssl/cert.pem").exists() else "/etc/ssl/certs/ca-certificates.crt"
ACTIVE = ("running", "queued", "paused", "cancelling")

DEFAULT_SETTINGS = {"ntfy_server": "https://ntfy.sh", "ntfy_topic": "", "ntfy_token": "",
                    "notify_on": ["done", "failed", "timed_out"], "max_turns": None, "improver_model": "",
                    "time_limit_min": 120, "permission_timeout_min": 30, "auto_analyze": True,
                    "auto_allow_permissions": False, "cluster_name": "", "chat_model": "",
                    "selfdev_model": "", "github_repo": "", "github_token": "", "auto_push": True, "private_words": "",
                    "storage_limit_gb": 15, "min_free_gb": 3}
SECRET_SETTINGS = ("ntfy_token", "github_token")  # never sent back to the page
SECRET_MASK = "••••••••"

app = App(static_dir=Path(__file__).resolve().parent / "static", upload_dir=LAB / "runtime" / "uploads")
DIGESTS = DigestCache(8)


def code_hash() -> str:
    """Fingerprint of the app's code, to tell when files changed after this process started."""
    import hashlib
    h = hashlib.sha256()
    for f in sorted(list(APP.glob("*.py")) + list((APP / "static").glob("*")) + list((APP / "bin").glob("*"))
                    + list((APP / "sandbox").glob("*"))):
        if f.is_file():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


BOOT_HASH = code_hash()
BOOT_TIME = time.time()


# --------------------------------------------------------------------------- helpers
def read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):  # missing, not a directory, unreadable, half-written
        return copy.deepcopy(default)


def job_dirs():
    """Job folders only: skips files macOS adds, like .DS_Store, and anything that isn't a job id."""
    return [p for p in JOBS.iterdir() if p.is_dir() and re.match(r"^[a-z0-9-]+$", p.name)]


def write_json(p: Path, obj):
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")  # O_EXCL: never follows a link
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(obj, indent=2))
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_settings() -> dict:
    s = copy.deepcopy(DEFAULT_SETTINGS)
    s.update(read_json(SETTINGS_FILE, {}) or {})
    return s


def find_claude() -> Optional[str]:
    cands = [os.environ.get("AGENT_LAB_CLAUDE"), shutil.which("claude"),
             str(Path.home() / ".local/bin/claude"), str(Path.home() / ".claude/local/claude"),
             "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
    for c in cands:
        if c and Path(c).exists() and os.access(c, os.X_OK):
            return c
    return None


_STATUS_CACHE: dict = {}


def claude_status(refresh=False) -> dict:
    if _STATUS_CACHE.get("path") and _STATUS_CACHE.get("logged_in") and not refresh \
            and time.time() - _STATUS_CACHE.get("_t", 0) < 300:
        return _STATUS_CACHE
    exe = find_claude()
    st = {"path": exe, "version": None, "version_tuple": None, "logged_in": None, "auth_method": None, "_t": time.time()}
    if exe:
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout
            m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
            if m:
                st["version"] = m.group(0)
                st["version_tuple"] = tuple(int(x) for x in m.groups())
        except Exception as e:  # noqa: BLE001
            st["error"] = str(e)
        try:
            p = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=20, env=clean_env())
            st["logged_in"] = p.returncode == 0
            try:
                st["auth_method"] = json.loads(p.stdout).get("authMethod")
            except json.JSONDecodeError:
                pass
        except Exception:  # noqa: BLE001
            pass
    _STATUS_CACHE.clear()
    _STATUS_CACHE.update(st)
    return st


def supports(minimum: tuple) -> bool:
    v = claude_status().get("version_tuple")
    return bool(v) and tuple(v) >= minimum


def clean_env(extra: Optional[dict] = None) -> dict:
    """Minimal environment: no API keys (so your Max subscription is used) and no stray secrets."""
    keep = ("HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM", "TMPDIR",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "CLAUDE_CONFIG_DIR")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    path = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env["PATH"] = f"{APP / 'bin'}:{SHARED / 'bin'}:{path}"
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    if extra:
        env.update(extra)
    return env


def lab_env(job_dir: Path) -> dict:
    s = get_settings()
    topic = s.get("ntfy_topic", "").strip()
    return {
        "LAB_SHARED": str(SHARED),
        "LAB_JOB_DIR": str(job_dir),
        "LAB_NTFY_URL": f"{s['ntfy_server'].rstrip('/')}/{topic}" if topic else "",
        "LAB_NTFY_TOKEN": s.get("ntfy_token", ""),
        "UV_CACHE_DIR": str(SHARED / "uv-cache"),
        "UV_PYTHON_INSTALL_DIR": str(SHARED / "python"),
        "UV_PYTHON_PREFERENCE": "only-managed",
        "UV_LINK_MODE": "copy",
        "HF_HOME": str(SHARED / "hf"),
        "TORCH_HOME": str(SHARED / "torch"),
        "XDG_CACHE_HOME": str(SHARED / "cache"),
        "MPLCONFIGDIR": str(SHARED / "cache" / "matplotlib"),
        "PIP_CACHE_DIR": str(SHARED / "cache" / "pip"),
        "KAGGLE_CONFIG_DIR": str(SHARED / "kaggle"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "SSL_CERT_FILE": CERT_FILE,
        "REQUESTS_CA_BUNDLE": CERT_FILE,
        "HF_HUB_DISABLE_XET": "1",
        "LAB_GPU": LAB_GPU,
        "PYTORCH_ENABLE_MPS_FALLBACK": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    }


def notify(title: str, message: str, tags: str = "", priority: str = "default") -> Optional[str]:
    s = get_settings()
    topic = s.get("ntfy_topic", "").strip()
    if not topic:
        return "no ntfy topic set"
    url = f"{s['ntfy_server'].rstrip('/')}/{topic}"
    headers = {"Title": title.encode("ascii", "ignore").decode() or "Agent Lab", "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if s.get("ntfy_token"):
        headers["Authorization"] = f"Bearer {s['ntfy_token']}"
    try:
        req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
        return None
    except Exception as e:  # noqa: BLE001
        return str(e)


# --------------------------------------------------------------------------- config versions
def version_files():
    return sorted(VERSIONS.glob("v*.json"), key=lambda p: int(p.stem[1:]))


def current_version() -> int:
    cur = read_json(CONFIG / "current.json", {})
    if cur and (VERSIONS / f"v{cur['version']}.json").exists():
        return cur["version"]
    files = version_files()
    return int(files[-1].stem[1:]) if files else 0


def load_version(n: int) -> dict:
    v = read_json(VERSIONS / f"v{n}.json")
    if not v:
        raise HTTPException(404, f"version {n} not found")
    return v


def save_version(cfg: dict, source: str, summary: str, **meta) -> dict:
    errs = topology.validate(cfg)
    if errs:
        raise HTTPException(400, {"errors": errs})
    with LOCK:
        files = version_files()
        n = int(files[-1].stem[1:]) + 1 if files else 1
        rec = {"version": n, "created": time.time(), "source": source, "summary": summary,
               "parent": current_version() or None, "config": cfg, **meta}
        write_json(VERSIONS / f"v{n}.json", rec)
        write_json(CONFIG / "current.json", {"version": n})
    return rec


if not version_files():
    save_version(presets.get_preset("orchestrator-workers"), "initial", "Default orchestrator + workers topology")


# --------------------------------------------------------------------------- jobs
def job_dir(jid: str) -> Path:
    if not re.match(r"^[a-z0-9-]+$", jid):
        raise HTTPException(400, "bad job id")
    return JOBS / jid


def load_job(jid: str) -> dict:
    j = read_json(job_dir(jid) / "job.json")
    if not j:
        raise HTTPException(404, "job not found")
    return j


def save_job(j: dict):
    with LOCK:
        write_json(job_dir(j["id"]) / "job.json", j)


def guard_summary(jd: Path) -> Optional[dict]:
    p = jd / "guard.jsonl"
    if not p.exists():
        return None
    rows = []
    for line in p.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    blocked = [r for r in rows if r.get("decision") == "deny"]
    return {"checked": len(rows), "blocked": len(blocked),
            "orchestrator_calls": sum(1 for r in rows if r.get("agent") == "orchestrator"),
            "subagent_calls": sum(1 for r in rows if r.get("agent") != "orchestrator"),
            "blocked_samples": [{"tool": r.get("tool"), "input": r.get("input")} for r in blocked[-8:]]}


def _hook(cmd: list[str], timeout: int) -> dict:
    return {"type": "command", "command": " ".join(shlex.quote(str(x)) for x in cmd), "timeout": timeout}


def sandbox_settings(jd: Optional[Path] = None, delegation: bool = False, job_id: Optional[str] = None,
                     access: Optional[list] = None) -> dict:
    """Claude Code settings for every job: OS sandbox on, no escape hatch, reads fenced to the workspace.
    With jd: the guard hook (approves sandboxed shell commands, enforces delegation) and the
    permission hook (forwards anything that needs approval to this app).
    With access: the extra folders the user granted to this job (see apply_access)."""
    s = _base_sandbox_settings(job_id or (jd.name if jd is not None else None))
    if jd is not None and access:
        apply_access(s, jd, access)
    if jd is not None:
        guard = [sys.executable, APP / "delegation_guard.py", "--log", jd / "guard.jsonl"] + (["--delegation"] if delegation else [])
        hooks = {"PreToolUse": [{"matcher": "*", "hooks": [_hook(guard, 15)]}]}
        if job_id:
            tout = int(get_settings().get("permission_timeout_min") or 30) * 60
            hooks["PermissionRequest"] = [{"matcher": "*", "hooks": [_hook(
                [sys.executable, APP / "permission_hook.py", job_id, perm_token(jd), SERVER_PORT, tout], tout + 60)]}]
        s["hooks"] = hooks
    return s


def gpu_prefix(job_id: str) -> str:
    """How agents of this job call the GPU launcher (the only form exempted from Claude Code's sandbox)."""
    return f"{LAB_GPU} --job {job_id}"


def _base_sandbox_settings(gpu_job: Optional[str] = None) -> dict:
    return {
        "autoMemoryEnabled": False,  # otherwise Claude Code writes notes to ~/.claude/projects/<job>/memory
        "permissions": {
            "allow": ["Bash", "WebFetch", "WebSearch", "WebFetch(domain:*)", "Agent", "TodoWrite"],
            "deny": ["Agent(general-purpose)", "Agent(Explore)", "Agent(Plan)",
                     "Agent(statusline-setup)", "Agent(claude-code-guide)"],
            "blockReadsOutsideWorkingDirectories": True,
            "disableBypassPermissionsMode": "disable",
            "disableAutoMode": "disable",
        },
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            # The GPU launcher runs outside Claude Code's sandbox but applies its own profile
            # (app/sandbox/gpu.sb). Agents can't edit app/, so they can't change what it allows.
            "excludedCommands": [f"{gpu_prefix(gpu_job)} *"] if gpu_job else [],
            "credentials": {
                "files": [{"path": p, "mode": "deny"} for p in
                          ("~/.ssh", "~/.aws", "~/.kaggle", "~/.config/gh", "~/.netrc", "~/.docker", "~/.gnupg")],
            },
        },
    }


# --------------------------------------------------------------------------- extra folders granted to a job
# A job (and its Ask chat) can be given read/write access to folders on this Mac, e.g. the Desktop.
# Agent Lab's own files stay protected even when a granted folder contains them: its code, settings
# (tokens), runtime files, other jobs, this job's records, and the Agent Lab icon.
NOT_GRANTABLE = {".ssh", ".aws", ".gnupg", ".config", ".kaggle", ".docker", ".netrc", "Library", ".claude",
                 ".claude.json", ".Trash"}
OWN_JOB_FILES = ("job.json", "settings.json", "system_prompt.md", "subagent_prompt.md", "agents.json",
                 "command.json", "events.jsonl", "stderr.log", "guard.jsonl", "metrics.json", "permissions.json",
                 "perm_token", "selftest_config.json", "analysis", "improve")


def parse_access(raw) -> list[str]:
    """'Desktop', '~/Documents/x' or '/Users/me/x', one per line or comma-separated -> checked absolute paths."""
    home = Path.home().resolve()
    out = []
    for item in (x.strip() for x in re.split(r"[\n,]", str(raw or ""))):
        if not item:
            continue
        p = Path(os.path.expanduser(item))
        if not p.is_absolute():
            p = home / item
        try:
            p = p.resolve(strict=True)
        except OSError:
            raise HTTPException(400, f"Folder not found: {item}")
        if not p.is_dir():
            raise HTTPException(400, f"Not a folder: {item}")
        if p == home or home not in p.parents:
            raise HTTPException(400, f"{item}: only folders inside your home folder can be granted, not the home folder itself")
        if p.relative_to(home).parts[0] in NOT_GRANTABLE:
            raise HTTPException(400, f"{item}: that folder holds credentials or system data, so it can't be granted")
        for own in (APP, CONFIG, LAB / "runtime"):
            if p == own or own in p.parents:
                raise HTTPException(400, f"{item}: that's part of Agent Lab itself")
        if re.search(r'["\\#&\n\r]', str(p)):
            raise HTTPException(400, f"{item}: folder names with \" \\ # or & aren't supported")
        if str(p) not in out:
            out.append(str(p))
    return out


def protected_paths(jd: Path) -> tuple[list[str], list[str]]:
    """(no writing, no reading) paths that stay off-limits inside granted folders."""
    write = [APP, CONFIG, LAB / "runtime", LAB / "README.md", LAB / "start.command", LAB / "setup.command",
             LAB / "restore-app.command", LAB / ".gitignore", LAB.parent / "Agent Lab.app"]
    write += sorted(p for p in JOBS.iterdir() if p.name != jd.name)  # every other job
    own = set(OWN_JOB_FILES) | {x.name for x in jd.iterdir()}
    write += [jd / x for x in sorted(own - {"work"})]  # this job's records (not its workspace)
    return [str(x) for x in write], [str(CONFIG), str(LAB / "runtime")]


def apply_access(s: dict, jd: Path, access: list):
    write, read = protected_paths(jd)
    fs = s["sandbox"].setdefault("filesystem", {})
    fs["allowWrite"] = list(access)
    fs["denyWrite"] = write  # takes precedence over allowWrite
    fs["denyRead"] = read
    deny = s["permissions"]["deny"]
    for p in write:  # the same rules for the file tools ("//" = an absolute path)
        deny += [f"Edit(/{p})", f"Edit(/{p}/**)"]
    for p in read:
        deny += [f"Read(/{p})", f"Read(/{p}/**)"]


ACCESS_DIR = LAB / "runtime" / "access"


def write_access_file(jid: str, access: Optional[list]):
    """The folders granted to a job. Kept in runtime/, which agents can neither read nor write, and read
    by app/bin/lab-gpu and when the job or its chat starts (job.json's copy is only for display)."""
    ACCESS_DIR.mkdir(parents=True, exist_ok=True)
    (ACCESS_DIR / f"{jid}.txt").write_text("".join(f"{p}\n" for p in (access or [])))


def read_access(jid: str) -> list:
    """The job's granted folders, each checked again."""
    try:
        lines = (ACCESS_DIR / f"{jid}.txt").read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out += [p for p in parse_access(line) if p not in out]
        except HTTPException:
            pass  # e.g. the folder was deleted since
    return out


def access_note(access: Optional[list]) -> str:
    if not access:
        return ""
    return ("\n\nYou also have read and write access to these folders on the user's computer:\n"
            + "\n".join(f"- {p}" for p in access)
            + "\nWork on files there only as the user asks. Everything else in them belongs to the user: don't delete, "
              "move or reorganize anything you weren't asked to touch. Agent Lab's own files stay off-limits even inside "
              "these folders. To use the GPU on code there, run the GPU launcher from this workspace and give the "
              "script's absolute path.")


def perm_token(jd: Path) -> str:
    p = jd / "perm_token"
    if not p.exists():
        p.write_text(secrets.token_hex(16))
        p.chmod(0o600)
    return p.read_text().strip()


def runtime_rules(job: dict) -> str:
    lim = job.get("time_limit_min")
    return (rules.RUNTIME_RULES.replace("{LAB_GPU}", gpu_prefix(job["id"]))
            .replace("{TIME_LIMIT}", f"{lim} minutes (time spent paused doesn't count)" if lim else "none set"))


def build_command(job: dict, cfg: dict, prompt: str, jd: Path) -> list[str]:
    exe = find_claude()
    if not exe:
        raise RuntimeError("Claude Code CLI not found. Install it (https://code.claude.com) and restart Agent Lab.")
    comp = topology.compile_config(cfg)
    rr = runtime_rules(job)
    (jd / "system_prompt.md").write_text(comp["system_append"] + "\n\n" + rr)
    sub_append = (comp["subagent_append"] + "\n\n" + rr).strip()
    (jd / "subagent_prompt.md").write_text(sub_append)
    write_json(jd / "agents.json", comp["agents"])
    access = read_access(job["id"])
    write_json(jd / "settings.json", sandbox_settings(jd, comp["require_delegation"], job["id"], access))
    union = set(["TodoWrite"])
    for n in cfg["nodes"]:
        union.update(n.get("tools") or [])
    if comp["agents"]:
        union.add("Agent")
    cmd = [exe, "-p", prompt,
           "--output-format", "stream-json", "--verbose",
           "--model", comp["model"],
           "--append-system-prompt-file", str(jd / "system_prompt.md"),
           "--settings", str(jd / "settings.json"),
           "--setting-sources", "project",
           "--permission-mode", "acceptEdits",
           "--add-dir", str(SHARED), *[a for p in access for a in ("--add-dir", p)],
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--no-session-persistence",
           "--tools", ",".join(sorted(union))]
    if comp["agents"]:
        cmd += ["--agents", json.dumps(comp["agents"]), "--append-subagent-system-prompt", sub_append]
    if supports((2, 1, 211)):
        cmd.append("--forward-subagent-text")
    if supports((2, 1, 259)):
        # Prompts nobody answers are denied, unless the PermissionRequest hook (this app) approves them.
        cmd += ["--permission-prompts", "none"]
    mt = job.get("max_turns") or get_settings().get("max_turns")
    if mt:
        cmd += ["--max-turns", str(int(mt))]
    return cmd


def active_seconds(job: dict) -> float:
    if not job.get("started"):
        return 0.0
    end = job.get("finished") or time.time()
    paused = job.get("paused_total", 0.0)
    if job.get("paused_at"):
        paused += time.time() - job["paused_at"]
    return max(0.0, end - job["started"] - paused)


def _watchdog(jid: str, proc: subprocess.Popen):
    while proc.poll() is None:
        time.sleep(5)
        try:
            job = load_job(jid)
        except HTTPException:
            return
        lim = job.get("time_limit_min")
        if lim and job.get("status") == "running" and active_seconds(job) > lim * 60:
            job["timed_out"] = True
            job["status"] = "cancelling"
            save_job(job)
            procs.stop(proc.pid)
            return


def run_process(jid: str, cmd: list[str], cwd: Path, env: dict, events_path: Path, stderr_path: Path) -> int:
    with stderr_path.open("w") as err, events_path.open("a") as out:
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=err,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True)
        PROCS[jid] = proc
        threading.Thread(target=_watchdog, args=(jid, proc), daemon=True).start()
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = {"ts": time.time(), "ev": shrink_event(json.loads(line))}
                except json.JSONDecodeError:
                    rec = {"ts": time.time(), "raw": line[:20000]}
                out.write(json.dumps(rec) + "\n")
                out.flush()
            return proc.wait()
        finally:
            PROCS.pop(jid, None)


def fmt_dur(s) -> str:
    if not s:
        return "?"
    s = int(s)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


STATUS_RE = re.compile(r"STATUS:\s*(SUCCESS|FAILED)\s*[:\-—]?\s*(.*)", re.I)


def run_job(jid: str):
    jd = job_dir(jid)
    job = load_job(jid)
    if job.get("override_config"):  # the self-test uses its own one-agent config
        cfg = read_json(jd / "selftest_config.json")
    else:
        cfg = load_version(job["config_version"])["config"]
    job.update(status="running", started=time.time(), paused_total=0.0,
               require_delegation=bool(cfg.get("require_delegation")) and len(cfg["nodes"]) > 1)
    save_job(job)
    try:
        spec_name = job.get("spec_file", "spec.md")
        if job.get("prior"):
            job["status_note"] = f"copying {len(job['prior'])} earlier job(s) into ./prior/"
            save_job(job)
            prior.snapshot(JOBS, job["prior"], jd / "work" / "prior")
            job.pop("status_note", None)
            save_job(job)
        prompt = (f"Experiment title: {job['title']}\n\nThe experiment specification is in ./{spec_name}. "
                  "Read it first, then carry it out following your instructions and team topology.")
        if job.get("prior"):
            prompt += ("\n\nThis job builds on earlier jobs. Copies of their workspaces are in ./prior/<job-id>/, "
                       "and ./prior/INDEX.md lists them with their status and final message. Start by reading the index "
                       "and the relevant `_JOB.md` files, and reuse the code and results that work rather than starting over. "
                       "These are copies, so the original jobs are never affected. Put this job's own deliverables in the "
                       "workspace root, not inside prior/.")
        if job.get("spec_files"):
            prompt += ("\n\nThe user attached files to the spec. They're in ./spec_files/ and listed in the spec "
                       "(zips are already unpacked next to them).")
        prompt += access_note(read_access(jid))
        if job.get("notes"):
            prompt += f"\n\nAdditional notes from the user:\n{job['notes']}"
        if job.get("kind") == "selftest":
            prompt = selftest_prompt(job)
        cmd = build_command(job, cfg, prompt, jd)
        if job.get("kind") == "selftest":
            cmd += ["--json-schema", json.dumps(rules.SELFTEST_SCHEMA)]
        write_json(jd / "command.json", cmd[:2] + ["<prompt>"] + cmd[3:])
        rc = run_process(jid, cmd, jd / "work", clean_env(lab_env(jd)), jd / "events.jsonl", jd / "stderr.log")
        job = load_job(jid)
        job.pop("paused_at", None)
        d = load_digest(jd / "events.jsonl")
        m = d.metrics(end_ts=time.time())
        m["guard"] = guard_summary(jd)
        write_json(jd / "metrics.json", m)
        job["result"] = (d.result or {}).get("result")
        st = STATUS_RE.findall(job["result"] or "")
        if job.get("timed_out"):
            job["status"] = "timed_out"
            job["error"] = f"Stopped after the {job.get('time_limit_min')}-minute time limit."
        elif job.get("status") == "cancelling":
            job["status"] = "cancelled"
        elif rc == 0 and d.result and not d.result.get("is_error"):
            if st and st[-1][0].upper() == "FAILED":
                job["status"] = "failed"
                job["gave_up"] = True
                job["error"] = "The agents stopped themselves: " + (st[-1][1].strip() or "see the result")
            else:
                job["status"] = "done"
        else:
            job["status"] = "failed"
            tail = (jd / "stderr.log").read_text()[-1500:] if (jd / "stderr.log").exists() else ""
            job["error"] = (d.result or {}).get("subtype") or tail or f"exit code {rc}"
        if job.get("kind") == "selftest":
            job["selftest"] = evaluate_selftest(d, jd, job)
            shutil.rmtree(SELFTEST_DECOY, ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        job = load_job(jid)
        job["status"] = "failed"
        job["error"] = str(e)
    job["finished"] = time.time()
    expire_pending(jd)
    save_job(job)
    STORAGE["event"].set()  # measure disk use again
    s = get_settings()
    if job["status"] in s.get("notify_on", []):
        m = read_json(jd / "metrics.json", {}) or {}
        icon = {"done": "white_check_mark", "failed": "x", "cancelled": "stop_sign", "timed_out": "hourglass"}.get(job["status"], "")
        body = (f"{job['status'].replace('_', ' ').upper()} after {fmt_dur(active_seconds(job))} · {m.get('num_turns') or '?'} turns · "
                f"{m.get('tool_calls', 0)} tool calls · {m.get('errors', 0)} errors\n\n"
                + (job.get("error") + "\n\n" if job.get("error") and job["status"] != "done" else "")
                + (job.get("result") or "")[:500])
        job["notify_error"] = notify(f"Agent Lab: {job['title']}", body, tags=icon,
                                     priority="high" if job["status"] in ("failed", "timed_out") else "default")
        save_job(job)
    if job.get("kind") != "selftest" and s.get("auto_analyze", True):
        start_diagnosis(jid)


def new_job(title: str, kind: str = "experiment", **fields) -> dict:
    jid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    jd = JOBS / jid
    (jd / "work" / "handoff").mkdir(parents=True)
    fields.setdefault("time_limit_min", get_settings().get("time_limit_min") or None)
    job = {"id": jid, "title": title or "Untitled experiment", "kind": kind, "status": "queued",
           "created": time.time(), "config_version": current_version(), **fields}
    save_job(job)
    return job


# --------------------------------------------------------------------------- sandbox self-test
def experiment_python() -> str:
    c = sorted((SHARED / "python").glob("cpython-3.12*/bin/python3.12"))
    return str(c[-1]) if c else "python3"


SELFTEST_DECOY = JOBS / "zz-selftest-decoy"  # stands in for "another job" in the granted-folder checks


def selftest_grant() -> Optional[Path]:
    """The folder that contains the lab (e.g. the Desktop), if it can be granted."""
    try:
        return Path(parse_access(str(LAB.parent))[0])
    except (HTTPException, IndexError):
        return None


def selftest_prompt(job: dict) -> str:
    text = rules.SELFTEST_PROMPT.format(PY=experiment_python(), METAL=rules.METAL_CHECK, LAB_GPU=gpu_prefix(job["id"]),
                                        HOME=str(Path.home()), APP=str(APP), PORT=SERVER_PORT)
    if job.get("access"):
        extra = rules.SELFTEST_GRANT_CHECKS.format(GRANT=job["access"][0], APP=str(APP), LAB=str(LAB),
                                                   DECOY=str(SELFTEST_DECOY), LAB_GPU=gpu_prefix(job["id"]))
        text = text.replace("For each check, report outcome:", extra + "For each check, report outcome:", 1)
    return text


def selftest_expect(job: dict) -> dict:
    return {**rules.SELFTEST_EXPECT, **(rules.SELFTEST_GRANT_EXPECT if job.get("access") else {})}


def selftest_probes(job: dict) -> dict:
    probes = dict(PROBES)
    if job.get("access"):
        g = Path(job["access"][0])
        probes.update(grant_write=g / "agent-lab-grant-probe.txt", grant_write_app=APP / "grant-probe",
                      grant_edit_app=APP / "edit-probe.txt", grant_write_other_job=SELFTEST_DECOY / "probe",
                      gpu_grant_write=g / "agent-lab-gpu-grant-probe")
    return probes


PROBES = {"write_home": Path.home() / "agent-lab-sandbox-probe",
          "gpu_write_home": Path.home() / "agent-lab-gpu-probe",
          "gpu_write_app": APP / "gpu-probe"}


def evaluate_selftest(d, jd: Path, job: Optional[dict] = None) -> dict:
    job = job or load_job(jd.name)
    probes = selftest_probes(job)
    so = (d.result or {}).get("structured_output") or {}
    if not so and d.result:
        try:
            so = json.loads(d.result.get("result") or "{}")
        except json.JSONDecodeError:
            so = {}
    reported = {c.get("id"): c for c in so.get("checks", [])}
    rows = []
    for cid, want in selftest_expect(job).items():
        got = reported.get(cid, {}).get("outcome", "missing")
        ev = reported.get(cid, {}).get("evidence", "")
        if cid in probes:  # verified independently of what the model reports
            probe = probes[cid]
            escaped = probe.exists()
            if escaped:
                try:
                    probe.unlink()
                except OSError:
                    pass
            got = "allowed" if escaped else "blocked"
            seen = (f"verified: the file was created at {probe}" if want == "allowed" else f"a file appeared at {probe}!") \
                if escaped else f"verified: nothing was created at {probe}"
            ev = seen + (" · " + ev if ev else "")
        if cid in ("grant_read_config", "grant_read_tool_config", "gpu_grant_read_config") and "ntfy_server" in ev:
            got = "allowed"  # the agent's evidence quotes Agent Lab's settings file, so it could read it
        if cid == "write_workspace" and (jd / "work" / "probe.txt").exists():
            got = "allowed"
        if cid in ("gpu_in_sandbox", "gpu_launcher"):
            if "GPU_OK" in ev:
                got = "allowed"
            elif "NO_GPU" in ev:
                got = "blocked"
        rows.append({"id": cid, "expected": want, "got": got, "ok": got == want, "evidence": ev[:300]})
    return {"passed": all(r["ok"] for r in rows), "checks": rows}


# --------------------------------------------------------------------------- permissions
def perms_path(jd: Path) -> Path:
    return jd / "permissions.json"


def load_perms(jd: Path) -> list:
    return read_json(perms_path(jd), []) or []


def expire_pending(jd: Path):
    with LOCK:
        ps = load_perms(jd)
        changed = False
        for r in ps:
            if r["status"] == "pending":
                r["status"], r["decided"], changed = "expired", time.time(), True
        if changed:
            write_json(perms_path(jd), ps)


def check_token(jd: Path, token: str):
    if not token or not hmac.compare_digest(token, perm_token(jd)):
        raise HTTPException(403, "bad token")


def short_input(tool: str, inp) -> str:
    if isinstance(inp, dict):
        for k in ("command", "file_path", "url", "path", "pattern", "query"):
            if k in inp:
                return str(inp[k])
    return json.dumps(inp)[:300]


# --------------------------------------------------------------------------- run analysis + self-improvement
def evidence_dir(jid: str, sub: str) -> Path:
    """Write everything the analyzer/improver needs to read into one folder."""
    jd = job_dir(jid)
    job = load_job(jid)
    d = load_digest(jd / "events.jsonl")
    out = jd / sub
    out.mkdir(parents=True, exist_ok=True)
    m = d.metrics(end_ts=job.get("finished"))
    m["guard"] = guard_summary(jd)
    write_json(out / "metrics.json", m)
    (out / "transcript.md").write_text(transcript_markdown(d))
    (out / "result.md").write_text(str(job.get("result") or "(no final message)"))
    write_json(out / "status.json", {k: job.get(k) for k in (
        "status", "error", "gave_up", "timed_out", "time_limit_min", "require_delegation", "title")}
        | {"active_minutes": round(active_seconds(job) / 60, 1)})
    write_json(out / "permissions.json", [{k: r.get(k) for k in ("tool_name", "summary", "agent", "status", "message")}
                                          for r in load_perms(jd)])
    cfg = load_version(current_version())["config"]
    write_json(out / "config.json", {"base_prompt": cfg["base_prompt"],
                                     "require_delegation": bool(cfg.get("require_delegation")), "nodes": [
        {k: n.get(k) for k in ("name", "role", "model", "description", "prompt", "tools")} for n in cfg["nodes"]],
        "edges": [{"from": topology._name(cfg, e["from"]), "to": topology._name(cfg, e["to"]), "type": e["type"]}
                  for e in cfg["edges"]]})
    (out / "runtime_rules.md").write_text(runtime_rules(job))
    write_json(out / "settings.json", sandbox_settings())
    return out


def run_structured(cwd: Path, prompt: str, schema: dict, model: str, max_turns: int = 15) -> dict:
    exe = find_claude()
    if not exe:
        raise RuntimeError("Claude Code CLI not found")
    cmd = [exe, "-p", prompt, "--output-format", "json", "--json-schema", json.dumps(schema), "--model", model,
           "--tools", "Read,Glob,Grep", "--settings", str(cwd / "settings.json"),
           "--setting-sources", "project", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--no-session-persistence", "--max-turns", str(max_turns)]
    if supports((2, 1, 259)):
        cmd += ["--permission-prompts", "none"]
    p = subprocess.run(cmd, cwd=str(cwd), env=clean_env(), capture_output=True, text=True, timeout=1800)
    (cwd / "output.json").write_text(p.stdout)
    (cwd / "stderr.log").write_text(p.stderr)
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"no JSON from Claude Code (exit {p.returncode}): {p.stderr[-400:]}")
    so = out.get("structured_output")
    if not so:
        try:
            so = json.loads(out.get("result") or "")
        except (json.JSONDecodeError, TypeError):
            raise RuntimeError(f"no structured output: {str(out.get('result'))[:300]}")
    so["_cost"] = out.get("total_cost_usd")
    return so


def analysis_model() -> str:
    cfg = load_version(current_version())["config"]
    orch = next(n for n in cfg["nodes"] if n["role"] == "orchestrator")
    m = get_settings().get("improver_model") or orch.get("model") or "fable"
    return "opus" if m == "inherit" else m


def start_diagnosis(jid: str):
    job = load_job(jid)
    if (job.get("diagnosis") or {}).get("status") == "running":
        return
    n = (job.get("diagnosis") or {}).get("n", 0) + 1
    job["diagnosis"] = {"n": n, "status": "running", "started": time.time()}
    save_job(job)
    threading.Thread(target=run_diagnosis, args=(jid, n), daemon=True).start()


def run_diagnosis(jid: str, n: int):
    diag = {"n": n}
    try:
        cwd = evidence_dir(jid, f"analysis/{n}")
        so = run_structured(cwd, rules.DIAGNOSE_PROMPT, rules.DIAGNOSE_SCHEMA, analysis_model(), 12)
        diag.update(status="ready", headline=so.get("headline", ""), issues=so.get("issues", []),
                    draft=so.get("draft", ""), cost=so.get("_cost"), finished=time.time())
    except Exception as e:  # noqa: BLE001
        diag.update(status="failed", error=str(e), finished=time.time())
    with LOCK:
        job = load_job(jid)
        job["diagnosis"] = diag
        save_job(job)


def run_improve(jid: str, request: str):
    job = load_job(jid)
    imp = job["improvement"]
    try:
        base_v = current_version()
        cfg = copy.deepcopy(load_version(base_v)["config"])
        cwd = evidence_dir(jid, f"improve/{imp['n']}")
        (cwd / "request.md").write_text(request.strip() or "Fix the biggest efficiency problems you can find in this run.")
        so = run_structured(cwd, rules.IMPROVE_PROMPT, rules.IMPROVE_SCHEMA, analysis_model(), 15)
        applied = []
        new_cfg = copy.deepcopy(cfg)
        for ch in so.get("changes", []):
            if ch.get("target") == "base_prompt":
                old = new_cfg["base_prompt"]
                new_cfg["base_prompt"] = ch["new_text"]
            else:
                node = next((x for x in new_cfg["nodes"] if x["name"] == ch.get("target")), None)
                if not node or ch.get("field") not in ("prompt", "description"):
                    continue
                old = node[ch["field"]]
                node[ch["field"]] = ch["new_text"]
            if old != ch["new_text"]:
                applied.append({**ch, "old_text": old})
        summary = (so.get("summary") or "").strip()[:400]
        if applied:
            rec = save_version(new_cfg, "improvement", summary, job=jid, changes=applied)
            imp.update(status="applied", version=rec["version"], parent=base_v)
        else:
            imp.update(status="no_change", version=None, parent=base_v)
        imp.update(summary=summary or "No changes were needed.", changes=[
            {k: c[k] for k in ("target", "field", "reason", "new_text", "old_text")} for c in applied],
            cost=so.get("_cost"), finished=time.time())
    except Exception as e:  # noqa: BLE001
        imp.update(status="failed", error=str(e), finished=time.time())
    with LOCK:
        job = load_job(jid)
        job["improvement"] = imp
        save_job(job)


# --------------------------------------------------------------------------- API
@app.get("/api/status")
def api_status(refresh: bool = False):
    st = {k: v for k, v in claude_status(refresh).items() if not k.startswith("_")}
    st["min_recommended"] = "2.1.281"
    st["lab_dir"] = str(LAB)
    st["ntfy_configured"] = bool(get_settings().get("ntfy_topic"))
    st["experiment_python"] = (SHARED / "python").exists()
    pending = []
    for p in job_dirs():
        j = read_json(p / "job.json")
        if j and j.get("status") in ACTIVE:
            pending += [{"job": j["id"], "title": j["title"]} for r in load_perms(p) if r["status"] == "pending"]
    for c in list(CHATS.values()):
        pending += [{"job": c.jid, "title": "chat", "chat": True} for r in c.perms if r["status"] == "pending"]
    if DEV["session"]:
        pending += [{"job": None, "title": "app change", "selfdev": True} for r in DEV["session"].perms if r["status"] == "pending"]
    st["pending_permissions"] = pending
    st["auto_allow_permissions"] = bool(get_settings().get("auto_allow_permissions"))
    last, s = STORAGE["last"], get_settings()
    if last:
        st["storage"] = {"total": last["total"], "limit_gb": s.get("storage_limit_gb"),
                         "over": last["total"] > float(s.get("storage_limit_gb") or 15) * 1e9,
                         "free": shutil.disk_usage(str(LAB)).free,
                         "free_low": shutil.disk_usage(str(LAB)).free < float(s.get("min_free_gb") or 3) * 1e9}
    st["code"] = {"running": BOOT_HASH, "on_disk": code_hash(), "started": BOOT_TIME}
    st["code_stale"] = st["code"]["running"] != st["code"]["on_disk"]
    return st


@app.post("/api/admin/shutdown")
def api_shutdown(body: dict):
    """Stop the Agent Lab server (used when it was started from the Agent Lab icon, with no Terminal window)."""
    running = [j["id"] for j in (read_json(p / "job.json") for p in job_dirs()) if j and j.get("status") in ACTIVE]
    if running and not body.get("force"):
        raise HTTPException(409, f"{len(running)} job(s) still running; stopping Agent Lab would stop them")

    def later():
        time.sleep(0.5)
        print("Agent Lab: stopped from the web page", flush=True)
        close_all_chats()
        os._exit(0)
    threading.Thread(target=later, daemon=True).start()
    return {"ok": True}


@app.post("/api/admin/close-terminal")
def api_close_terminal(body: dict):
    """start.command asks this (already running) server to close its Terminal window."""
    tty = str(body.get("tty") or "")
    if not re.match(r"^/dev/ttys?\d+$", tty):
        raise HTTPException(400, "bad tty")
    close_terminal_window(tty, 1.5)
    return {"ok": True}


@app.post("/api/admin/restart")
def api_restart(body: dict):
    """Restart this server in place (same process, same Terminal window) to load updated code."""
    running = [j["id"] for j in (read_json(p / "job.json") for p in job_dirs()) if j and j.get("status") in ACTIVE]
    if running and not body.get("force"):
        raise HTTPException(409, f"{len(running)} job(s) still running; restarting would stop them")

    restart_server()
    return {"ok": True}


def restart_server(delay: float = 0.7):
    """Restart in place (same process id, same log) to load updated code."""
    def later():
        time.sleep(delay)
        print("Agent Lab: restarting to load updated code", flush=True)
        close_all_chats()
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=later, daemon=True).start()


@app.get("/api/settings")
def api_get_settings():
    s = get_settings()
    for k in SECRET_SETTINGS:
        s[k] = SECRET_MASK if s.get(k) else ""
    return s


@app.post("/api/settings")
def api_set_settings(body: dict):
    s = get_settings()
    for k in DEFAULT_SETTINGS:
        if k in body and not (k in SECRET_SETTINGS and body[k] == SECRET_MASK):  # the mask means "unchanged"
            s[k] = body[k]
    if s.get("ntfy_topic") and not re.match(r"^[A-Za-z0-9_-]{1,64}$", s["ntfy_topic"]):
        raise HTTPException(400, "ntfy topic: letters, digits, - and _ only")
    for k, lo in (("storage_limit_gb", 1), ("min_free_gb", 1)):
        try:
            s[k] = max(lo, float(s.get(k) if s.get(k) not in ("", None) else DEFAULT_SETTINGS[k]))
        except (TypeError, ValueError):
            raise HTTPException(400, f"{k} must be a number")
    for k in ("time_limit_min", "permission_timeout_min", "max_turns"):
        if s.get(k) in ("", None):
            s[k] = None if k != "permission_timeout_min" else 30
        else:
            try:
                s[k] = max(1, int(s[k]))
            except (TypeError, ValueError):
                raise HTTPException(400, f"{k} must be a whole number")
    s["auto_allow_permissions"] = bool(s.get("auto_allow_permissions"))
    s["auto_push"] = bool(s.get("auto_push"))
    s["github_repo"] = str(s.get("github_repo") or "").strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if s["github_repo"] and not re.fullmatch(r"[\w.-]+/[\w.-]+", s["github_repo"]):
        raise HTTPException(400, "GitHub repository: use the form owner/name")
    write_json(SETTINGS_FILE, s)
    if s["auto_allow_permissions"]:
        approve_all_pending()
    return api_get_settings()


def approve_all_pending():
    """Auto-allow was switched on: approve anything already waiting in running jobs and chats."""
    for c in list(CHATS.values()) + ([DEV["session"]] if DEV["session"] else []):
        with c.lock:
            for r in c.perms:
                if r["status"] == "pending":
                    r.update(status="allowed", auto=True, decided=time.time(), message="auto-allowed")
    for p in job_dirs():
        j = read_json(p / "job.json")
        if not j or j.get("status") not in ACTIVE:
            continue
        with LOCK:
            ps = load_perms(p)
            changed = False
            for r in ps:
                if r["status"] == "pending":
                    r.update(status="allowed", auto=True, decided=time.time(), message="auto-allowed")
                    changed = True
            if changed:
                write_json(perms_path(p), ps)


@app.post("/api/settings/test-notify")
def api_test_notify():
    err = notify("Agent Lab test", "Notifications are working. 🎉", tags="bell")
    if err:
        raise HTTPException(400, err)
    return {"ok": True}


@app.get("/api/presets")
def api_presets():
    return [{"key": k, "label": v[0]} for k, v in presets.PRESETS.items()]


@app.get("/api/presets/{key}")
def api_preset(key: str):
    if key not in presets.PRESETS:
        raise HTTPException(404)
    cfg = presets.get_preset(key)
    cfg["base_prompt"] = load_version(current_version())["config"]["base_prompt"]
    return cfg


@app.get("/api/meta")
def api_meta():
    return {"tools": presets.ALL_TOOLS, "models": presets.MODELS}


@app.get("/api/config")
def api_config():
    v = load_version(current_version())
    return v


@app.post("/api/config")
def api_save_config(body: dict):
    cfg = body.get("config")
    if not isinstance(cfg, dict):
        raise HTTPException(400, "missing config")
    return save_version(cfg, "manual", body.get("note") or "Edited in the topology editor")


@app.post("/api/config/validate")
def api_validate(body: dict):
    return {"errors": topology.validate(body.get("config") or {})}


@app.get("/api/versions")
def api_versions():
    cur = current_version()
    out = []
    for p in reversed(version_files()):
        v = read_json(p, {})
        out.append({k: v.get(k) for k in ("version", "created", "source", "summary", "parent", "job")} | {"current": v.get("version") == cur})
    return out


@app.post("/api/versions/{n}/restore")
def api_restore(n: int):
    v = load_version(n)
    return save_version(v["config"], "restore", f"Restored v{n}")


@app.get("/api/compiled")
def api_compiled():
    """What will actually be passed to Claude Code for the current topology (for inspection)."""
    return topology.compile_config(load_version(current_version())["config"])


SPEC_DOC_EXT = (".pdf", ".md", ".txt", ".tex")


def safe_name(name: str) -> str:
    base = Path(str(name).replace("\\", "/")).name
    base = re.sub(r"[^\w.\- ()+@,]", "_", base).strip(" .") or "file"
    return base[:150]


def _put(f: dict, dest: Path):
    """Move an uploaded file (streamed to disk by webkit) into place."""
    if f.get("path"):
        shutil.move(f["path"], dest)
    else:
        dest.write_bytes(f["data"])


def extract_zip(zpath: Path, dest: Path, max_bytes: int = 4 * 2**30, max_files: int = 50000) -> str:
    """Unpack a zip the user attached. Skips absolute paths, '..', symlinks and macOS metadata.
    Refuses a zip whose contents wouldn't fit (a "zip bomb", or just a full disk)."""
    import stat
    import zipfile
    n = total = 0
    try:
        with zipfile.ZipFile(zpath) as z:
            declared = sum(i.file_size for i in z.infolist())
            room = shutil.disk_usage(str(dest.parent)).free - int(float(get_settings().get("min_free_gb") or 3) * 2**30)
            if declared > min(max_bytes, room):
                return (f"not unpacked: its contents are {storage.fmt(declared)}, more than "
                        + ("the 4 GB limit" if declared > max_bytes else "the free disk space allows"))
            for info in z.infolist():
                name = info.filename.replace("\\", "/")
                parts = [x for x in name.split("/") if x not in ("", ".")]
                if (not parts or ".." in parts or name.startswith("/") or ":" in parts[0]
                        or parts[0] == "__MACOSX" or parts[-1] == ".DS_Store"):
                    continue
                if stat.S_ISLNK(info.external_attr >> 16):
                    continue
                target = dest.joinpath(*parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                n, total = n + 1, total + info.file_size
                if n > max_files or total > max_bytes:
                    return "only partly unpacked: it's too big to unpack automatically"
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
    except (zipfile.BadZipFile, OSError, RuntimeError) as e:
        return f"couldn't be unpacked ({e})"
    return ""


def save_spec(job: dict, work: Path, spec_text: str, files: list):
    """A lone PDF/Markdown/text/TeX file stays ./spec.<ext> as before. Anything else (any file type,
    several files, or text plus files) goes to ./spec_files/, with ./spec.md holding the text and a list."""
    if len(files) == 1 and not spec_text.strip() and Path(files[0]["filename"]).suffix.lower() in SPEC_DOC_EXT:
        name = "spec" + Path(files[0]["filename"]).suffix.lower()
        _put(files[0], work / name)
        job["spec_file"] = name
        return
    text = spec_text.strip() or "The specification is in the attached files listed below."
    saved, lines = [], []
    if files:
        sf = work / "spec_files"
        sf.mkdir(exist_ok=True)
        for f in files:
            fn = safe_name(f["filename"])
            stem, ext, i = Path(fn).stem, Path(fn).suffix, 2
            while (sf / fn).exists():
                fn, i = f"{stem}-{i}{ext}", i + 1
            _put(f, sf / fn)
            saved.append(fn)
            line = f"- `spec_files/{fn}`"
            if fn.lower().endswith(".zip"):
                dest = sf / Path(fn).stem
                problem = extract_zip(sf / fn, dest)
                line += f" (a zip: {problem})" if problem else f" (a zip, already unpacked into `spec_files/{dest.name}/`)"
            lines.append(line)
        text += "\n\n## Files attached to the spec\n" + "\n".join(lines) + "\n"
    (work / "spec.md").write_text(text + ("\n" if not text.endswith("\n") else ""))
    job["spec_file"] = "spec.md"
    if saved:
        job["spec_files"] = saved


@app.post("/api/jobs")
def api_create_job(form: dict):
    title, spec_text = form.get("title", ""), form.get("spec_text", "")
    notes, max_turns = form.get("notes", ""), str(form.get("max_turns", ""))
    tl = str(form.get("time_limit_min", "")).strip()
    files = [f for f in ((form.get("_files") or {}).get("file") or [])
             if isinstance(f, dict) and f.get("filename") and (f.get("path") or f.get("data") is not None)]
    if not find_claude():
        raise HTTPException(400, "Claude Code CLI not found on this Mac")
    if not spec_text.strip() and not files:
        raise HTTPException(400, "Give a specification (text or a file)")
    extra = {"time_limit_min": int(tl)} if tl.isdigit() and int(tl) > 0 else {}
    access = parse_access(form.get("access", ""))
    if access:
        extra["access"] = access
    want = str(form.get("prior", "")).strip()
    if want:
        usable = [j["id"] for j in api_jobs() if j.get("kind") != "selftest" and j.get("status") not in ACTIVE]
        ids = usable if want == "all" else [x for x in want.split(",") if x]
        bad = [x for x in ids if x not in usable]
        if bad:
            raise HTTPException(400, f"can't build on these jobs (missing, still running, or self-tests): {', '.join(bad)}")
        if ids:
            extra["prior"] = ids
    job = new_job(title, max_turns=int(max_turns) if max_turns.strip().isdigit() else None, notes=notes.strip(), **extra)
    write_access_file(job["id"], access)
    save_spec(job, job_dir(job["id"]) / "work", spec_text, files)
    save_job(job)
    threading.Thread(target=run_job, args=(job["id"],), daemon=True).start()
    return job


@app.post("/api/selftest")
def api_selftest():
    if not find_claude():
        raise HTTPException(400, "Claude Code CLI not found on this Mac")
    grant = selftest_grant()
    extra = {}
    if grant:
        extra["access"] = [str(grant)]
        SELFTEST_DECOY.mkdir(exist_ok=True)
        if not SETTINGS_FILE.exists():
            write_json(SETTINGS_FILE, get_settings())
    job = new_job("Sandbox self-test", kind="selftest", max_turns=60, time_limit_min=20, **extra)
    write_access_file(job["id"], extra.get("access"))
    (job_dir(job["id"]) / "work" / "spec.md").write_text("Sandbox self-test")
    # self-test runs with a solo topology so it is quick
    solo = presets.get_preset("solo")
    solo["nodes"][0]["model"] = "sonnet"
    jd = job_dir(job["id"])
    write_json(jd / "selftest_config.json", solo)
    job["config_version"] = current_version()
    job["override_config"] = True
    save_job(job)
    threading.Thread(target=run_job, args=(job["id"],), daemon=True).start()
    return job


@app.get("/api/jobs")
def api_jobs():
    out = []
    for p in sorted(job_dirs(), reverse=True):
        j = read_json(p / "job.json")
        if j:
            row = {k: j.get(k) for k in ("id", "title", "kind", "status", "created", "started", "finished", "config_version", "prior")}
            row["pending_permissions"] = sum(1 for r in load_perms(p) if r["status"] == "pending") if j.get("status") in ACTIVE else 0
            out.append(row)
    return out


@app.get("/api/jobs/{jid}")
def api_job(jid: str, since: int = 0):
    jd = job_dir(jid)
    job = load_job(jid)
    d = DIGESTS.get(jd / "events.jsonl")
    m = read_json(jd / "metrics.json") or d.metrics(end_ts=time.time())
    m = dict(m)
    m["guard"] = guard_summary(jd)
    m["active_s"] = round(active_seconds(job), 1)
    return {"job": job, "items": d.items[since:], "total_items": len(d.items), "metrics": m,
            "permissions": [{k: v for k, v in r.items() if k != "tool_input"} for r in load_perms(jd)]}


@app.post("/api/jobs/{jid}/cancel")
def api_cancel(jid: str):
    job = load_job(jid)
    proc = PROCS.get(jid)
    if not proc:
        raise HTTPException(400, "job is not running")
    if job.get("paused_at"):
        job["paused_total"] = job.get("paused_total", 0.0) + time.time() - job.pop("paused_at")
    job["status"] = "cancelling"
    save_job(job)
    procs.stop(proc.pid)
    return {"ok": True}


@app.post("/api/jobs/{jid}/pause")
def api_pause(jid: str):
    job = load_job(jid)
    proc = PROCS.get(jid)
    if not proc or job.get("status") != "running":
        raise HTTPException(400, "job is not running")
    n = procs.pause(proc.pid)
    job["status"], job["paused_at"] = "paused", time.time()
    save_job(job)
    return {"ok": True, "processes": n}


@app.post("/api/jobs/{jid}/resume")
def api_resume(jid: str):
    job = load_job(jid)
    proc = PROCS.get(jid)
    if not proc or job.get("status") != "paused":
        raise HTTPException(400, "job is not paused")
    procs.resume(proc.pid)
    job["paused_total"] = job.get("paused_total", 0.0) + time.time() - job.pop("paused_at", time.time())
    job["status"] = "running"
    save_job(job)
    return {"ok": True}


@app.post("/api/jobs/{jid}/permissions")
def api_perm_create(jid: str, body: dict, token: str = ""):
    """Called by permission_hook.py (outside the sandbox) when a tool call needs approval."""
    jd = job_dir(jid)
    check_token(jd, token)
    job = load_job(jid)
    rec = {"id": uuid.uuid4().hex[:10], "created": time.time(), "status": "pending",
           "tool_name": str(body.get("tool_name") or "?"), "tool_input": body.get("tool_input"),
           "summary": short_input(str(body.get("tool_name")), body.get("tool_input"))[:2000],
           "agent": str(body.get("agent") or "")}
    auto = bool(get_settings().get("auto_allow_permissions"))
    if auto:
        rec.update(status="allowed", auto=True, decided=time.time(), message="auto-allowed")
    with LOCK:
        ps = load_perms(jd)
        ps.append(rec)
        write_json(perms_path(jd), ps)
    if auto:
        notify(f"Auto-allowed: {job['title']}", f"{rec['agent']} used {rec['tool_name']}:\n{rec['summary'][:400]}",
               tags="unlock", priority="low")
    else:
        notify(f"Permission needed: {job['title']}", f"{rec['agent']} wants to use {rec['tool_name']}:\n{rec['summary'][:400]}\n\n"
               "Open Agent Lab to allow or deny.", tags="raised_hand", priority="high")
    return {"id": rec["id"], "status": rec["status"]}


def _perm(jd: Path, rid: str):
    ps = load_perms(jd)
    r = next((x for x in ps if x["id"] == rid), None)
    if not r:
        raise HTTPException(404, "no such request")
    return ps, r


@app.get("/api/jobs/{jid}/permissions/{rid}")
def api_perm_get(jid: str, rid: str, token: str = ""):
    jd = job_dir(jid)
    check_token(jd, token)
    _, r = _perm(jd, rid)
    return {"status": r["status"], "message": r.get("message")}


@app.post("/api/jobs/{jid}/permissions/{rid}/expire")
def api_perm_expire(jid: str, rid: str, token: str = ""):
    jd = job_dir(jid)
    check_token(jd, token)
    with LOCK:
        ps, r = _perm(jd, rid)
        if r["status"] == "pending":
            r["status"], r["decided"] = "expired", time.time()
            write_json(perms_path(jd), ps)
    return {"ok": True}


@app.post("/api/jobs/{jid}/permissions/{rid}/decide")
def api_perm_decide(jid: str, rid: str, body: dict):
    """Called from the web page when you click Allow or Deny."""
    jd = job_dir(jid)
    if body.get("behavior") not in ("allow", "deny"):
        raise HTTPException(400, "behavior must be allow or deny")
    with LOCK:
        ps, r = _perm(jd, rid)
        if r["status"] != "pending":
            raise HTTPException(400, f"this request is already {r['status']}")
        r["status"] = "allowed" if body["behavior"] == "allow" else "denied"
        r["message"] = str(body.get("message") or "")[:1000]
        r["decided"] = time.time()
        write_json(perms_path(jd), ps)
    return r


@app.get("/api/jobs/{jid}/permissions/{rid}/detail")
def api_perm_detail(jid: str, rid: str):
    _, r = _perm(job_dir(jid), rid)
    return r


@app.get("/api/jobs/{jid}/files")
def api_files(jid: str):
    work = job_dir(jid) / "work"
    out = []
    skip = {".venv", "__pycache__", ".git", ".pytest_cache", "node_modules"}
    for root, dirs, files in os.walk(work):
        dirs[:] = sorted(d for d in dirs if d not in skip)
        for f in sorted(files):
            p = Path(root) / f
            out.append({"path": str(p.relative_to(work)), "size": p.stat().st_size})
            if len(out) > 500:
                return out
    return out


@app.get("/api/jobs/{jid}/file")
def api_file(jid: str, path: str):
    work = (job_dir(jid) / "work").resolve()
    p = (work / path).resolve()
    if work not in p.parents or not p.is_file():
        raise HTTPException(404)
    if p.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg"):
        return FileResp(p)
    data = p.read_bytes()[:300_000]
    return {"path": path, "text": data.decode("utf-8", "replace"), "truncated": p.stat().st_size > 300_000}


@app.post("/api/jobs/{jid}/analyze")
def api_analyze(jid: str):
    job = load_job(jid)
    if job["status"] in ACTIVE:
        raise HTTPException(400, "wait for the job to finish first")
    start_diagnosis(jid)
    return load_job(jid)["diagnosis"]


@app.post("/api/jobs/{jid}/improve")
def api_improve(jid: str, body: dict):
    job = load_job(jid)
    if job["status"] in ACTIVE:
        raise HTTPException(400, "wait for the job to finish first")
    if (job.get("improvement") or {}).get("status") == "running":
        raise HTTPException(400, "already improving")
    request = str(body.get("request") or body.get("guidance") or "")
    n = (job.get("improvement") or {}).get("n", 0) + 1
    job["improvement"] = {"n": n, "status": "running", "started": time.time(), "request": request}
    save_job(job)
    threading.Thread(target=run_improve, args=(jid, request), daemon=True).start()
    return job["improvement"]


@app.post("/api/jobs/{jid}/improve/undo")
def api_undo(jid: str):
    job = load_job(jid)
    imp = job.get("improvement") or {}
    if imp.get("status") != "applied":
        raise HTTPException(400, "nothing to undo")
    cfg = copy.deepcopy(load_version(current_version())["config"])
    for ch in imp.get("changes", []):
        if ch["target"] == "base_prompt":
            if cfg["base_prompt"] == ch["new_text"]:
                cfg["base_prompt"] = ch["old_text"]
        else:
            node = next((n for n in cfg["nodes"] if n["name"] == ch["target"]), None)
            if node and node.get(ch["field"]) == ch["new_text"]:
                node[ch["field"]] = ch["old_text"]
    rec = save_version(cfg, "undo", f"Undid improvement v{imp['version']} (from job {jid})", job=jid)
    imp.update(status="undone", undo_version=rec["version"])
    job["improvement"] = imp
    save_job(job)
    return imp


# --------------------------------------------------------------------------- throwaway Q&A chat
# One single-agent session per finished job, held only in memory (see chat.py). Nothing is written
# to disk: Claude Code's settings and the context are passed on the command line, and the session
# runs with --no-session-persistence. Restarting or stopping Agent Lab ends every chat.
CHATS: dict[str, chatmod.ChatSession] = {}  # job id -> its chat
CHAT_TOOLS = "Bash,Edit,Glob,Grep,Read,WebFetch,WebSearch,Write"


def close_all_chats():
    for jid in list(CHATS):
        c = CHATS.pop(jid, None)
        if c:
            c.close(wait=True)
    if DEV.get("session"):
        DEV["session"].close(wait=True)


import atexit  # noqa: E402
atexit.register(close_all_chats)


MAX_LIVE_CHATS = 3  # each open chat is a Claude Code process using a few hundred MB of memory


def make_room_for_chat(jid: str):
    """End the least recently used idle chat if too many are open. Its messages stay on its page."""
    live = [c for k, c in CHATS.items() if k != jid and not c.ended]
    if len(live) < MAX_LIVE_CHATS:
        return
    idle = sorted((c for c in live if not c.busy), key=lambda c: c.last_active)
    if not idle:
        raise HTTPException(409, f"{len(live)} Ask chats are busy answering. Wait for one to finish.")
    old = idle[0]
    with old.lock:
        old._add("note", f"This chat's session was ended to save memory (at most {MAX_LIVE_CHATS} chats stay open). "
                         "Send a message to start a new session. It won't remember the messages above.")
    old.close()


def chat_model() -> str:
    return get_settings().get("chat_model") or analysis_model()


def _work_listing(work: Path, limit: int = 250) -> list[str]:
    skip = {".venv", "venv", "__pycache__", ".git", ".pytest_cache", "node_modules", ".claude", ".mypy_cache"}
    out = []
    for root, dirs, files in os.walk(work):
        rel = Path(root).relative_to(work)
        if rel.parts[:1] == ("prior",):  # earlier jobs' copies: list the folders only
            out += [f"prior/{d}/" for d in sorted(dirs)] if rel == Path("prior") else []
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if d not in skip and not d.endswith("_venv"))
        out += [str(rel / f) if str(rel) != "." else f for f in sorted(files) if f != ".DS_Store"]
        if len(out) > limit:
            return out[:limit] + ["… (more files not listed)"]
    return out


def chat_context(jid: str) -> str:
    """The assistant's instructions plus what the agents did, from the job's own records."""
    jd = job_dir(jid)
    job = load_job(jid)
    d = load_digest(jd / "events.jsonl")
    m = read_json(jd / "metrics.json") or d.metrics(end_ts=job.get("finished"))
    parts = [rules.CHAT_PROMPT.replace("{LAB_GPU}", gpu_prefix(jid)), "",
             "# The job this chat is about", "",
             f"- title: {job.get('title')}", f"- job id: {jid}",
             f"- status: {job.get('status')}" + (f" ({job.get('error')})" if job.get("error") and job.get("status") != "done" else ""),
             f"- ran: {fmt_dur(active_seconds(job))} · {m.get('num_turns') or '?'} turns · {m.get('tool_calls', 0)} tool calls · "
             f"{m.get('errors', 0)} tool errors", f"- topology version: v{job.get('config_version')}",
             f"- spec: ./{job.get('spec_file', 'spec.md')}"]
    if job.get("notes"):
        parts.append(f"- the user's notes for the run: {job['notes']}")
    if job.get("prior"):
        parts.append(f"- built on earlier jobs (copies in ./prior/): {', '.join(job['prior'])}")
    if job.get("spec_files"):
        parts.append(f"- files the user attached to the spec (in ./spec_files/): {', '.join(job['spec_files'])}")
    if read_access(jid):
        parts.append(access_note(read_access(jid)).strip())
    dg = job.get("diagnosis") or {}
    if dg.get("status") == "ready":
        parts += ["", "## Agent Lab's analysis of the run", dg.get("headline", "")]
        parts += [f"- {i.get('title')}: {i.get('evidence')}" for i in dg.get("issues", [])]
    parts += ["", "## The job's final message", str(job.get("result") or "(none)")[:8000],
              "", "## Files in the workspace (virtual environments and caches left out)", *_work_listing(jd / "work"),
              "", "## What the agents did (activity log, oldest first; times in seconds from the start)",
              "Long logs have their middle cut out. The files in the workspace are the source of truth.",
              transcript_markdown(d, 40000)]
    return "\n".join(parts)


def chat_command(jid: str, c: chatmod.ChatSession) -> list[str]:
    exe = find_claude()
    if not exe:
        raise HTTPException(400, "Claude Code CLI not found on this Mac")
    s = _base_sandbox_settings(jid)
    s["permissions"]["allow"] = ["Bash", "WebFetch", "WebSearch", "WebFetch(domain:*)"]
    access = read_access(jid)
    if access:
        apply_access(s, job_dir(jid), access)
    tout = int(get_settings().get("permission_timeout_min") or 30) * 60
    s["hooks"] = {  # the guard approves sandboxed shell commands (no log, no delegation rule)
        "PreToolUse": [{"matcher": "*", "hooks": [_hook([sys.executable, APP / "delegation_guard.py"], 15)]}],
        "PermissionRequest": [{"matcher": "*", "hooks": [_hook(
            [sys.executable, APP / "permission_hook.py", c.id, c.token, SERVER_PORT, tout, "chats"], tout + 60)]}]}
    cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--model", c.model,
           "--append-system-prompt", chat_context(jid),
           "--settings", json.dumps(s), "--setting-sources", "project",
           "--permission-mode", "acceptEdits", "--add-dir", str(SHARED), *[a for p in access for a in ("--add-dir", p)],
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--no-session-persistence", "--tools", CHAT_TOOLS]
    if supports((2, 1, 259)):
        cmd += ["--permission-prompts", "none"]
    return cmd


def chat_for(cid: str, token: str) -> chatmod.ChatSession:
    c = next((x for x in list(CHATS.values()) + [DEV["session"]] if x and x.id == cid), None)
    if not c:
        raise HTTPException(404, "no such chat")
    if not token or not hmac.compare_digest(token, c.token):
        raise HTTPException(403, "bad token")
    return c


def _chat_job(jid: str) -> dict:
    job = load_job(jid)
    if job.get("kind") == "selftest":
        raise HTTPException(400, "self-tests don't have a chat")
    if job.get("status") in ACTIVE:
        raise HTTPException(400, "the chat opens once the job has finished")
    return job


@app.get("/api/jobs/{jid}/chat")
def api_chat_get(jid: str, since: int = 0):
    load_job(jid)
    c = CHATS.get(jid)
    return {"chat": c.view(since) if c else None}


@app.post("/api/jobs/{jid}/chat")
def api_chat_send(jid: str, body: dict):
    text = str(body.get("message") or "").strip()
    if not text:
        raise HTTPException(400, "empty message")
    _chat_job(jid)
    with LOCK:
        c = CHATS.get(jid)
        if c and c.busy:
            raise HTTPException(409, "The assistant is still answering. Wait for it, or press Stop.")
        if not c or c.ended:
            make_room_for_chat(jid)
            new = chatmod.ChatSession(jid, chat_model(), carry=c.items if c else None)
            cmd = chat_command(jid, new)
            jd = job_dir(jid)
            new.start(cmd, str(jd / "work"), clean_env(lab_env(jd)))
            CHATS[jid], c = new, new
    try:
        c.send(text)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "id": c.id}


@app.post("/api/jobs/{jid}/chat/stop")
def api_chat_stop(jid: str):
    c = CHATS.get(jid)
    if c:
        try:
            c.interrupt()
        except RuntimeError as e:
            raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/jobs/{jid}/chat/close")
def api_chat_close(jid: str):
    """End the chat and forget it (the "New chat" button)."""
    c = CHATS.pop(jid, None)
    if c:
        c.close()
    return {"ok": True}


@app.post("/api/chats/{cid}/permissions")
def api_chat_perm_create(cid: str, body: dict, token: str = ""):
    """Called by permission_hook.py for the chat assistant. Kept in memory only."""
    c = chat_for(cid, token)
    rec = {"id": uuid.uuid4().hex[:10], "created": time.time(), "status": "pending",
           "tool_name": str(body.get("tool_name") or "?"), "tool_input": body.get("tool_input"),
           "summary": short_input(str(body.get("tool_name")), body.get("tool_input"))[:2000], "agent": "chat"}
    if get_settings().get("auto_allow_permissions"):
        rec.update(status="allowed", auto=True, decided=time.time(), message="auto-allowed")
    c.add_perm(rec)
    return {"id": rec["id"], "status": rec["status"]}


@app.get("/api/chats/{cid}/permissions/{rid}")
def api_chat_perm_get(cid: str, rid: str, token: str = ""):
    r = chat_for(cid, token).get_perm(rid)
    if not r:
        raise HTTPException(404, "no such request")
    return {"status": r["status"], "message": r.get("message")}


@app.post("/api/chats/{cid}/permissions/{rid}/expire")
def api_chat_perm_expire(cid: str, rid: str, token: str = ""):
    c = chat_for(cid, token)
    with c.lock:
        r = c.get_perm(rid)
        if r and r["status"] == "pending":
            r["status"], r["decided"] = "expired", time.time()
    return {"ok": True}


@app.post("/api/jobs/{jid}/chat/permissions/{rid}/decide")
def api_chat_perm_decide(jid: str, rid: str, body: dict):
    if body.get("behavior") not in ("allow", "deny"):
        raise HTTPException(400, "behavior must be allow or deny")
    c = CHATS.get(jid)
    r = c.get_perm(rid) if c else None
    if not r:
        raise HTTPException(404, "no such request")
    with c.lock:
        if r["status"] != "pending":
            raise HTTPException(400, f"this request is already {r['status']}")
        r["status"] = "allowed" if body["behavior"] == "allow" else "denied"
        r["message"] = str(body.get("message") or "")[:1000]
        r["decided"] = time.time()
    return {k: v for k, v in r.items() if k != "tool_input"}


# --------------------------------------------------------------------------- the "Modify app" tab: Agent Lab changing itself
# One session at a time edits a copy of the app (runtime/selfdev/<id>/work, laid out like the GitHub
# repository). When it finishes, Agent Lab checks the new code, applies it (keeping a backup), restarts,
# and, after the restarted app is up, pushes it to GitHub if asked. See selfdev.py.
SELFDEV_DIR = LAB / "runtime" / "selfdev"
DEV: dict = {"session": None, "rid": None, "poller": False}
DEV_LOCK = threading.RLock()
PUSH_LOCK = threading.Lock()
IN_PROGRESS = ("running", "checking", "waiting", "applying", "restarting", "undo_restarting")


def dev_dir(rid: str) -> Path:
    if not re.match(r"^[a-z0-9-]+$", rid):
        raise HTTPException(400, "bad id")
    return SELFDEV_DIR / rid


def load_dev(rid: str) -> dict:
    rec = read_json(dev_dir(rid) / "record.json")
    if not rec:
        raise HTTPException(404, "no such change")
    return rec


def save_dev(rec: dict):
    with DEV_LOCK:
        write_json(dev_dir(rec["id"]) / "record.json", rec)


def dev_records() -> list[dict]:
    if not SELFDEV_DIR.exists():
        return []
    out = [read_json(p / "record.json") for p in sorted(SELFDEV_DIR.iterdir(), reverse=True) if p.is_dir()]
    return [r for r in out if r]


def dev_last_applied() -> Optional[str]:
    try:
        return (SELFDEV_DIR / "LAST_APPLIED").read_text().strip() or None
    except OSError:
        return None


def selfdev_command(c: chatmod.ChatSession) -> list[str]:
    exe = find_claude()
    if not exe:
        raise HTTPException(400, "Claude Code CLI not found on this Mac")
    s = _base_sandbox_settings()
    s["permissions"]["allow"] = ["Bash", "WebFetch", "WebSearch", "WebFetch(domain:*)"]
    tout = int(get_settings().get("permission_timeout_min") or 30) * 60
    s["hooks"] = {
        "PreToolUse": [{"matcher": "*", "hooks": [_hook([sys.executable, APP / "delegation_guard.py"], 15)]}],
        "PermissionRequest": [{"matcher": "*", "hooks": [_hook(
            [sys.executable, APP / "permission_hook.py", c.id, c.token, SERVER_PORT, tout, "chats"], tout + 60)]}]}
    cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--model", c.model, "--append-system-prompt", rules.SELFDEV_PROMPT,
           "--settings", json.dumps(s), "--setting-sources", "project", "--permission-mode", "acceptEdits",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--no-session-persistence", "--tools", CHAT_TOOLS]
    if supports((2, 1, 259)):
        cmd += ["--permission-prompts", "none"]
    return cmd


def dev_env() -> dict:
    """Job agents can write to shared/, so the self-change session gets none of it: the app's own Python,
    and a PATH without Agent Lab's folders."""
    env = clean_env({"LAB_PYTHON": sys.executable, "SSL_CERT_FILE": CERT_FILE, "REQUESTS_CA_BUNDLE": CERT_FILE,
                     "PYTHONDONTWRITEBYTECODE": "1"})
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    return env


def dev_blockers() -> list[str]:
    """Why applying (which restarts Agent Lab) has to wait."""
    out = []
    running = [j for j in (read_json(p / "job.json") for p in job_dirs()) if j and j.get("status") in ACTIVE]
    if running:
        out.append(f"{len(running)} job{'s' if len(running) > 1 else ''} running")
    if any(c.busy for c in list(CHATS.values())):
        out.append("an Ask chat is answering")
    return out


@app.get("/api/selfdev")
def api_dev_get(since: int = 0):
    c = DEV["session"]
    active = None
    if c and DEV["rid"]:
        active = {"record": read_json(dev_dir(DEV["rid"]) / "record.json"), "chat": c.view(since)}
    runs = []
    last = dev_last_applied()
    for rec in dev_records()[:30]:
        row = {k: rec.get(k) for k in ("id", "created", "request", "push", "model", "status", "title", "summary",
                                       "changes", "stray", "checks", "review", "waiting_for", "push_result",
                                       "applied", "undone", "error", "cost", "push_pending")}
        row["can_undo"] = rec["id"] == last and rec.get("status") in ("applied", "failed")
        runs.append(row)
    s = get_settings()
    return {"active": active, "runs": runs, "repo": s.get("github_repo"), "auto_push": s.get("auto_push", True),
            "git_problem": selfdev.git_problem() if s.get("github_repo") else None}


@app.post("/api/selfdev")
def api_dev_start(body: dict):
    request = str(body.get("request") or "").strip()
    if not request:
        raise HTTPException(400, "Describe the change first")
    with DEV_LOCK:
        if DEV["session"] and not DEV["session"].ended:
            raise HTTPException(409, "A change is already being made. Wait for it, or stop it.")
        if any(r.get("status") in IN_PROGRESS for r in dev_records()):
            raise HTTPException(409, "The last change is still being checked or applied.")
        rid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        rd = dev_dir(rid)
        selfdev.stage(LAB, rd / "base")
        selfdev.stage(LAB, rd / "work")
        rec = {"id": rid, "created": time.time(), "request": request, "push": bool(body.get("push", True)),
               "model": get_settings().get("selfdev_model") or chat_model(), "status": "running"}
        save_dev(rec)
        c = chatmod.ChatSession("selfdev:" + rid, rec["model"])
        try:
            c.start(selfdev_command(c), str(rd / "work"), dev_env())
            c.send(request)
        except Exception as e:  # noqa: BLE001
            rec.update(status="failed", error=str(e))
            save_dev(rec)
            raise HTTPException(500, f"couldn't start: {e}")
        DEV.update(session=c, rid=rid)
    threading.Thread(target=dev_watch, args=(rid, c), daemon=True).start()
    return rec


def dev_watch(rid: str, c: chatmod.ChatSession):
    """When the session's turn ends: record it, compare, check, and apply if everything passes."""
    while c.busy and not c.ended:
        time.sleep(1)
    time.sleep(0.5)
    view = c.view()
    c.close(wait=True)
    rec = load_dev(rid)
    items = view["items"]
    texts = [it["text"] for it in items if it["role"] == "assistant"]
    summary = (texts[-1] if texts else "").strip()
    rec.update(transcript=items[-500:], cost=round(view["cost"], 4), summary=summary,
               title=(summary.splitlines() or [""])[0].strip("# *`").strip()[:100] or "Update Agent Lab")
    with DEV_LOCK:
        if DEV["rid"] == rid:
            DEV.update(session=None, rid=None)
        if rec["status"] == "cancelled":
            save_dev(rec)
            return
        rec["status"] = "checking"
        save_dev(rec)  # from here on, Stop no longer applies
    try:
        rd = dev_dir(rid)
        chs, stray = selfdev.changes(rd / "base", rd / "work")
        rec.update(changes=chs, stray=stray)
        if not chs:
            rec["status"] = "no_change"
            save_dev(rec)
            return
        (rd / "diff.patch").write_text(selfdev.unified_diff(rd / "base", rd / "work", chs))
        rec["status"] = "checking"
        save_dev(rec)
        res = selfdev.run_checks(rd / "work", sys.executable, rd / "check", [x["path"] for x in chs], base=rd / "base")
        rec["checks"] = {"ok": res["ok"], "log": res["log"]}
        review = list(res["needs_review"])
        if items and items[-1]["role"] == "error":
            review.append("the assistant's session ended with an error")
        rec["review"] = review
        rec["status"] = "checks_failed" if not res["ok"] else "needs_review" if review else "waiting"
    except Exception as e:  # noqa: BLE001
        rec.update(status="failed", error=f"{type(e).__name__}: {e}")
    with DEV_LOCK:
        if (read_json(dev_dir(rid) / "record.json") or {}).get("status") == "cancelled":
            return  # stopped meanwhile
        save_dev(rec)
    if rec["status"] == "waiting":
        dev_try_apply(rid)


def dev_try_apply(rid: str):
    rec = load_dev(rid)
    if rec.get("status") != "waiting":
        return
    blockers = dev_blockers()
    if not blockers:
        dev_apply(rid)
        return
    rec["waiting_for"] = blockers
    save_dev(rec)
    with DEV_LOCK:
        if DEV["poller"]:
            return
        DEV["poller"] = True

    def poll():
        try:
            while True:
                time.sleep(10)
                r = read_json(dev_dir(rid) / "record.json") or {}
                if r.get("status") != "waiting":
                    return
                b = dev_blockers()
                if not b:
                    dev_apply(rid)
                    return
                if b != r.get("waiting_for"):
                    r["waiting_for"] = b
                    save_dev(r)
        finally:
            DEV["poller"] = False
    threading.Thread(target=poll, daemon=True).start()


def dev_apply(rid: str):
    rec = load_dev(rid)
    if rec.get("status") != "waiting":
        return
    rec.update(status="applying", waiting_for=None)
    save_dev(rec)
    try:
        selfdev.apply(LAB, dev_dir(rid) / "work", dev_dir(rid), rec["changes"])
    except Exception as e:  # noqa: BLE001
        rec.update(status="failed", error=f"applying failed part-way: {e}. Press Undo to put back the previous files.")
        save_dev(rec)
        return
    rec.update(status="restarting", applied=time.time(), push_pending=bool(rec.get("push")))
    save_dev(rec)
    restart_server(1.0)


def dev_push(rid: str, undo: bool = False, overwrite: bool = False):
    with PUSH_LOCK:
        rec = load_dev(rid)
        rec["push_result"] = {"status": "pushing"}
        save_dev(rec)
        title = rec.get("title") or "Update Agent Lab"
        if undo:
            msg = f"Undo: {title}\n\nPutting back the previous version (done from Agent Lab's Modify app tab)."
        else:
            body = "\n".join((rec.get("summary") or "").splitlines()[1:]).strip()[:1500]
            msg = f"{title}\n\n{body}\n\nMade from Agent Lab's Modify app tab.".replace("\n\n\n", "\n\n")
        res = selfdev.publish(LAB, get_settings(), msg, overwrite=overwrite)
        rec = load_dev(rid)
        rec.update(push_result=res, push_pending=False)
        save_dev(rec)


@app.post("/api/selfdev/stop")
def api_dev_stop():
    c, rid = DEV["session"], DEV["rid"]
    if not c or not rid:
        raise HTTPException(400, "nothing is running")
    rec = load_dev(rid)
    rec["status"] = "cancelled"
    save_dev(rec)
    c.close()
    return {"ok": True}


@app.post("/api/selfdev/{rid}/apply")
def api_dev_apply(rid: str):
    rec = load_dev(rid)
    if rec.get("status") not in ("needs_review", "waiting"):
        raise HTTPException(400, f"this change is {rec.get('status')}")
    rec.update(status="waiting", reviewed=True)
    save_dev(rec)
    dev_try_apply(rid)
    return load_dev(rid)


@app.post("/api/selfdev/{rid}/discard")
def api_dev_discard(rid: str):
    rec = load_dev(rid)
    if rec.get("status") not in ("needs_review", "waiting", "checks_failed", "no_change"):
        raise HTTPException(400, f"this change is {rec.get('status')}")
    rec["status"] = "discarded"
    save_dev(rec)
    return rec


@app.post("/api/selfdev/{rid}/undo")
def api_dev_undo(rid: str):
    rec = load_dev(rid)
    if rid != dev_last_applied() or rec.get("status") not in ("applied", "failed"):
        raise HTTPException(400, "Only the most recent applied change can be undone.")
    b = dev_blockers()
    if b:
        raise HTTPException(409, "Undoing restarts Agent Lab. Wait until nothing is running: " + ", ".join(b))
    selfdev.undo(LAB, dev_dir(rid))
    pushed = bool((rec.get("push_result") or {}).get("ok"))
    rec.update(status="undo_restarting", undone=time.time(), push_pending=pushed and bool(rec.get("push")))
    save_dev(rec)
    restart_server(1.0)
    return rec


@app.post("/api/selfdev/{rid}/push")
def api_dev_push(rid: str, body: dict):
    rec = load_dev(rid)
    if rec.get("status") not in ("applied", "undone"):
        raise HTTPException(400, "Only applied (or undone) changes can be pushed.")
    if (rec.get("push_result") or {}).get("status") == "pushing":
        raise HTTPException(409, "already pushing")
    threading.Thread(target=dev_push, args=(rid, rec["status"] == "undone", bool(body.get("overwrite"))), daemon=True).start()
    return {"ok": True}


@app.get("/api/selfdev/{rid}/diff")
def api_dev_diff(rid: str):
    p = dev_dir(rid) / "diff.patch"
    return {"diff": p.read_text() if p.exists() else ""}


@app.post("/api/selfdev/permissions/{pid}/decide")
def api_dev_perm_decide(pid: str, body: dict):
    if body.get("behavior") not in ("allow", "deny"):
        raise HTTPException(400, "behavior must be allow or deny")
    c = DEV["session"]
    r = c.get_perm(pid) if c else None
    if not r:
        raise HTTPException(404, "no such request")
    with c.lock:
        if r["status"] != "pending":
            raise HTTPException(400, f"this request is already {r['status']}")
        r["status"] = "allowed" if body["behavior"] == "allow" else "denied"
        r["message"] = str(body.get("message") or "")[:1000]
        r["decided"] = time.time()
    return {k: v for k, v in r.items() if k != "tool_input"}


def dev_prune(keep: int = 5):
    """Old self-changes keep their record and diff, but not their copies of the app (a few MB each).
    Only the latest applied change keeps its backup, since only it can be undone."""
    last = dev_last_applied()
    for i, rec in enumerate(dev_records()):
        if rec.get("status") in IN_PROGRESS:
            continue
        rd = dev_dir(rec["id"])
        shutil.rmtree(rd / "check", ignore_errors=True)
        if i >= keep:
            shutil.rmtree(rd / "base", ignore_errors=True)
            shutil.rmtree(rd / "work", ignore_errors=True)
        if rec["id"] != last:
            shutil.rmtree(rd / "backup", ignore_errors=True)


def dev_boot():
    """After a restart: finish what a self-change started (or note what a restart interrupted)."""
    for rec in dev_records():
        rid, st = rec["id"], rec.get("status")
        marker = dev_dir(rid) / "RESTORED"
        if marker.exists():
            marker.unlink()
            rec.update(status="undone", undone=time.time(), push_pending=False,
                       error="Undone with restore-app.command.")
            save_dev(rec)
        elif st == "restarting":
            rec["status"] = "applied"
            save_dev(rec)
            if rec.get("push_pending"):
                threading.Thread(target=dev_push, args=(rid,), daemon=True).start()
        elif st == "undo_restarting":
            rec["status"] = "undone"
            save_dev(rec)
            if rec.get("push_pending"):
                threading.Thread(target=dev_push, args=(rid, True), daemon=True).start()
        elif st in ("running", "checking", "applying"):
            rec.update(status="failed", error="Agent Lab was restarted while this change was in progress."
                       + (" Some files may already be changed: press Undo." if st == "applying" else ""))
            save_dev(rec)
        elif st == "waiting":
            threading.Thread(target=lambda r=rid: (time.sleep(3), dev_try_apply(r)), daemon=True).start()
        elif (rec.get("push_result") or {}).get("status") == "pushing":
            rec["push_result"] = {"ok": False, "error": "Agent Lab restarted during the push. Press Push again."}
            save_dev(rec)
    try:
        dev_prune()
    except OSError:
        pass


# --------------------------------------------------------------------------- disk space
# A background scan measures the agent-lab folder every STORAGE_EVERY seconds and after each job. Above
# the limit in Settings (15 GB by default) you get a notification and a warning at the top of the page.
# Free disk space is checked every minute: if it drops below the minimum, running jobs are paused, because
# a full disk breaks macOS, not just Agent Lab.
STORAGE_EVERY = 15 * 60
STORAGE: dict = {"last": None, "scanning": False, "event": threading.Event()}
STORAGE_STATE = LAB / "runtime" / "storage_state.json"


def storage_scan() -> dict:
    info = {}
    for p in job_dirs():
        j = read_json(p / "job.json") or {}
        info[p.name] = {"title": j.get("title"), "status": j.get("status")}
    STORAGE["scanning"] = True
    try:
        res = storage.scan(LAB, info)
    finally:
        STORAGE["scanning"] = False
    STORAGE["last"] = res
    storage_check_limit(res)
    return res


def storage_check_limit(res: dict):
    s = get_settings()
    limit = float(s.get("storage_limit_gb") or 15) * 1e9
    st = read_json(STORAGE_STATE, {}) or {}
    if res["total"] <= limit:
        if st.get("warned_total"):
            write_json(STORAGE_STATE, {})
        return
    again = (not st.get("warned_total") or res["total"] >= st["warned_total"] * 1.1
             or time.time() - st.get("warned_at", 0) > 24 * 3600)
    if again:
        biggest = ", ".join(f"{(j['title'] or j['id'])[:40]} {storage.fmt(j['bytes'])}" for j in res["jobs"][:3])
        shared = ", ".join(f"{k} {storage.fmt(v)}" for k, v in list(res["shared"].items())[:3])
        notify("Agent Lab: storage over the limit",
               f"Agent Lab is using {storage.fmt(res['total'])}, over your {s.get('storage_limit_gb')} GB limit.\n"
               f"Biggest jobs: {biggest or '-'}\nShared caches: {shared or '-'}\n\n"
               "Settings → Storage shows what's using space and can free some.", tags="floppy_disk", priority="high")
        write_json(STORAGE_STATE, {"warned_total": res["total"], "warned_at": time.time()})


def storage_check_free():
    s = get_settings()
    free = shutil.disk_usage(str(LAB)).free
    if free >= float(s.get("min_free_gb") or 3) * 1e9:
        return
    paused = []
    for jid, proc in list(PROCS.items()):
        try:
            job = load_job(jid)
        except HTTPException:
            continue
        if job.get("status") == "running":
            procs.pause(proc.pid)
            job.update(status="paused", paused_at=time.time(),
                       status_note="paused automatically: the disk is nearly full. Free some space, then press Resume")
            save_job(job)
            paused.append(job["title"])
    if paused:
        notify("Agent Lab: disk nearly full, jobs paused",
               f"Only {storage.fmt(free)} free on the disk. Paused: {', '.join(paused)}.\n"
               "Free some space (Settings → Storage), then resume them.", tags="warning", priority="urgent")


def storage_loop():
    last_scan = 0.0
    while True:
        try:
            storage_check_free()
            if time.time() - last_scan > STORAGE_EVERY or STORAGE["event"].is_set():
                STORAGE["event"].clear()
                last_scan = time.time()
                storage_scan()
        except Exception as e:  # noqa: BLE001
            print(f"Agent Lab: storage check failed: {e}", flush=True)
        STORAGE["event"].wait(60)


@app.get("/api/storage")
def api_storage():
    s = get_settings()
    return {"last": STORAGE["last"], "scanning": STORAGE["scanning"], "limit_gb": s.get("storage_limit_gb"),
            "min_free_gb": s.get("min_free_gb")}


@app.post("/api/storage/scan")
def api_storage_scan():
    STORAGE["event"].set()
    return {"ok": True}


@app.post("/api/storage/clean")
def api_storage_clean(body: dict):
    what = body.get("what")
    if what == "venvs":
        busy = {jid for jid, c in CHATS.items() if c.busy}
        works = [p / "work" for p in job_dirs()
                 if (read_json(p / "job.json") or {}).get("status") not in ACTIVE and p.name not in busy]
        n, freed = storage.delete_venvs(works)
        msg = f"Deleted {n} virtual environment{'s' if n != 1 else ''} of finished jobs, freeing {storage.fmt(freed)}."
    elif what == "uv_cache":
        if any((read_json(p / "job.json") or {}).get("status") in ACTIVE for p in job_dirs()):
            raise HTTPException(409, "Wait until no job is running: jobs use the package cache.")
        freed, out = storage.prune_uv_cache(LAB)
        msg = f"Pruned the package cache, freeing {storage.fmt(freed)}."
    elif what == "model":
        if any((read_json(p / "job.json") or {}).get("status") in ACTIVE for p in job_dirs()):
            raise HTTPException(409, "Wait until no job is running: a job may be using the model.")
        try:
            freed = storage.delete_model(LAB, str(body.get("name") or ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        last = STORAGE["last"]
        if last and last.get("models"):
            last["models"] = [m for m in last["models"] if m["name"] != body.get("name")]
        msg = f"Deleted {str(body.get('name')).split('--', 1)[-1].replace('--', '/')}, freeing {storage.fmt(freed)}. It's downloaded again if a job needs it."
    else:
        raise HTTPException(400, "what must be venvs, uv_cache or model")
    STORAGE["event"].set()
    return {"ok": True, "message": msg}



# Recover jobs that were running when the server stopped
for p in job_dirs():
    j = read_json(p / "job.json")
    if j and j.get("status") in ACTIVE:
        j["status"] = "interrupted"
        j.pop("paused_at", None)
        write_json(p / "job.json", j)
        expire_pending(p)
    for key in ("improvement", "diagnosis"):
        if j and (j.get(key) or {}).get("status") == "running":
            j[key]["status"] = "failed"
            j[key]["error"] = "Agent Lab was restarted while this was running. Try again."
            write_json(p / "job.json", j)



CLOSE_TERMINAL_SCRIPT = """
on run argv
  set target to item 1 of argv
  tell application "Terminal"
    repeat with w in windows
      try
        repeat with t in tabs of w
          if tty of t is target then
            close w
            return
          end if
        end repeat
      end try
    end repeat
  end tell
end run
"""


def close_terminal_window(tty: str, delay: float = 2.0):
    """Close the Terminal window that launched us (start.command), once it has finished."""
    def go():
        time.sleep(delay)
        try:
            subprocess.run(["/usr/bin/osascript", "-e", CLOSE_TERMINAL_SCRIPT, tty],
                           capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001
            print(f"Agent Lab: couldn't close the Terminal window: {e}", flush=True)
    threading.Thread(target=go, daemon=True).start()


def detach():
    """Run in the background, out of the Terminal session, logging to runtime/server.log."""
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    (LAB / "runtime").mkdir(exist_ok=True)
    for name in ("server.log", "launcher.log"):  # keep logs from growing forever
        lp = LAB / "runtime" / name
        try:
            if lp.stat().st_size > 5 * 2**20:
                lp.replace(lp.with_name(name + ".1"))
        except OSError:
            pass
    log = os.open(str(LAB / "runtime" / "server.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.dup2(log, 1)
    os.dup2(log, 2)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("AGENT_LAB_PORT", 8765)))
    ap.add_argument("--detach", action="store_true", help="run in the background (used by start.command)")
    ap.add_argument("--close-tty", default="", help="close the Terminal window on this tty once running")
    a = ap.parse_args()
    SERVER_PORT = a.port
    if a.detach:
        detach()
        # a later in-place restart must not try to detach or close the window again
        sys.argv = [sys.argv[0], "--port", str(a.port)]
    if a.close_tty.startswith("/dev/tty"):
        close_terminal_window(a.close_tty)
    dev_boot()  # after detach(): threads it starts must live in this process
    threading.Thread(target=storage_loop, daemon=True).start()
    for leftover in (LAB / "runtime" / "uploads").glob(".*.tmp"):  # from an upload a crash interrupted
        leftover.unlink(missing_ok=True)
    app.serve("127.0.0.1", a.port)
