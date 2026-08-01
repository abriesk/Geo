#!/usr/bin/env python3
"""FLOODPY flood-extent wrapper (M4.2b).

Obeys the §5.3 wrapper contract:
    --query-id --aoi <geojson> --dates <start,end> --input-dir --output-dir --params
and emits result.json per §6.3 plus a flood-extent PNG.

HOW IT WORKS
1. ERA5 finds the heaviest rainfall episode in the requested window and derives
   a pre-flood baseline window and a flood window (floodpy_event.py). If no
   notable rainfall occurred we stop here and say so — running change detection
   over a dry period yields a confident-looking map of noise.
2. FLOODPY (statistical mode) is driven through the sequence its own notebook
   uses: landcover -> S1 query -> pick flood date -> download -> stack ->
   slope -> t-scores -> flood map.
3. The final map is `Flood_local_map_RG_morph` (after region growing and
   morphological filtering) from FLOODPY's output netCDF.

THE ONE STEP THE NOTEBOOK LEAVES TO A HUMAN
`sel_S1_data()` needs ONE date chosen from `flood_candidate_dates`. A person
picks it by eye. We pick the earliest acquisition at or after the rainfall peak,
because floodwater recedes: the first pass after the storm sees the most water.
The gap between peak and acquisition is then reported in the answer, since a
four-day gap can mean most of the flooding has already drained away — an
underestimate the reader deserves to know about.

Method is "floodpy"; the summary_stats group is "flood".
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Fraction of the AOI flooded above which we call it widespread.
WIDESPREAD_FRACTION = 0.05
# ESA WorldCover class 80 == permanent water bodies. Radar change detection
# will happily flag rivers and lakes; counting those as "flooding" would
# overstate the event, so they are excluded and reported separately.
WORLDCOVER_WATER_CLASS = 80


# Every key FLOODPYapp.__init__ reads with bracket access, i.e. all mandatory.
# Extracted by parsing the constructor, NOT copied from the notebook: the
# notebook's params_dict omits `flood_event`, so following it produces a
# KeyError the moment FloodwaterEstimation is constructed. Upstream drift here
# is silent and fatal, so the diagnostic re-derives this list from FLOODPY's
# source and complains if it no longer matches.
FLOODPY_REQUIRED_KEYS = (
    "projectfolder", "flood_event", "src_dir", "GPTBIN_PATH", "snap_orbit_dir",
    "AOI_File", "relOrbit", "RAM", "Copernicus_username", "Copernicus_password",
    "pre_flood_start", "pre_flood_end", "flood_start", "flood_end",
    "LATMIN", "LONMIN", "LATMAX", "LONMAX",
    "minimum_mapping_unit_area_m2", "CPU",
)


def _check_floodpy_params(params_dict: dict) -> None:
    missing = [k for k in FLOODPY_REQUIRED_KEYS if k not in params_dict]
    if missing:
        raise KeyError(
            "FLOODPY's FloodwaterEstimation requires parameters we did not "
            f"supply: {missing}. Failing here rather than inside FLOODPY, where "
            "it surfaces as a bare KeyError with no context.")


def progress(pct: int, msg: str) -> None:
    print(f"PROGRESS {pct} {msg}", flush=True)


def _safe_cpu_ram(params: dict):
    """Pick FLOODPY's CPU (parallel gpt count) and RAM (per-gpt heap) so their
    PRODUCT fits the container, with headroom.

    FLOODPY launches `CPU` gpt processes at once, each a JVM with a `RAM` heap
    plus off-heap overhead. The failure mode that cost 20 hours was CPU*RAM far
    exceeding physical memory, so the box swapped itself to a standstill. We
    budget from the cgroup memory limit rather than trusting defaults.
    """
    import multiprocessing

    # explicit override wins, but is still bounded below
    cpu_override = params.get("cpu")
    ram_override = params.get("ram")

    mem_gb = _container_mem_gb()
    cores = multiprocessing.cpu_count()

    # Per-gpt heap, and assume ~1.6x that in real RES (heap + native + cache).
    heap_gb = float(str(ram_override or os.environ.get("FLOODPY_RAM_GB", 4)).rstrip("Gg"))
    per_proc_gb = heap_gb * 1.6
    # Leave ~4 GB for the OS, python, xarray. Never fewer than 1, never more
    # than half the cores (gpt is memory- not CPU-bound once parallelism=1).
    budget_gb = max(mem_gb - 4.0, per_proc_gb)
    by_mem = int(budget_gb // per_proc_gb)
    cpu = int(cpu_override) if cpu_override else max(1, min(by_mem, cores // 2, 4))
    ram = f"{int(heap_gb)}G"
    print(f"[floodpy] resources: container~{mem_gb:.0f}GB {cores}core -> "
          f"CPU={cpu} parallel gpt x RAM={ram} heap "
          f"(~{cpu*per_proc_gb:.0f}GB peak)", flush=True)
    return cpu, ram


def _container_mem_gb() -> float:
    """Memory limit visible to THIS container (cgroup v2 then v1), else host."""
    for path in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            v = open(path).read().strip()
            if v not in ("max", ""):
                gb = int(v) / (1024 ** 3)
                if 0 < gb < 1e6:
                    return gb
        except Exception:  # noqa: BLE001
            pass
    try:
        import os as _os
        return _os.sysconf("SC_PAGE_SIZE") * _os.sysconf("SC_PHYS_PAGES") / (1024 ** 3)
    except Exception:  # noqa: BLE001
        return 8.0


def _aoi_bbox(aoi: dict):
    ring = aoi["coordinates"][0]
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return min(lons), min(lats), max(lons), max(lats)


def _aoi_km2(aoi: dict) -> float:
    lonmin, latmin, lonmax, latmax = _aoi_bbox(aoi)
    midlat = math.radians((latmin + latmax) / 2)
    return abs((lonmax - lonmin) * math.cos(midlat) * 111.0) * abs((latmax - latmin) * 111.0)


def _parse_dates(dates_arg: str | None):
    """--dates 'YYYY-MM-DD,YYYY-MM-DD'; null-safe with a flood-appropriate
    default lookback of 3 months (§6.1)."""
    end = dt.date.today()
    start = end - dt.timedelta(days=90)
    if dates_arg:
        parts = [p.strip() for p in dates_arg.split(",")]
        try:
            if len(parts) > 0 and parts[0]:
                start = dt.date.fromisoformat(parts[0])
            if len(parts) > 1 and parts[1]:
                end = dt.date.fromisoformat(parts[1])
        except ValueError:
            pass
    return start, end


def _write_result(args, payload: dict) -> int:
    sys.path.insert(0, "/libs/contracts")
    from geohazard_contracts import ResultJson

    os.makedirs(args.output_dir, exist_ok=True)
    result = ResultJson.model_validate(payload)
    with open(os.path.join(args.output_dir, "result.json"), "w") as f:
        f.write(result.model_dump_json(indent=2))
    return 0


ATTRIBUTION = [
    "Contains modified Copernicus Sentinel data processed with FLOODPY "
    "(Karamvasis & Karathanassi, 2021).",
    "Contains modified Copernicus Climate Change Service information (ERA5).",
    "Contains ESA WorldCover data.",
]


def _no_event_result(args, start: dt.date, end: dt.date, event) -> int:
    """Honest 'nothing to analyse' result. Exit 0: a dry period is a real
    answer, not a failure to be retried three times."""
    caveats = [event.reason]
    if event.window_max_mm is not None:
        caveats.append(
            "Flood mapping from radar compares before-and-after images of a "
            "specific event. Without one, there is nothing to compare, so no "
            "flood map was produced for this period.")
    caveats.append(
        "This only means no significant rainfall was recorded over this area in "
        "this period. Flooding from river surges, dam releases or snowmelt "
        "originating elsewhere would not be captured by a rainfall screen.")
    return _write_result(args, {
        "query_id": args.query_id,
        "method": "floodpy",
        "status": "failed",
        "summary_stats": {},
        "quality": {
            "scene_count": 0,
            "date_coverage": [start.isoformat(), end.isoformat()],
            "coherence_mean": None, "masked_fraction": None, "cloud_fraction": None,
            "confidence": "low",
            "caveats": caveats,
        },
        "artifacts": [],
        "attribution": ATTRIBUTION,
    })


# FLOODPYapp.query_S1_data() assigns `flood_datetimes` (a list of pandas
# Timestamps). The notebook prints `flood_candidate_dates`, which the code at
# this commit never sets — reading the notebook rather than the source cost a
# silent no-op run. Both names are tried, newest first, and we fail loudly if
# neither exists rather than quietly deciding there are no images.
_CANDIDATE_ATTRS = ("flood_datetimes", "flood_candidate_dates")


# FLOODPY's orbit downloader (Sentinel_1_orbits_download.py) passes the scene's
# mission id straight to the bundled `eof` library, which only accepts "S1A" or
# "S1B". Sentinel-1C is operational as of 2025 and now appears in the CDSE
# catalogue, so a run can select an S1C scene and then die with
#   ValueError: missions argument must be "S1A" or "S1B"
# We filter the query DataFrame down to supported missions BEFORE the flood
# date is chosen, so FLOODPY never picks a scene it cannot process. If that
# removes the only images near the peak, the normal "no usable image" path
# handles it honestly. A backlog item tracks proper S1C support upstream.
_SUPPORTED_MISSIONS = ("S1A", "S1B")


def _drop_unsupported_missions(app) -> None:
    import pandas as pd  # noqa: F401
    df = getattr(app, "query_S1_df", None)
    if df is None or not len(df):
        return
    col = None
    for c in ("platformSerialIdentifier", "platform", "mission"):
        if c in df.columns:
            col = c
            break
    if col is None:
        print("[floodpy] cannot see a platform column; not filtering missions",
              flush=True)
        return
    # platformSerialIdentifier holds values like "A"/"B"/"C" or "S1A"/"S1C".
    def supported(v):
        v = str(v).upper()
        tag = v if v.startswith("S1") else f"S1{v}"
        return tag in _SUPPORTED_MISSIONS
    before = len(df)
    keep = df[df[col].map(supported)]
    dropped = before - len(keep)
    if dropped:
        missions = sorted(set(str(x) for x in df[col]) - set(
            x for x in keep[col]))
        print(f"[floodpy] dropped {dropped} scene(s) on unsupported missions "
              f"{missions} (FLOODPY orbit downloader is S1A/S1B only)", flush=True)
        app.query_S1_df = keep
        # keep the selected-orbit frame consistent if it exists already
        if getattr(app, "query_S1_sel_df", None) is not None:
            try:
                app.query_S1_sel_df = app.query_S1_sel_df[
                    app.query_S1_sel_df[col].map(supported)]
            except Exception:  # noqa: BLE001
                pass


def _candidate_dates(app):
    for attr in _CANDIDATE_ATTRS:
        v = getattr(app, attr, None)
        if v is not None:
            try:
                n = len(v)
            except TypeError:
                continue
            print(f"[floodpy] {n} candidate acquisition(s) from app.{attr}", flush=True)
            return list(v)
    raise AttributeError(
        "FLOODPY exposed none of "
        f"{_CANDIDATE_ATTRS} after query_S1_data(). The installed FLOODPY "
        "differs from the one this wrapper was written against; check which "
        "attribute now holds the candidate acquisition datetimes."
    )


def _select_flood_date(candidates, peak_date: str):
    """Earliest Sentinel-1 acquisition at or after the rainfall peak.

    Returns (chosen, latency_days) or (None, None). Floodwater drains, so the
    first pass after the storm is the best chance of seeing it; if every
    acquisition predates the peak we fall back to the last one and let the
    caveat carry the bad news.
    """
    import pandas as pd

    if candidates is None or len(candidates) == 0:
        return None, None
    peak = pd.Timestamp(peak_date)
    parsed = sorted((pd.Timestamp(str(c)), c) for c in candidates)
    after = [(ts, c) for ts, c in parsed if ts >= peak]
    if after:
        ts, chosen = after[0]
        return chosen, (ts - peak).total_seconds() / 86400.0
    ts, chosen = parsed[-1]
    return chosen, (ts - peak).total_seconds() / 86400.0


def _render_flood_png(flood_mask, water_mask, path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import ListedColormap

    # 0 dry, 1 permanent water, 2 flooded
    img = np.zeros(flood_mask.shape, dtype=np.uint8)
    if water_mask is not None:
        img[water_mask] = 1
    img[flood_mask] = 2
    cmap = ListedColormap(["#e8e4dc", "#5b8db8", "#c1352b"])
    fig, ax = plt.subplots(figsize=(7, 6))
    ax.imshow(img, cmap=cmap, vmin=0, vmax=2, interpolation="nearest")
    ax.set_title(title, fontsize=10)
    ax.set_xticks([]); ax.set_yticks([])
    handles = [
        plt.Rectangle((0, 0), 1, 1, color="#c1352b", label="flooded"),
        plt.Rectangle((0, 0), 1, 1, color="#5b8db8", label="permanent water"),
        plt.Rectangle((0, 0), 1, 1, color="#e8e4dc", label="dry land"),
    ]
    ax.legend(handles=handles, loc="lower right", fontsize=8, framealpha=0.9)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def run_floodpy(args, params: dict) -> int:
    import numpy as np
    import xarray as xr
    import floodpy_event

    os.makedirs(args.output_dir, exist_ok=True)
    aoi = json.load(open(args.aoi))
    lonmin, latmin, lonmax, latmax = _aoi_bbox(aoi)
    start, end = _parse_dates(args.dates)

    # ---- 1. find the rainfall event ------------------------------------
    progress(4, "checking rainfall records (ERA5) for a flood event")
    event = floodpy_event.detect(
        lonmin, latmin, lonmax, latmax, start, end,
        min_event_mm=float(params.get("min_event_mm",
                                      floodpy_event.DEFAULT_MIN_EVENT_MM)),
        out_dir=os.path.join(args.output_dir, "_era5"))
    if not event.found:
        print(f"[floodpy] no event: {event.reason}", flush=True)
        progress(100, "no rainfall event to analyse")
        return _no_event_result(args, start, end, event)
    print(f"[floodpy] {event.reason}", flush=True)

    # ---- 2. drive FLOODPY ----------------------------------------------
    _cpu, _ram = _safe_cpu_ram(params)
    projectfolder = params.get(
        "projectfolder",
        os.path.join("/data/scratch/floodpy", str(args.query_id)))
    os.makedirs(projectfolder, exist_ok=True)

    params_dict = {
        "projectfolder": projectfolder,
        "src_dir": os.path.join(os.environ.get("FLOODPY_HOME", "/opt/FLOODPY"), "floodpy"),
        "snap_orbit_dir": os.environ.get("SNAP_ORBIT_DIR",
                                         "/data/scratch/floodpy/orbits"),
        "GPTBIN_PATH": os.environ.get("GPTBIN_PATH", "/opt/snap/bin/gpt"),
        "pre_flood_start": event.pre_flood_start,
        "pre_flood_end": event.pre_flood_end,
        "flood_start": event.flood_start,
        "flood_end": event.flood_end,
        "AOI_File": "None",
        "LONMIN": lonmin, "LATMIN": latmin, "LONMAX": lonmax, "LATMAX": latmax,
        "relOrbit": params.get("relOrbit", "Auto"),
        "minimum_mapping_unit_area_m2": float(
            params.get("minimum_mapping_unit_area_m2", 4000)),
        # Sized from the container's real memory limit (see _safe_cpu_ram):
        # CPU * per-gpt heap must fit RAM or the box swaps itself to death.
        "CPU": _cpu,
        "RAM": _ram,
        "Copernicus_username": os.environ.get("CDSE_USERNAME", ""),
        "Copernicus_password": os.environ.get("CDSE_PASSWORD", ""),
        # Label only — FLOODPY interpolates it into its output filenames
        # (Flooded_regions_<flood_event>_<date>(UTC).nc). Keep it
        # filesystem-safe and human-meaningful for debugging.
        "flood_event": params.get(
            "flood_event", f"event_{event.peak_date.replace('-', '')}"),
    }
    _check_floodpy_params(params_dict)
    os.makedirs(params_dict["snap_orbit_dir"], exist_ok=True)

    from floodpy.FLOODPYapp import FloodwaterEstimation
    app = FloodwaterEstimation(params_dict=params_dict)

    progress(10, "fetching land cover")
    app.download_landcover_data()

    progress(15, "fetching precipitation for the event window")
    app.download_ERA5_Precipitation_data()

    progress(20, "searching Sentinel-1 acquisitions")
    app.query_S1_data()
    _drop_unsupported_missions(app)
    candidates = _candidate_dates(app)
    chosen, latency_days = _select_flood_date(candidates, event.peak_date)
    if chosen is None:
        # Emit a progress line before returning: a bare exit here reads as a
        # crash to anyone watching the log.
        progress(100, "no Sentinel-1 image available around the rainfall peak")
        return _write_result(args, {
            "query_id": args.query_id, "method": "floodpy", "status": "failed",
            "summary_stats": {},
            "quality": {
                "scene_count": 0,
                "date_coverage": [start.isoformat(), end.isoformat()],
                "coherence_mean": None, "masked_fraction": None,
                "cloud_fraction": None, "confidence": "low",
                "caveats": [
                    "No Sentinel-1 radar image was available over this area "
                    f"around the rainfall peak on {event.peak_date}, so the "
                    "flooding could not be mapped.",
                    "Radar satellites revisit a given place every few days; "
                    "short-lived flooding between passes can be missed entirely.",
                ],
            },
            "artifacts": [], "attribution": ATTRIBUTION,
        })
    print(f"[floodpy] flood image {chosen} ({latency_days:.1f} d after peak)",
          flush=True)
    app.sel_S1_data(chosen)

    progress(30, "downloading Sentinel-1 scenes (large, slow)")
    app.download_S1_GRD_products()
    app.download_S1_orbits()

    progress(50, "preprocessing radar stack with SNAP (slow)")
    app.create_S1_stack(overwrite=bool(params.get("overwrite", False)))

    progress(70, "computing terrain slope")
    app.calc_slope()

    progress(78, "comparing before/after backscatter")
    app.calc_t_scores()

    progress(85, "delineating floodwater")
    app.calc_floodmap_dataset()

    # ---- 3. turn the map into numbers ----------------------------------
    progress(90, "measuring flooded area")
    ds = xr.open_dataset(app.Flood_map_dataset_filename)
    try:
        if "Flood_local_map_RG_morph" not in ds:
            raise KeyError(
                f"expected Flood_local_map_RG_morph in FLOODPY output, got "
                f"{list(ds.data_vars)}")
        flood = np.asarray(ds["Flood_local_map_RG_morph"].values) > 0
        slope_mask = (np.asarray(ds["slope_mask"].values) > 0
                      if "slope_mask" in ds else None)
        pixel_km2 = _pixel_area_km2(ds)
    finally:
        ds.close()

    water = _permanent_water_mask(app, flood.shape)
    flood_only = flood & (~water if water is not None else True)

    n_flood = int(flood_only.sum())
    flooded_km2 = n_flood * pixel_km2
    aoi_km2 = _aoi_km2(aoi)
    flooded_fraction = flooded_km2 / aoi_km2 if aoi_km2 > 0 else 0.0
    water_km2 = float(water.sum()) * pixel_km2 if water is not None else None
    masked_fraction = (float((~slope_mask).sum()) / slope_mask.size
                       if slope_mask is not None and slope_mask.size else None)

    progress(94, "rendering flood map")
    png = os.path.join(args.output_dir, "flood_extent.png")
    _render_flood_png(flood_only, water, png,
                      f"Flood extent, {str(chosen)[:10]} "
                      f"(rain peak {event.peak_date})")

    scene_count = _scene_count(app)
    stats, quality = _assemble(
        event, chosen, latency_days, flooded_km2, flooded_fraction,
        water_km2, aoi_km2, scene_count, masked_fraction, start, end)

    progress(96, "writing result.json")
    rc = _write_result(args, {
        "query_id": args.query_id,
        "method": "floodpy",
        "status": "ok",
        "summary_stats": {"flood": stats},
        "quality": quality,
        "artifacts": [{
            "type": "map_png", "path": "flood_extent.png",
            "caption": (f"Flooded area from Sentinel-1 radar on "
                        f"{str(chosen)[:10]}, {latency_days:.1f} days after peak "
                        f"rainfall on {event.peak_date}"),
        }],
        "attribution": ATTRIBUTION,
    })
    progress(100, "done")
    return rc


def _pixel_area_km2(ds) -> float:
    """Pixel area in km^2 from the dataset's own coordinates."""
    import numpy as np
    try:
        x = np.asarray(ds["x"].values, dtype=float)
        y = np.asarray(ds["y"].values, dtype=float)
        dx = abs(float(np.median(np.diff(x))))
        dy = abs(float(np.median(np.diff(y))))
    except Exception:  # noqa: BLE001
        return 0.0
    # FLOODPY geocodes to WGS84 degrees; anything above ~0.01 is degrees, not m.
    if dx < 0.01 and dy < 0.01:
        midlat = float(np.median(y))
        return (dx * 111.0 * math.cos(math.radians(midlat))) * (dy * 111.0)
    return (dx / 1000.0) * (dy / 1000.0)


