"""Changing Agent Lab's own code (the "Modify app" tab), and publishing it to GitHub.

A change is made by one Claude Code session working on a copy of the app in
runtime/selfdev/<id>/work, laid out like the GitHub repository. Agent Lab then compares
it with the copy it started from, checks the new code (it must compile, boot and keep the
sandbox settings), applies it with a backup, restarts, and pushes it if you asked.
Everything in this file runs in the server, outside the sandbox.
"""
from __future__ import annotations

import base64
import difflib
import getpass
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SKIP = {"__pycache__", ".DS_Store", ".git"}
# Changes to these always wait for your review: they enforce the sandbox, run outside it, or decide
# what gets applied automatically. (Other files are compared by what they produce: see FINGERPRINT.)
SECURITY_FILES = ("agent-lab/app/bin/", "agent-lab/app/sandbox/", "agent-lab/app/delegation_guard.py",
                  "agent-lab/app/permission_hook.py", "agent-lab/app/webkit.py", "agent-lab/app/selfdev.py",
                  "agent-lab/app/chat.py", "agent-lab/app/procs.py", "agent-lab/start.command",
                  "agent-lab/setup.command", "agent-lab/restore-app.command", "Agent Lab.app/")
DOC_FILES = ("README.md", ".gitignore")  # the only files brought in automatically from GitHub


# --------------------------------------------------------------------------- what's published, and where it lives
def managed(lab: Path) -> list[tuple[str, Path]]:
    """(path in the repository, path on this computer) for everything that is published."""
    return [("README.md", lab / "README.md"), (".gitignore", lab / ".gitignore"),
            ("agent-lab/app", lab / "app"), ("agent-lab/start.command", lab / "start.command"),
            ("agent-lab/setup.command", lab / "setup.command"),
            ("agent-lab/restore-app.command", lab / "restore-app.command"),
            ("Agent Lab.app", lab.parent / "Agent Lab.app")]


def is_managed(rel: str) -> bool:
    return any(rel == r or rel.startswith(r + "/") for r, _ in managed(Path("/")))


def local_path(lab: Path, rel: str) -> Path:
    """Where a repository path lives on this computer (same mapping as restore-app.command)."""
    if rel.startswith("agent-lab/"):
        return lab / rel[len("agent-lab/"):]
    if rel == "Agent Lab.app" or rel.startswith("Agent Lab.app/"):
        return lab.parent / rel
    return lab / rel


def _walk(root: Path, prefix: str) -> dict[str, Path]:
    out = {}
    if root.is_file():
        return {prefix: root}
    if not root.is_dir():
        return out
    for dp, dns, fns in os.walk(root):  # os.walk doesn't descend into symlinked folders
        links = [d for d in dns if (Path(dp) / d).is_symlink()]
        dns[:] = sorted(d for d in dns if d not in SKIP and d not in links)
        for f in sorted(fns) + links:
            if f in SKIP or f.endswith(".pyc"):
                continue
            p = Path(dp) / f
            out[f"{prefix}/{p.relative_to(root).as_posix()}"] = p
    return out


def symlinks(root: Path) -> list[str]:
    """Symbolic links under the published paths of a repository-layout folder (never allowed in a change)."""
    return sorted(rel for rel, p in tree_files(root).items() if p.is_symlink())


def live_files(lab: Path) -> dict[str, Path]:
    out = {}
    for rel, loc in managed(lab):
        out.update(_walk(loc, rel))
    return out


def tree_files(root: Path) -> dict[str, Path]:
    """Files under a repository-layout folder (only the published paths)."""
    out = {}
    for rel, _ in managed(Path("/")):
        out.update(_walk(root / rel, rel))
    return out


def stage(lab: Path, dest: Path):
    """Copy the published files into dest, laid out like the repository."""
    for rel, src in live_files(lab).items():
        t = dest / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, t)


def _read(p: Path):
    try:
        if p.is_symlink():
            return b"<symlink>" + os.readlink(p).encode()
        return p.read_bytes()
    except OSError:
        return None


