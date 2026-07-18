"""Query payload: frontend -> backend (§6.1)."""
from __future__ import annotations

from datetime import date
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import Depth
from .geometry import AoiValidationError, aoi_hash, canonical_aoi_geojson

# Default lookback windows when dates are null (§6.1):
# deformation 24 months, flood 3 months, NDVI 12 months.
DEFAULT_LOOKBACK_MONTHS = {"deformation": 24, "flood": 3, "vegetation": 12}


class AoiPolygon(BaseModel):
    """GeoJSON Polygon, EPSG:4326, lon-lat, RHR winding (§6.1)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["Polygon"]
    coordinates: List[List[List[float]]] = Field(
        ..., description="[[ [lon, lat], ... ]] — exterior ring; holes are ignored/rejected"
    )

    @field_validator("coordinates")
    @classmethod
    def _validate_rings(cls, v: List[List[List[float]]]) -> List[List[List[float]]]:
        if len(v) == 0:
            raise ValueError("Polygon has no rings")
        if len(v) > 1:
            raise ValueError("Only exterior ring is supported (no holes) — §6.2 canonical form")
        try:
            # Validates ranges / self-intersection and normalizes winding.
            canonical = canonical_aoi_geojson(v[0])
        except AoiValidationError as e:
            raise ValueError(str(e)) from e
        # Store the *original precision* coords but with normalized winding &
        # validated ring; canonical rounding applies only to the hash (§6.2).
        return v

    def hash(self) -> str:
        return aoi_hash(self.coordinates[0])


class DateRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: Optional[date] = None
    end: Optional[date] = None

    @model_validator(mode="after")
    def _order(self) -> "DateRange":
        if self.start and self.end and self.start > self.end:
            raise ValueError("dates.start must be <= dates.end")
        return self


class QueryPayload(BaseModel):
    """POST /query body (§6.1)."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., min_length=1)
    aoi: AoiPolygon
    dates: DateRange = Field(default_factory=DateRange)
    depth: Depth = Depth.STANDARD
    expert_raw: bool = False