def _permanent_water_mask(app, shape):
    """Permanent water from ESA WorldCover, resampled onto the flood grid.

    Returns None if unavailable — in which case the caveats say permanent water
    could not be separated, rather than silently counting rivers as flooding.
    """
    import numpy as np
    path = getattr(app, "lc_mosaic_filename", None)
    if not path or not os.path.exists(path):
        return None
    try:
        import rasterio
        from rasterio.enums import Resampling
        with rasterio.open(path) as src:
            lc = src.read(1, out_shape=shape, resampling=Resampling.nearest)
        return np.asarray(lc) == WORLDCOVER_WATER_CLASS
    except Exception as e:  # noqa: BLE001
        print(f"[floodpy] permanent-water mask unavailable: {e}", file=sys.stderr)
        return None


def _scene_count(app) -> int:
    """Number of Sentinel-1 acquisitions in the stack.

    FLOODPY holds these in `query_S1_sel_df` — the query DataFrame filtered to
    the chosen flood date's orbit, i.e. the scenes actually coregistered. The
    first version of this guessed wrong attribute names and silently returned
    0, which not only mis-reported the count but disabled the thin-baseline
    confidence downgrade (that logic keys off scene_count). Each acquisition is
    two GRD frames in this AOI, but FLOODPY counts rows as acquisitions, which
    is what "scenes" means for the baseline.
    """
    for attr in ("query_S1_sel_df", "query_S1_df"):
        df = getattr(app, attr, None)
        try:
            if df is not None and len(df):
                return int(len(df))
        except Exception:  # noqa: BLE001
            continue
    # Fall back to the candidate datetimes if the frames are somehow absent.
    for attr in ("flood_datetimes",):
        v = getattr(app, attr, None)
        try:
            if v is not None and len(v):
                return int(len(v))
        except Exception:  # noqa: BLE001
            continue
    return 0


