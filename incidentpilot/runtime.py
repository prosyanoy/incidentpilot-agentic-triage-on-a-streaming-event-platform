"""Wires store + pipeline + agent together; optionally consumes the bus."""
from __future__ import annotations

import asyncio
import json
import logging

from .agent import TriageAgent
from .bus import Bus, connect_bus
from .config import Settings
from .llm import make_llm
from .pipeline import Pipeline
from .store import Store

log = logging.getLogger("incidentpilot.runtime")


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.db_path)
        self.llm = make_llm(settings.llm, settings.ollama_url, settings.ollama_model)
        self.agent = TriageAgent(self.store, self.llm)
        self.pipeline = Pipeline(self.store, self.agent.investigate)
        self._bus: Bus | None = None
        self._buffer: list[dict] = []
        self._task: asyncio.Task | None = None

    # -- bus consumption -------------------------------------------------
    async def start_consumer(self) -> None:
        self._bus = await connect_bus(self.settings.bus_url, self.settings.bus_subject)

        async def on_message(data: bytes) -> None:
            try:
                self._buffer.append(json.loads(data))
            except json.JSONDecodeError:
                self.pipeline.stats["rejected"] += 1

        await self._bus.subscribe(on_message)
        self._task = asyncio.create_task(self._drain_loop())
        log.info("consuming %s from %s", self.settings.bus_subject, self.settings.bus_url)

    async def _drain_loop(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            if self._buffer:
                batch, self._buffer = self._buffer, []
                try:
                    await asyncio.to_thread(self.pipeline.process, batch)
                except Exception:  # never let one bad batch kill the consumer
                    log.exception("batch processing failed")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._bus:
            await self._bus.close()
        self.store.close()
