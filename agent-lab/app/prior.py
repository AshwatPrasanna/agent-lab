"""Give a new job copies of earlier jobs' workspaces under ./prior/<job-id>/.

On macOS the copies are APFS clones (clonefile): they're instant and take no extra disk
space until one side changes, so even large outputs and checkpoints are cheap to include.
Jobs only get copies, so a new job can never change an earlier job's files.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
import time
from pathlib import Path

SKIP_DIRS = {".venv", "venv", "__pycache__", ".pytest_cache", "node_modules", ".mypy_cache", ".ipynb_checkpoints"}
SKIP_FILES = {"_argv.json"}

_libc = None
if sys.platform == "darwin":
    try:
        _libc = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
        _libc.clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    except (OSError, AttributeError):
        _libc = None


def _clone(src, dst, *, follow_symlinks=True):
    if _libc is not None:
        if _libc.clonefile(os.fsencode(src), os.fsencode(dst), 0) == 0:
            return dst
    return shutil.copy2(src, dst, follow_symlinks=follow_symlinks)


def _ignore(_dir, names):
    return {n for n in names if n in SKIP_DIRS or n in SKIP_FILES}


def snapshot(jobs_dir: Path, ids: list[str], dest: Path) -> list[dict]:
    """Copy each job's work/ into dest/<id>/ and write dest/INDEX.md. Returns index rows."""
    dest.mkdir(parents=True, exist_ok=True)
    rows = []
    for jid in ids:
        jd = jobs_dir / jid
        job = json.loads((jd / "job.json").read_text())
        src, out = jd / "work", dest / jid
        if out.exists():
            shutil.rmtree(out)
        shutil.copytree(src, out, symlinks=True, ignore=_ignore, copy_function=_clone, dirs_exist_ok=False)
        # the copied job's own prior/ folder would nest copies of copies; keep only its index
        nested = out / "prior"
        if nested.is_dir():
            idx = nested / "INDEX.md"
            keep = idx.read_text() if idx.exists() else ""
            shutil.rmtree(nested)
            if keep:
                nested.mkdir()
                (nested / "INDEX.md").write_text(keep)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(job.get("created", 0)))
        meta = [f"# {job.get('title')}", "",
                f"- job id: {jid}", f"- created: {when}", f"- status: {job.get('status')}",
                f"- topology version: v{job.get('config_version')}",
                f"- spec: ./{job.get('spec_file', 'spec.md')}"]
        if job.get("prior"):
            meta.append(f"- it built on: {', '.join(job['prior'])}")
        if job.get("error") and job.get("status") != "done":
            meta += ["", "## Error", str(job["error"])]
        meta += ["", "## Final message from that job", str(job.get("result") or "(none)")]
        (out / "_JOB.md").write_text("\n".join(meta) + "\n")
        first = (str(job.get("result") or job.get("error") or "").strip().splitlines() or [""])[0]
        rows.append({"id": jid, "title": job.get("title"), "status": job.get("status"), "created": when, "summary": first[:160]})
    lines = ["# Earlier jobs available to this job", "",
             "Each folder is a copy of that job's workspace (virtual environments left out).",
             "`_JOB.md` in each one has the job's status and final message. These are copies: change them freely.", "",
             "| folder | title | status | created | final message (first line) |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| `prior/{r['id']}/` | {r['title']} | {r['status']} | {r['created']} | {r['summary'].replace('|', '/')} |")
    (dest / "INDEX.md").write_text("\n".join(lines) + "\n")
    return rows
