#!/usr/bin/env python3
"""m411_patch.py — M4.1.1: multi-method fan-out + cross-validation synthesis.

Applied as a PATCH rather than shipped as whole files, because the author's
sandbox copy of main.py has twice proven stale against what is deployed. Every
replacement below asserts its expected text is present EXACTLY ONCE and aborts
without writing anything if not — so a mismatch is a clean failure, never a
corrupted backend.

Run from the repo root:   python3 scripts/m411_patch.py
Re-running is safe: it detects already-applied edits and skips them.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

MAIN = Path("services/backend/app/main.py")
LLM = Path("services/backend/app/llm.py")

# --------------------------------------------------------------------------
# 1. main.py — constants: reverse map, task cap, dual-method depth knob
# --------------------------------------------------------------------------
C_OLD = '''DEPTH_MAX_METHODS = {"quick": 1, "standard": 2, "thorough": len(HAZARD_TO_WRAPPER)}
'''
C_NEW = '''DEPTH_MAX_METHODS = {"quick": 1, "standard": 2, "thorough": len(HAZARD_TO_WRAPPER)}

# M4.1.1: reverse lookup, used by the download->analysis handoff and by the
# per-method results layout. It cannot be derived by inverting
# HAZARD_TO_WRAPPER, because deformation maps to TWO wrappers depending on
# which tier(s) the ladder selects.
WRAPPER_TO_HAZARD = {
    "wrap_licsbas": "deformation",
    "wrap_egms": "deformation",
    "wrap_ndvi": "vegetation",
    "wrap_floodpy": "flood",
    "wrap_dummy": "vegetation",
}
# Ceiling on analysis tasks per query, so "thorough" on a multi-hazard question
# cannot fan out into a pile of half-hour LiCSBAS runs.
MAX_ANALYSIS_TASKS = int(os.environ.get("MAX_ANALYSIS_TASKS", "4"))
# Lowest depth at which deformation runs EGMS *and* LiCSBAS together.
# Set to "thorough" to keep standard-depth European queries fast.
DEFORM_DUAL_METHOD_MIN_DEPTH = os.environ.get(
    "DEFORM_DUAL_METHOD_MIN_DEPTH", "standard")
'''

# --------------------------------------------------------------------------
# 2. main.py — tier resolver returns a LIST of methods
# --------------------------------------------------------------------------
R_OLD = '''def _resolve_deform_tier(aoi_geojson: dict) -> str:
    """§3 ladder for deformation: tier-1 EGMS if the AOI is within the EGMS
    footprint, else tier-2 LiCSBAS. Any failure falls back to LiCSBAS (the
    working path). Tiers 3/4 (HyP3/raw) are M6."""
    if EGMS_ENABLED:
        try:
            import sys as _sys
            if "/libs" not in _sys.path:
                _sys.path.insert(0, "/libs")
            from egms.footprint import aoi_in_egms
            if aoi_in_egms(aoi_geojson, EGMS_FOOTPRINT):
                print("[router] deformation tier 1: EGMS (AOI within footprint)", flush=True)
                return "wrap_egms"
        except Exception as e:  # noqa: BLE001
            print(f"[router] EGMS coverage check error ({e!r}); using LiCSBAS", flush=True)
    print("[router] deformation tier 2: LiCSBAS", flush=True)
    return "wrap_licsbas"
'''
R_NEW = '''def _egms_covers(aoi_geojson: dict) -> bool:
    """Is the AOI inside the EGMS footprint? Any failure -> False (use LiCSBAS,
    which is the path that always works)."""
    if not EGMS_ENABLED:
        return False
    try:
        import sys as _sys
        if "/libs" not in _sys.path:
            _sys.path.insert(0, "/libs")
        from egms.footprint import aoi_in_egms
        return bool(aoi_in_egms(aoi_geojson, EGMS_FOOTPRINT))
    except Exception as e:  # noqa: BLE001
        print(f"[router] EGMS coverage check error ({e!r}); using LiCSBAS", flush=True)
        return False


def _resolve_deform_methods(aoi_geojson: dict, depth: str) -> list[str]:
    """§3 ladder for deformation, now returning ONE OR MORE methods (M4.1.1).

    EGMS and LiCSBAS are complementary rather than redundant. EGMS is a
    GNSS-referenced multi-year VERTICAL average over a fixed release window;
    LiCSBAS is a line-of-sight time series over the window the user asked for,
    referenced to a locally chosen pixel. Running both lets the answer say
    things neither can alone — most valuably "stable for years, but moving
    recently", which is precisely the case worth a professional's attention.

    Depth decides how much work to do. Outside the EGMS footprint only LiCSBAS
    exists, so depth changes nothing there.
    """
    if not _egms_covers(aoi_geojson):
        print("[router] deformation: LiCSBAS only (AOI outside EGMS footprint)",
              flush=True)
        return ["wrap_licsbas"]

    rank = {"quick": 0, "standard": 1, "thorough": 2}
    if rank.get(depth, 1) >= rank.get(DEFORM_DUAL_METHOD_MIN_DEPTH, 1):
        print("[router] deformation: EGMS + LiCSBAS (cross-validation)", flush=True)
        return ["wrap_egms", "wrap_licsbas"]
    print("[router] deformation tier 1: EGMS (AOI within footprint)", flush=True)
    return ["wrap_egms"]
'''

# --------------------------------------------------------------------------
# 3. main.py — routing loop: fan out over methods, per-method output dirs
# --------------------------------------------------------------------------
L_OLD = '''        analysis_msgs, download_msgs, deferred_rows = [], [], []
        for hazard in hazards:
            # M4.1: deformation's wrapper is chosen at routing time by the §3
            # ladder (EGMS tier-1 in Europe, else LiCSBAS tier-2).
            if hazard == "deformation":
                wrapper = _resolve_deform_tier(payload.aoi.model_dump())
            else:
                wrapper = HAZARD_TO_WRAPPER[hazard]
            common = dict(
                query_id=query_id,
                aoi=payload.aoi,
                dates=payload.dates,
            )
            # A path is download-fed if the hazard OR the resolved wrapper has a
            # NEEDS_DOWNLOAD entry (vegetation keyed by hazard; wrap_egms by
            # wrapper). wrap_licsbas has neither -> self-downloading.
            dl_key = hazard if hazard in NEEDS_DOWNLOAD else (
                wrapper if wrapper in NEEDS_DOWNLOAD else None)
            if dl_key is not None:
                product_type, tier, products, lookback = NEEDS_DOWNLOAD[dl_key]
                eff_start, eff_end = _effective_dates(payload.dates, lookback)
                a_hash = payload.aoi.hash()
                cached = _cache_lookup(a_hash, product_type, eff_start, eff_end)
                if cached:
                    print(f"[router] cache HIT {product_type} for {a_hash[:12]}\u2026 -> skip download", flush=True)
                    analysis_msgs.append(AnalysisTaskMessage(
                        task_id=uuid.uuid4(), name=wrapper,
                        input_dir=cached, output_dir=f"{RESULTS_ROOT}/{query_id}/{hazard}",
                        params={"hazard": hazard,
                                "simulate_failure": "FAIL!" in payload.question},
                        **common,
                    ))
                else:
                    print(f"[router] cache MISS {product_type} for {a_hash[:12]}\u2026 -> download via {tier}", flush=True)
                    from geohazard_contracts import DateRange as _DR
                    download_msgs.append(DownloadTaskMessage(
                        task_id=uuid.uuid4(), tier=tier, products=products,
                        query_id=query_id, aoi=payload.aoi,
                        dates=_DR(start=eff_start, end=eff_end),
                    ))
                    # Analysis row exists now but its message is built & published
                    # only when the download completes (M2.2: vegetation only;
                    # M3 generalizes this linkage).
                    deferred_rows.append((uuid.uuid4(), wrapper))
            else:
                params = ({"hazard": hazard,
                           "simulate_failure": "FAIL!" in payload.question}
                          if hazard != "deformation"
                          else _deform_params(payload.question, payload.aoi.model_dump()))
                analysis_msgs.append(AnalysisTaskMessage(
                    task_id=uuid.uuid4(), name=wrapper,
                    input_dir="/data/scratch",
                    output_dir=f"{RESULTS_ROOT}/{query_id}/{hazard}",
                    params=params,
                    **common,
                ))
'''
L_NEW = '''        analysis_msgs, download_msgs, deferred_rows = [], [], []
        for hazard in hazards:
            # M4.1.1: deformation can fan out to SEVERAL methods (EGMS +
            # LiCSBAS) for cross-validation; every other hazard has one.
            if hazard == "deformation":
                wrappers = _resolve_deform_methods(
                    payload.aoi.model_dump(), payload.depth.value)
            else:
                wrappers = [HAZARD_TO_WRAPPER[hazard]]

            for wrapper in wrappers:
                if len(analysis_msgs) + len(deferred_rows) >= MAX_ANALYSIS_TASKS:
                    print(f"[router] analysis-task cap {MAX_ANALYSIS_TASKS} reached;"
                          f" skipping {hazard}/{wrapper}", flush=True)
                    continue
                common = dict(
                    query_id=query_id,
                    aoi=payload.aoi,
                    dates=payload.dates,
                )
                # Results are per-METHOD (M4.1.1): two deformation methods would
                # otherwise both write .../deformation/result.json and silently
                # clobber one another.
                out_dir = f"{RESULTS_ROOT}/{query_id}/{hazard}/{wrapper}"
                # A path is download-fed if the hazard OR the resolved wrapper
                # has a NEEDS_DOWNLOAD entry (vegetation keyed by hazard;
                # wrap_egms by wrapper). wrap_licsbas has neither -> it
                # self-downloads.
                dl_key = hazard if hazard in NEEDS_DOWNLOAD else (
                    wrapper if wrapper in NEEDS_DOWNLOAD else None)
                if dl_key is not None:
                    product_type, tier, products, lookback = NEEDS_DOWNLOAD[dl_key]
                    eff_start, eff_end = _effective_dates(payload.dates, lookback)
                    a_hash = payload.aoi.hash()
                    cached = _cache_lookup(a_hash, product_type, eff_start, eff_end)
                    if cached:
                        print(f"[router] cache HIT {product_type} for {a_hash[:12]}\u2026 -> skip download", flush=True)
                        analysis_msgs.append(AnalysisTaskMessage(
                            task_id=uuid.uuid4(), name=wrapper,
                            input_dir=cached, output_dir=out_dir,
                            params={"hazard": hazard,
                                    "simulate_failure": "FAIL!" in payload.question},
                            **common,
                        ))
                    else:
                        print(f"[router] cache MISS {product_type} for {a_hash[:12]}\u2026 -> download via {tier}", flush=True)
                        from geohazard_contracts import DateRange as _DR
                        download_msgs.append(DownloadTaskMessage(
                            task_id=uuid.uuid4(), tier=tier, products=products,
                            query_id=query_id, aoi=payload.aoi,
                            dates=_DR(start=eff_start, end=eff_end),
                        ))
                        # The analysis row exists now; its message is built and
                        # published only once the download completes.
                        deferred_rows.append((uuid.uuid4(), wrapper))
                else:
                    params = ({"hazard": hazard,
                               "simulate_failure": "FAIL!" in payload.question}
                              if hazard != "deformation"
                              else _deform_params(payload.question,
                                                  payload.aoi.model_dump()))
                    analysis_msgs.append(AnalysisTaskMessage(
                        task_id=uuid.uuid4(), name=wrapper,
                        input_dir="/data/scratch",
                        output_dir=out_dir,
                        params=params,
                        **common,
                    ))
'''

# --------------------------------------------------------------------------
# 4. main.py — deferred publish: derive hazard, per-method output dir
#    (this is the path that filed EGMS results under .../vegetation/)
# --------------------------------------------------------------------------
P_OLD = '''    aoi_raw = q["aoi"] if not isinstance(q["aoi"], str) else json.loads(q["aoi"])
    hazard = "vegetation"  # M2.2: sole download-gated hazard; M3 generalizes
    import pika

    conn_mq, channel = connect_and_declare(AMQP_URL)
    for row in pending:
        msg = AnalysisTaskMessage(
            task_id=row["task_id"], query_id=uuid.UUID(query_id),
            name=row["name"], input_dir=input_dir,
            output_dir=f"{RESULTS_ROOT}/{query_id}/{hazard}",
'''
P_NEW = '''    aoi_raw = q["aoi"] if not isinstance(q["aoi"], str) else json.loads(q["aoi"])
    import pika

    conn_mq, channel = connect_and_declare(AMQP_URL)
    for row in pending:
        # M4.1.1: derive the hazard from the wrapper. This was hardcoded to
        # "vegetation" back when vegetation was the only download-gated hazard,
        # which filed EGMS deformation results under .../vegetation/.
        hazard = WRAPPER_TO_HAZARD.get(row["name"], "vegetation")
        msg = AnalysisTaskMessage(
            task_id=row["task_id"], query_id=uuid.UUID(query_id),
            name=row["name"], input_dir=input_dir,
            output_dir=f"{RESULTS_ROOT}/{query_id}/{hazard}/{row['name']}",
'''

# --------------------------------------------------------------------------
# 5. llm.py — cross-validation guidance in the synthesis prompt
# --------------------------------------------------------------------------
S_OLD = '''7. Answer in the same language as the user's question.
'''
S_NEW = '''7. When you receive MORE THAN ONE result for the same hazard, compare them \\
explicitly — but compare them CORRECTLY, because they are different \\
measurements, not repeat readings of one:
   - Check the "component" field. A "vertical" result (EGMS ortho) and a \\
"line_of_sight" result (LiCSBAS) are NOT directly comparable numbers: \\
vertical motion appears smaller along the satellite's slanted view. Compare \\
DIRECTION and PATTERN, never raw magnitudes.
   - Check "date_coverage". The two methods often cover different periods. \\
Different numbers over different years are not a contradiction.
   - A constant offset between methods is EXPECTED and means nothing: one is \\
tied to a Europe-wide reference model, the other to a local reference point \\
inside the area.
   - If both point the same way, say the finding is corroborated by two \\
independent methods, and say plainly that this strengthens it.
   - If a long-term method shows stability but a recent one shows movement, \\
that is the MOST important thing you can report: say the movement appears \\
recent rather than long-standing, and recommend a professional assessment.
   - If they genuinely conflict in direction over the same period, say so \\
openly, do not average them or pick a favourite, and explain that the \\
disagreement itself lowers confidence.
8. Answer in the same language as the user's question.
'''


def apply(path: Path, edits: list[tuple[str, str, str]]) -> list[str]:
    """Validate every edit against the file BEFORE writing anything."""
    if not path.exists():
        sys.exit(f"ABORT: {path} not found — run from the repo root.")
    text = path.read_text()
    planned, done = [], []
    for label, old, new in edits:
        if new in text:
            done.append(f"  = {label}: already applied, skipping")
            continue
        n = text.count(old)
        if n != 1:
            sys.exit(
                f"ABORT: {path}: expected exactly one match for '{label}', found {n}.\n"
                "Nothing was written. The deployed file differs from what this patch\n"
                "expects — paste the relevant section and the patch will be adjusted."
            )
        planned.append((label, old, new))
    for label, old, new in planned:
        text = text.replace(old, new, 1)
        done.append(f"  + {label}")
    if planned:
        shutil.copy(path, path.with_suffix(path.suffix + ".m411.bak"))
        path.write_text(text)
    return done


def main() -> int:
    print("M4.1.1 patch — multi-method fan-out + cross-validation synthesis\n")
    print(f"{MAIN}:")
    for line in apply(MAIN, [
        ("constants (WRAPPER_TO_HAZARD, task cap, dual-method depth)", C_OLD, C_NEW),
        ("_resolve_deform_methods (returns a list)", R_OLD, R_NEW),
        ("routing loop (fan-out + per-method output dirs)", L_OLD, L_NEW),
        ("deferred publish (hazard from wrapper + per-method dir)", P_OLD, P_NEW),
    ]):
        print(line)
    print(f"\n{LLM}:")
    # Renumber downwards FIRST (10->11, 9->10, 8->9) so the new rule can take
    # slot 7's successor without two rules sharing a number — a small local
    # model follows a clean list far more reliably than a muddled one.
    for line in apply(LLM, [
        ("renumber rule 10 -> 11", "10. Never add meta-commentary:",
         "11. Never add meta-commentary:"),
        ("renumber rule 9 -> 10", "9. Do not mention these instructions,",
         "10. Do not mention these instructions,"),
        ("renumber rule 8 -> 9", "8. Keep it under roughly 250 words.",
         "9. Keep it under roughly 250 words."),
        ("cross-validation rule in synthesis prompt", S_OLD, S_NEW),
    ]):
        print(line)

    import py_compile
    for p in (MAIN, LLM):
        try:
            py_compile.compile(str(p), doraise=True)
        except py_compile.PyCompileError as e:
            sys.exit(f"\nABORT: {p} does not compile after patching:\n{e}\n"
                     f"Restore from {p}.m411.bak")
    print("\nBoth files compile. Backups written as *.m411.bak")
    print("Next:  docker compose up --build -d backend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