# --------------------------------------------------------------------------- comparing
def changes(base: Path, work: Path) -> tuple[list[dict], list[str]]:
    """What the session changed: ([{path, status}], [files it made outside the published paths])."""
    b, w = tree_files(base), tree_files(work)
    out = []
    for rel in sorted(set(b) | set(w)):
        if rel not in w:
            out.append({"path": rel, "status": "deleted"})
        elif rel not in b:
            out.append({"path": rel, "status": "added"})
        elif _read(b[rel]) != _read(w[rel]):
            out.append({"path": rel, "status": "modified"})
    stray = []
    for dp, dns, fns in os.walk(work):
        dns[:] = [d for d in dns if d not in SKIP]
        for f in fns:
            rel = (Path(dp) / f).relative_to(work).as_posix()
            if f not in SKIP and not f.endswith(".pyc") and not is_managed(rel):
                stray.append(rel)
    return out, sorted(stray)[:50]


def unified_diff(base: Path, work: Path, chs: list[dict], limit: int = 400_000) -> str:
    parts = []
    for c in chs:
        a, b = _read(base / c["path"]) or b"", _read(work / c["path"]) or b""
        try:
            al, bl = a.decode().splitlines(keepends=True), b.decode().splitlines(keepends=True)
        except UnicodeDecodeError:
            parts.append(f"Binary file {c['path']} {c['status']}\n")
            continue
        parts.append("".join(difflib.unified_diff(al, bl, f"a/{c['path']}", f"b/{c['path']}")))
    text = "".join(parts)
    return text if len(text) <= limit else text[:limit] + "\n… (diff cut short)\n"


# --------------------------------------------------------------------------- checking the new code
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(port: int, path: str, method: str = "GET", body=None, header: bool = True):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"X-Agent-Lab": "check"} if header else {})})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# What the code produces for the sandbox: the base and the changed app must agree, or the change waits for review.
FINGERPRINT_SNIPPET = r"""
import json, os, sys
sys.path.insert(0, "app")
import server
out = {}
def grab(name, f):
    try:
        out[name] = f()
    except Exception as e:
        out[name] = "ERROR " + type(e).__name__ + ": " + str(e)
home = os.path.expanduser("~")
grant = os.path.join(home, "fingerprint-grant")
os.makedirs(grant, exist_ok=True)
jd = server.JOBS / "fingerprint-job"
os.makedirs(str(jd / "work"), exist_ok=True)
(jd / "perm_token").write_text("0" * 32)
grab("base_sandbox", lambda: server._base_sandbox_settings())
grab("job_sandbox", lambda: server.sandbox_settings(jd, True, "fingerprint-job", server.parse_access(grant)))
grab("not_grantable", lambda: sorted(server.NOT_GRANTABLE))
for bad in ("~", "/etc", ".ssh", "Library", os.path.join(home, "..")):
    try:
        server.parse_access(bad)
        out["grant " + bad] = "ALLOWED"
    except Exception:
        out["grant " + bad] = "refused"
grab("runtime_rules", lambda: server.rules.RUNTIME_RULES)
grab("chat_rules", lambda: server.rules.CHAT_PROMPT)
print(json.dumps(out, sort_keys=True, default=str))
"""

# Run the code under check in a macOS sandbox: it can read and write only its scratch folder (plus
# system files), and can only talk to this computer. So unreviewed code can't touch your files.
CHECK_PROFILE = """(version 1)
(allow default)
(deny file-read* (subpath "/Users") (subpath "/Volumes"))
(allow file-read* (subpath "@SCRATCH@")@PYTHON@)
(allow file-read-metadata @ANCESTORS@)
(deny file-write* (subpath "/"))
(allow file-write* (subpath "@SCRATCH@") (subpath "/private/tmp") (subpath "/private/var/folders") (subpath "/dev"))
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
(allow network-outbound (remote unix-socket (path-regex #"^/private/var/run/mDNSResponder")))
(deny appleevent-send)
(deny process-exec (literal "/usr/bin/open") (literal "/usr/bin/osascript") (literal "/usr/bin/sandbox-exec"))
"""


