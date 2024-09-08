"""FastAPI service: ingest, incidents, ad-hoc SPL queries."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from . import __version__
from .config import Settings
from .query import QueryError, run_query
from .report import to_markdown
from .runtime import Runtime


class IngestBody(BaseModel):
    records: list[dict[str, Any]]


class QueryBody(BaseModel):
    query: str


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    rt = Runtime(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not settings.bus_url.startswith("memory://"):
            await rt.start_consumer()
        yield
        await rt.stop()

    app = FastAPI(title="IncidentPilot", version=__version__, lifespan=lifespan)
    app.state.runtime = rt

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__, "llm": rt.llm.name,
                "bus": settings.bus_url, "stats": rt.pipeline.stats,
                "incidents": len(rt.pipeline.incidents)}

    @app.post("/ingest")
    def ingest(body: IngestBody):
        done = rt.pipeline.process(body.records)
        return {"accepted": len(body.records), "dispatched": [i.id for i in done]}

    @app.post("/finish")
    def finish():
        """Signal end of stream: close the open window and triage any pending incident."""
        rt.pipeline.flush()
        return {"dispatched": [i.id for i in rt.pipeline.finish()]}

    @app.get("/incidents")
    def incidents():
        return [{"id": i.id, "status": i.status, "onset_ts": i.onset_ts,
                 "anomalies": len(i.anomalies),
                 "root_cause": i.report.root_cause if i.report else None}
                for i in rt.pipeline.incidents]

    def _get(incident_id: str):
        for i in rt.pipeline.incidents:
            if i.id == incident_id:
                return i
        raise HTTPException(404, f"no incident {incident_id}")

    @app.get("/incidents/{incident_id}")
    def incident(incident_id: str):
        return _get(incident_id)

    @app.get("/incidents/{incident_id}/report", response_class=PlainTextResponse)
    def incident_report(incident_id: str):
        inc = _get(incident_id)
        if inc.report is None:
            raise HTTPException(409, f"incident is {inc.status}; no report yet")
        return to_markdown(inc.report)

    @app.post("/query")
    def query(body: QueryBody):
        try:
            res = run_query(body.query, rt.store)
        except QueryError as exc:
            raise HTTPException(400, str(exc))
        return {"columns": res.columns, "rows": res.rows, "truncated": res.truncated, "sql": res.sql}

    return app


def app_factory() -> FastAPI:  # uvicorn --factory incidentpilot.api:app_factory
    return create_app()
