#!/usr/bin/env python3
"""M4.2c backend routing patch — self-verifying, idempotent.

Ship-a-whole-main.py has bitten us repeatedly (the sandbox copy is stale vs the
deployed file). So this patches the DEPLOYED services/backend/app/main.py in
place, asserting each anchor appears exactly once before touching it and
aborting without writing on any mismatch.

Three changes, all matching the M4.2c doc amendment:
  1. import FLOOD_QUEUE from geohazard_contracts.queues
  2. HAZARD_TO_WRAPPER["flood"]: "wrap_dummy" -> "wrap_floodpy"
  3. the analysis-publish loop routes wrap_floodpy -> FLOOD_QUEUE, everything
     else -> ANALYSIS_QUEUE (there are TWO such loops: the main router and the
     deferred download->analysis release; both are handled)

Run on the box:
    docker compose cp scripts/m42c_backend_patch.py backend:/tmp/patch.py   # or bind mount
    # simpler: from repo root where main.py lives:
    python3 scripts/m42c_backend_patch.py services/backend/app/main.py
Then rebuild backend.
"""
from __future__ import annotations

import sys


def die(msg: str) -> "None":
    print(f"ABORT: {msg}")
    print("No changes written.")
    raise SystemExit(1)


def patch(path: str) -> None:
    src = open(path).read()
    orig = src

    if 'FLOOD_QUEUE' in src and 'wrap_floodpy' in src and '"flood": "wrap_dummy"' not in src:
        print("Already patched (FLOOD_QUEUE present, flood->wrap_floodpy). Nothing to do.")
        return

    # --- 1. import FLOOD_QUEUE -------------------------------------------------
    imp_anchor = "from geohazard_contracts.queues import (\n    ANALYSIS_QUEUE,\n    DOWNLOAD_QUEUE,\n"
    if src.count(imp_anchor) != 1:
        die(f"queues import anchor found {src.count(imp_anchor)}x (expected 1). "
            "The import block differs from what M4.2c expects.")
    src = src.replace(
        imp_anchor,
        "from geohazard_contracts.queues import (\n    ANALYSIS_QUEUE,\n    DOWNLOAD_QUEUE,\n    FLOOD_QUEUE,\n",
        1,
    )

    # --- 2. HAZARD_TO_WRAPPER flood -> wrap_floodpy ---------------------------
    hz_anchor = '"flood": "wrap_dummy",         # M4: wrap_floodpy'
    if src.count(hz_anchor) != 1:
        # try a looser match
        loose = '"flood": "wrap_dummy"'
        if src.count(loose) != 1:
            die(f'HAZARD_TO_WRAPPER flood anchor not uniquely found '
                f'({src.count(loose)}x for "{loose}").')
        src = src.replace(loose, '"flood": "wrap_floodpy"', 1)
    else:
        src = src.replace(
            hz_anchor,
            '"flood": "wrap_floodpy",       # M4.2c: real flood via flood-worker',
            1,
        )

    # --- 3. route wrap_floodpy analysis tasks to FLOOD_QUEUE -------------------
    # Both publish loops share this exact statement. Replace ALL occurrences,
    # each with a name-based routing decision.
    pub_anchor = (
        'channel.basic_publish(exchange="", routing_key=ANALYSIS_QUEUE,\n'
        '                              body='
    )
    n = src.count(pub_anchor)
    if n < 1:
        die(f"analysis-publish anchor not found (expected >=1, got {n}).")

    # The two call sites differ only in the message variable name (t vs msg).
    # Handle both by replacing the routing_key expression with a helper call
    # that inspects the message's .name. Inject the helper once.
    helper = (
        "\ndef _analysis_route(name: str) -> str:\n"
        "    # M4.2c: wrap_floodpy runs in the separate flood-worker via its own\n"
        "    # queue; every other wrapper stays on tasks.analysis. Only the\n"
        "    # routing key changes — the AnalysisTaskMessage contract is unchanged.\n"
        "    from geohazard_contracts.queues import ANALYSIS_QUEUE, FLOOD_QUEUE\n"
        "    return FLOOD_QUEUE if name == \"wrap_floodpy\" else ANALYSIS_QUEUE\n\n"
    )

    # site A: `for t in analysis_msgs:` ... routing_key=ANALYSIS_QUEUE, body=t.model_dump_json()
    siteA = (
        'channel.basic_publish(exchange="", routing_key=ANALYSIS_QUEUE,\n'
        '                              body=t.model_dump_json(),\n'
        '                              properties=pika.BasicProperties(delivery_mode=2))'
    )
    siteA_new = (
        'channel.basic_publish(exchange="", routing_key=_analysis_route(t.name),\n'
        '                              body=t.model_dump_json(),\n'
        '                              properties=pika.BasicProperties(delivery_mode=2))'
    )
    # site B: deferred release uses `msg` (from AnalysisTaskMessage(...)) — see
    # _release_downloads_for. Its publish reads routing_key=ANALYSIS_QUEUE, body=msg...
    siteB = (
        'channel.basic_publish(exchange="", routing_key=ANALYSIS_QUEUE,\n'
        '                              body=msg.model_dump_json(),\n'
        '                              properties=pika.BasicProperties(delivery_mode=2))'
    )
    siteB_new = (
        'channel.basic_publish(exchange="", routing_key=_analysis_route(msg.name),\n'
        '                              body=msg.model_dump_json(),\n'
        '                              properties=pika.BasicProperties(delivery_mode=2))'
    )

    replaced = 0
    if siteA in src:
        src = src.replace(siteA, siteA_new, 1); replaced += 1
    if siteB in src:
        src = src.replace(siteB, siteB_new, 1); replaced += 1
    if replaced == 0:
        die("neither analysis-publish site matched the expected exact text; "
            "the publish statements differ. Inspect main.py and adjust the patch.")

    # inject the helper just before HAZARD_TO_WRAPPER (a stable, unique anchor)
    hz_map = "HAZARD_TO_WRAPPER = {"
    if src.count(hz_map) != 1:
        die("HAZARD_TO_WRAPPER definition not uniquely found for helper injection.")
    src = src.replace(hz_map, helper + hz_map, 1)

    if src == orig:
        die("no changes produced — anchors matched but replacements were no-ops.")

    bak = path + ".m42c.bak"
    open(bak, "w").write(orig)
    open(path, "w").write(src)
    print(f"Patched {path}")
    print(f"  backup: {bak}")
    print(f"  routing sites updated: {replaced}")
    print("  flood -> wrap_floodpy; wrap_floodpy -> FLOOD_QUEUE")
    # sanity: it still parses
    import ast
    ast.parse(src)
    print("  syntax OK")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python3 m42c_backend_patch.py <path/to/backend/app/main.py>")
        raise SystemExit(2)
    patch(sys.argv[1])
