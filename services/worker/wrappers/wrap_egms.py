#!/usr/bin/env python3
"""EGMS analysis wrapper — M4.1b (L3 ORTHO-UP).

Reads the EGMS L3 ortho product downloaded by the egms tier and reports
VERTICAL ground velocity over the AOI.

Real file structure (read off a live tile, not assumed):
    EGMS_L3_E37N28_100km_U_2020_2024_1.csv — 315 columns, ~1M rows, 340 MB
      pid, easting, northing, height_ortho, rmse_ts,
      mean_velocity, mean_velocity_std, acceleration, acceleration_std,
      seasonality, seasonality_std, gnss_velocity_n/e/u,
      then ~301 epoch columns named YYYYMMDD (6-day cadence).

Three consequences that shape this wrapper:

  * Coordinates are EPSG:3035 easting/northing on a regular 100 m grid, NOT
    lon/lat. We project the AOI once into EPSG:3035 and filter there, rather
    than reprojecting a million points.
  * The file is far too large to load whole, so it is read in chunks with only
    the four columns we need.
  * mean_velocity_std lets us apply the same significance test the LiCSBAS path
    uses (|v| > 1.96 sigma), so "significant" means the same thing across
    methods.

L3 ORTHO-UP is the vertical (up-down) component, so unlike the LOS products this
path does NOT need the "cannot separate vertical from horizontal" caveat — the
number genuinely is vertical motion. East-west is a separate product
(ORTHO-EAST) and is not fetched here.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import sys
from datetime import date as _date

HOTSPOT_MM_YR = 10.0     # |v| above this counts as a hotspot point
SIG_SIGMA = 1.96         # 95% two-sided significance on velocity

# Systematic accuracy floor for EGMS velocities, in mm/yr.
#
# WHY THIS EXISTS. The CSV's mean_velocity_std is the FORMAL uncertainty of the
# linear fit. With ~300 epochs over 5 years that value is tiny — in a real tile
# it is quantised to one decimal and its median is literally 0.0. Testing
# |v| > 1.96*std against it marks essentially every point "significant", which
# is true of the fit and meaningless about the ground: it ignores the systematic
# errors (atmosphere, unwrapping, reference-frame) that actually dominate.
#
# Published validation of EGMS against levelling/GNSS finds agreement with
# ground truth typically within 1-2 mm/yr, but explicitly NOT universally —
# discrepancies of 5-10 mm/yr occur at some sites. So we combine the formal
# error with a floor representing that systematic component, and treat
# velocities below it as not resolvable. 1.5 mm/yr is a middle-of-the-road
# choice; raise it to be more conservative.
EGMS_ACCURACY_MM_YR = float(os.environ.get("EGMS_ACCURACY_MM_YR", "1.5"))


def progress(pct: int, msg: str) -> None:
    print(f"PROGRESS {pct} {msg}", flush=True)


# Column-name candidates. L3 uses easting/northing + mean_velocity; the L2
# products (lat/lon based) are kept as a fallback so this wrapper still works if
# EGMS_LEVEL is switched to L2A/L2B.
_VEL_CANDIDATES = ["mean_velocity", "velocity", "vel", "mean_vel"]
_VELSTD_CANDIDATES = ["mean_velocity_std", "velocity_std", "vel_std"]
_EAST_CANDIDATES = ["easting", "x"]
_NORTH_CANDIDATES = ["northing", "y"]
_LON_CANDIDATES = ["longitude", "lon"]
_LAT_CANDIDATES = ["latitude", "lat"]

_EPOCH_RE = re.compile(r"^(20\d{2})(\d{2})(\d{2})$")
_EGMS_ERA_START = "2015-01-01"
EGMS_CRS = "EPSG:3035"


def _pick_col(columns, candidates):
    lower = {str(c).lower(): c for c in columns}
    for cand in candidates:
        if cand in lower:
            return lower[cand]
    return None


def _epochs_from_keys(keys) -> list:
    out = []
    for k in keys:
        m = _EPOCH_RE.match(str(k))
        if m:
            out.append(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
    return out


def _valid_iso(s):
    try:
        return _date.fromisoformat(str(s)).isoformat()
    except Exception:  # noqa: BLE001
        return None


def _safe_date_pair(epoch_span, query_dates):
    """Always returns two valid ISO dates: real epoch span > query dates > era."""
    if epoch_span and _valid_iso(epoch_span[0]) and _valid_iso(epoch_span[1]):
        return epoch_span[0], epoch_span[1], False
    if query_dates:
        s, e = _valid_iso(query_dates[0]), _valid_iso(query_dates[1])
        if s and e:
            return s, e, False
    return _EGMS_ERA_START, _date.today().isoformat(), True


def _find_csvs(input_dir: str) -> list:
    files = []
    for ext in ("*.csv", "*.CSV"):
        files.extend(glob.glob(os.path.join(input_dir, "**", ext), recursive=True))
    return sorted(set(files))


def _aoi_ring_lonlat(aoi_geojson: dict) -> list:
    return aoi_geojson["coordinates"][0]


def _aoi_to_egms_crs(aoi_geojson: dict):
    """Project the AOI ring into EPSG:3035 once.

    Returns (ring_xy, (xmin, xmax, ymin, ymax)), or (None, None) if pyproj is
    unavailable — in which case the caller must not pretend it subset anything.
    """
    try:
        from pyproj import Transformer
    except ImportError:
        return None, None
    ring = _aoi_ring_lonlat(aoi_geojson)
    tf = Transformer.from_crs("EPSG:4326", EGMS_CRS, always_xy=True)
    xs, ys = tf.transform([p[0] for p in ring], [p[1] for p in ring])
    ring_xy = list(zip(xs, ys))
    return ring_xy, (min(xs), max(xs), min(ys), max(ys))


def _contains_mask(ring_xy, x, y):
    """Point-in-polygon using matplotlib.path (already a worker dependency)."""
    import numpy as np
    try:
        from matplotlib.path import Path as MplPath
        pth = MplPath([(float(a), float(b)) for a, b in ring_xy])
        return pth.contains_points(np.column_stack([x, y]))
    except Exception:  # noqa: BLE001
        return np.ones(len(x), dtype=bool)


def _load_points_l3(input_dir: str, aoi_geojson: dict):
    """Stream the EGMS CSV(s), keeping only points inside the AOI.

    Returns (DataFrame[x, y, vel, vel_std] | None, epoch_span | None, mode)
    where mode is 'projected' (EPSG:3035) or 'lonlat'.
    """
    import pandas as pd

    files = _find_csvs(input_dir)
    if not files:
        return None, None, "none"

    ring_xy, bounds = _aoi_to_egms_crs(aoi_geojson)
    ring_ll = _aoi_ring_lonlat(aoi_geojson)
    lons = [p[0] for p in ring_ll]
    lats = [p[1] for p in ring_ll]

    frames = []
    epochs: list = []
    mode = "none"

    for fp in files:
        try:
            head = pd.read_csv(fp, nrows=0)
        except Exception as e:  # noqa: BLE001
            print(f"[egms] cannot read header of {os.path.basename(fp)}: {e}",
                  file=sys.stderr)
            continue
        cols = list(head.columns)
        epochs.extend(_epochs_from_keys(cols))

        velcol = _pick_col(cols, _VEL_CANDIDATES)
        stdcol = _pick_col(cols, _VELSTD_CANDIDATES)
        eastcol = _pick_col(cols, _EAST_CANDIDATES)
        northcol = _pick_col(cols, _NORTH_CANDIDATES)
        loncol = _pick_col(cols, _LON_CANDIDATES)
        latcol = _pick_col(cols, _LAT_CANDIDATES)

        if velcol is None:
            print(f"[egms] no velocity column in {os.path.basename(fp)}; skipping",
                  file=sys.stderr)
            continue

        if eastcol and northcol and ring_xy is not None:
            xcol, ycol, this_mode = eastcol, northcol, "projected"
            xmin, xmax, ymin, ymax = bounds
        elif loncol and latcol:
            xcol, ycol, this_mode = loncol, latcol, "lonlat"
            xmin, xmax, ymin, ymax = min(lons), max(lons), min(lats), max(lats)
        elif eastcol and northcol:
            # Projected data but no pyproj: we cannot locate the AOI. Refuse
            # rather than silently returning a whole 100 km tile as "the AOI".
            print("[egms] projected coordinates but pyproj is unavailable; "
                  "cannot subset to AOI", file=sys.stderr)
            continue
        else:
            print(f"[egms] no usable coordinate columns in {os.path.basename(fp)}",
                  file=sys.stderr)
            continue

        usecols = [c for c in (xcol, ycol, velcol, stdcol) if c]
        kept = 0
        try:
            for chunk in pd.read_csv(fp, usecols=usecols, chunksize=200_000):
                sub = chunk[(chunk[xcol] >= xmin) & (chunk[xcol] <= xmax)
                            & (chunk[ycol] >= ymin) & (chunk[ycol] <= ymax)]
                if sub.empty:
                    continue
                ren = {xcol: "x", ycol: "y", velcol: "vel"}
                if stdcol:
                    ren[stdcol] = "vel_std"
                sub = sub.rename(columns=ren)
                if "vel_std" not in sub.columns:
                    sub = sub.assign(vel_std=float("nan"))
                frames.append(sub[["x", "y", "vel", "vel_std"]])
                kept += len(sub)
        except Exception as e:  # noqa: BLE001
            print(f"[egms] error streaming {os.path.basename(fp)}: {e}",
                  file=sys.stderr)
            continue
        mode = this_mode
        print(f"[egms] {os.path.basename(fp)}: {kept} points within AOI bbox",
              flush=True)

    span = (min(epochs), max(epochs)) if epochs else None
    if not frames:
        return None, span, mode

    df = frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True)
    df = df.dropna(subset=["x", "y", "vel"])

    # Precise polygon test — the bbox filter above is only a cheap prefilter.
    if not df.empty:
        ring = ring_xy if mode == "projected" else list(zip(lons, lats))
        m = _contains_mask(ring, df["x"].to_numpy(), df["y"].to_numpy())
        df = df[m]

    return df, span, mode


def _aoi_km2(aoi_geojson: dict) -> float:
    ring = _aoi_ring_lonlat(aoi_geojson)
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    midlat = math.radians((min(lats) + max(lats)) / 2)
    return (abs((max(lons) - min(lons)) * math.cos(midlat) * 111.0)
            * abs((max(lats) - min(lats)) * 111.0))


def _render_points_png(df, path, title, mode):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    x = df["x"].to_numpy(dtype=float)
    y = df["y"].to_numpy(dtype=float)
    xlabel, ylabel = "longitude", "latitude"
    if mode == "projected":
        # Reproject only the AOI subset back to lon/lat so the map is readable.
        try:
            from pyproj import Transformer
            tf = Transformer.from_crs(EGMS_CRS, "EPSG:4326", always_xy=True)
            x, y = tf.transform(x, y)
        except Exception:  # noqa: BLE001
            xlabel, ylabel = "easting (m, EPSG:3035)", "northing (m, EPSG:3035)"

    v = df["vel"].to_numpy(dtype=float)
    lim = float(np.percentile(np.abs(v), 95)) if v.size else 10.0
    lim = max(lim, 1.0)
    fig, ax = plt.subplots(figsize=(7, 6))
    sc = ax.scatter(x, y, c=v, cmap="RdBu", vmin=-lim, vmax=lim, s=6)
    ax.set_title(title, fontsize=10)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    cb = fig.colorbar(sc, ax=ax, shrink=0.8)
    cb.set_label("EGMS vertical velocity (mm/yr)  |  blue = uplift, red = subsidence")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def _no_data_result(args, date_pair, reason: str) -> int:
    sys.path.insert(0, "/libs/contracts")
    from geohazard_contracts import ResultJson

    result = ResultJson.model_validate({
        "query_id": args.query_id,
        "method": "egms",
        "status": "failed",
        "summary_stats": {},
        "quality": {
            "scene_count": 0,
            "date_coverage": [date_pair[0], date_pair[1]],
            "coherence_mean": None,
            "masked_fraction": None,
            "cloud_fraction": None,
            "confidence": "low",
            "caveats": [reason],
        },
        "artifacts": [],
        "attribution": [
            "Contains modified Copernicus Sentinel data; "
            "European Ground Motion Service (EGMS), Copernicus Land Monitoring Service.",
        ],
    })
    with open(os.path.join(args.output_dir, "result.json"), "w") as f:
        f.write(result.model_dump_json(indent=2))
    progress(100, "no EGMS data for this area")
    return 0


def run_egms(args, params: dict) -> int:
    import numpy as np
    sys.path.insert(0, "/libs/contracts")
    from geohazard_contracts import ResultJson

    os.makedirs(args.output_dir, exist_ok=True)
    ds, de = (args.dates.split(",") + [None, None])[:2] if args.dates else (None, None)
    aoi = json.load(open(args.aoi))

    progress(5, "reading EGMS tile (large file, streaming)")
    df, epoch_span, mode = _load_points_l3(args.input_dir, aoi)
    date_start, date_end, date_fallback = _safe_date_pair(epoch_span, (ds, de))
    dates = (date_start, date_end)

    if df is None or df.empty:
        return _no_data_result(
            args, dates,
            "No EGMS measurement points fall inside this area. EGMS measures "
            "stable-scattering surfaces such as buildings and bare rock; fields, "
            "forests and water often carry no measurement points.")

    n = len(df)
    progress(60, f"computing statistics over {n} points")
    v = df["vel"].to_numpy(dtype=float)
    vstd = df["vel_std"].to_numpy(dtype=float)

    mean_v = float(np.mean(v))
    p5, p95 = float(np.percentile(v, 5)), float(np.percentile(v, 95))
    spread = p95 - p5

    # Effective uncertainty = formal fit error combined in quadrature with the
    # systematic accuracy floor. The floor dominates whenever the formal error
    # is degenerate, which for EGMS L3 is most of the time.
    fit_std = np.nan_to_num(vstd, nan=0.0)
    sigma_eff = np.sqrt(fit_std ** 2 + EGMS_ACCURACY_MM_YR ** 2)
    sig = np.abs(v) > (SIG_SIGMA * sigma_eff)
    significant_fraction = float(sig.sum() / n)
    fit_std_mean = float(np.nanmean(vstd)) if np.isfinite(vstd).any() else None
    # Flag when the published uncertainty carries essentially no information,
    # so the answer can say so rather than quietly leaning on the floor.
    degenerate_std = bool(np.nanmedian(fit_std) <= 0.0)

    hotspot_fraction = float(((np.abs(v) > HOTSPOT_MM_YR) & sig).sum() / n)

    # A trend is only claimed when the AOI mean exceeds what the service can
    # resolve. Previously this used a fixed 1 mm/yr threshold, which labelled a
    # -1.04 mm/yr Paris average "subsiding" — a direction taken from inside the
    # noise floor.
    #
    # "stable" and "mixed" must not be conflated: an area where points move
    # +10 and -10 mm/yr averages to zero but is not stable at all, and reporting
    # that as stable would hide exactly the differential movement a user cares
    # about (a building settling unevenly, a slope creeping one way).
    wide_spread = spread > (4.0 * EGMS_ACCURACY_MM_YR)
    if abs(mean_v) <= EGMS_ACCURACY_MM_YR:
        trend = "mixed" if (wide_spread and significant_fraction >= 0.30) else "stable"
    elif mean_v < 0:
        trend = "subsiding"
    else:
        trend = "uplifting"

    component = "vertical" if mode == "projected" else "line_of_sight"
    stats = {
        "velocity_mm_yr_mean_aoi": round(mean_v, 2),
        "velocity_mm_yr_p5": round(p5, 2),
        "velocity_mm_yr_p95": round(p95, 2),
        "hotspot_fraction": round(hotspot_fraction, 4),
        "point_count": int(n),
        "trend": trend,
        "component": component,
        "significant_fraction": round(significant_fraction, 4),
        "accuracy_floor_mm_yr": EGMS_ACCURACY_MM_YR,
    }
    if fit_std_mean is not None:
        # Named to make clear this is the fit's formal error, NOT overall accuracy.
        stats["velocity_fit_std_mm_yr_mean"] = round(fit_std_mean, 2)

    progress(80, "rendering velocity map")
    png = os.path.join(args.output_dir, "velocity_points.png")
    _render_points_png(df, png,
                       f"EGMS vertical velocity ({dates[0]}–{dates[1]})", mode)

    # Confidence. EGMS L3 is a regular 100 m grid, so full coverage is about
    # 100 points/km^2 — thresholds are set against that real ceiling.
    km2 = _aoi_km2(aoi)
    density = n / km2 if km2 > 0 else 0.0
    if density >= 40 and n >= 500:
        data_quality = "high"
    elif density >= 8 and n >= 50:
        data_quality = "moderate"
    else:
        data_quality = "low"

    # A well-sampled area where nothing exceeds the accuracy floor is a
    # CONFIDENT finding of stability, not a failure to measure — those two must
    # not collapse into the same "low confidence". What genuinely warrants low
    # confidence is scatter we cannot resolve: velocities spread wider than the
    # accuracy floor while none of them are individually significant.
    resolvable = significant_fraction >= 0.30
    tight = spread <= (2.0 * EGMS_ACCURACY_MM_YR)
    if resolvable or tight:
        confidence = data_quality
    else:
        confidence = "moderate" if data_quality == "high" else "low"

    caveats = [
        "EGMS is a multi-year average updated annually: it describes long-term "
        "motion, not recent or short-term change. For 'what happened lately', an "
        "InSAR time-series over a recent window is more appropriate.",
        "EGMS velocities are tied to a GNSS-calibrated European reference frame; "
        "values are relative to that datum rather than to a local benchmark.",
    ]
    if component == "vertical":
        caveats.append(
            "This is the vertical (up-down) component of motion. Horizontal "
            "movement is not included here; EGMS publishes it separately.")
    else:
        caveats.append(
            "Velocity is measured along the satellite line of sight, not purely "
            "vertical; a single geometry cannot separate vertical from horizontal "
            "motion.")

    # The single most important honesty statement: how much of this is resolvable.
    if not resolvable and tight:
        caveats.append(
            f"All velocities here fall within about ±{EGMS_ACCURACY_MM_YR:g} mm/yr, "
            "which is the level at which EGMS agrees with ground-truth surveys. "
            "The area is best described as stable: any real movement is at or "
            "below what this service can resolve.")
    elif not resolvable:
        caveats.append(
            f"No point moves by more than the service's resolving limit "
            f"(~{EGMS_ACCURACY_MM_YR:g} mm/yr), yet velocities are scattered across "
            f"{spread:.1f} mm/yr, so this reads as measurement scatter rather than "
            "a ground-motion signal.")
    if trend == "mixed":
        caveats.append(
            f"Points here move in BOTH directions — from {p5:.1f} to {p95:.1f} mm/yr — "
            "so the near-zero average does not mean the ground is uniformly stable. "
            "Differential movement across a small area is often more significant "
            "than a uniform trend, and this pattern warrants a closer look.")
    if trend != "stable" and trend != "mixed" and abs(mean_v) < 2 * EGMS_ACCURACY_MM_YR:
        caveats.append(
            f"The average of {mean_v:.2f} mm/yr is only just above the resolving "
            "limit, so treat the direction as indicative rather than established.")
    if degenerate_std:
        caveats.append(
            "The per-point uncertainty published with this product is the formal "
            "error of the trend fit and is near zero for most points; it does not "
            "capture atmospheric or reference-frame error. Significance here is "
            f"judged against a {EGMS_ACCURACY_MM_YR:g} mm/yr accuracy floor instead. "
            "Independent validation finds EGMS usually agrees with ground truth to "
            "within 1-2 mm/yr, though larger differences occur at some sites.")
    if data_quality == "low":
        caveats.append(
            f"Only {n} measurement points fall inside this area (~{density:.1f}/km²); "
            "vegetation, water and smooth ground yield few EGMS points, which "
            "lowers confidence.")
    if date_fallback:
        caveats.append(
            "The exact EGMS product time span could not be read from the data; the "
            "reported date range approximates the archive era rather than this "
            "product's precise coverage.")

    progress(90, "writing result.json")
    result = ResultJson.model_validate({
        "query_id": args.query_id,
        "method": "egms",
        "status": "ok",
        "summary_stats": {"deformation": stats},
        "quality": {
            "scene_count": int(n),      # measurement points stand in for scenes
            "date_coverage": [dates[0], dates[1]],
            "coherence_mean": None,
            "masked_fraction": None,
            "cloud_fraction": None,
            "confidence": confidence,
            "caveats": caveats,
        },
        "artifacts": [{
            "type": "map_png", "path": "velocity_points.png",
            "caption": (f"EGMS vertical velocity, {n} points "
                        f"({dates[0]}–{dates[1]}); blue = uplift, red = subsidence"),
        }],
        "attribution": [
            "Contains modified Copernicus Sentinel data; "
            "European Ground Motion Service (EGMS), Copernicus Land Monitoring Service.",
        ],
    })
    with open(os.path.join(args.output_dir, "result.json"), "w") as f:
        f.write(result.model_dump_json(indent=2))
    progress(100, "done")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="EGMS L3 analysis wrapper (M4.1b)")
    ap.add_argument("--query-id", required=True)
    ap.add_argument("--aoi", required=True)
    ap.add_argument("--dates", default=None)
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--params", default="{}")
    args = ap.parse_args()
    params = json.loads(args.params or "{}")
    return run_egms(args, params)


if __name__ == "__main__":
    sys.exit(main())
