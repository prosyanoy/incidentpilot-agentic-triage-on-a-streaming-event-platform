"""DuckDB-backed log and metric store."""
from __future__ import annotations

import threading
from typing import Any, Iterable

import duckdb

from .otel import LOG_COLUMNS, METRIC_COLUMNS

_LOG_TYPES = {
    "ts": "DOUBLE", "http_status": "INTEGER", "latency_ms": "DOUBLE",
}
_METRIC_TYPES = {"ts": "DOUBLE", "value": "DOUBLE"}
NUMERIC_COLUMNS = {"ts", "http_status", "latency_ms", "value"}


def _ddl(table: str, cols: list[str], types: dict[str, str]) -> str:
    body = ", ".join(f'"{c}" {types.get(c, "VARCHAR")}' for c in cols)
    return f"CREATE TABLE IF NOT EXISTS {table} ({body})"


class Store:
    def __init__(self, path: str = ":memory:"):
        self._con = duckdb.connect(path)
        self._lock = threading.RLock()
        with self._lock:
            self._con.execute(_ddl("logs", LOG_COLUMNS, _LOG_TYPES))
            self._con.execute(_ddl("metrics", METRIC_COLUMNS, _METRIC_TYPES))

    def _insert(self, table: str, cols: list[str], types: dict[str, str], rows: Iterable[dict]) -> int:
        """Bulk insert by binding one list per column and UNNESTing it (far faster than per-row binds)."""
        rows = list(rows)
        if not rows:
            return 0
        quoted = ", ".join(f'"{c}"' for c in cols)
        select = ", ".join(f"unnest(?::{types.get(c, 'VARCHAR')}[])" for c in cols)
        with self._lock:
            self._con.execute(f"INSERT INTO {table} ({quoted}) SELECT {select}",
                              [[r.get(c) for r in rows] for c in cols])
        return len(rows)

    def insert_logs(self, rows: Iterable[dict]) -> int:
        return self._insert("logs", LOG_COLUMNS, _LOG_TYPES, rows)

    def insert_metrics(self, rows: Iterable[dict]) -> int:
        return self._insert("metrics", METRIC_COLUMNS, _METRIC_TYPES, rows)

    def execute(self, sql: str, params: list | None = None) -> tuple[list[str], list[list]]:
        with self._lock:
            cur = self._con.execute(sql, params or [])
            cols = [d[0] for d in cur.description]
            return cols, [list(r) for r in cur.fetchall()]

    def max_ts(self, table: str = "logs") -> float | None:
        _, rows = self.execute(f"SELECT max(ts) FROM {table}")
        return rows[0][0]

    def count(self, table: str = "logs") -> int:
        return self.execute(f"SELECT count(*) FROM {table}")[1][0][0]

    def close(self) -> None:
        with self._lock:
            self._con.close()
