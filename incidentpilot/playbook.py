"""Known failure modes: signatures the agent can match and the standard remediation.

Shared by the deterministic mock LLM (which scores evidence against it) and by the
real-LLM prompt (which lists the categories the model must choose from).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Play:
    category: str
    title: str
    severity: str
    summary: str
    keywords: tuple[str, ...]
    # (metric name, "max>=" | "ratio>=", threshold) evaluated on the root service
    metric_hints: tuple[tuple[str, str, float], ...] = ()
    # extra signals: deploy | config | few_ips_many_users | many_ips
    signals: tuple[str, ...] = ()
    root_is_peer: bool = False
    fix: tuple[str, ...] = field(default_factory=tuple)


PLAYBOOK: dict[str, Play] = {p.category: p for p in [
    Play("memory_leak", "Memory leak exhausting the heap", "high",
         "Heap usage grows without bound until the process is out of memory.",
         ("outofmemoryerror", "gc pause", "heap"),
         metric_hints=(("memory_mb", "ratio>=", 1.8),),
         fix=("Restart or roll the affected pods to restore service now.",
              "Take a heap dump from a pod before it restarts and diff retained objects.",
              "Roll back to the last release with a flat memory profile if the leak is new.",
              "Add a memory-growth alert and a pod memory limit so failures stay contained.")),
    Play("bad_deploy", "Faulty release causing request failures", "critical",
         "A recent deployment introduced an unhandled exception on the request path.",
         ("nullpointerexception", "unhandled exception", "panic:", "traceback"),
         signals=("deploy",),
         fix=("Roll back the service to the previous version immediately.",
              "Block further promotion of the bad release in the pipeline.",
              "Reproduce the exception with the failing payload and add a regression test.")),
    Play("credential_stuffing", "Credential-stuffing attack on login", "high",
         "A small set of IPs is trying thousands of distinct usernames against /login.",
         ("login failed", "invalid credentials"),
         signals=("few_ips_many_users",),
         fix=("Block or rate-limit the offending source IPs at the edge/WAF.",
              "Enable CAPTCHA or step-up auth on /login and throttle per-IP failures.",
              "Force password resets for accounts that logged in successfully from those IPs.")),
    Play("db_pool_exhaustion", "Database connection pool exhausted", "high",
         "All pooled DB connections are in use, so new requests time out waiting for one.",
         ("connection pool", "acquiring connection"),
         metric_hints=(("db_pool_in_use", "max>=", 45.0),),
         fix=("Find and kill long-running queries holding connections.",
              "Temporarily raise the pool size or add replicas while the root query is fixed.",
              "Set statement and acquire timeouts and alert on pool saturation.")),
    Play("disk_full", "Disk full: writes failing", "high",
         "A volume has run out of space and writes fail with ENOSPC.",
         ("no space left", "enospc", "disk usage"),
         metric_hints=(("disk_used_pct", "max>=", 95.0),),
         fix=("Free space now: rotate or delete old logs/temp files, or expand the volume.",
              "Find the growth source (du by directory) and fix its retention.",
              "Alert at 80% disk usage.")),
    Play("dependency_saturation", "Downstream service saturated; callers timing out", "high",
         "A downstream service is CPU/thread saturated and its callers hit timeouts.",
         ("timeout calling", "worker threads busy", "queue saturated"),
         metric_hints=(("cpu_pct", "max>=", 90.0),), root_is_peer=True,
         fix=("Scale out the saturated service and shed non-critical load.",
              "Add circuit breakers and bounded retries in the callers.",
              "Profile the saturated service for the hot path that regressed.")),
    Play("cert_expiry", "Expired TLS certificate", "critical",
         "A service's TLS certificate expired so every client handshake fails.",
         ("x509", "certificate has expired", "tls:"), root_is_peer=True,
         fix=("Renew and deploy the certificate on the named service now.",
              "Reload callers' trust/connection pools if they cache failures.",
              "Automate renewal (cert-manager) and alert 14 days before expiry.")),
    Play("config_change", "Bad configuration change", "high",
         "A configuration push with an invalid value broke the service; no code was deployed.",
         ("invalid configuration", "config rev"),
         signals=("config",),
         fix=("Revert the configuration revision named in the errors.",
              "Add schema validation and a canary stage to config rollout.")),
    Play("queue_backlog", "Message queue consumers falling behind", "medium",
         "Consumer lag on a topic grows continuously; downstream data goes stale.",
         ("consumer lag",),
         metric_hints=(("consumer_lag", "ratio>=", 20.0),),
         fix=("Scale out the consumer group and check for a stuck partition.",
              "Inspect the slowest handler for a recent regression.",
              "Alert on consumer-lag growth rate, not only absolute lag.")),
    Play("cache_failure", "Cache tier rejecting writes", "medium",
         "Redis reached maxmemory and rejects writes, pushing load onto the origin.",
         ("maxmemory", "redis", "falling back to origin"),
         fix=("Raise maxmemory or flush low-value keys; verify the eviction policy (allkeys-lru).",
              "Audit key TTLs for the recent growth.",
              "Protect the origin with request coalescing while the cache recovers.")),
    Play("traffic_spike", "Traffic flood tripping rate limits", "medium",
         "Request volume from hundreds of clients exceeds capacity and triggers 429s.",
         ("rate limit exceeded", "429"),
         signals=("many_ips",),
         fix=("Enable autoscaling / add gateway capacity.",
              "Tighten per-client rate limits and enable WAF bot rules.",
              "Check marketing/partner traffic to tell a surge from abuse.")),
    Play("dns_failure", "DNS resolution failures", "high",
         "The service cannot resolve an internal hostname.",
         ("no such host", "dns resolution", "lookup"),
         fix=("Check cluster DNS (CoreDNS) health and recent service/record changes.",
              "Verify the target Service/record exists and the resolver config is intact.",
              "Add DNS caching and resolution-failure alerts.")),
]}
UNKNOWN_FIX = ("Escalate to the on-call engineer with the attached evidence.",
               "Widen the search window and compare against a healthy baseline.")
