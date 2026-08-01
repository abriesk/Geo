"""ERA5 rainfall event detection for the flood path (M4.2b).

WHY THIS EXISTS
FLOODPY is event-driven: it wants an explicit pre-flood baseline window and a
flood window, and compares radar backscatter between them. Our users ask a
different question — "was there a flood here?" over some period — and cannot be
expected to know the date of an event they are asking about.

FLOODPY's own notebook resolves this by plotting ERA5 precipitation so a human
can eyeball the storm and type the date in. This module automates exactly that
step: find the heaviest rainfall episode in the requested window and derive the
two windows from it.

Two honesty properties matter more than cleverness here:

  * If no notable rainfall occurred, we say so and run nothing. Manufacturing a
    "flood comparison" for a dry period would produce a confident-looking map of
    pure noise.
  * The gap between peak rainfall and the first usable Sentinel-1 pass is
    reported, because floodwater recedes. A radar image four days after the peak
    can miss most of the flooding, and the answer has to say that rather than
    quietly under-reporting the extent.

We query ERA5 ourselves rather than reusing FLOODPY's downloader, because
FLOODPY's ERA5 fetch is scoped to a flood window we do not know yet.
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
from dataclasses import dataclass, asdict
from typing import Optional

# Rainfall below this over the accumulation window is treated as "no event
# worth analysing". This is a screening heuristic, NOT a flood prediction
# threshold — it decides whether radar change detection has anything to look
# at, nothing more. Deliberately generous: missing a real event is worse than
# running an analysis that finds nothing.
DEFAULT_MIN_EVENT_MM = float(os.environ.get("FLOOD_MIN_EVENT_MM", "40"))
# Days over which rainfall is accumulated when hunting for the episode.
ACCUM_DAYS = int(os.environ.get("FLOOD_ACCUM_DAYS", "3"))
# Baseline length. FLOODPY's own example used roughly two months of pre-flood
# imagery to build a stable backscatter reference.
PRE_FLOOD_DAYS = int(os.environ.get("FLOOD_PRE_FLOOD_DAYS", "60"))
# How far past the peak to keep looking for a usable Sentinel-1 acquisition.
FLOOD_WINDOW_DAYS = int(os.environ.get("FLOOD_WINDOW_DAYS", "5"))


@dataclass
class RainEvent:
    """The rainfall episode we will analyse, plus the windows derived from it."""
    found: bool
    reason: str
    peak_date: Optional[str] = None          # ISO date of heaviest single day
    peak_mm: Optional[float] = None          # that day's total, mm
    accum_mm: Optional[float] = None         # heaviest ACCUM_DAYS total, mm
    window_max_mm: Optional[float] = None    # best accumulation seen, even if below threshold
    pre_flood_start: Optional[str] = None    # YYYYMMDDTHHMMSS, FLOODPY format
    pre_flood_end: Optional[str] = None
    flood_start: Optional[str] = None
    flood_end: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


def _fp(d: dt.date, hour: int = 0) -> str:
    """FLOODPY's datetime format: YYYYMMDDTHHMMSS, UTC."""
    return dt.datetime(d.year, d.month, d.day, hour).strftime("%Y%m%dT%H%M%S")


def daily_totals_mm(nc_path: str) -> "dict[dt.date, float]":
    """Daily precipitation totals (mm) from an ERA5 netCDF.

    ERA5 `tp` is accumulated precipitation in METRES per hourly step, so a day's
    total is the sum of its hours times 1000. Variable and dimension names have
    shifted across CDS backends, so both the legacy ('time') and newer
    ('valid_time') layouts are handled.
    """
    import numpy as np
    import xarray as xr

    ds = xr.open_dataset(nc_path)
    try:
        varname = "tp" if "tp" in ds.variables else next(
            (v for v in ds.data_vars if "precip" in v.lower() or v == "tp"), None)
        if varname is None:
            raise ValueError(f"no precipitation variable in {list(ds.data_vars)}")
        timedim = "valid_time" if "valid_time" in ds.dims else (
            "time" if "time" in ds.dims else None)
        if timedim is None:
            raise ValueError(f"no time dimension in {list(ds.dims)}")

        da = ds[varname]
        # Average over space first: we want "rain over this area", not a
        # single grid cell that may sit on the edge of the storm.
        spatial = [d for d in da.dims if d != timedim]
        series = da.mean(dim=spatial) if spatial else da

        out: dict[dt.date, float] = {}
        times = series[timedim].values
        vals = np.asarray(series.values, dtype=float) * 1000.0  # m -> mm
        for t, v in zip(times, vals):
            day = np.datetime64(t, "D").astype(dt.date)
            if np.isfinite(v):
                out[day] = out.get(day, 0.0) + float(v)
        return out
    finally:
        ds.close()


