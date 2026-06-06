from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Sequence

from errors import UsageError

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_KEY_VALUE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_.:-]*)=(.+?)\s*$")
_MARKER_RE = re.compile(
    r"^\s*>>>\s*([A-Z][A-Z0-9 _/-]*):\s*(PASS|FAIL|SKIP|WARN)\b\s*(.*?)\s*$"
)
_RESULT_RE = re.compile(r"^\s*(RESULT|STATUS):\s*(PASS|FAIL|SKIP|WARN)\b\s*(.*?)\s*$")
_GATE_ROW_RE = re.compile(
    r"^\s*([A-Z][A-Z0-9 _/-]{1,}?)\s+(PASS|FAIL|SKIP|WARN|----)(?:\b|\s)\s*(.*?)\s*$"
)


@dataclass(frozen=True)
class ArtifactSummary:
    path: str
    kind: str
    records: int


@dataclass(frozen=True)
class GateSummary:
    name: str
    status: str
    source: str
    detail: str = ""


@dataclass(frozen=True)
class ReportValue:
    name: str
    value: int | float | str | bool | None
    source: str


@dataclass(frozen=True)
class ReportSummary:
    title: str
    overall: str
    artifacts: tuple[ArtifactSummary, ...]
    gates: tuple[GateSummary, ...]
    values: tuple[ReportValue, ...]


def build_report(paths: Sequence[str], *, title: str = "3d report") -> ReportSummary:
    """Build a deterministic summary from existing logs/JSON artifacts.

    This function intentionally only reads files. It does not render, mesh-check,
    invoke OpenSCAD, or import any heavy dependency.
    """
    artifacts: list[ArtifactSummary] = []
    gates: list[GateSummary] = []
    values: list[ReportValue] = []

    for path in sorted(paths):
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise UsageError(f"could not read artifact {path}: {exc}", command="report") from exc
        kind, records, found_gates, found_values = _parse_artifact(path, text)
        artifacts.append(ArtifactSummary(path=path, kind=kind, records=records))
        gates.extend(found_gates)
        values.extend(found_values)

    values.sort(key=lambda v: (v.source, v.name))
    return ReportSummary(
        title=title,
        overall=_overall(gates),
        artifacts=tuple(artifacts),
        gates=tuple(gates),
        values=tuple(values),
    )


def report_to_json_data(report: ReportSummary) -> dict[str, Any]:
    return {
        "title": report.title,
        "overall": report.overall,
        "artifacts": [
            {"path": a.path, "kind": a.kind, "records": a.records}
            for a in report.artifacts
        ],
        "gates": [
            {"name": g.name, "status": g.status, "source": g.source, "detail": g.detail}
            for g in report.gates
        ],
        "values": [
            {"name": v.name, "value": v.value, "source": v.source}
            for v in report.values
        ],
    }


def render_json(report: ReportSummary) -> str:
    return json.dumps(report_to_json_data(report), indent=2, sort_keys=True) + "\n"


def render_text(report: ReportSummary) -> str:
    lines = [report.title, f"Overall: {report.overall}"]
    if report.gates:
        lines.extend(["", "Gates:"])
        for gate in report.gates:
            detail = f" - {gate.detail}" if gate.detail else ""
            lines.append(f"- {gate.name} {gate.status} ({gate.source}){detail}")
    if report.values:
        lines.extend(["", "Values:"])
        for value in report.values:
            lines.append(f"- {value.name}={_format_value(value.value)} ({value.source})")
    return "\n".join(lines) + "\n"


def _parse_artifact(path: str, text: str) -> tuple[str, int, list[GateSummary], list[ReportValue]]:
    stripped = text.lstrip()
    if path.endswith(".jsonl"):
        return _parse_jsonl(path, text)
    if path.endswith(".json"):
        record = _loads_json(path, stripped)
        gates, values = _parse_record(path, record)
        return "json", 1, gates, values
    return _parse_text(path, text)


def _parse_jsonl(path: str, text: str) -> tuple[str, int, list[GateSummary], list[ReportValue]]:
    gates: list[GateSummary] = []
    values: list[ReportValue] = []
    records = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise UsageError(f"{path}:{lineno}: invalid JSONL: {exc.msg}", command="report") from exc
        records += 1
        found_gates, found_values = _parse_record(path, record)
        gates.extend(found_gates)
        values.extend(found_values)
    values.sort(key=lambda v: v.name)
    return "jsonl", records, gates, values


