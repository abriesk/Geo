"""Tests for the §6 contracts — the M0 acceptance gate for the package."""
import json
from datetime import date
from uuid import uuid4

import pytest
from pydantic import ValidationError

from geohazard_contracts import (
    AoiValidationError,
    QueryPayload,
    ResultJson,
    aoi_area_km2,
    aoi_hash,
    canonical_aoi_geojson,
)

# A small square near Yerevan (lon, lat), closed ring, clockwise on purpose.
RING_CW_CLOSED = [
    [44.50, 40.20],
    [44.50, 40.10],
    [44.60, 40.10],
    [44.60, 40.20],
    [44.50, 40.20],
]
RING_CCW_OPEN = [
    [44.50, 40.20],
    [44.60, 40.20],
    [44.60, 40.10],
    [44.50, 40.10],
]


class TestAoiHash:
    def test_winding_and_closure_invariance(self):
        """§6.2: hash must not depend on winding direction or ring closure."""
        assert aoi_hash(RING_CW_CLOSED) == aoi_hash(RING_CCW_OPEN)

    def test_rounding_to_4dp(self):
        """§6.2: coords rounded to 4 decimal places -> sub-11m jitter is identical."""
        jittered = [[lon + 0.00001, lat - 0.00001] for lon, lat in RING_CCW_OPEN]
        assert aoi_hash(jittered) == aoi_hash(RING_CCW_OPEN)

    def test_different_aoi_different_hash(self):
        shifted = [[lon + 1.0, lat] for lon, lat in RING_CCW_OPEN]
        assert aoi_hash(shifted) != aoi_hash(RING_CCW_OPEN)

    def test_canonical_form_is_open_ccw_exterior_only(self):
        c = canonical_aoi_geojson(RING_CW_CLOSED)
        ring = c["coordinates"][0]
        assert ring[0] != ring[-1], "closing point must be deduplicated"
        # CCW check via shoelace
        area = sum(
            ring[i][0] * ring[(i + 1) % len(ring)][1]
            - ring[(i + 1) % len(ring)][0] * ring[i][1]
            for i in range(len(ring))
        )
        assert area > 0, "canonical winding must be CCW (RFC 7946 RHR)"

    def test_deterministic_serialization(self):
        c = canonical_aoi_geojson(RING_CCW_OPEN)
        s1 = json.dumps(c, sort_keys=True, separators=(",", ":"))
        s2 = json.dumps(json.loads(s1), sort_keys=True, separators=(",", ":"))
        assert s1 == s2


class TestAoiValidation:
    def test_self_intersection_rejected(self):
        bowtie = [[0, 0], [1, 1], [1, 0], [0, 1]]
        with pytest.raises(AoiValidationError):
            aoi_hash(bowtie)

    def test_out_of_range_rejected(self):
        with pytest.raises(AoiValidationError):
            aoi_hash([[200, 0], [201, 0], [201, 1], [200, 1]])

    def test_area_estimate_sane(self):
        # 0.1 deg x 0.1 deg near lat 40 ~ 11.13 km x 8.53 km ~ 95 km2
        area = aoi_area_km2(RING_CCW_OPEN)
        assert 80 < area < 110


class TestQueryPayload:
    def _payload(self, **over):
        base = {
            "question": "is the ground moving here?",
            "aoi": {"type": "Polygon", "coordinates": [RING_CCW_OPEN]},
            "dates": {"start": "2024-06-01", "end": "2026-06-15"},
            "depth": "standard",
            "expert_raw": False,
        }
        base.update(over)
        return base

    def test_valid_payload(self):
        p = QueryPayload.model_validate(self._payload())
        assert p.depth.value == "standard"
        assert len(p.aoi.hash()) == 64

    def test_defaults(self):
        p = QueryPayload.model_validate(
            {"question": "flood?", "aoi": {"type": "Polygon", "coordinates": [RING_CCW_OPEN]}}
        )
        assert p.depth.value == "standard"
        assert p.expert_raw is False
        assert p.dates.start is None and p.dates.end is None

    def test_reversed_dates_rejected(self):
        with pytest.raises(ValidationError):
            QueryPayload.model_validate(
                self._payload(dates={"start": "2026-01-01", "end": "2024-01-01"})
            )

    def test_holes_rejected(self):
        hole = [[44.52, 40.12], [44.54, 40.12], [44.54, 40.14], [44.52, 40.14]]
        with pytest.raises(ValidationError):
            QueryPayload.model_validate(
                self._payload(aoi={"type": "Polygon", "coordinates": [RING_CCW_OPEN, hole]})
            )

    def test_unknown_fields_rejected(self):
        with pytest.raises(ValidationError):
            QueryPayload.model_validate(self._payload(bogus=1))


class TestResultJson:
    def _result(self, **over):
        base = {
            "query_id": str(uuid4()),
            "method": "licsbas",
            "status": "ok",
            "summary_stats": {
                "deformation": {
                    "velocity_mm_yr_min": -14.2,
                    "velocity_mm_yr_max": 3.1,
                    "velocity_mm_yr_mean_aoi": -4.7,
                    "hotspot_fraction": 0.12,
                    "trend": "subsiding",
                }
            },
            "quality": {
                "scene_count": 28,
                "date_coverage": ["2024-06-01", "2026-06-15"],
                "coherence_mean": 0.62,
                "masked_fraction": 0.31,
                "cloud_fraction": None,
                "confidence": "moderate",
                "caveats": ["sparse winter epochs"],
            },
            "artifacts": [
                {"type": "map_png", "path": "velocity_map.png", "caption": "LOS velocity"}
            ],
            "attribution": ["Contains modified Copernicus Sentinel data [2026]"],
        }
        base.update(over)
        return base

    def test_valid_result(self):
        r = ResultJson.model_validate(self._result())
        assert r.quality.confidence.value == "moderate"
        assert r.quality.date_coverage[0] == date(2024, 6, 1)

    def test_nested_stats_rejected(self):
        bad = self._result(summary_stats={"deformation": {"nested": {"x": 1}}})
        with pytest.raises(ValidationError):
            ResultJson.model_validate(bad)

    def test_bad_method_rejected(self):
        with pytest.raises(ValidationError):
            ResultJson.model_validate(self._result(method="magic"))
