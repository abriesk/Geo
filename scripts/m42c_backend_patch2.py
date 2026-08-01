#!/usr/bin/env python3
"""M4.2c backend routing patch 2 — completes the MAIN router publish site.

Patch 1 injected _analysis_route and rewired the deferred-release site (line
258) but MISSED the main router's publish loop, because that site is indented 8
spaces (inside a try/for) and patch 1's anchor assumed 4. Result: fresh queries
published wrap_floodpy to tasks.analysis -> main worker -> dummy.

This patches ONLY that remaining site, matched byte-for-byte from the deployed
file, and then ASSERTS no bare `routing_key=ANALYSIS_QUEUE` publish remains — so
it cannot leave a partial fix the way patch 1 did.
"""
from __future__ import annotations

import sys


def die(msg: str) -> None:
    print(f"ABORT: {msg}\nNo changes written.")
    raise SystemExit(1)


def patch(path: str) -> None:
    src = open(path).read()
    orig = src

    if "_analysis_route" not in src:
        die("patch 1 markers absent — run m42c_backend_patch.py first.")

    # Exact block from the deployed file (8-space indent, loop var `t`).
    site = (
        '        for t in analysis_msgs:\n'
        '            channel.basic_publish(exchange="", routing_key=ANALYSIS_QUEUE,\n'
        '                                  body=t.model_dump_json(),\n'
        '                                  properties=pika.BasicProperties(delivery_mode=2))'
    )
    new = (
        '        for t in analysis_msgs:\n'
        '            channel.basic_publish(exchange="", routing_key=_analysis_route(t.name),\n'
        '                                  body=t.model_dump_json(),\n'
        '                                  properties=pika.BasicProperties(delivery_mode=2))'
    )

    n = src.count(site)
    if n == 0:
        # maybe already patched
        if '_analysis_route(t.name)' in src:
            print("Main router site already routes via _analysis_route(t.name).")
        else:
            die("main-router publish site not found with expected text. "
                "Paste sed -n '617,620p' so the anchor can be matched.")
    elif n == 1:
        src = src.replace(site, new, 1)
        print("Rewired main-router publish site -> _analysis_route(t.name)")
    else:
        die(f"main-router anchor found {n}x (expected 1).")

    # Safety net: after patching, the ONLY bare ANALYSIS_QUEUE publish allowed is
    # none — every analysis publish must go through _analysis_route. (Download
    # publishes use DOWNLOAD_QUEUE and are unaffected.)
    import re as _re
    bare = _re.findall(r'routing_key=ANALYSIS_QUEUE\b', src)
    if bare:
        die(f"{len(bare)} publish(es) still use routing_key=ANALYSIS_QUEUE directly. "
            "The fix would be partial; refusing to write.")

    if src == orig:
        print("No change needed.")
        return

    bak = path + ".m42c2.bak"
    open(bak, "w").write(orig)
    open(path, "w").write(src)
    import ast; ast.parse(src)
    print(f"Patched {path}\n  backup: {bak}\n  syntax OK")
    print("  verified: no bare routing_key=ANALYSIS_QUEUE publishes remain")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python3 m42c_backend_patch2.py <path/to/backend/app/main.py>")
        raise SystemExit(2)
    patch(sys.argv[1])
