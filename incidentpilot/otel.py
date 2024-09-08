"""Conversion between flat rows (the DuckDB schema) and OTLP/JSON-style records.

Every message on the bus is one record: a ``resource`` plus either a
``logRecord`` or a ``metric`` (gauge). Field names follow the OpenTelemetry
logs/metrics data model and semantic conventions.
"""
from __future__ import annotations

from typing import Any

LOG_COLUMNS = [
    "ts", "service", "host", "version", "level", "message", "trace_id",
    "http_status", "latency_ms", "client_ip", "user", "peer", "event", "path",
]
METRIC_COLUMNS = ["ts", "service", "host", "name", "value"]

SEVERITY = {"DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17}

# flat column -> OTel attribute key
_ATTRS = {
    "http_status": ("http.response.status_code", "int"),
    "latency_ms": ("http.server.request.duration_ms", "double"),
    "client_ip": ("client.address", "str"),
    "user": ("enduser.id", "str"),
    "peer": ("peer.service", "str"),
    "event": ("event.name", "str"),
    "path": ("url.path", "str"),
}
_ATTR_TO_COL = {v[0]: k for k, v in _ATTRS.items()}


def _kv(key: str, value: Any, kind: str) -> dict:
    field = {"str": "stringValue", "int": "intValue", "double": "doubleValue"}[kind]
    return {"key": key, "value": {field: str(value) if kind == "int" else value}}


def _unkv(item: dict) -> Any:
    v = item["value"]
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        return int(v["intValue"])
    return v["doubleValue"]


def _resource(row: dict) -> dict:
    attrs = [_kv("service.name", row["service"], "str"), _kv("host.name", row["host"], "str")]
    if row.get("version"):
        attrs.append(_kv("service.version", row["version"], "str"))
    return {"attributes": attrs}


def _read_resource(res: dict) -> dict:
    m = {a["key"]: _unkv(a) for a in res.get("attributes", [])}
    return {
        "service": m.get("service.name", "unknown"),
        "host": m.get("host.name", "unknown"),
        "version": m.get("service.version"),
    }


def log_to_otlp(row: dict) -> dict:
    attrs = []
    for col, (key, kind) in _ATTRS.items():
        if row.get(col) is not None:
            attrs.append(_kv(key, row[col], kind))
    rec = {
        "timeUnixNano": str(int(round(row["ts"] * 1e9))),
        "severityText": row["level"],
        "severityNumber": SEVERITY[row["level"]],
        "body": {"stringValue": row["message"]},
        "attributes": attrs,
    }
    if row.get("trace_id"):
        rec["traceId"] = row["trace_id"]
    return {"resource": _resource(row), "logRecord": rec}


def metric_to_otlp(row: dict) -> dict:
    point = {"timeUnixNano": str(int(round(row["ts"] * 1e9))), "asDouble": row["value"]}
    return {
        "resource": _resource(row),
        "metric": {"name": row["name"], "gauge": {"dataPoints": [point]}},
    }


def parse_record(rec: dict) -> tuple[str, list[dict]]:
    """Return ("log"|"metric", rows). Raises ValueError for malformed records."""
    try:
        base = _read_resource(rec["resource"])
        if "logRecord" in rec:
            lr = rec["logRecord"]
            row = {c: None for c in LOG_COLUMNS}
            row.update(base)
            row["ts"] = int(lr["timeUnixNano"]) / 1e9
            row["level"] = str(lr.get("severityText", "INFO")).upper()
            row["message"] = lr["body"]["stringValue"]
            row["trace_id"] = lr.get("traceId")
            for item in lr.get("attributes", []):
                col = _ATTR_TO_COL.get(item["key"])
                if col:
                    row[col] = _unkv(item)
            return "log", [row]
        if "metric" in rec:
            m = rec["metric"]
            rows = []
            for p in m["gauge"]["dataPoints"]:
                rows.append({
                    "ts": int(p["timeUnixNano"]) / 1e9,
                    "service": base["service"],
                    "host": base["host"],
                    "name": m["name"],
                    "value": float(p["asDouble"]),
                })
            return "metric", rows
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"malformed OTLP record: {exc!r}") from exc
    raise ValueError("record has neither logRecord nor metric")
