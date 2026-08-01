"""result.json: every wrapper -> backend (§6.3).

Agreed deviation from the doc's illustrative example: summary_stats keys are
the *hazard/stat-group names without any "example_" prefix* (e.g.
"deformation", "flood", "ndvi"). Keys are method-specific but values stay
flat: numbers, strings, or null only — the LLM never sees rasters, only this
JSON plus artifact captions.
"""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import Confidence, Method, NoDataReason, ResultStatus

# Flat scalar values only — no nested objects/arrays inside a stats group.
StatValue = Union[float, int, str, None]


class QualityBlock(BaseModel):
    """Feeds the LLM's confidence language (§5.4, §8.3)."""

    model_config = ConfigDict(extra="forbid")

    scene_count: int = Field(..., ge=0)
    date_coverage: List[date] = Field(..., min_length=2, max_length=2)
    coherence_mean: Optional[float] = Field(None, ge=0.0, le=1.0)
    masked_fraction: Optional[float] = Field(None, ge=0.0, le=1.0)
    cloud_fraction: Optional[float] = Field(None, ge=0.0, le=1.0)
    confidence: Confidence
    caveats: List[str] = Field(
        default_factory=list,
        description="Machine-generated; passed to the LLM verbatim (§6.3)",
    )


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., description='e.g. "map_png", "timeseries_png"')
    path: str = Field(..., description="Relative to the wrapper's --output-dir")
    caption: str


class ResultJson(BaseModel):
    """Schema for result.json emitted by every wrapper (§6.3)."""

    model_config = ConfigDict(extra="forbid")

    query_id: UUID
    method: Method
    status: ResultStatus
    # M5.1 (§6.3): required iff status == no_data, null otherwise. Coupling is
    # enforced by _no_data_reason_coupling below.
    no_data_reason: Optional[NoDataReason] = None
    summary_stats: Dict[str, Dict[str, StatValue]] = Field(
        default_factory=dict,
        description="Per-group flat numeric/string stats, e.g. "
        '{"deformation": {"velocity_mm_yr_mean_aoi": -4.7, "trend": "subsiding"}}',
    )
    quality: QualityBlock
    artifacts: List[Artifact] = Field(default_factory=list)
    attribution: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_data_reason_coupling(self) -> "ResultJson":
        """M5.1 (§6.3): no_data_reason present iff status == no_data."""
        if self.status == ResultStatus.NO_DATA:
            if self.no_data_reason is None:
                raise ValueError(
                    "no_data_reason is required when status == no_data (§6.3)"
                )
        elif self.no_data_reason is not None:
            raise ValueError(
                "no_data_reason must be null unless status == no_data (§6.3)"
            )
        return self

    @field_validator("summary_stats")
    @classmethod
    def _flat_scalars_only(
        cls, v: Dict[str, Dict[str, StatValue]]
    ) -> Dict[str, Dict[str, StatValue]]:
        for group, stats in v.items():
            for key, value in stats.items():
                if isinstance(value, (dict, list)):
                    raise ValueError(
                        f"summary_stats.{group}.{key} must be a flat scalar (§6.3)"
                    )
        return v
