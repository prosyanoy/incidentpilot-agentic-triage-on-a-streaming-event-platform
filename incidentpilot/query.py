"""A small SPL-like query language compiled to DuckDB SQL.

    search [source=metrics] <field><op><value> ... "free text" [earliest=..] [latest=..]
      | where <cond> | stats <fn>(<field>) [as <alias>] ... [by f1,f2]
      | timechart [span=30s] [<fn>(<field>)] [by <field>]
      | top [N] <field> | sort [-]f ... | head N | fields a,b

Only identifiers from the known schema (or a previous stage's output) are ever
interpolated into SQL; every literal is bound as a parameter.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .otel import LOG_COLUMNS, METRIC_COLUMNS
from .store import NUMERIC_COLUMNS, Store

MAX_ROWS = 500
_OPS = {"=": "=", "!=": "!=", ">": ">", "<": "<", ">=": ">=", "<=": "<="}
_COND = re.compile(r"^([A-Za-z_][\w.]*?)(!=|>=|<=|=|>|<)(.*)$", re.S)
_AGG = re.compile(r"^(count|dc|avg|min|max|sum|median|p50|p90|p95|p99)(?:\(([A-Za-z_][\w.]*)\))?$")
_SPAN = re.compile(r"^(\d+)(s|m|h)$")
_REL = re.compile(r"^-(\d+)(s|m|h)$")
_UNIT = {"s": 1, "m": 60, "h": 3600}
_QUANT = {"median": 0.5, "p50": 0.5, "p90": 0.9, "p95": 0.95, "p99": 0.99}


class QueryError(ValueError):
    """Raised for malformed or unsafe queries."""


@dataclass
class QueryResult:
    query: str
    sql: str
    columns: list[str]
    rows: list[list]
    truncated: bool = False

    def to_text(self, max_rows: int = 20) -> str:
        def fmt(v):
            return f"{v:.2f}" if isinstance(v, float) else str(v)
        lines = [" | ".join(self.columns)]
        lines += [" | ".join(fmt(v) for v in r) for r in self.rows[:max_rows]]
        if len(self.rows) > max_rows:
            lines.append(f"... ({len(self.rows) - max_rows} more rows)")
        return "\n".join(lines)

    def as_dicts(self) -> list[dict]:
        return [dict(zip(self.columns, r)) for r in self.rows]


def _split_pipes(q: str) -> list[str]:
    parts, cur, quote = [], [], None
    for ch in q:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch == "|":
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if quote:
        raise QueryError("unterminated quote")
    parts.append("".join(cur).strip())
    return parts


@dataclass
class _Tok:
    text: str
    bare: bool  # a quoted phrase => free-text search


def _tokenize(s: str) -> list[_Tok]:
    toks, cur, quote, started_quoted, had = [], [], None, False, False
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
            else:
                cur.append(ch)
        elif ch in "\"'":
            if not cur:
                started_quoted = True
            quote, had = ch, True
        elif ch.isspace() or ch == ",":
            if ch == "," and cur and not had and _COND.match("".join(cur)):
                cur.append(ch)  # comma inside a value list: level=ERROR,WARN
            elif ch == "," and cur and had:
                cur.append(ch)
            elif cur or had:
                toks.append(_Tok("".join(cur), started_quoted))
                cur, started_quoted, had = [], False, False
        else:
            cur.append(ch)
    if quote:
        raise QueryError("unterminated quote")
    if cur or had:
        toks.append(_Tok("".join(cur), started_quoted))
    return toks


def _parse_span(v: str) -> int:
    m = _SPAN.match(v)
    if not m:
        raise QueryError(f"bad span {v!r}; use e.g. 30s, 1m, 1h")
    n = int(m.group(1)) * _UNIT[m.group(2)]
    if n <= 0:
        raise QueryError("span must be positive")
    return n


@dataclass
class _State:
    cols: list[str]
    ctes: list[str] = field(default_factory=list)
    params: list = field(default_factory=list)
    order: str = ""  # pending ORDER BY, applied by the next limit / at the end

    @property
    def cur(self) -> str:
        return f"s{len(self.ctes) - 1}"

    def push(self, select: str, params: list, cols: list[str]) -> None:
        self.ctes.append(f"s{len(self.ctes)} AS ({select})")
        self.params += params
        self.cols = cols
        self.order = ""


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _check_col(name: str, cols: list[str]) -> str:
    if name not in cols:
        raise QueryError(f"unknown field {name!r}; available: {', '.join(cols)}")
    return _q(name)


def _condition(tok: _Tok, cols: list[str]) -> tuple[str, list]:
    if tok.bare:
        return "message ILIKE ?" if "message" in cols else "FALSE", [f"%{tok.text}%"]
    m = _COND.match(tok.text)
    if not m:
        if "message" not in cols:
            raise QueryError(f"cannot parse condition {tok.text!r}")
        return "message ILIKE ?", [f"%{tok.text}%"]
    name, op, raw = m.groups()
    col = _check_col(name, cols)
    numeric = name in NUMERIC_COLUMNS or op in (">", "<", ">=", "<=")
    if name == "level":
        raw = raw.upper()

    def val(x: str):
        if numeric:
            try:
                return float(x)
            except ValueError:
                raise QueryError(f"field {name!r} needs a number, got {x!r}")
        return x

    if op in ("=", "!=") and "," in raw:
        vals = [val(x) for x in raw.split(",") if x]
        neg = "NOT " if op == "!=" else ""
        return f"{col} {neg}IN ({', '.join('?' * len(vals))})", vals
    if op in ("=", "!=") and "*" in raw:
        neg = "NOT " if op == "!=" else ""
        return f"{col} {neg}ILIKE ?", [raw.replace("*", "%")]
    return f"{col} {_OPS[op]} ?", [val(raw)]


def _resolve_time(v: str, store: Store, table: str) -> float:
    m = _REL.match(v)
    if m:
        anchor = store.max_ts(table)
        if anchor is None:
            raise QueryError("relative time used on an empty store")
        return anchor - int(m.group(1)) * _UNIT[m.group(2)]
    try:
        return float(v)
    except ValueError:
        raise QueryError(f"bad time {v!r}; use epoch seconds or -5m")


def _search(args: list[_Tok], store: Store) -> _State:
    table = "logs"
    for t in args:
        if t.text.lower() in ("source=metrics", "source=logs") and not t.bare:
            table = t.text.split("=")[1].lower()
    cols = LOG_COLUMNS if table == "logs" else METRIC_COLUMNS
    where, params = [], []
    for t in args:
        if not t.bare and t.text.lower().startswith("source="):
            continue
        m = _COND.match(t.text) if not t.bare else None
        if m and m.group(1) in ("earliest", "latest") and m.group(2) == "=":
            ts = _resolve_time(m.group(3), store, table)
            where.append("ts >= ?" if m.group(1) == "earliest" else "ts <= ?")
            params.append(ts)
            continue
        cond, p = _condition(t, cols)
        where.append(cond)
        params += p
    st = _State(list(cols))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    st.push(f"SELECT * FROM {table}{clause}", params, list(cols))
    st.order = "ts"
    return st


def _stats(args: list[_Tok], st: _State) -> None:
    texts = [t.text for t in args]
    by: list[str] = []
    if "by" in [x.lower() for x in texts]:
        i = [x.lower() for x in texts].index("by")
        by = [x for x in texts[i + 1:] if x]
        texts = texts[:i]
    aggs, i = [], 0
    while i < len(texts):
        m = _AGG.match(texts[i])
        if not m:
            raise QueryError(f"bad aggregation {texts[i]!r}")
        fn, fld = m.groups()
        alias = f"{fn}({fld})" if fld else fn
        if i + 2 < len(texts) and texts[i + 1].lower() == "as":
            alias = texts[i + 2]
            i += 2
        i += 1
        if fn == "count":
            expr = f"count({_check_col(fld, st.cols)})" if fld else "count(*)"
        else:
            if not fld:
                raise QueryError(f"{fn} needs a field, e.g. {fn}(latency_ms)")
            c = _check_col(fld, st.cols)
            expr = {"dc": f"count(DISTINCT {c})", "avg": f"avg({c})", "min": f"min({c})",
                    "max": f"max({c})", "sum": f"sum({c})"}.get(fn) or \
                f"quantile_cont({c}, {_QUANT[fn]})"
        aggs.append((expr, alias))
    if not aggs:
        aggs = [("count(*)", "count")]
    bycols = [_check_col(b, st.cols) for b in by]
    sel = ", ".join(bycols + [f"{e} AS {_q(a)}" for e, a in aggs])
    grp = f" GROUP BY {', '.join(bycols)}" if bycols else ""
    st.push(f"SELECT {sel} FROM {st.cur}{grp}", [], by + [a for _, a in aggs])


def _timechart(args: list[_Tok], st: _State) -> None:
    if "ts" not in st.cols:
        raise QueryError("timechart needs the ts field; use it right after search")
    span, by, agg = 60, None, ("count(*)", "count")
    texts = [t.text for t in args]
    i = 0
    while i < len(texts):
        x = texts[i]
        if x.lower().startswith("span="):
            span = _parse_span(x[5:])
        elif x.lower() == "by":
            if i + 1 >= len(texts):
                raise QueryError("by needs a field")
            by = texts[i + 1]
            i += 1
        else:
            m = _AGG.match(x)
            if not m:
                raise QueryError(f"bad timechart argument {x!r}")
            fn, fld = m.groups()
            if fn == "count":
                agg = ("count(*)", "count")
            else:
                if not fld:
                    raise QueryError(f"{fn} needs a field")
                c = _check_col(fld, st.cols)
                agg = ({"dc": f"count(DISTINCT {c})", "avg": f"avg({c})", "min": f"min({c})",
                        "max": f"max({c})", "sum": f"sum({c})"}.get(fn)
                       or f"quantile_cont({c}, {_QUANT[fn]})", f"{fn}({fld})")
        i += 1
    tcol = f"CAST(floor(ts / {span}) * {span} AS BIGINT) AS _time"
    if by:
        b = _check_col(by, st.cols)
        st.push(f"SELECT {tcol}, {b}, {agg[0]} AS {_q(agg[1])} FROM {st.cur} "
                f"GROUP BY 1, 2 ORDER BY 1, 2", [], ["_time", by, agg[1]])
    else:
        st.push(f"SELECT {tcol}, {agg[0]} AS {_q(agg[1])} FROM {st.cur} "
                f"GROUP BY 1 ORDER BY 1", [], ["_time", agg[1]])
    st.order = "_time"


def _top(args: list[_Tok], st: _State) -> None:
    limit, fld = 10, None
    for t in args:
        if t.text.isdigit():
            limit = int(t.text)
        elif t.text.startswith("limit="):
            limit = int(t.text[6:])
        elif fld is None:
            fld = t.text
    if fld is None:
        raise QueryError("top needs a field")
    c = _check_col(fld, st.cols)
    st.push(f"SELECT {c}, count(*) AS count, round(100.0 * count(*) / sum(count(*)) OVER (), 2) "
            f"AS percent FROM {st.cur} GROUP BY {c} ORDER BY count DESC, {c} LIMIT {limit}",
            [], [fld, "count", "percent"])
    st.order = "count DESC"


def _sort(args: list[_Tok], st: _State) -> None:
    parts = []
    for t in args:
        name, desc = (t.text[1:], True) if t.text.startswith("-") else (t.text.lstrip("+"), False)
        parts.append(f"{_check_col(name, st.cols)} {'DESC' if desc else 'ASC'}")
    if not parts:
        raise QueryError("sort needs at least one field")
    st.order = ", ".join(parts)


def _head(args: list[_Tok], st: _State) -> None:
    n = int(args[0].text) if args and args[0].text.isdigit() else 10
    order = f" ORDER BY {st.order}" if st.order else ""
    cols = st.cols
    st.push(f"SELECT * FROM {st.cur}{order} LIMIT {n}", [], cols)
    st.order = ""


def _where(args: list[_Tok], st: _State) -> None:
    conds, params = [], []
    toks = [t for t in args if t.text.lower() != "and"]
    if not toks:
        raise QueryError("where needs a condition")
    for t in toks:
        c, p = _condition(_Tok(t.text, False), st.cols)
        conds.append(c)
        params += p
    order, cols = st.order, st.cols
    st.push(f"SELECT * FROM {st.cur} WHERE {' AND '.join(conds)}", params, cols)
    st.order = order


def _fields(args: list[_Tok], st: _State) -> None:
    names = [t.text for t in args if t.text]
    if not names:
        raise QueryError("fields needs at least one field")
    sel = ", ".join(_check_col(n, st.cols) for n in names)
    order = st.order if st.order in names else ""
    st.push(f"SELECT {sel} FROM {st.cur}", [], names)
    st.order = order


def compile_query(query: str, store: Store) -> tuple[str, list]:
    stages = _split_pipes(query.strip())
    if not stages or not stages[0]:
        raise QueryError("empty query")
    first = _tokenize(stages[0])
    if not first or first[0].text.lower() != "search" or first[0].bare:
        raise QueryError("a query must start with 'search'")
    st = _search(first[1:], store)
    handlers = {"stats": _stats, "timechart": _timechart, "top": _top, "sort": _sort,
                "head": _head, "where": _where, "fields": _fields, "table": _fields}
    for stage in stages[1:]:
        toks = _tokenize(stage)
        if not toks:
            raise QueryError("empty pipeline stage")
        cmd = toks[0].text.lower()
        if cmd not in handlers:
            raise QueryError(f"unknown command {cmd!r}; supported: search, {', '.join(handlers)}")
        handlers[cmd](toks[1:], st)
    order = f" ORDER BY {st.order}" if st.order else ""
    sql = f"WITH {', '.join(st.ctes)} SELECT * FROM {st.cur}{order} LIMIT {MAX_ROWS + 1}"
    return sql, st.params


def run_query(query: str, store: Store) -> QueryResult:
    sql, params = compile_query(query, store)
    try:
        cols, rows = store.execute(sql, params)
    except Exception as exc:  # duckdb errors -> user-facing QueryError
        raise QueryError(f"execution failed: {exc}") from exc
    truncated = len(rows) > MAX_ROWS
    return QueryResult(query, sql, cols, rows[:MAX_ROWS], truncated)
