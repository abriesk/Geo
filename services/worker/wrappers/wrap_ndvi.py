#!/usr/bin/env python3
"""wrap_ndvi.py — Sentinel-2 L2A NDVI change detection (M2.3, §5.4).

Wrapper contract: CLI args --query-id --aoi <geojson-file> --dates <start,end>
--input-dir --output-dir --params <json>; PROGRESS lines on stdout;
result.json (§6.3) + PNGs in --output-dir; exit 0/nonzero.

Method:
- Take the earliest and latest S2 L2A .SAFE products in --input-dir.
- Per scene: clip B04/B08 (10 m) to the AOI, apply the PB>=04.00
  BOA_ADD_OFFSET (read from MTD_MSIL2A.xml; fallback -1000 for baseline
  N>=0400 parsed from the product name), scale 1/10000, compute NDVI.
- Mask via SCL (20 m, nearest-upsampled to the 10 m grid): classes
  0 no-data, 1 saturated/defective, 3 cloud shadow, 8 cloud medium,
  9 cloud high, 10 thin cirrus. DN==0 is also no-data. Snow (11) is NOT
  masked (a caveat notes it).
- dNDVI = late - early over pixels valid in both. Loss pixels: dNDVI<-0.2.
- One scene only -> single-date NDVI, status=partial.

Quality: cloud_fraction = max per-scene fraction of cloud classes
{3,8,9,10}; masked_fraction = fraction of AOI pixels invalid in the pair;
confidence: high if cloud<0.10 and masked<0.20; moderate if masked<0.45;
else low — pairs closer than 90 days are capped at moderate with a caveat.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import date

import numpy as np

SCL_MASKED = {0, 1, 3, 8, 9, 10}
SCL_CLOUDY = {3, 8, 9, 10}
LOSS_THRESHOLD = -0.2


def progress(pct: int, msg: str) -> None:
    print(f"PROGRESS {pct} {msg}", flush=True)


def _sensing_date(safe_dir: str) -> date:
    m = re.search(r"_MSIL2A_(\d{8})T", os.path.basename(safe_dir))
    if not m:
        raise ValueError(f"cannot parse sensing date from {safe_dir}")
    s = m.group(1)
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _boa_offset(safe_dir: str) -> float:
    """BOA_ADD_OFFSET from product metadata; fallback by baseline number."""
    mtd = os.path.join(safe_dir, "MTD_MSIL2A.xml")
    try:
        text = open(mtd, encoding="utf-8", errors="ignore").read()
        vals = re.findall(r"<BOA_ADD_OFFSET[^>]*>(-?\d+)</BOA_ADD_OFFSET>", text)
        if vals:
            return float(vals[0])
    except OSError:
        pass
    m = re.search(r"_N(\d{4})_", os.path.basename(safe_dir))
    if m and int(m.group(1)) >= 400:
        return -1000.0
    return 0.0


def _find_band(safe_dir: str, res: str, band: str) -> str:
    pat = os.path.join(safe_dir, "GRANULE", "*", "IMG_DATA", res, f"*_{band}_*.jp2")
    hits = glob.glob(pat)
    if not hits:
        raise FileNotFoundError(f"{band} not found under {safe_dir} ({res})")
    return hits[0]


def _nearest_resize(arr: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    ri = (np.arange(shape[0]) * arr.shape[0] / shape[0]).astype(int).clip(0, arr.shape[0] - 1)
    ci = (np.arange(shape[1]) * arr.shape[1] / shape[1]).astype(int).clip(0, arr.shape[1] - 1)
    return arr[np.ix_(ri, ci)]


def read_scene(safe_dir: str, aoi_geojson: dict):
    """Returns (ndvi masked-array, valid_mask, cloud_fraction, sensing_date)."""
    import rasterio
    from rasterio.mask import mask as rio_mask
    from rasterio.warp import transform_geom

    b04_path = _find_band(safe_dir, "R10m", "B04")
    b08_path = _find_band(safe_dir, "R10m", "B08")
    scl_path = _find_band(safe_dir, "R20m", "SCL")
    offset = _boa_offset(safe_dir)

    with rasterio.open(b04_path) as src:
        geom = transform_geom("EPSG:4326", src.crs, aoi_geojson)
        b04, _ = rio_mask(src, [geom], crop=True, nodata=0)
    with rasterio.open(b08_path) as src:
        b08, _ = rio_mask(src, [transform_geom("EPSG:4326", src.crs, aoi_geojson)],
                          crop=True, nodata=0)
    with rasterio.open(scl_path) as src:
        scl, _ = rio_mask(src, [transform_geom("EPSG:4326", src.crs, aoi_geojson)],
                          crop=True, nodata=0)

    b04 = b04[0].astype(np.float64)
    b08 = b08[0].astype(np.float64)
    scl = _nearest_resize(scl[0], b04.shape)

    nodata = (b04 == 0) | (b08 == 0)
    scl_bad = np.isin(scl, list(SCL_MASKED))
    valid = ~nodata & ~scl_bad

    aoi_px = ~nodata  # pixels inside AOI with data
    cloudy = np.isin(scl, list(SCL_CLOUDY)) & aoi_px
    cloud_fraction = float(cloudy.sum() / aoi_px.sum()) if aoi_px.sum() else 1.0

    red = (b04 + offset) / 10000.0
    nir = (b08 + offset) / 10000.0
    denom = nir + red
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = np.where(np.abs(denom) > 1e-6, (nir - red) / denom, np.nan)
    ndvi = np.where(valid, ndvi, np.nan)
    return ndvi, valid, cloud_fraction, _sensing_date(safe_dir)


def render_png(array: np.ndarray, path: str, title: str, cmap: str, vmin: float, vmax: float):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 6))
    im = ax.imshow(array, cmap=cmap, vmin=vmin, vmax=vmax)
    ax.set_title(title, fontsize=10)
    ax.set_xticks([]), ax.set_yticks([])
    fig.colorbar(im, ax=ax, shrink=0.8)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query-id", required=True)
    ap.add_argument("--aoi", required=True, help="path to GeoJSON polygon file")
    ap.add_argument("--dates", required=True, help="start,end (may be 'None,None')")
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--params", default="{}")
    args = ap.parse_args()

    sys.path.insert(0, "/libs/contracts")  # container path; harmless elsewhere
    from geohazard_contracts import ResultJson

    os.makedirs(args.output_dir, exist_ok=True)
    aoi = json.load(open(args.aoi))

    progress(5, "locating Sentinel-2 scenes")
    safes = sorted(
        [p for p in glob.glob(os.path.join(args.input_dir, "*"))
         if re.search(r"MSIL2A", os.path.basename(p)) and os.path.isdir(p)],
        key=_sensing_date,
    )
    if not safes:
        print(f"ERROR no Sentinel-2 L2A products in {args.input_dir}", file=sys.stderr)
        return 2

    pair = [safes[0], safes[-1]] if len(safes) >= 2 else [safes[0]]
    single = len(pair) == 1 or pair[0] == pair[-1]
    if single:
        pair = [safes[0]]

    ndvis, valids, clouds, dates_ = [], [], [], []
    for i, safe in enumerate(pair):
        progress(15 + i * 30, f"computing NDVI for {os.path.basename(safe)[:32]}…")
        ndvi, valid, cf, d = read_scene(safe, aoi)
        ndvis.append(ndvi), valids.append(valid), clouds.append(cf), dates_.append(d)

    progress(75, "computing change statistics")
    caveats = ["Snow/ice pixels (SCL 11) are not masked and can mimic vegetation change"]
    stats: dict[str, object] = {}
    artifacts = []

    def q(a):  # nan-safe rounded mean
        v = np.nanmean(a)
        return None if np.isnan(v) else round(float(v), 4)

    if single:
        status = "partial"
        caveats.append("Only one usable scene — change over time cannot be computed")
        both_valid = valids[0]
        stats = {"ndvi_mean": q(ndvis[0]), "scene_date": str(dates_[0]), "trend": "unknown"}
        render_png(ndvis[0], os.path.join(args.output_dir, "ndvi.png"),
                   f"NDVI {dates_[0]}", "RdYlGn", -0.2, 0.9)
        artifacts.append({"type": "map_png", "path": "ndvi.png",
                          "caption": f"NDVI on {dates_[0]} (single usable scene)"})
    else:
        status = "ok"
        both_valid = valids[0] & valids[1]
        dndvi = np.where(both_valid, ndvis[1] - ndvis[0], np.nan)
        loss = np.nansum(dndvi < LOSS_THRESHOLD)
        n_valid = int(both_valid.sum())
        pair_days = (dates_[1] - dates_[0]).days
        stats = {
            "ndvi_mean_early": q(ndvis[0]),
            "ndvi_mean_late": q(ndvis[1]),
            "dndvi_mean": q(dndvi),
            "loss_fraction": round(float(loss / n_valid), 4) if n_valid else None,
            "date_early": str(dates_[0]),
            "date_late": str(dates_[1]),
            "pair_separation_days": pair_days,
        }
        lf = stats["loss_fraction"] or 0.0
        dm = stats["dndvi_mean"] or 0.0
        stats["trend"] = ("vegetation_loss" if (lf > 0.10 or dm < -0.10)
                          else "vegetation_gain" if dm > 0.10 else "stable")
        for i, name in ((0, "early"), (1, "late")):
            fn = f"ndvi_{name}.png"
            render_png(ndvis[i], os.path.join(args.output_dir, fn),
                       f"NDVI {dates_[i]}", "RdYlGn", -0.2, 0.9)
            artifacts.append({"type": "map_png", "path": fn,
                              "caption": f"NDVI on {dates_[i]}"})
        render_png(dndvi, os.path.join(args.output_dir, "dndvi.png"),
                   f"NDVI change {dates_[0]} → {dates_[1]}", "RdBu", -0.5, 0.5)
        artifacts.append({"type": "map_png", "path": "dndvi.png",
                          "caption": f"NDVI change {dates_[0]} → {dates_[1]} "
                                     "(red = vegetation loss)"})
        if pair_days < 90:
            caveats.append(
                f"Compared scenes are only {pair_days} days apart — seasonal effects "
                "dominate and long-term change cannot be separated"
            )

    aoi_data = valids[0] | (valids[1] if not single else valids[0])
    total_px = int(np.prod(ndvis[0].shape))
    masked_fraction = round(1.0 - float(both_valid.sum()) / total_px, 4) if total_px else 1.0
    cloud_fraction = round(max(clouds), 4)

    confidence = ("high" if cloud_fraction < 0.10 and masked_fraction < 0.20
                  else "moderate" if masked_fraction < 0.45 else "low")
    if not single and stats.get("pair_separation_days", 999) < 90 and confidence == "high":
        confidence = "moderate"
    if single:
        confidence = "low"

    progress(90, "writing result.json")
    result = ResultJson.model_validate({
        "query_id": args.query_id,
        "method": "ndvi",
        "status": status,
        "summary_stats": {"ndvi": stats},
        "quality": {
            "scene_count": len(pair),
            "date_coverage": [str(dates_[0]), str(dates_[-1])],
            "coherence_mean": None,
            "masked_fraction": min(masked_fraction, 1.0),
            "cloud_fraction": min(cloud_fraction, 1.0),
            "confidence": confidence,
            "caveats": caveats,
        },
        "artifacts": artifacts,
        "attribution": ["Contains modified Copernicus Sentinel data [2026]"],
    })
    with open(os.path.join(args.output_dir, "result.json"), "w") as f:
        f.write(result.model_dump_json(indent=2))
    progress(100, "done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
