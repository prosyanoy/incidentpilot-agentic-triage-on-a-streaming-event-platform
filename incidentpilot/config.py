"""Environment-driven configuration with safe local defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    bus_url: str = "memory://"
    bus_subject: str = "incidentpilot.events"
    db_path: str = ":memory:"
    llm: str = "mock"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    scenario: str = "bad_deploy"
    seed: int = 7
    speed: float = 20.0

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            bus_url=e("INCIDENTPILOT_BUS_URL", cls.bus_url),
            bus_subject=e("INCIDENTPILOT_BUS_SUBJECT", cls.bus_subject),
            db_path=e("INCIDENTPILOT_DB_PATH", cls.db_path) or ":memory:",
            llm=e("INCIDENTPILOT_LLM", cls.llm),
            ollama_url=e("OLLAMA_URL", cls.ollama_url),
            ollama_model=e("OLLAMA_MODEL", cls.ollama_model),
            scenario=e("INCIDENTPILOT_SCENARIO", cls.scenario),
            seed=int(e("INCIDENTPILOT_SEED", cls.seed)),
            speed=float(e("INCIDENTPILOT_SPEED", cls.speed)),
        )
