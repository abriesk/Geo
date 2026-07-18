"""Enumerations shared across all geohazard-chat contracts.

Source of truth: Technical Reference v2, §5.5 and §6.
Any change here is a breaking change (§6 preamble).
"""
from enum import Enum


class HazardType(str, Enum):
    DEFORMATION = "deformation"
    FLOOD = "flood"
    VEGETATION = "vegetation"


class Depth(str, Enum):
    QUICK = "quick"
    STANDARD = "standard"
    THOROUGH = "thorough"


class QueryStatus(str, Enum):
    RECEIVED = "received"
    ROUTING = "routing"
    DOWNLOADING = "downloading"
    ANALYZING = "analyzing"
    SUMMARIZING = "summarizing"
    DONE = "done"
    FAILED = "failed"
    NEEDS_CLARIFICATION = "needs_clarification"


class TaskKind(str, Enum):
    DOWNLOAD = "download"
    ANALYSIS = "analysis"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Method(str, Enum):
    EGMS = "egms"
    LICSBAS = "licsbas"
    MINTPY = "mintpy"
    FLOODPY = "floodpy"
    NDVI = "ndvi"


class ResultStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class DownloadTier(str, Enum):
    EGMS = "egms"
    LICSAR = "licsar"
    HYP3 = "hyp3"
    CDSE = "cdse"
    AUX = "aux"


class Confidence(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class Trend(str, Enum):
    SUBSIDING = "subsiding"
    UPLIFTING = "uplifting"
    STABLE = "stable"
    MIXED = "mixed"
