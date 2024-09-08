"""Human-readable rendering of incident reports."""
from __future__ import annotations

from .models import IncidentReport


def to_markdown(r: IncidentReport) -> str:
    rc = r.root_cause
    lines = [
        f"# [{r.severity.upper()}] {r.title}  ({r.incident_id})",
        "",
        f"**Root cause:** `{rc.category}` in **{rc.service}** (confidence {rc.confidence:.0%})",
        "",
        rc.summary,
        "",
        f"**Affected services:** {', '.join(r.affected_services)}",
        "",
        "## Timeline",
        *[f"- {t}" for t in r.timeline],
        "",
        "## Evidence",
    ]
    for e in r.evidence:
        lines += [f"- **[{e.id}]** {e.finding}", f"  - query: `{e.query}` ({e.rows} rows)"]
    lines += ["", "## Recommended fix", *[f"{i}. {s}" for i, s in enumerate(r.recommended_fix, 1)],
              "", f"_Agent: {r.llm}, {r.steps} steps, {r.agent_seconds:.2f}s_"]
    return "\n".join(lines)