def _parse_text(path: str, text: str) -> tuple[str, int, list[GateSummary], list[ReportValue]]:
    gates: list[GateSummary] = []
    values: list[ReportValue] = []
    lines = text.splitlines()
    for line in lines:
        clean = _strip_ansi(line)
        marker = _MARKER_RE.match(clean) or _RESULT_RE.match(clean)
        if marker:
            gates.append(_gate(path, marker.group(1), marker.group(2), marker.group(3)))
            continue
        row = _GATE_ROW_RE.match(clean)
        if row:
            gates.append(_gate(path, row.group(1), row.group(2), row.group(3)))
            continue
        kv = _KEY_VALUE_RE.match(clean)
        if kv:
            values.append(ReportValue(kv.group(1), _parse_scalar(kv.group(2)), path))
    return "text", len(lines), gates, values


def _loads_json(path: str, text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"{path}: invalid JSON: {exc.msg}", command="report") from exc


def _parse_record(path: str, record: Any) -> tuple[list[GateSummary], list[ReportValue]]:
    gates: list[GateSummary] = []
    values: list[ReportValue] = []
    if not isinstance(record, dict):
        return gates, values

    raw_gates = record.get("gates")
    if isinstance(raw_gates, list):
        for item in raw_gates:
            if isinstance(item, dict):
                gate = _gate_from_mapping(path, item)
                if gate is not None:
                    gates.append(gate)

    gate = _gate_from_mapping(path, record)
    if gate is not None:
        gates.append(gate)

    raw_metrics = record.get("metrics")
    if isinstance(raw_metrics, dict):
        for name in sorted(raw_metrics):
            if _is_scalar(raw_metrics[name]):
                values.append(ReportValue(str(name), raw_metrics[name], path))
    raw_values = record.get("values")
    if isinstance(raw_values, dict):
        for name in sorted(raw_values):
            if _is_scalar(raw_values[name]):
                values.append(ReportValue(str(name), raw_values[name], path))
    elif isinstance(raw_values, list):
        for item in raw_values:
            value = _value_from_mapping(path, item)
            if value is not None:
                values.append(value)
    if isinstance(record.get("metric"), str) and _is_scalar(record.get("value")):
        values.append(ReportValue(record["metric"], record["value"], path))
    return gates, values


def _value_from_mapping(path: str, data: object) -> ReportValue | None:
    if not isinstance(data, dict):
        return None
    raw_name = data.get("name", data.get("metric"))
    if not isinstance(raw_name, str) or not raw_name.strip():
        return None
    value = data.get("value")
    if not _is_scalar(value):
        return None
    return ReportValue(raw_name, value, path)


def _gate_from_mapping(path: str, data: dict[str, Any]) -> GateSummary | None:
    raw_status = data.get("status", data.get("verdict"))
    if not isinstance(raw_status, str):
        return None
    status = _status(raw_status)
    if status not in {"PASS", "FAIL", "SKIP", "WARN", "INFO"}:
        return None
    raw_name = data.get("name", data.get("gate", data.get("command")))
    if not isinstance(raw_name, str) or not raw_name.strip():
        return None
    detail = data.get("detail", data.get("message", ""))
    return _gate(path, raw_name, status, str(detail) if detail is not None else "")


def _gate(path: str, name: str, status: str, detail: str) -> GateSummary:
    return GateSummary(
        name=" ".join(name.strip().upper().split()),
        status=_status(status),
        source=path,
        detail=detail.strip(),
    )


def _status(status: str) -> str:
    normalized = status.strip().upper()
    if normalized == "----":
        return "INFO"
    return normalized


def _overall(gates: Sequence[GateSummary]) -> str:
    statuses = {gate.status for gate in gates}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    if "PASS" in statuses:
        return "PASS"
    if statuses:
        return "INFO"
    return "UNKNOWN"


def _parse_scalar(value: str) -> int | float | str | bool | None:
    v = value.strip()
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    if v.lower() in {"none", "null"}:
        return None
    if re.fullmatch(r"[+-]?\d+", v):
        return int(v)
    if re.fullmatch(r"[+-]?(\d+\.\d*|\d*\.\d+)([eE][+-]?\d+)?|[+-]?\d+[eE][+-]?\d+", v):
        return float(v)
    return v


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _format_value(value: int | float | str | bool | None) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)
