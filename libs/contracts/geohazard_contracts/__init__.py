"""geohazard_contracts — §6 of Technical Reference v2 as code.

This package is the cross-session consistency anchor for LLM-assisted
development (§11.3). Any change here is a breaking change and must be
reflected in the technical reference first.
"""
from .enums import (
    Confidence,
    Depth,
    DownloadTier,
    HazardType,
    Method,
    QueryStatus,
    ResultStatus,
    TaskKind,
    TaskStatus,
    Trend,
)
from .geometry import AoiValidationError, aoi_area_km2, aoi_hash, canonical_aoi_geojson
from .licsar_frames import FrameMatch, find_frames, find_frames_for_point
from .messages import (
    AnalysisTaskMessage,
    DownloadTaskMessage,
    ProgressMessage,
    ResultMessage,
)
from .query import AoiPolygon, DateRange, QueryPayload, DEFAULT_LOOKBACK_MONTHS
from .result import Artifact, QualityBlock, ResultJson

__version__ = "0.3.0"

__all__ = [
    "AnalysisTaskMessage", "AoiPolygon", "AoiValidationError", "Artifact",
    "Confidence", "DateRange", "Depth", "DownloadTaskMessage", "DownloadTier",
    "DEFAULT_LOOKBACK_MONTHS", "FrameMatch", "HazardType", "Method",
    "ProgressMessage", "QualityBlock", "QueryPayload", "QueryStatus",
    "ResultJson", "ResultMessage", "ResultStatus", "TaskKind", "TaskStatus",
    "Trend",
    "aoi_area_km2", "aoi_hash", "canonical_aoi_geojson",
    "find_frames", "find_frames_for_point",
]
