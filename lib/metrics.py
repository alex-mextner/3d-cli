"""Compatibility wrapper for registries.metrics."""
from __future__ import annotations

from registries.metrics import (
    JSONValue,
    MetricFileSummary,
    MetricRecord,
    append_record,
    list_metric_files,
    metrics_dir,
    read_records,
)

__all__ = [
    "JSONValue",
    "MetricFileSummary",
    "MetricRecord",
    "append_record",
    "list_metric_files",
    "metrics_dir",
    "read_records",
]
