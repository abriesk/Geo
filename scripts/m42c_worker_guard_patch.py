#!/usr/bin/env python3
"""M4.2c worker guard — the main worker must never fake a flood result.

If a wrap_floodpy task ever lands on tasks.analysis (misroute, replay, operator
error), run_dummy would fabricate a synthetic 'subsiding' deformation result —
a confident lie. This makes run_task REFUSE wrap_floodpy loudly instead, so a
misroute fails (and dead-letters) rather than silently producing fiction.

Self-verifying: matches the exact run_task from services/worker/worker_main.py.
"""
from __future__ import annotations
import sys


def die(msg): print(f"ABORT: {msg}\nNo changes written."); raise SystemExit(1)


def patch(path: str) -> None:
    src = open(path).read(); orig = src
    if "wrap_floodpy belongs on tasks.flood" in src:
        print("Already guarded. Nothing to do."); return

    anchor = (
        "def run_task(task: AnalysisTaskMessage, emit) -> str:\n"
        "    if task.name in REAL_WRAPPERS:\n"
        "        return run_wrapper_subprocess(task, emit)\n"
        "    return run_dummy(task, emit)"
    )
    if src.count(anchor) != 1:
        die(f"run_task anchor found {src.count(anchor)}x (expected 1).")

    new = (
        "def run_task(task: AnalysisTaskMessage, emit) -> str:\n"
        "    if task.name == \"wrap_floodpy\":\n"
        "        # wrap_floodpy belongs on tasks.flood (the flood-worker). If it\n"
        "        # reaches the main worker it must NOT be dummied into a fake\n"
        "        # 'subsiding' result — that would be a confident lie. Fail so it\n"
        "        # dead-letters and the misroute is visible.\n"
        "        raise RuntimeError(\n"
        "            \"wrap_floodpy received on tasks.analysis; it must run on the \"\n"
        "            \"flood-worker via tasks.flood. Refusing to fake a result.\")\n"
        "    if task.name in REAL_WRAPPERS:\n"
        "        return run_wrapper_subprocess(task, emit)\n"
        "    return run_dummy(task, emit)"
    )
    src = src.replace(anchor, new, 1)
    if src == orig: die("no change produced.")
    open(path + ".m42cguard.bak", "w").write(orig)
    open(path, "w").write(src)
    import ast; ast.parse(src)
    print(f"Patched {path}\n  backup: {path}.m42cguard.bak\n  syntax OK")
    print("  wrap_floodpy on tasks.analysis now fails loudly instead of faking")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python3 m42c_worker_guard_patch.py <services/worker/worker_main.py>")
        raise SystemExit(2)
    patch(sys.argv[1])
