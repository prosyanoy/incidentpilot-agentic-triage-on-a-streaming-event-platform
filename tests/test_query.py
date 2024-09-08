import pytest

from incidentpilot.query import QueryError, compile_query, run_query


def test_search_and_stats_by(small_store):
    r = run_query("search level=ERROR earliest=1760000180 | stats count by service | sort -count",
                  small_store)
    assert r.columns == ["service", "count"]
    counts = {row[0]: row[1] for row in r.rows}
    assert counts["payments"] > 100 and counts["checkout"] > 100
    assert r.rows[0][1] >= r.rows[1][1]  # sorted desc


def test_free_text_and_wildcards(small_store):
    r = run_query('search "NullPointer" | stats count', small_store)
    assert r.rows[0][0] > 0
    r2 = run_query("search message=*nullpointer* service=payments | stats count", small_store)
    assert r2.rows[0][0] == r.rows[0][0]


def test_in_list_and_not_equal(small_store):
    both = run_query("search level=ERROR,WARN | stats count", small_store).rows[0][0]
    err = run_query("search level=ERROR | stats count", small_store).rows[0][0]
    warn = run_query("search level=WARN | stats count", small_store).rows[0][0]
    assert both == err + warn
    not_info = run_query("search level!=INFO | stats count", small_store).rows[0][0]
    assert not_info == both


def test_numeric_comparison_and_percentiles(small_store):
    r = run_query("search service=payments latency_ms>0 | stats p50(latency_ms) as p50 "
                  "p95(latency_ms) as p95 dc(host) as hosts", small_store)
    row = r.as_dicts()[0]
    assert 0 < row["p50"] < row["p95"]
    assert row["hosts"] == 2


def test_timechart_buckets_and_by(small_store):
    r = run_query("search service=payments level=ERROR | timechart span=30s count", small_store)
    assert r.columns == ["_time", "count"]
    times = [row[0] for row in r.rows]
    assert times == sorted(times) and all(t % 30 == 0 for t in times)
    r2 = run_query("search level=ERROR | timechart span=1m count by service", small_store)
    assert r2.columns == ["_time", "service", "count"]


def test_metrics_source(small_store):
    r = run_query("search source=metrics name=cpu_pct service=gateway | stats avg(value) as v",
                  small_store)
    assert 20 < r.rows[0][0] < 40


def test_top_and_head_and_fields(small_store):
    r = run_query("search service=payments level=ERROR | top 2 message", small_store)
    assert len(r.rows) <= 2 and r.columns == ["message", "count", "percent"]
    r = run_query("search event=deploy | fields ts,service | sort ts | head 1", small_store)
    assert r.columns == ["ts", "service"] and len(r.rows) == 1
    assert r.rows[0][1] == "inventory"  # the earliest (decoy) deploy


def test_where_on_aggregates(small_store):
    r = run_query("search level=ERROR | stats count by service | where count>100", small_store)
    assert {row[0] for row in r.rows} == {"payments", "checkout"}


def test_relative_time_anchors_on_latest_event(small_store):
    recent = run_query("search earliest=-1m | stats count", small_store).rows[0][0]
    total = run_query("search | stats count", small_store).rows[0][0]
    assert 0 < recent < total


@pytest.mark.parametrize("q", [
    "", "stats count", "search | bogus", "search nosuchfield=1", "search | stats avg(nosuch)",
    "search | stats avg()", "search | sort -nosuch", "search | timechart span=5x",
    'search "unterminated', "search latency_ms>abc", "search | top", "search | fields",
])
def test_bad_queries_raise(q, small_store):
    with pytest.raises(QueryError):
        run_query(q, small_store)


def test_sql_injection_is_inert(small_store):
    r = run_query("search service=\"payments'; DROP TABLE logs; --\" | stats count", small_store)
    assert r.rows[0][0] == 0
    assert small_store.count("logs") > 0
    with pytest.raises(QueryError):
        run_query("search | stats count by service; DROP TABLE logs", small_store)
    sql, params = compile_query("search service=x' OR '1'='1", small_store)
    assert "OR '1'" not in sql


def test_result_cap_and_text_render(small_store):
    r = run_query("search", small_store)
    assert len(r.rows) == 500 and r.truncated
    assert "more rows" in r.to_text(max_rows=3)