def _sandboxed(scratch: Path, python: str):
    """(command prefix, note). On macOS: sandbox-exec with CHECK_PROFILE, or None if it can't run."""
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists():
        return [], "(not in a sandbox: sandbox-exec is macOS-only)"
    scratch = scratch.resolve()
    anc, p = [], scratch
    while p != p.parent:
        p = p.parent
        anc.append(f'(literal "{p}")')
    py = ""
    for d in {os.path.realpath(sys.base_prefix), os.path.realpath(sys.prefix), os.path.dirname(os.path.realpath(python))}:
        if d.startswith("/Users/") or d.startswith("/Volumes/"):
            py += f' (subpath "{d}")'
    prof = (CHECK_PROFILE.replace("@SCRATCH@", str(scratch)).replace("@PYTHON@", py)
            .replace("@ANCESTORS@", " ".join(anc) or '(literal "/")'))
    pre = ["/usr/bin/sandbox-exec", "-p", prof]
    try:
        ok = subprocess.run(pre + [python, "-c", "print('ok')"], capture_output=True, text=True, timeout=60).stdout.strip() == "ok"
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    return (pre, "(in a sandbox)") if ok else (None, "the check sandbox couldn't start on this Mac")


INVARIANTS_SNIPPET = r"""
import json, sys
sys.path.insert(0, "app")
import server
s = server._base_sandbox_settings()
sb, pm = s.get("sandbox", {}), s.get("permissions", {})
bad = []
if sb.get("enabled") is not True: bad.append("sandbox.enabled is no longer true")
if sb.get("failIfUnavailable") is not True: bad.append("sandbox.failIfUnavailable is no longer true")
if sb.get("allowUnsandboxedCommands") is not False: bad.append("unsandboxed commands are now allowed")
if pm.get("blockReadsOutsideWorkingDirectories") is not True: bad.append("reads outside the workspace are no longer blocked")
if pm.get("disableBypassPermissionsMode") != "disable": bad.append("bypass-permissions mode is no longer disabled")
print(json.dumps(bad))
"""


def _fingerprint(app_src: Path, lab: Path, pre: list, python: str, env: dict) -> dict:
    """FINGERPRINT_SNIPPET's output for one version of the app, run in the same scratch lab (so paths match)."""
    for d in ("app", "jobs", "config", "runtime", "shared"):
        shutil.rmtree(lab / d, ignore_errors=True)
    shutil.copytree(app_src, lab / "app", ignore=shutil.ignore_patterns(*SKIP, "*.pyc"))
    r = subprocess.run(pre + [python, "-c", FINGERPRINT_SNIPPET], cwd=str(lab), capture_output=True, text=True,
                       timeout=120, env=env)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"error": (r.stderr or r.stdout)[-400:]}


