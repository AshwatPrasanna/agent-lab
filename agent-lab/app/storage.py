"""How much disk space Agent Lab uses, and safe ways to free some.

The total covers everything in the agent-lab folder. Copies of earlier jobs (work/prior/) are counted
separately: they're APFS clones, which share their blocks with the original until one side changes,
so they use almost no real space even though each file reports its full size.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

VENV_NAMES = {".venv", "venv"}


def _is_venv(entry: os.DirEntry) -> bool:
    return entry.name in VENV_NAMES or entry.name.endswith("_venv") or os.path.exists(os.path.join(entry.path, "pyvenv.cfg"))


def walk(root: Path, seen: set, classify=None) -> dict:
    """Bytes on disk under root by category ("" = root's own). classify(entry, depth) can return a
    category name for a subfolder; everything under it is counted there. Symlinks aren't followed;
    hard-linked files are counted once."""
    out: dict = {}
    stack = [(str(root), "", 0)]
    while stack:
        path, cat, depth = stack.pop()
        try:
            it = os.scandir(path)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    sub = classify(e, depth) if classify and not cat else None
                    stack.append((e.path, sub or cat, depth + 1))
                    continue
                if st.st_nlink > 1:
                    key = (st.st_dev, st.st_ino)
                    if key in seen:
                        continue
                    seen.add(key)
                size = getattr(st, "st_blocks", 0) * 512 or st.st_size
                out[cat] = out.get(cat, 0) + size
    return out


def scan(lab: Path, jobs_info: dict | None = None) -> dict:
    """{total, prior_copies, parts, jobs: [...], shared: {...}, free, disk, scanned, seconds}."""
    t0 = time.time()
    seen: set = set()
    parts, jobs, shared = {}, [], {}
    for top in ("app", "config", "runtime"):
        parts[top] = sum(walk(lab / top, seen).values())
    models = []
    for p in sorted((lab / "shared").iterdir()) if (lab / "shared").is_dir() else []:
        if p.name == "hf" and p.is_dir() and not p.is_symlink():  # Hugging Face cache: one entry per model
            c = walk(p, seen, lambda e, depth: e.name if depth == 1 and os.path.basename(os.path.dirname(e.path)) == "hub" else None)
            shared[p.name] = sum(c.values())
            models = sorted(({"name": k, "bytes": v} for k, v in c.items() if k), key=lambda m: -m["bytes"])
        elif p.is_dir() and not p.is_symlink():
            shared[p.name] = sum(walk(p, seen).values())
        elif p.is_file():
            shared[p.name] = p.lstat().st_size
    parts["shared"] = sum(shared.values())
    prior_total = 0
    if (lab / "jobs").is_dir():
        for jd in sorted((lab / "jobs").iterdir()):
            if not jd.is_dir() or jd.is_symlink():
                continue

            def classify(e, depth, jd=jd):
                if depth == 1 and e.name == "prior" and os.path.dirname(e.path) == str(jd / "work"):
                    return "prior"
                if e.is_dir(follow_symlinks=False) and _is_venv(e):
                    return "venv"
                return None
            c = walk(jd, seen, classify)
            own = c.get("", 0) + c.get("venv", 0)
            prior_total += c.get("prior", 0)
            info = (jobs_info or {}).get(jd.name, {})
            jobs.append({"id": jd.name, "title": info.get("title"), "status": info.get("status"),
                         "bytes": own, "venv": c.get("venv", 0), "prior": c.get("prior", 0)})
    parts["jobs"] = sum(j["bytes"] for j in jobs)
    for p in lab.iterdir():  # loose files at the top (README, scripts)
        if p.is_file() and not p.is_symlink():
            parts["other"] = parts.get("other", 0) + p.lstat().st_size
    du = shutil.disk_usage(str(lab))
    jobs.sort(key=lambda j: -j["bytes"])
    return {"total": sum(parts.values()), "prior_copies": prior_total, "parts": parts, "jobs": jobs,
            "shared": dict(sorted(shared.items(), key=lambda kv: -kv[1])), "models": models,
            "free": du.free, "disk": du.total,
            "scanned": time.time(), "seconds": round(time.time() - t0, 1)}


def venv_dirs(work: Path) -> list[Path]:
    """Virtual environments in a job's workspace (not inside prior/ copies). They can be recreated."""
    out = []
    stack = [work]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if not e.is_dir(follow_symlinks=False) or (d == work and e.name == "prior"):
                continue
            if _is_venv(e):
                out.append(Path(e.path))
            elif e.name not in ("node_modules", "__pycache__", ".git"):
                stack.append(Path(e.path))
    return out


def delete_venvs(work_dirs: list[Path]) -> tuple[int, int]:
    """Delete the virtual environments in these workspaces. Returns (folders, bytes freed)."""
    n = freed = 0
    for w in work_dirs:
        for v in venv_dirs(w):
            freed += sum(walk(v, set()).values())
            shutil.rmtree(v, ignore_errors=True)
            n += 1
    return n, freed


def delete_model(lab: Path, name: str) -> int:
    """Delete one entry of the shared Hugging Face cache (e.g. models--org--name). It's downloaded again
    the next time a job uses it. Returns bytes freed."""
    import re
    if not re.fullmatch(r"(models|datasets|spaces)--[\w.-]+(--[\w.-]+)*", name):
        raise ValueError("not a Hugging Face cache entry")
    d = lab / "shared" / "hf" / "hub" / name
    if not d.is_dir() or d.is_symlink():
        raise ValueError("no such model in the cache")
    freed = sum(walk(d, set()).values())
    shutil.rmtree(d)
    return freed


def prune_uv_cache(lab: Path) -> tuple[int, str]:
    """`uv cache prune`: removes unused entries from the shared package cache. Returns (bytes freed, output)."""
    uv, cache = lab / "shared" / "bin" / "uv", lab / "shared" / "uv-cache"
    if not uv.exists() or not cache.exists():
        return 0, "no uv cache"
    before = sum(walk(cache, set()).values())
    r = subprocess.run([str(uv), "cache", "prune"], env=dict(os.environ, UV_CACHE_DIR=str(cache)),
                       capture_output=True, text=True, timeout=600)
    after = sum(walk(cache, set()).values())
    return max(0, before - after), (r.stdout + r.stderr).strip()[-500:]


def fmt(n: float) -> str:
    """Sizes the way Finder shows them (1 GB = 1,000,000,000 bytes)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit not in ("B", "KB") else f"{n:.0f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"
