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
    # M5.1: a definite terminal outcome with no measurements that is NOT an
    # error — the wrapper exits 0 and this is a legitimate answer, never
    # retried. See §6.3. Ordered before FAILED as a usability gradient.
    NO_DATA = "no_data"
    FAILED = "failed"


class NoDataReason(str, Enum):
    """M5.1: distinguishes the two meanings of NO_DATA (§6.3). Required iff
    status == no_data, null otherwise (enforced on ResultJson)."""
    # The method observed the AOI and there is genuinely nothing to report
    # (deformation over water/dense veg with no coherent scatterers after
    # inversion; a flood search that found no event/anomaly). A real answer.
    MEASURED_ABSENCE = "measured_absence"
    # The method could not observe the AOI in the window (no covering frame,
    # no IFGs in range, no low-cloud scenes, no acquisitions). A coverage gap.
    NO_COVERAGE = "no_coverage"


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
