"""Queue message contracts (§6.4): tasks / progress / results."""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .enums import DownloadTier
from .query import AoiPolygon, DateRange


class DownloadTaskMessage(BaseModel):
    """tasks queue, kind=download (§6.4)."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    query_id: UUID
    kind: Literal["download"] = "download"
    tier: DownloadTier
    aoi: AoiPolygon
    dates: DateRange
    products: List[str] = Field(default_factory=list)


class AnalysisTaskMessage(BaseModel):
    """tasks queue, kind=analysis (§6.4)."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    query_id: UUID
    kind: Literal["analysis"] = "analysis"
    name: str = Field(..., description='wrapper name, e.g. "wrap_licsbas"')
    input_dir: str
    output_dir: str
    aoi: AoiPolygon
    dates: DateRange
    params: dict = Field(default_factory=dict)


class ProgressMessage(BaseModel):
    """progress queue (§6.4). Wrappers emit 'PROGRESS <int> <message>' lines
    on stdout; the worker relays them here (§5.4)."""

    model_config = ConfigDict(extra="forbid")

    query_id: UUID
    task_id: Optional[UUID] = None
    message: str
    percent: int = Field(..., ge=0, le=100)
    ts: datetime


class ResultMessage(BaseModel):
    """results queue (§6.4)."""

    model_config = ConfigDict(extra="forbid")

    query_id: UUID
    task_id: UUID
    status: Literal["done", "failed"]
    result_json_path: Optional[str] = None
    error: Optional[str] = None