def _assemble(event, chosen, latency_days, flooded_km2, flooded_fraction,
              water_km2, aoi_km2, scene_count, masked_fraction, start, end):
    """Stats + quality block, including the confidence reasoning."""
    stats = {
        "flooded_area_km2": round(flooded_km2, 3),
        "flooded_fraction_of_aoi": round(flooded_fraction, 4),
        "aoi_area_km2": round(aoi_km2, 1),
        "peak_rainfall_date": event.peak_date,
        "peak_rainfall_mm": event.peak_mm,
        "rainfall_3day_total_mm": event.accum_mm,
        "radar_image_date": str(chosen)[:19],
        "days_after_rain_peak": round(latency_days, 1),
        "extent": "widespread" if flooded_fraction >= WIDESPREAD_FRACTION else (
            "localised" if flooded_km2 > 0 else "none_detected"),
    }
    if water_km2 is not None:
        stats["permanent_water_km2_excluded"] = round(water_km2, 3)

    # Confidence. The dominant factor is LATENCY: radar sees water only while
    # it is still standing. A pass three days after the peak can legitimately
    # find nothing where a severe flood occurred, so a low-extent result from a
    # late image must not be reported as a confident "no flooding".
    if latency_days <= 1.5:
        confidence = "high"
    elif latency_days <= 3.0:
        confidence = "moderate"
    else:
        confidence = "low"
    # The t-score baseline is a mean over the pre-flood images. With only a
    # handful, that mean is unstable and normal seasonal variation can look
    # like a change. Downgrade one full step rather than only touching
    # "moderate" — a thin baseline undermines a prompt pass just as much.
    thin_baseline = bool(scene_count) and scene_count < 4
    if thin_baseline:
        confidence = {"high": "moderate", "moderate": "low"}.get(confidence, confidence)

    caveats = [
        f"Radar mapped this area {latency_days:.1f} days after the heaviest "
        f"rain ({event.peak_date}). Floodwater drains quickly, so water present "
        "at the peak may already have receded when the satellite passed — "
        "flooded area is a lower bound, not a maximum.",
        "Flooding is inferred from changes in radar brightness against a "
        "pre-flood baseline, not observed directly.",
        "Radar struggles to see water beneath dense vegetation or inside "
        "built-up areas, where flooding can be under-detected. Smooth dry "
        "surfaces such as tarmac or sand can occasionally be mistaken for water.",
    ]
    if water_km2 is not None:
        caveats.append(
            "Permanent rivers and lakes were identified from land-cover data and "
            "excluded, so the reported area is additional water rather than "
            "normal water bodies.")
    else:
        caveats.append(
            "Permanent water bodies could NOT be separated from floodwater for "
            "this area, so rivers and lakes may be included in the reported "
            "area, overstating it.")
    if thin_baseline:
        caveats.append(
            f"Only {scene_count} radar images were available to build the "
            "before-flood baseline. With so few, ordinary seasonal variation is "
            "harder to tell apart from genuine flooding.")
    if confidence == "low" and latency_days > 3.0:
        caveats.append(
            "Because of the delay between the rainfall and the satellite pass, "
            "a small or zero flooded area here should not be read as evidence "
            "that no flooding occurred.")
    if stats["extent"] == "none_detected":
        caveats.append(
            "No floodwater was detected. Given the caveats above, this is best "
            "read as 'no standing water visible to radar at that moment'.")

    quality = {
        "scene_count": int(scene_count),
        "date_coverage": [start.isoformat(), end.isoformat()],
        "coherence_mean": None,
        "masked_fraction": round(masked_fraction, 4) if masked_fraction is not None else None,
        "cloud_fraction": None,   # radar sees through cloud; not applicable
        "confidence": confidence,
        "caveats": caveats,
    }
    return stats, quality


def main() -> int:
    ap = argparse.ArgumentParser(description="FLOODPY flood-extent wrapper (M4.2b)")
    ap.add_argument("--query-id", required=True)
    ap.add_argument("--aoi", required=True)
    ap.add_argument("--dates", default=None)
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--params", default="{}")
    args = ap.parse_args()
    return run_floodpy(args, json.loads(args.params or "{}"))


if __name__ == "__main__":
    sys.exit(main())
