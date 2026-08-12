"""Shared display-only score buckets for dashboard analytics."""

from __future__ import annotations

import math


DASHBOARD_SCORE_BANDS = (
    ("0-9", 0.0, 10.0),
    ("10-19", 10.0, 20.0),
    ("20-29", 20.0, 30.0),
    ("30-39", 30.0, 40.0),
    ("40-49", 40.0, 50.0),
    ("50-59", 50.0, 60.0),
    ("60-69", 60.0, 70.0),
    ("70-79", 70.0, 80.0),
    ("80-89", 80.0, 90.0),
    ("90-100", 90.0, 101.0),
)
DASHBOARD_SCORE_BUCKETS = tuple(label for label, _lower, _upper in DASHBOARD_SCORE_BANDS)


def dashboard_score_bucket(score) -> str | None:
    try:
        value = float(score)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    for label, lower, upper in DASHBOARD_SCORE_BANDS:
        if lower <= value < upper:
            return label
    return None
