#!/usr/bin/env python3
"""wrap_licsbas.py — InSAR deformation via LiCSAR + LiCSBAS (§5.4).

Self-downloading wrapper (architecture decision, this session): unlike the
optical path, InSAR does its own data acquisition through LiCSBAS step 01,
so it does NOT go through the downloader service. The download/analysis split
applies to the S2/optical path only (§5.3 amendment).

Two modes:
  --check-coverage   fast, requests-only: is the frame live + does it have
                     interferograms in the window? (M3.1)
  (default)          full LiCSBAS run -> result.json (M3.2)

Frame resolution: the AOI->frame lookup is not solved client-side (the
authoritative geometry is COMET's server-side LiCSInfo DB — see BACKLOG).
For now the frame ID is passed in params["frame_id"], configured per test
AOI from a one-time portal lookup. Automated resolution is a backlog item.

URL structure verified from comet-licsar/LiCSBAS LiCSBAS01_get_geotiff.py.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date

FRAME_RE = re.compile(r"^\d{3}[AD]_\d{5}_\d{6}$")
PAIR_RE = re.compile(r"(\d{8})_(\d{8})")
LICSAR_ROOTS = [
    "https://gws-access.jasmin.ac.uk/public/nceo_geohazards/LiCSAR_products",
    "https://gws-access.jasmin.ac.uk/public/nceo_geohazards/LiCSAR_products.future",
]
PROBE_TIMEOUT = 20


def progress(pct: int, msg: str) -> None:
    print(f"PROGRESS {pct} {msg}", flush=True)


def track_of(frame: str) -> str:
    return str(int(frame[0:3]))


# ---------------------------------------------------------------- coverage check
def check_coverage(frame: str, start: int, end: int) -> dict:
    """Return {live, root, ifg_in_range, epoch_span, ...}. requests-only."""
    import requests

    if not FRAME_RE.match(frame):
        return {"live": False, "error": f"invalid frame id '{frame}'"}

    track = track_of(frame)
    best: dict = {"live": False, "frame": frame, "track": track}
    for root in LICSAR_ROOTS:
        base = f"{root}/{track}/{frame}"
        try:
            meta = requests.get(f"{base}/metadata/metadata.txt", timeout=PROBE_TIMEOUT)
            meta_ok = meta.status_code == 200 and "master" in meta.text.lower()
            ifg = requests.get(f"{base}/interferograms/", timeout=PROBE_TIMEOUT)
            pairs = set(PAIR_RE.findall(ifg.text)) if ifg.status_code == 200 else set()
            in_range = [
                (a, b) for (a, b) in pairs
                if start <= int(a) <= end and start <= int(b) <= end
            ]
            if meta_ok and in_range:
                epochs = sorted({e for p in in_range for e in p})
                return {
                    "live": True, "frame": frame, "track": track, "root": root,
                    "base_url": base, "ifg_total": len(pairs),
                    "ifg_in_range": len(in_range),
                    "epoch_span": f"{epochs[0]}..{epochs[-1]}",
                    "epoch_count": len(epochs),
                }
            best = {"live": False, "frame": frame, "track": track,
                    "ifg_total": len(pairs), "ifg_in_range": len(in_range),
                    "last_root": root}
        except Exception as e:  # noqa: BLE001
            best = {"live": False, "frame": frame, "error": f"{type(e).__name__}: {e}"}
    return best


# ---------------------------------------------------------------- full run (M3.2)
import glob
import shutil
from datetime import timedelta

HOTSPOT_MM_YR = 10.0          # |LOS velocity| above this = "hotspot" pixel
TREND_MM_YR = 2.0             # mean |velocity| below this = stable
DEFORM_LOOKBACK_MONTHS = 24   # §6.1 default when dates are null


def _aoi_bbox(aoi_geojson: dict) -> tuple[float, float, float, float]:
    ring = aoi_geojson["coordinates"][0]
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return min(lons), max(lons), min(lats), max(lats)


def _yyyymmdd(d: str | None, fallback) -> str:
    if not d or d == "None":
        return fallback
    return d.replace("-", "")


def _configure_batch(batch_src: str, batch_dst: str, *, nlook: int,
                     clip_geo: str, start: str, end: str) -> None:
    """Copy batch_LiCSBAS.sh and set the handful of vars we drive."""
    import re as _re
    text = open(batch_src).read()
    subs = {
        r'^start_step=.*$': 'start_step="01"',
        r'^end_step=.*$': 'end_step="16"',
        r'^nlook=.*$': f'nlook="{nlook}"',
        r'^do05op_clip=.*$': 'do05op_clip="y"',
        r'^p05_clip_range_geo=.*$': f'p05_clip_range_geo="{clip_geo}"',
        r'^p01_start_date=.*$': f'p01_start_date="{start}"',
        r'^p01_end_date=.*$': f'p01_end_date="{end}"',
        r'^p01_get_gacos=.*$': 'p01_get_gacos="n"',
    }
    for pat, repl in subs.items():
        text, n = _re.subn(pat, repl, text, count=1, flags=_re.MULTILINE)
        if n == 0:
            print(f"WARN batch var not found for pattern {pat}", file=sys.stderr)
    open(batch_dst, "w").write(text)


# Map LiCSBAS step numbers seen on stdout to a coarse progress bar.
_STEP_PCT = {"01": 10, "02": 35, "03": 40, "04": 45, "05": 50,
             "11": 60, "12": 70, "13": 80, "15": 90, "16": 95}
_STEP_RE = re.compile(r"LiCSBAS(\d{2})\w*\.py")


def _run_batch(workdir: str, emit_line) -> int:
    """Run batch_LiCSBAS.sh in workdir, relaying step progress. Returns rc."""
    env = dict(os.environ)
    # ensure the licsbas env bin (this interpreter's dir) leads PATH so the
    # batch's child LiCSBAS*.py use the right python/gdal.
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    proc = subprocess.Popen(
        ["bash", "batch_LiCSBAS.sh"], cwd=workdir,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
    )
    for line in proc.stdout:
        line = line.rstrip()
        m = _STEP_RE.search(line)
        if m and m.group(1) in _STEP_PCT:
            emit_line(_STEP_PCT[m.group(1)], f"LiCSBAS step {m.group(1)}")
        if line:
            print(f"[licsbas] {line[:200]}", flush=True)
    proc.wait()
    return proc.returncode


def _iso_date(yyyymmdd) -> str:
    """LiCSBAS imdates are ints like 20250101 -> ISO 'YYYY-MM-DD' for §6.3."""
    d = str(int(yyyymmdd))
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def _read_results(workdir: str) -> dict:
    """Parse TS_*/cum.h5 + results/ into deformation stats + quality.
    Uses licsbas env deps (h5py, numpy) and LiCSBAS_io_lib.read_img."""
    import h5py
    import numpy as np
    sys.path.insert(0, os.path.join(os.environ.get("LiCSBAS", "/opt/LiCSBAS"), "LiCSBAS_lib"))
    import LiCSBAS_io_lib as io

    cum_paths = glob.glob(os.path.join(workdir, "TS_*", "cum.h5"))
    if not cum_paths:
        raise RuntimeError("no TS_*/cum.h5 produced — LiCSBAS did not finish inversion")
    tsdir = os.path.dirname(cum_paths[0])
    resultsdir = os.path.join(tsdir, "results")

    with h5py.File(cum_paths[0], "r") as h5:
        vel = h5["vel"][()].astype(np.float64)              # (length, width) mm/yr
        imdates = [int(x) for x in h5["imdates"][()]]
    length, width = vel.shape

    def _res(name):
        fp = os.path.join(resultsdir, name)
        if not os.path.exists(fp):
            return None
        return io.read_img(fp, length, width).astype(np.float64)

    coh = _res("coh_avg")
    mask = _res("mask")
    vstd = _res("vstd")   # per-pixel velocity std (mm/yr) from the inversion

    if mask is not None:
        valid = (mask == 1) & np.isfinite(vel)
    else:
        valid = np.isfinite(vel)
    n_total = int(vel.size)
    n_valid = int(valid.sum())
    if n_valid == 0:
        raise RuntimeError("no valid (unmasked, coherent) pixels in AOI after inversion")

    vv = vel[valid]
    coh_mean = float(np.nanmean(coh[valid])) if coh is not None else None
    mean_v = float(np.mean(vv))

    # Statistical significance: a pixel's velocity is meaningful only if it
    # exceeds its own uncertainty (|v| > 1.96*vstd ~ 95%). Without this, a
    # noisy short series shows huge min/max that are pure scatter (M3.2 finding).
    if vstd is not None:
        vsd = vstd[valid]
        significant = np.abs(vv) > 1.96 * np.where(vsd > 0, vsd, np.inf)
        sig_fraction = float(significant.sum() / n_valid)
        vstd_mean = float(np.nanmean(vsd[np.isfinite(vsd)])) if np.isfinite(vsd).any() else None
    else:
        significant = np.ones_like(vv, dtype=bool)
        sig_fraction = None
        vstd_mean = None

    # Robust range: p5/p95 instead of raw min/max, so single noisy pixels don't
    # define the reported spread.
    p5, p95 = (float(np.percentile(vv, 5)), float(np.percentile(vv, 95)))
    # Hotspots must be BOTH large and statistically significant.
    hotspot_fraction = float(((np.abs(vv) > HOTSPOT_MM_YR) & significant).sum() / n_valid)

    if mean_v < -TREND_MM_YR:
        trend = "subsiding"
    elif mean_v > TREND_MM_YR:
        trend = "uplifting"
    else:
        trend = "stable"

    masked_fraction = round(1.0 - n_valid / n_total, 4)
    stats = {
        "velocity_mm_yr_mean_aoi": round(mean_v, 2),
        "velocity_mm_yr_p5": round(p5, 2),
        "velocity_mm_yr_p95": round(p95, 2),
        "hotspot_fraction": round(hotspot_fraction, 4),
        "trend": trend,
    }
    if sig_fraction is not None:
        stats["significant_fraction"] = round(sig_fraction, 4)
    if vstd_mean is not None:
        stats["velocity_std_mm_yr_mean"] = round(vstd_mean, 2)

    return {
        "vel": vel, "valid": valid, "imdates": imdates,
        "stats": stats,
        "sig_fraction": sig_fraction,
        "coherence_mean": round(coh_mean, 3) if coh_mean is not None else None,
        "masked_fraction": masked_fraction,
        "scene_count": len(imdates),
        "date_coverage": [_iso_date(imdates[0]), _iso_date(imdates[-1])],
    }


def _render_velocity_png(vel, valid, path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    disp = np.where(valid, vel, np.nan)
    finite = disp[np.isfinite(disp)]
    lim = float(np.percentile(np.abs(finite), 95)) if finite.size else 10.0
    lim = max(lim, 1.0)
    fig, ax = plt.subplots(figsize=(7, 6))
    im = ax.imshow(disp, cmap="RdBu", vmin=-lim, vmax=lim)
    ax.set_title(title, fontsize=10)
    ax.set_xticks([]); ax.set_yticks([])
    cb = fig.colorbar(im, ax=ax, shrink=0.8)
    cb.set_label("LOS velocity (mm/yr)  |  blue = toward satellite")
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)


def run_licsbas(args, params: dict) -> int:
    """Drive batch_LiCSBAS.sh end to end, then parse -> result.json (§6.3)."""
    from datetime import date as _date
    sys.path.insert(0, "/libs/contracts")
    from geohazard_contracts import ResultJson

    aoi = json.load(open(args.aoi))
    lon1, lon2, lat1, lat2 = _aoi_bbox(aoi)
    clip_geo = f"{lon1:.4f}/{lon2:.4f}/{lat1:.4f}/{lat2:.4f}"

    ds, de = (args.dates.split(",") + [None, None])[:2] if args.dates else (None, None)
    today = _date.today()
    end = _yyyymmdd(de, today.strftime("%Y%m%d"))
    default_start = (today - timedelta(days=30 * DEFORM_LOOKBACK_MONTHS)).strftime("%Y%m%d")
    start = _yyyymmdd(ds, default_start)
    nlook = int(params.get("nlook", 1))

    # --- coverage pre-check + candidate fallback (M3.4a follow-on) ---------
    # The catalog ranks frames by spatial overlap only; a spatially-perfect
    # frame can be temporally DEAD (processing stopped years ago -> 0 IFGs in
    # window), which crashes LiCSBAS01 with an empty imdates list. So before
    # launching a doomed batch, probe each candidate for IFGs in-window (reuses
    # the M3.1 check_coverage) and use the first that has data. If none do, emit
    # a clean no-data result (exit 0 -> no wasteful 3x retry).
    candidates = params.get("candidate_frames") or (
        [args.frame or params.get("frame_id")] if (args.frame or params.get("frame_id")) else [])
    candidates = [c for c in candidates if c]
    if not candidates:
        print("ERROR no candidate frames provided", file=sys.stderr)
        return 2

    frame = None
    probed = []
    for cand in candidates:
        cov = check_coverage(cand, int(start), int(end))
        probed.append((cand, cov.get("ifg_in_range", 0)))
        if cov.get("live") and cov.get("ifg_in_range", 0) > 0:
            frame = cand
            progress(4, f"selected frame {cand} ({cov['ifg_in_range']} interferograms in window)")
            break
        print(f"[wrap_licsbas] candidate {cand}: no data in {start}..{end}, skipping",
              flush=True)

    if frame is None:
        # No candidate has interferograms in-window: honest no-data result.
        summary = ", ".join(f"{c}:{n}" for c, n in probed)
        print(f"[wrap_licsbas] no candidate frame has data in window ({summary})", flush=True)
        os.makedirs(args.output_dir, exist_ok=True)
        result = ResultJson.model_validate({
            "query_id": args.query_id,
            "method": "licsbas",
            "status": "failed",
            "summary_stats": {},
            "quality": {
                "scene_count": 0, "date_coverage": [_iso_date(start), _iso_date(end)],
                "coherence_mean": None, "masked_fraction": None,
                "cloud_fraction": None, "confidence": "low",
                "caveats": [
                    "No InSAR interferograms are available for this area in the "
                    f"requested period ({_iso_date(start)} to {_iso_date(end)}). "
                    f"Checked {len(candidates)} candidate frame(s); their most "
                    "recent processed data predates the window, or this area lacks "
                    "recent LiCSAR coverage.",
                    "Try a different (earlier) date range, or this location may not "
                    "have up-to-date ground-motion data in the LiCSAR archive.",
                ],
            },
            "artifacts": [],
            "attribution": [
                "LiCSAR contains modified Copernicus Sentinel data analysed by COMET.",
            ],
        })
        with open(os.path.join(args.output_dir, "result.json"), "w") as f:
            f.write(result.model_dump_json(indent=2))
        progress(100, "no InSAR data available for this area/period")
        return 0

    # Workdir MUST be named the frame ID (LiCSBAS01 reads frame from CWD name).
    workroot = params.get("licsbas_workroot", "/data/scratch/licsbas")
    workdir = os.path.join(workroot, str(args.query_id or "adhoc"), frame)
    os.makedirs(workdir, exist_ok=True)
    os.makedirs(args.output_dir, exist_ok=True)

    progress(3, f"preparing LiCSBAS run for frame {frame} ({start}..{end}, clip {clip_geo})")
    licsbas_home = os.environ.get("LiCSBAS", "/opt/LiCSBAS")
    _configure_batch(os.path.join(licsbas_home, "batch_LiCSBAS.sh"),
                     os.path.join(workdir, "batch_LiCSBAS.sh"),
                     nlook=nlook, clip_geo=clip_geo, start=start, end=end)

    progress(5, "starting LiCSBAS pipeline (download + inversion — this is slow)")
    rc = _run_batch(workdir, lambda pct, msg: progress(pct, msg))
    if rc != 0:
        print(f"ERROR batch_LiCSBAS.sh exited {rc}", file=sys.stderr)
        return 4

    progress(96, "parsing velocity field")
    R = _read_results(workdir)

    png = os.path.join(args.output_dir, "velocity_map.png")
    _render_velocity_png(
        R["vel"], R["valid"], png,
        f"LOS velocity {R['date_coverage'][0]}–{R['date_coverage'][1]} (frame {frame})",
    )

    caveats = [
        "LOS velocity is measured along the satellite line of sight, not purely "
        "vertical; a single track cannot separate vertical from horizontal motion.",
        "Velocities are relative to an automatically chosen reference area, not an "
        "absolute geodetic datum.",
    ]

    def _d(v):
        v = str(v)
        return _date(int(v[:4]), int(v[4:6]), int(v[6:8]))
    sep_days = (_d(R["imdates"][-1]) - _d(R["imdates"][0])).days

    cm = R["coherence_mean"]
    mf = R["masked_fraction"]
    sig = R.get("sig_fraction")

    # Two independent axes (§ confidence): DATA QUALITY (coherence, masking,
    # scene count) vs METHODOLOGICAL CERTAINTY (time span, velocity
    # significance). Confidence is the weaker of the two; caveats name which.
    data_ok = cm is not None and cm > 0.6 and mf < 0.3 and R["scene_count"] >= 15
    data_fair = mf < 0.6 and R["scene_count"] >= 8

    # Prescriptive, hazard-aware window guidance (deterministic).
    short_window = sep_days < 365
    if short_window:
        caveats.append(
            f"The analysed time span is only {sep_days} days. Ground-motion "
            "velocities need at least ~1 year (ideally 2+) to be reliable; over a "
            "short span the estimates are dominated by noise. Re-run with a longer "
            "date range, or leave the dates blank to use the 2-year default.")

    # Velocity-dispersion / significance: if few pixels are statistically
    # distinguishable from zero, the individual extremes are noise even when the
    # area-average is meaningful (M3.2 finding: -14/+30 spread around ~0 mean).
    mostly_noise = sig is not None and sig < 0.10
    if mostly_noise:
        caveats.append(
            "Most pixels show no motion that is statistically distinguishable from "
            "measurement noise; individual high/low values in this area are not "
            "reliable, though the area-average trend may still be informative.")
    elif sig is not None and sig < 0.30:
        caveats.append(
            "Only a minority of pixels show statistically significant motion; treat "
            "the extreme values with caution and rely on the area-average.")

    if short_window or mostly_noise:
        confidence = "low"
    elif data_ok and (sig is None or sig >= 0.30):
        confidence = "high"
    elif data_fair:
        confidence = "moderate"
    else:
        confidence = "low"

    result = ResultJson.model_validate({
        "query_id": args.query_id,
        "method": "licsbas",
        "status": "ok",
        "summary_stats": {"deformation": R["stats"]},
        "quality": {
            "scene_count": R["scene_count"],
            "date_coverage": R["date_coverage"],
            "coherence_mean": cm,
            "masked_fraction": mf,
            "cloud_fraction": None,
            "confidence": confidence,
            "caveats": caveats,
        },
        "artifacts": [{"type": "map_png", "path": "velocity_map.png",
                       "caption": f"InSAR LOS velocity, frame {frame} "
                                  f"({R['date_coverage'][0]}–{R['date_coverage'][1]})"}],
        "attribution": [
            "LiCSAR contains modified Copernicus Sentinel data analysed by the "
            "Centre for the Observation and Modelling of Earthquakes, Volcanoes and "
            "Tectonics (COMET). LiCSAR uses JASMIN, the UK's collaborative data "
            "analysis environment (http://jasmin.ac.uk).",
        ],
    })
    with open(os.path.join(args.output_dir, "result.json"), "w") as f:
        f.write(result.model_dump_json(indent=2))
    progress(100, "done")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query-id")
    ap.add_argument("--aoi", help="path to GeoJSON polygon file")
    ap.add_argument("--dates", help="start,end (yyyy-mm-dd,yyyy-mm-dd or None,None)")
    ap.add_argument("--input-dir")
    ap.add_argument("--output-dir")
    ap.add_argument("--params", default="{}")
    ap.add_argument("--check-coverage", action="store_true",
                    help="probe LiCSAR frame liveness only; print JSON; exit")
    ap.add_argument("--frame", help="frame id (overrides params.frame_id)")
    ap.add_argument("-s", type=int, help="coverage start yyyymmdd")
    ap.add_argument("-e", type=int, help="coverage end yyyymmdd")
    args = ap.parse_args()

    params = json.loads(args.params) if args.params else {}
    frame = args.frame or params.get("frame_id")

    if args.check_coverage:
        if not frame:
            print(json.dumps({"live": False, "error": "no frame id given"}))
            return 2
        start = args.s or 20141001
        end = args.e or int(date.today().strftime("%Y%m%d"))
        result = check_coverage(frame, start, end)
        print(json.dumps(result))
        return 0 if result.get("live") else 1

    if not frame or not FRAME_RE.match(frame):
        print(f"ERROR no valid frame id (got {frame!r}); "
              "AOI->frame resolution is a backlog item, pass params.frame_id",
              file=sys.stderr)
        return 2
    return run_licsbas(args, params)


if __name__ == "__main__":
    sys.exit(main())