def find_event(daily: "dict[dt.date, float]",
               min_event_mm: float = DEFAULT_MIN_EVENT_MM,
               accum_days: int = ACCUM_DAYS) -> RainEvent:
    """Pick the heaviest rainfall episode from daily totals."""
    if not daily:
        return RainEvent(found=False,
                         reason="No ERA5 precipitation data was returned for this "
                                "area and period.")

    days = sorted(daily)
    best_sum, best_end_idx = -1.0, None
    for i in range(len(days)):
        lo = max(0, i - accum_days + 1)
        window = days[lo:i + 1]
        # Only accumulate over genuinely consecutive days.
        if (window[-1] - window[0]).days > accum_days - 1:
            continue
        total = sum(daily[d] for d in window)
        if total > best_sum:
            best_sum, best_end_idx = total, i

    if best_end_idx is None:
        return RainEvent(found=False,
                         reason="ERA5 returned data but no usable consecutive days.")

    lo = max(0, best_end_idx - accum_days + 1)
    window = days[lo:best_end_idx + 1]
    peak_day = max(window, key=lambda d: daily[d])

    if best_sum < min_event_mm:
        return RainEvent(
            found=False,
            window_max_mm=round(best_sum, 1),
            reason=(f"The wettest {accum_days}-day stretch in this period brought "
                    f"only about {best_sum:.0f} mm of rain over the area, below the "
                    f"{min_event_mm:.0f} mm screening threshold. There is no rainfall "
                    "event here for a flood analysis to examine."),
        )

    flood_start_d = peak_day - dt.timedelta(days=1)
    flood_end_d = peak_day + dt.timedelta(days=FLOOD_WINDOW_DAYS)
    pre_start_d = flood_start_d - dt.timedelta(days=PRE_FLOOD_DAYS)
    return RainEvent(
        found=True,
        reason=(f"Heaviest rainfall centred on {peak_day.isoformat()} "
                f"({daily[peak_day]:.0f} mm that day, {best_sum:.0f} mm over "
                f"{accum_days} days)."),
        peak_date=peak_day.isoformat(),
        peak_mm=round(daily[peak_day], 1),
        accum_mm=round(best_sum, 1),
        window_max_mm=round(best_sum, 1),
        pre_flood_start=_fp(pre_start_d, 3),
        pre_flood_end=_fp(flood_start_d, 3),
        flood_start=_fp(flood_start_d, 3),
        flood_end=_fp(flood_end_d, 3),
    )


def fetch_era5_daily(lonmin: float, latmin: float, lonmax: float, latmax: float,
                     start: dt.date, end: dt.date,
                     out_dir: Optional[str] = None) -> "dict[dt.date, float]":
    """Retrieve ERA5 total precipitation over the AOI and reduce to daily mm."""
    import cdsapi

    out_dir = out_dir or tempfile.mkdtemp()
    # cdsapi writes straight to `target` and will not create parents; when the
    # caller passes a path under the output dir it may not exist yet. The
    # retrieval itself succeeds and then the write fails, which reads like an
    # ERA5 problem but is not one.
    os.makedirs(out_dir, exist_ok=True)
    target = os.path.join(out_dir, "era5_precip.nc")

    years, months, days = set(), set(), set()
    d = start
    while d <= end:
        years.add(f"{d.year:04d}")
        months.add(f"{d.month:02d}")
        days.add(f"{d.day:02d}")
        d += dt.timedelta(days=1)

    # ERA5 `area` is [north, west, south, east]. Pad slightly so a small AOI
    # still intersects the 0.25-degree grid rather than falling between cells.
    pad = 0.25
    area = [round(latmax + pad, 2), round(lonmin - pad, 2),
            round(latmin - pad, 2), round(lonmax + pad, 2)]

    cdsapi.Client().retrieve(
        "reanalysis-era5-single-levels",
        {
            "product_type": "reanalysis",
            "variable": "total_precipitation",
            "year": sorted(years),
            "month": sorted(months),
            "day": sorted(days),
            "time": [f"{h:02d}:00" for h in range(24)],
            "area": area,
            "format": "netcdf",
        },
        target,
    )
    return daily_totals_mm(target)


def detect(lonmin: float, latmin: float, lonmax: float, latmax: float,
           start: dt.date, end: dt.date,
           min_event_mm: float = DEFAULT_MIN_EVENT_MM,
           out_dir: Optional[str] = None) -> RainEvent:
    """AOI + date window -> rainfall event and the windows FLOODPY needs."""
    try:
        daily = fetch_era5_daily(lonmin, latmin, lonmax, latmax, start, end, out_dir)
    except Exception as e:  # noqa: BLE001
        return RainEvent(
            found=False,
            reason=f"Rainfall data could not be retrieved from ERA5 ({e}).")
    return find_event(daily, min_event_mm=min_event_mm)
