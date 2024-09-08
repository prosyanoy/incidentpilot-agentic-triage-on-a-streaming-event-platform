"""Shared data models."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Anomaly(BaseModel):
    window_start: float
    window_end: float
    service: str
    signal: str
    value: float
    baseline: float
    zscore: float


class EvidenceItem(BaseModel):
    id: str
    query: str
    finding: str
    rows: int = 0


class RootCause(BaseModel):
    category: str
    service: str
    summary: str
    confidence: float = Field(ge=0, le=1)


class IncidentReport(BaseModel):
    incident_id: str
    title: str
    severity: str
    root_cause: RootCause
    affected_services: list[str]
    timeline: list[str]
    evidence: list[EvidenceItem]
    recommended_fix: list[str]
    steps: int = 0
    llm: str = "mock"
    agent_seconds: float = 0.0


class Incident(BaseModel):
    id: str
    onset_ts: float            # start of the first anomalous window
    detected_ts: float         # end of the window in which the first anomaly was confirmed
    dispatch_ts: float = 0.0   # event time at which the agent was started
    anomalies: list[Anomaly] = Field(default_factory=list)
    status: str = "open"       # open | triaged | failed
    report: Optional[IncidentReport] = None
    error: Optional[str] = None