def run_checks(work: Path, python: str, scratch: Path, changed: list[str], base: Path | None = None) -> dict:
    """Compile, boot the new app on a spare port with an empty lab, call its API, check the sandbox
    invariants, and compare what it produces for the sandbox with the base. On macOS all of this runs
    in a sandbox. Returns {ok, needs_review: [reasons], log}."""
    log, ok, review = [], True, []
    app = work / "agent-lab" / "app"
    links = symlinks(work)
    if links:
        return {"ok": False, "needs_review": review,
                "log": "The change contains symbolic links, which aren't allowed: " + ", ".join(links)}
    pys = sorted(str(p) for p in app.glob("*.py"))
    r = subprocess.run([python, "-c", "import sys\nfor f in sys.argv[1:]: compile(open(f, 'rb').read(), f, 'exec')", *pys],
                       capture_output=True, text=True, timeout=120)  # syntax only; writes nothing
    log.append(f"$ compile {len(pys)} Python files: {'ok' if r.returncode == 0 else 'FAILED'}\n{r.stderr[-3000:]}")
    if r.returncode != 0:
        return {"ok": False, "needs_review": review, "log": "\n".join(log)}
    node = shutil.which("node")
    if node:
        for js in sorted((app / "static").glob("*.js")):
            r = subprocess.run([node, "-e", "new Function(require('fs').readFileSync(process.argv[1], 'utf8'))", str(js)],
                               capture_output=True, text=True, timeout=60)
            log.append(f"$ check {js.name} syntax: {'ok' if r.returncode == 0 else 'FAILED'}\n{r.stderr[-2000:]}")
            ok = ok and r.returncode == 0
    else:
        log.append("(node isn't installed, so the JavaScript syntax wasn't checked separately)")
    scratch.mkdir(parents=True, exist_ok=True)
    lab = scratch / "lab"
    shutil.rmtree(lab, ignore_errors=True)
    shutil.copytree(app, lab / "app", ignore=shutil.ignore_patterns(*SKIP, "*.pyc"))
    (scratch / "home").mkdir(exist_ok=True)
    pre, note = _sandboxed(scratch, python)
    if pre is None:
        review.append("the start-up checks couldn't run in their sandbox on this Mac, so the new code wasn't started")
        log.append(f"$ start the new app: skipped: {note}")
        return {"ok": ok, "needs_review": review, "log": "\n".join(log)}
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_LAB") and not k.startswith("GIT_")}
    env.update(PYTHONDONTWRITEBYTECODE="1", HOME=str((scratch / "home").resolve()))
    out = (scratch / "boot.log").open("w")
    proc = subprocess.Popen(pre + [python, "-u", "app/server.py", "--port", str(port)], cwd=str(lab), env=env,
                            stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        up = False
        for _ in range(120):
            if proc.poll() is not None:
                break
            try:
                if _http(port, "/api/settings")[0] == 200:
                    up = True
                    break
            except OSError:
                pass
            time.sleep(0.25)
        if not up:
            out.flush()
            log.append("$ start the new app: FAILED\n" + (scratch / "boot.log").read_text()[-4000:])
            return {"ok": False, "needs_review": review, "log": "\n".join(log)}
        log.append(f"$ start the new app on a spare port {note}: ok")
        for path in ("/", "/app.js", "/style.css", "/api/jobs", "/api/config", "/api/settings", "/api/versions", "/api/status"):
            st, _ = _http(port, path)
            log.append(f"  GET {path}: {st}")
            ok = ok and st == 200
        st, _ = _http(port, "/api/config/validate", "POST", {"config": {}})
        log.append(f"  POST /api/config/validate: {st}")
        ok = ok and st == 200
        st, _ = _http(port, "/api/config/validate", "POST", {"config": {}}, header=False)
        log.append(f"  POST without the page's header is refused: {'yes' if st == 403 else 'NO (' + str(st) + ')'}")
        if st != 403:
            review.append("requests from other websites are no longer refused (the X-Agent-Lab header check)")
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
        out.close()
    r = subprocess.run(pre + [python, "-c", INVARIANTS_SNIPPET], cwd=str(lab), capture_output=True, text=True, timeout=60, env=env)
    try:
        bad = json.loads(r.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        bad = [f"couldn't read the sandbox settings ({(r.stderr or r.stdout)[-300:].strip()})"]
    log.append("$ sandbox settings unchanged: " + ("ok" if not bad else "NO: " + "; ".join(bad)))
    review += bad
    if not re.search(r'app\.serve\(\s*"127\.0\.0\.1"', (app / "server.py").read_text()):
        review.append("the server may no longer listen only on this computer (127.0.0.1)")
    if base is not None:
        fp_new = _fingerprint(app, lab, pre, python, env)
        fp_old = _fingerprint(base / "agent-lab" / "app", lab, pre, python, env)
        diff = sorted(k for k in set(fp_old) | set(fp_new) if fp_old.get(k) != fp_new.get(k))
        log.append("$ sandbox and permission settings the app produces, compared with before: "
                   + ("unchanged" if not diff else "CHANGED: " + ", ".join(diff)))
        if diff:
            review.append("it changes the sandbox or permission settings that Agent Lab gives agents (" + ", ".join(diff) + ")")
    touched = [c for c in changed if any(c == f or (f.endswith("/") and c.startswith(f)) for f in SECURITY_FILES)]
    if touched:
        review.append("it changes files that enforce the sandbox or control self-changes: " + ", ".join(touched))
    return {"ok": ok, "needs_review": review, "log": "\n".join(log)}


# --------------------------------------------------------------------------- applying and undoing
def apply(lab: Path, work: Path, run_dir: Path, chs: list[dict]):
    """Copy the changed files into the live app, keeping the old versions in run_dir/backup."""
    backup = run_dir / "backup"
    added = []
    for c in chs:
        live = local_path(lab, c["path"])
        if live.exists() or live.is_symlink():
            t = backup / c["path"]
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live, t, follow_symlinks=False)
        else:
            added.append(c["path"])
    # everything Undo and restore-app.command need is written before the first file changes
    (run_dir / "added.txt").write_text("".join(f"{a}\n" for a in added))
    (lab / "runtime" / "selfdev" / "LAST_APPLIED").write_text(run_dir.name + "\n")
    for c in chs:
        live = local_path(lab, c["path"])
        if c["status"] == "deleted":
            live.unlink(missing_ok=True)
        else:
            src = work / c["path"]
            if src.is_symlink():
                raise RuntimeError(f"{c['path']} is a symbolic link")
            live.parent.mkdir(parents=True, exist_ok=True)
            if live.is_symlink():
                live.unlink()
            shutil.copy2(src, live, follow_symlinks=False)


def undo(lab: Path, run_dir: Path):
    """Put back the files from before this change."""
    backup = run_dir / "backup"
    for rel, src in tree_files(backup).items():
        dst = local_path(lab, rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    added = run_dir / "added.txt"
    if added.exists():
        for rel in added.read_text().splitlines():
            if rel.strip():
                local_path(lab, rel.strip()).unlink(missing_ok=True)
    last = lab / "runtime" / "selfdev" / "LAST_APPLIED"
    if last.exists() and last.read_text().strip() == run_dir.name:
        last.unlink()


# --------------------------------------------------------------------------- keeping personal details out of the repository
SECRET_RE = re.compile(r"sk-ant-[\w-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|hf_[A-Za-z0-9]{20,}"
                       r"|AKIA[0-9A-Z]{16}|xox[bp]-[\w-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
OK_EMAILS = ("noreply@anthropic.com", "users.noreply.github.com", "agent-lab@users.noreply.github.com")


def full_name() -> str:
    try:
        if sys.platform == "darwin":
            return subprocess.run(["id", "-F"], capture_output=True, text=True, timeout=10).stdout.strip()
        import pwd
        return pwd.getpwnam(getpass.getuser()).pw_gecos.split(",")[0]
    except Exception:  # noqa: BLE001
        return ""


GENERIC = {"cluster", "clusters", "example", "computing", "compute", "center", "centre", "research", "university",
           "institute", "lab", "labs", "laboratory", "slurm", "gpu", "gpus", "hpc", "the", "our", "main", "server",
           "servers", "node", "nodes", "campus", "school", "college", "department", "group", "team", "local", "remote"}


def private_terms(settings: dict) -> list[tuple[str, bool]]:
    """(term, case_sensitive) pairs that must never be published: your name and username, your home
    folder, tokens, the ntfy topic, the cluster name (and its acronyms), and the words listed in Settings."""
    terms = {(str(Path.home()), False)}
    for w in [getpass.getuser()] + re.findall(r"[\w'-]+", full_name()):
        if len(w) >= 3:
            terms.add((w, False))
    cluster = (settings.get("cluster_name") or "").strip()
    if len(cluster) >= 3:
        terms.add((cluster, False))
        for w in re.findall(r"[A-Za-z0-9]+", cluster):
            if w.lower() in GENERIC:
                continue
            if len(w) >= 2 and w.isupper():
                terms.add((w, True))  # an acronym, e.g. an institution: match exactly
            elif len(w) >= 4 and w[0].isupper():
                terms.add((w, True))  # a distinctive name: match when capitalized
    for k in ("ntfy_topic", "ntfy_token", "github_token"):
        if len(str(settings.get(k) or "")) >= 4:
            terms.add((str(settings[k]), False))
    for w in re.split(r"[,\n]", settings.get("private_words") or ""):
        if w.strip():
            terms.add((w.strip(), False))
    return sorted(terms, key=lambda t: -len(t[0]))


def scan(texts: dict[str, str], terms: list) -> list[str]:
    """Findings like 'README.md:12: contains “…”' (never echoing tokens in full)."""
    found = []
    pats = []
    for t in terms:
        t, cs = t if isinstance(t, tuple) else (t, False)
        word = re.fullmatch(r"[\w'-]+", t) is not None
        pats.append((t, re.compile((r"(?<![\w])" + re.escape(t) + r"(?![\w])") if word else re.escape(t), 0 if cs else re.I)))
    for name, text in texts.items():
        for i, line in enumerate(text.splitlines(), 1):
            for t, pat in pats:
                if pat.search(line):
                    shown = t if len(t) < 16 else t[:4] + "…"
                    found.append(f"{name}:{i}: contains “{shown}”")
            if SECRET_RE.search(line):
                found.append(f"{name}:{i}: looks like an access key or token")
            for m in EMAIL_RE.finditer(line):
                if not any(m.group(0).endswith(ok) for ok in OK_EMAILS):
                    found.append(f"{name}:{i}: contains an email address")
    return found[:40]


# --------------------------------------------------------------------------- GitHub
def git_problem() -> str | None:
    """Why git can't run here, or None."""
    g = shutil.which("git")
    if not g:
        return "git isn't installed. Install Apple's command line tools by running `xcode-select --install` in Terminal."
    if sys.platform == "darwin" and g == "/usr/bin/git":
        if subprocess.run(["xcode-select", "-p"], capture_output=True).returncode != 0:
            return "git needs Apple's command line tools. Install them by running `xcode-select --install` in Terminal."
    return None


def _git_env(token: str) -> dict:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    if token:  # passed in the environment, so it never appears in a command line or in .git/config
        b = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {b}")
    return env


def _git(args: list[str], cwd: Path, env: dict, timeout: int = 180, check: bool = True, binary: bool = False):
    r = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, timeout=timeout, text=not binary)
    if check and r.returncode != 0:
        err = (r.stderr if not binary else r.stderr.decode("utf-8", "replace")).strip()
        raise RuntimeError(f"git {args[0]} failed: {err[-600:]}")
    return r


def _git_show(clone: Path, env: dict, commit: str, rel: str):
    r = _git(["show", f"{commit}:{rel}"], clone, env, check=False, binary=True)
    return r.stdout if r.returncode == 0 else None


def publish(lab: Path, settings: dict, message: str, overwrite: bool = False) -> dict:
    """Bring the GitHub repository in line with the app on this computer, and push.

    Changes made on GitHub since Agent Lab's last push are respected: README.md and .gitignore changed
    only there are copied to this computer first. Anything else changed on GitHub (app code, or a file
    changed in both places), or a first push to a repository that differs, stops the push until you
    confirm (overwrite=True), so nothing on GitHub is replaced without you knowing."""
    repo, token = (settings.get("github_repo") or "").strip(), settings.get("github_token") or ""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        return {"ok": False, "error": "Set your GitHub repository (owner/name) in Settings first."}
    problem = git_problem()
    if problem:
        return {"ok": False, "error": problem}
    env = _git_env(token)
    rt = lab / "runtime"
    clone, state_file = rt / "repo", rt / "repo_state.json"
    url = f"https://github.com/{repo}.git"
    try:
        if not (clone / ".git").is_dir():
            shutil.rmtree(clone, ignore_errors=True)
            _git(["clone", url, str(clone)], rt, env, timeout=600)
        else:
            _git(["remote", "set-url", "origin", url], clone, env)
            _git(["fetch", "--prune", "origin"], clone, env, timeout=600)
        r = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], clone, env, check=False)
        branch = r.stdout.strip().split("/", 1)[1] if r.returncode == 0 and "/" in r.stdout else "main"
        has_remote = _git(["rev-parse", "--verify", "-q", f"origin/{branch}"], clone, env, check=False).returncode == 0
        if has_remote:
            _git(["checkout", "-q", "-B", branch, f"origin/{branch}"], clone, env)
            _git(["reset", "-q", "--hard", f"origin/{branch}"], clone, env)
        else:
            _git(["checkout", "-q", "-B", branch], clone, env)
        _git(["clean", "-qfd"], clone, env)
        head = _git(["rev-parse", "HEAD"], clone, env, check=False).stdout.strip() if has_remote else ""
        state = {}
        try:
            state = json.loads(state_file.read_text())
        except (OSError, json.JSONDecodeError):
            pass
        pulled, conflicts, remote_code = [], [], []
        base = state.get("commit") if state.get("repo") == repo else None
        base_ok = bool(base and head) and _git(["cat-file", "-e", base], clone, env, check=False).returncode == 0
        if head and base_ok and base != head:
            for rel in _git(["diff", "--name-only", base, head], clone, env).stdout.split("\n"):
                rel = rel.strip()
                if not rel or not is_managed(rel):
                    continue
                old, new = _git_show(clone, env, base, rel), _git_show(clone, env, head, rel)
                loc = local_path(lab, rel)
                cur = _read(loc)
                if cur == new:
                    continue
                if cur != old:
                    conflicts.append(rel)
                elif rel in DOC_FILES:  # changed only on GitHub: bring it to this computer
                    if new is None:
                        loc.unlink(missing_ok=True)
                    else:
                        loc.parent.mkdir(parents=True, exist_ok=True)
                        loc.write_bytes(new)
                    pulled.append(rel)
                else:
                    remote_code.append(rel)
        elif head and not base_ok and not overwrite:  # no record of the last push: compare everything
            remote, live0 = tree_files(clone), live_files(lab)
            here = [r for r, loc in managed(lab) if loc.exists()]  # published paths that exist on this computer
            differ = []
            for rel in sorted(set(remote) | set(live0)):
                if not any(rel == r or rel.startswith(r + "/") for r in here):
                    continue
                if (_read(remote[rel]) if rel in remote else None) != (_read(live0[rel]) if rel in live0 else None):
                    differ.append(rel)
            if differ:
                return {"ok": False, "pulled": pulled, "needs_confirm": True, "error":
                        "This is Agent Lab's first push to this repository, and these files differ from GitHub's: "
                        + ", ".join(differ[:20]) + (" …" if len(differ) > 20 else "")
                        + ". Pushing replaces GitHub's versions with this computer's."}
        if (conflicts or remote_code) and not overwrite:
            parts = []
            if conflicts:
                parts.append("changed both on GitHub and on this computer: " + ", ".join(conflicts))
            if remote_code:
                parts.append("changed only on GitHub (Agent Lab doesn't copy app code from GitHub into the running app): "
                             + ", ".join(remote_code))
            return {"ok": False, "pulled": pulled, "needs_confirm": True, "error":
                    "Nothing was pushed, because some files were " + "; and some ".join(parts) + ". Bring the changes "
                    "you want into the app (for example with a Modify app change), or push anyway to replace GitHub's versions."}
        live = live_files(lab)
        roots_here = {r for r, loc in managed(lab) if loc.exists()}
        for rel, p in tree_files(clone).items():
            root = next(r for r, _ in managed(lab) if rel == r or rel.startswith(r + "/"))
            if rel not in live and root in roots_here:
                p.unlink()
        for rel, src in live.items():
            t = clone / rel
            if _read(t) != _read(src):
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, t)
        _git(["add", "-A"], clone, env)
        names = [x for x in _git(["diff", "--cached", "--name-only", "--diff-filter=ACMR"], clone, env).stdout.split("\n") if x]
        if not _git(["diff", "--cached", "--name-only"], clone, env).stdout.strip():
            state_file.write_text(json.dumps({"repo": repo, "commit": head}))
            return {"ok": True, "nothing": True, "pulled": pulled, "url": f"https://github.com/{repo}"}
        texts = {"commit message": message}
        for n in names:
            try:
                raw = (clone / n).read_bytes()
            except OSError:
                continue
            try:
                texts[n] = raw.decode("utf-8")
            except UnicodeDecodeError:
                texts[n] = raw.decode("latin-1")  # binary files are scanned too
        found = scan(texts, private_terms(settings))
        if found:
            _git(["reset", "-q", "--hard"], clone, env)
            return {"ok": False, "pulled": pulled, "findings": found,
                    "error": "Nothing was pushed: the change contains personal details (listed below). Remove them "
                             "(for example with another change in this tab), then press Push again. Words come from "
                             "your name, folders, tokens, the cluster name and the word list in Settings."}
        subprocess.run(["git", "-c", "user.name=Agent Lab", "-c", "user.email=agent-lab@users.noreply.github.com",
                        "commit", "-q", "-F", "-"], cwd=str(clone), env=env, input=message, text=True,
                       capture_output=True, check=True, timeout=60)
        _git(["push", "origin", f"HEAD:{branch}"], clone, env, timeout=600)
        sha = _git(["rev-parse", "HEAD"], clone, env).stdout.strip()
        state_file.write_text(json.dumps({"repo": repo, "commit": sha}))
        return {"ok": True, "commit": sha[:7], "url": f"https://github.com/{repo}/commit/{sha}", "pulled": pulled}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": f"git commit failed: {(e.stderr or '')[-400:]}"}
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        msg = str(e)
        if "could not read Username" in msg or "Authentication failed" in msg or "403" in msg:
            msg += ("\n\ngit on this Mac isn't signed in to GitHub for this repository. Either paste a GitHub token "
                    "(fine-grained, with Contents: read and write on this repository) in Settings, or sign in once "
                    "with `gh auth login` in Terminal.")
        return {"ok": False, "error": msg}
