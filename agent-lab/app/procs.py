"""Process-tree helpers: pause, resume and stop a job together with everything it started."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time


def _table() -> list[tuple[int, int, int]]:
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid="], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(p.lstrip("-").isdigit() for p in parts):
            rows.append(tuple(int(p) for p in parts))
    return rows


def tree(root: int) -> list[int]:
    """root, its descendants, and anything else in root's process group (root first)."""
    rows = _table()
    kids: dict[int, list[int]] = {}
    for pid, ppid, _ in rows:
        kids.setdefault(ppid, []).append(pid)
    seen, order, stack = set(), [], [root]
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        order.append(p)
        stack.extend(kids.get(p, []))
    for pid, _, pgid in rows:
        if pgid == root and pid not in seen:
            seen.add(pid)
            order.append(pid)
    return order


def _send(pids, sig):
    for p in pids:
        try:
            os.kill(p, sig)
        except (ProcessLookupError, PermissionError):
            pass


def pause(root: int) -> int:
    pids = tree(root)
    _send(pids, signal.SIGSTOP)
    return len(pids)


def resume(root: int) -> int:
    pids = tree(root)
    _send(pids, signal.SIGCONT)
    return len(pids)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop(root: int, grace: float = 6.0):
    """SIGTERM the job's main process, then make sure every process it started is gone.
    The tree is captured first, because children are re-parented once the main process exits."""
    pids = tree(root)
    _send(pids, signal.SIGCONT)  # a paused tree can't handle SIGTERM until resumed
    _send([root], signal.SIGTERM)

    def finish():
        deadline = time.time() + grace
        while time.time() < deadline and _alive(root):
            time.sleep(0.3)
        rest = [p for p in pids if _alive(p)]
        _send(rest, signal.SIGTERM)
        time.sleep(1.5)
        _send([p for p in rest if _alive(p)], signal.SIGKILL)

    threading.Thread(target=finish, daemon=True).start()
