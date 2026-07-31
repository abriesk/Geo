#!/usr/bin/env python3
"""M4.2c flood-dates patch — resolve null dates for self-downloading wrappers.

The GUI map draw sends null dates. The download path fills them via
_effective_dates(payload.dates, lookback); but self-downloading wrappers
(wrap_floodpy, wrap_licsbas) build `common` with raw payload.dates and skip
that branch, so nulls reach the wrapper and wrap_floodpy falls back to its own
blind 90-day window (observed: 2026-05..07, no storm).

Fix: a per-wrapper self-download lookback table, and resolve dates when building
`common` so both flood and licsbas get a real window. MVP: flood = 3 months
(user decision); licsbas keeps its effective behaviour (its own default is
unchanged if we pass through). Self-verifying + idempotent.
"""
from __future__ import annotations
import sys


def die(msg): print(f"ABORT: {msg}\nNo changes written."); raise SystemExit(1)


def patch(path: str) -> None:
    src = open(path).read(); orig = src
    if "SELF_DOWNLOAD_LOOKBACK" in src:
        print("Already patched (SELF_DOWNLOAD_LOOKBACK present)."); return

    # 1. add a self-download lookback table right after NEEDS_DOWNLOAD closes.
    #    Anchor: the wrap_egms line + closing brace of NEEDS_DOWNLOAD.
    tbl_anchor = '    "wrap_egms": ("egms", "egms", ["EGMS_L2b"], 24),\n}'
    if src.count(tbl_anchor) != 1:
        die(f"NEEDS_DOWNLOAD close anchor found {src.count(tbl_anchor)}x (expected 1).")
    tbl_new = tbl_anchor + '''

# Self-downloading wrappers (no NEEDS_DOWNLOAD entry) still need null dates
# resolved, or they fall back to their own blind default window. §6.1-style
# per-wrapper lookback in months. MVP: flood screens the recent 3 months for a
# rainfall event; the frontend date picker + pre-analysis windowing (backlog)
# will replace this. licsbas listed for parity; its window is otherwise its own.
SELF_DOWNLOAD_LOOKBACK = {
    "wrap_floodpy": 3,
    "wrap_licsbas": 3,
}'''
    src = src.replace(tbl_anchor, tbl_new, 1)

    # 2. resolve dates when building `common`.
    common_anchor = (
        '                common = dict(\n'
        '                    query_id=query_id,\n'
        '                    aoi=payload.aoi,\n'
        '                    dates=payload.dates,\n'
        '                )'
    )
    if src.count(common_anchor) != 1:
        die(f"`common` dict anchor found {src.count(common_anchor)}x (expected 1).")
    common_new = (
        '                # Resolve null dates for self-downloading wrappers so they do\n'
        '                # not fall back to a blind default window (M4.2c). Download-fed\n'
        '                # paths re-resolve with their own lookback below; passing an\n'
        '                # already-resolved range through _effective_dates is a no-op.\n'
        '                _self_lb = SELF_DOWNLOAD_LOOKBACK.get(wrapper)\n'
        '                if _self_lb is not None:\n'
        '                    _cs, _ce = _effective_dates(payload.dates, _self_lb)\n'
        '                    from geohazard_contracts import DateRange as _DRc\n'
        '                    _common_dates = _DRc(start=_cs, end=_ce)\n'
        '                else:\n'
        '                    _common_dates = payload.dates\n'
        '                common = dict(\n'
        '                    query_id=query_id,\n'
        '                    aoi=payload.aoi,\n'
        '                    dates=_common_dates,\n'
        '                )'
    )
    src = src.replace(common_anchor, common_new, 1)

    if src == orig:
        die("anchors matched but no change produced.")
    open(path + ".m42cdates.bak", "w").write(orig)
    open(path, "w").write(src)
    import ast; ast.parse(src)
    print(f"Patched {path}\n  backup: {path}.m42cdates.bak\n  syntax OK")
    print("  wrap_floodpy null dates -> 3-month lookback (MVP)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python3 m42c_flood_dates_patch.py <backend/app/main.py>")
        raise SystemExit(2)
    patch(sys.argv[1])
