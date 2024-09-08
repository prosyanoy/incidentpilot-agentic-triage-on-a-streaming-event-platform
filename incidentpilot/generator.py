"""Synthetic multi-service telemetry with injectable failure scenarios.

``generate`` is fully deterministic for a given (scenario, service, seed): it
returns time-ordered events (flat rows) plus the ground truth used by the
evaluation harness. ``to_records`` converts them to OTLP/JSON-style records.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Iterator

from .otel import log_to_otlp, metric_to_otlp

T0 = 1_760_000_000.0
SERVICES = ["gateway", "auth", "checkout", "payments", "inventory", "search", "cache"]
CALLERS = {  # who calls whom: service -> its callers
    "payments": ["checkout"], "checkout": ["gateway"], "inventory": ["checkout"],
    "auth": ["gateway"], "search": ["gateway"], "cache": ["search"], "gateway": [],
}
DEPENDENCY = {  # service -> a dependency it resolves/dials
    "gateway": "checkout", "checkout": "payments", "payments": "auth",
    "inventory": "cache", "search": "cache", "auth": "cache", "cache": "inventory",
}
BASE_LATENCY_MS = {"gateway": 40, "auth": 25, "checkout": 80, "payments": 120,
                   "inventory": 35, "search": 60, "cache": 3}
PATHS = {
    "gateway": ["/api/cart", "/api/items", "/api/orders", "/healthz"],
    "auth": ["/login", "/token/refresh", "/session"],
    "checkout": ["/checkout", "/cart/total"],
    "payments": ["/charge", "/refund"],
    "inventory": ["/stock", "/reserve"],
    "search": ["/search", "/suggest"],
    "cache": ["/get", "/set"],
}
METRICS = {  # name -> (mean, stddev)
    "cpu_pct": (30.0, 3.0), "disk_used_pct": (40.0, 0.15),
    "db_pool_in_use": (8.0, 1.0), "consumer_lag": (20.0, 4.0),
}
DECOY_DEPLOY_AT = 60  # benign deploy of an unrelated service before any incident


@dataclass
class GroundTruth:
    scenario: str
    category: str
    service: str
    start_ts: float


@dataclass
class Generated:
    events: list[dict]  # {"kind": "log"|"metric", "row": {...}}
    truth: GroundTruth | None
    t0: float
    duration: int


class _Ctx:
    """Mutable generation context handed to scenarios."""

    def __init__(self, rng: random.Random, t0: float):
        self.rng = rng
        self.t0 = t0
        self.events: list[dict] = []
        self.versions = {s: f"1.{i}.0" for i, s in enumerate(SERVICES)}
        self.host_of = {s: [f"{s}-0", f"{s}-1"] for s in SERVICES}

    def poisson(self, lam: float) -> int:
        if lam <= 0:
            return 0
        limit, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= limit:
                return k
            k += 1

    def log(self, ts: float, service: str, level: str, message: str, **kw) -> None:
        row = {
            "ts": ts, "service": service, "host": self.rng.choice(self.host_of[service]),
            "version": self.versions[service], "level": level, "message": message,
            "trace_id": f"{self.rng.getrandbits(128):032x}", "http_status": None,
            "latency_ms": None, "client_ip": None, "user": None, "peer": None,
            "event": None, "path": None,
        }
        row.update(kw)
        self.events.append({"kind": "log", "row": row})

    def metric(self, ts: float, service: str, host: str, name: str, value: float) -> None:
        self.events.append({"kind": "metric", "row": {
            "ts": ts, "service": service, "host": host, "name": name, "value": value}})

    def burst(self, base: float, per_sec: float) -> Iterator[float]:
        """Timestamps for a Poisson burst within the second starting at ``base``."""
        for _ in range(self.poisson(per_sec)):
            yield base + self.rng.random()


class Scenario:
    name = ""
    category = ""
    description = ""
    default_service = "checkout"

    def __init__(self, service: str | None = None):
        self.service = service or self.default_service

    # hooks -------------------------------------------------------------
    def inject(self, c: _Ctx, t: int, now: float) -> None:
        """Emit extra events for relative second ``t`` (>= 0)."""

    def metric_value(self, service: str, name: str, t: int, value: float) -> float:
        return value

    def latency_factor(self, service: str, t: int) -> float:
        return 1.0


class MemoryLeak(Scenario):
    name, category, default_service = "memory_leak", "memory_leak", "checkout"
    description = "Heap grows ~12MB/s until the JVM runs out of memory."

    def inject(self, c, t, now):
        if t >= 25 and c.rng.random() < 0.8:
            c.log(now + c.rng.random(), self.service, "WARN",
                  "GC pause 1800ms (heap 91% full)", http_status=None)
        if t >= 70:
            for ts in c.burst(now, 2):
                c.log(ts, self.service, "ERROR",
                      "java.lang.OutOfMemoryError: Java heap space", http_status=500)

    def metric_value(self, service, name, t, value):
        if service == self.service and name == "memory_mb":
            return min(2048.0, value + 12.0 * t)
        return value

    def latency_factor(self, service, t):
        return 1 + min(t, 80) / 40 if service == self.service else 1.0


class BadDeploy(Scenario):
    name, category, default_service = "bad_deploy", "bad_deploy", "payments"
    description = "A new release starts throwing NullPointerExceptions on most requests."
    new_version = "2.7.0"

    def inject(self, c, t, now):
        if t == 0:
            old = c.versions[self.service]
            c.versions[self.service] = self.new_version
            c.log(now + 0.5, self.service, "INFO",
                  f"deployed version {self.new_version} (previous {old})", event="deploy")
        if t >= 1:
            for ts in c.burst(now, 2):
                c.log(ts, self.service, "ERROR",
                      "NullPointerException in ChargeHandler.apply: card.token is null",
                      http_status=500, path="/charge")
            for caller in CALLERS[self.service]:
                for ts in c.burst(now, 2):
                    c.log(ts, caller, "ERROR",
                          f"{self.service} returned HTTP 500 for POST /charge",
                          http_status=502, peer=self.service)


class CredentialStuffing(Scenario):
    name, category, default_service = "credential_stuffing", "credential_stuffing", "auth"
    description = "A few IPs hammer /login with thousands of distinct usernames."
    ips = ["203.0.113.7", "203.0.113.8", "203.0.113.9"]

    def inject(self, c, t, now):
        for ts in c.burst(now, 25):
            if c.rng.random() < 0.02:
                c.log(ts, self.service, "INFO", "login succeeded", http_status=200,
                      path="/login", client_ip=c.rng.choice(self.ips),
                      user=f"user{c.rng.randrange(5000)}", latency_ms=30.0)
            else:
                c.log(ts, self.service, "WARN", "login failed: invalid credentials",
                      http_status=401, path="/login", client_ip=c.rng.choice(self.ips),
                      user=f"user{c.rng.randrange(5000)}", latency_ms=28.0)

    def metric_value(self, service, name, t, value):
        return value + 50 if (service == self.service and name == "cpu_pct") else value


class DbPoolExhaustion(Scenario):
    name, category, default_service = "db_pool_exhaustion", "db_pool_exhaustion", "checkout"
    description = "Slow queries hold connections until the pool is fully drained."

    def inject(self, c, t, now):
        if t >= 3:
            for ts in c.burst(now, 1):
                c.log(ts, self.service, "WARN", "connection pool at 100% capacity")
        if t >= 5:
            for ts in c.burst(now, 3):
                c.log(ts, self.service, "ERROR",
                      "timeout acquiring connection from pool after 5000ms (50/50 connections in use)",
                      http_status=503)

    def metric_value(self, service, name, t, value):
        if service == self.service and name == "db_pool_in_use":
            return min(50.0, value + 6.0 * t)
        return value

    def latency_factor(self, service, t):
        return 6.0 if service == self.service and t >= 3 else 1.0


class DiskFull(Scenario):
    name, category, default_service = "disk_full", "disk_full", "inventory"
    description = "A volume fills up and writes start failing with ENOSPC."

    def inject(self, c, t, now):
        if t >= 25 and c.rng.random() < 0.8:
            c.log(now + c.rng.random(), self.service, "WARN",
                  "disk usage above 90% on /var/lib/data")
        if t >= 37:
            for ts in c.burst(now, 3):
                c.log(ts, self.service, "ERROR",
                      "write failed: No space left on device (ENOSPC)", http_status=500)

    def metric_value(self, service, name, t, value):
        if service == self.service and name == "disk_used_pct":
            return min(100.0, value + 1.6 * t)
        return value


class DependencySaturation(Scenario):
    name, category, default_service = "dependency_saturation", "dependency_saturation", "inventory"
    description = "A downstream service is CPU-saturated; its callers time out."

    def inject(self, c, t, now):
        if t >= 3:
            for ts in c.burst(now, 1):
                c.log(ts, self.service, "WARN",
                      "request queue saturated: 200/200 worker threads busy")
            for caller in CALLERS[self.service]:
                for ts in c.burst(now, 2.5):
                    c.log(ts, caller, "ERROR",
                          f"timeout calling {self.service} after 2000ms",
                          http_status=504, peer=self.service)

    def metric_value(self, service, name, t, value):
        if service == self.service and name == "cpu_pct":
            return min(98.0, value + 20.0 * t)
        return value

    def latency_factor(self, service, t):
        return 12.0 if service == self.service and t >= 2 else 1.0


class CertExpiry(Scenario):
    name, category, default_service = "cert_expiry", "cert_expiry", "payments"
    description = "A service's TLS certificate expires; every caller fails the handshake."

    def inject(self, c, t, now):
        for caller in CALLERS[self.service]:
            for ts in c.burst(now, 3):
                c.log(ts, caller, "ERROR",
                      "tls: failed to verify certificate: x509: certificate has expired "
                      f"or is not yet valid (CN={self.service}.internal)",
                      http_status=502, peer=self.service)


class ConfigChange(Scenario):
    name, category, default_service = "config_change", "config_change", "search"
    description = "A configuration push contains a malformed value; no code was deployed."

    def inject(self, c, t, now):
        if t == 0:
            c.log(now + 0.5, self.service, "INFO",
                  "config rev 41 applied by ops-bot: ranking.weights, feature flag new_ranker=on",
                  event="config_change")
        if t >= 2:
            for ts in c.burst(now, 3):
                c.log(ts, self.service, "ERROR",
                      "invalid configuration value for ranking.weights: expected float, "
                      "got string (config rev 41)", http_status=500)


class QueueBacklog(Scenario):
    name, category, default_service = "queue_backlog", "queue_backlog", "inventory"
    description = "Consumers fall behind the stock-updates topic; lag grows without bound."

    def lag(self, t: int) -> float:
        return min(9000.0, 150.0 * t)

    def inject(self, c, t, now):
        if t >= 5:
            for ts in c.burst(now, 1.5):
                c.log(ts, self.service, "WARN",
                      f"consumer lag above threshold: {int(self.lag(t))} messages behind "
                      "on topic stock-updates")

    def metric_value(self, service, name, t, value):
        if service == self.service and name == "consumer_lag":
            return value + self.lag(t)
        return value

    def latency_factor(self, service, t):
        return 1.5 if service == self.service and t >= 5 else 1.0


class CacheFailure(Scenario):
    name, category, default_service = "cache_failure", "cache_failure", "cache"
    description = "Redis hits maxmemory and rejects writes; callers fall back to the origin."

    def inject(self, c, t, now):
        for ts in c.burst(now, 2):
            c.log(ts, self.service, "ERROR",
                  "redis: maxmemory reached, command rejected "
                  "(OOM command not allowed when used memory > 'maxmemory')", http_status=500)
        for caller in CALLERS[self.service]:
            for ts in c.burst(now, 2):
                c.log(ts, caller, "WARN", "cache write failed, falling back to origin",
                      peer=self.service)

    def metric_value(self, service, name, t, value):
        if service == self.service and name == "memory_mb":
            return value * 1.4
        return value

    def latency_factor(self, service, t):
        return 4.0 if service in CALLERS[self.service] else 1.0


class TrafficSpike(Scenario):
    name, category, default_service = "traffic_spike", "traffic_spike", "gateway"
    description = "A flood of requests from hundreds of clients trips the rate limiter."

    def inject(self, c, t, now):
        for ts in c.burst(now, 45):
            ip = f"198.51.{c.rng.randrange(100, 104)}.{c.rng.randrange(1, 200)}"
            path = f"/api/items/{c.rng.randrange(40)}"
            if c.rng.random() < 0.4:
                c.log(ts, self.service, "WARN", "rate limit exceeded: 429 Too Many Requests",
                      http_status=429, client_ip=ip, path=path, latency_ms=2.0)
            else:
                c.log(ts, self.service, "INFO", f"GET {path} 200", http_status=200,
                      client_ip=ip, path=path, latency_ms=c.rng.uniform(60, 160))

    def metric_value(self, service, name, t, value):
        return max(value, 92.0) if (service == self.service and name == "cpu_pct") else value

    def latency_factor(self, service, t):
        return 3.0 if service == self.service else 1.0


class DnsFailure(Scenario):
    name, category, default_service = "dns_failure", "dns_failure", "checkout"
    description = "Name resolution for an internal hostname fails."

    def inject(self, c, t, now):
        host = f"{DEPENDENCY[self.service]}.internal"
        for ts in c.burst(now, 3):
            c.log(ts, self.service, "ERROR",
                  f"dial tcp: lookup {host}: no such host (dns resolution failed)",
                  http_status=502)


SCENARIOS: dict[str, type[Scenario]] = {
    cls.name: cls for cls in (
        MemoryLeak, BadDeploy, CredentialStuffing, DbPoolExhaustion, DiskFull,
        DependencySaturation, CertExpiry, ConfigChange, QueueBacklog, CacheFailure,
        TrafficSpike, DnsFailure,
    )
}


def generate(scenario: str | None, *, service: str | None = None, start: int = 180,
             duration: int = 420, seed: int = 7, t0: float = T0,
             metric_interval: int = 5, rate: float = 1.5) -> Generated:
    """Generate ``duration`` seconds of telemetry; the failure starts at ``start``.

    ``scenario=None`` produces a healthy baseline (used to check for false alarms).
    """
    rng = random.Random(seed)
    scn = None
    if scenario is not None:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}; choose from {sorted(SCENARIOS)}")
        scn = SCENARIOS[scenario](service)
        if scn.service not in SERVICES:
            raise ValueError(f"unknown service {scn.service!r}")
    c = _Ctx(rng, t0)
    users = [f"user{i}" for i in range(200)]
    ips = [f"10.0.{i // 50}.{i % 50 + 1}" for i in range(150)]

    for sec in range(duration):
        now = t0 + sec
        rel = sec - start
        active = scn is not None and rel >= 0
        if sec == DECOY_DEPLOY_AT:
            decoy = "search" if (scn and scn.service == "inventory") else "inventory"
            old = c.versions[decoy]
            c.versions[decoy] = f"{old[:-1]}1"
            c.log(now + 0.3, decoy, "INFO",
                  f"deployed version {c.versions[decoy]} (previous {old})", event="deploy")
        for svc in SERVICES:
            factor = scn.latency_factor(svc, rel) if active else 1.0
            for ts in c.burst(now, rate):
                roll = rng.random()
                path = rng.choice(PATHS[svc])
                lat = rng.lognormvariate(math.log(BASE_LATENCY_MS[svc] * factor), 0.25)
                ip, user = rng.choice(ips), rng.choice(users)
                if roll < 0.004:
                    c.log(ts, svc, "ERROR", "request failed: internal error",
                          http_status=500, path=path, latency_ms=lat, client_ip=ip, user=user)
                elif roll < 0.014:
                    c.log(ts, svc, "WARN", "transient error, retrying request",
                          http_status=503, path=path, latency_ms=lat, client_ip=ip, user=user)
                else:
                    c.log(ts, svc, "INFO", f"{rng.choice(['GET', 'POST'])} {path} 200",
                          http_status=200, path=path, latency_ms=lat, client_ip=ip, user=user)
        if active:
            scn.inject(c, rel, now)
        if sec % metric_interval == 0:
            for idx, svc in enumerate(SERVICES):
                for host in c.host_of[svc]:
                    table = dict(METRICS, memory_mb=(400.0 + 40 * idx, 6.0))
                    for name, (mean, sd) in table.items():
                        v = rng.gauss(mean, sd)
                        if active:
                            v = scn.metric_value(svc, name, rel, v)
                        c.metric(now + 0.01, svc, host, name, round(v, 3))

    c.events.sort(key=lambda e: e["row"]["ts"])
    truth = GroundTruth(scn.name, scn.category, scn.service, t0 + start) if scn else None
    return Generated(c.events, truth, t0, duration)


def to_records(events: list[dict]) -> Iterator[dict]:
    for e in events:
        yield log_to_otlp(e["row"]) if e["kind"] == "log" else metric_to_otlp(e["row"])
