"""Stage 1 of the prompt: turn everything the database knows about an incident into a short visual brief.

The dossier (title, description, editor notes, entities, taxonomy classifications, report excerpts) goes
to a text model, which answers with JSON: scene, subject, key_details, accent, avoid, mood. Stage 2
(imagegen.build_prompt) drops those fields into the fixed design brief in prompt.txt.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

import requests

from .aiid import Incident

log = logging.getLogger(__name__)

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_BRIEF_MODEL = "openai/gpt-5-mini"
DOSSIER_BUDGET = 24_000  # characters; keeps the stage-1 call at a fraction of a cent
MAX_REPORTS = 6
APP_REFERER = "https://github.com/responsible-ai-collaborative/generative-covers"
APP_TITLE = "AIID generative covers"

# Annotator bookkeeping that says nothing about what happened.
_SKIP_ATTRIBUTES = {
    "Annotation Status", "Annotator", "Peer Reviewer", "Quality Control", "Incident Number", "Publish",
    "Notes", "Notes (special interest intangible harm)", "Notes (AI special interest intangible harm)",
    "Notes (Environmental and Temporal Characteristics)", "Notes (Information about AI System)",
    "Notes (Information about Entities)", "Notes ( Tangible Harm Quantities Information)",
}
_FIELDS = ("scene", "subject", "key_details", "accent", "avoid", "mood")


class BriefError(RuntimeError):
    pass


@dataclass
class Brief:
    scene: str
    subject: str
    key_details: list[str]
    accent: str
    avoid: list[str]
    mood: str
    model: str
    fallback: bool = False
    cost_usd: float | None = None

    def as_fields(self) -> dict[str, str]:
        return {
            "scene": self.scene,
            "subject": self.subject,
            "key_details": "; ".join(self.key_details),
            "accent": self.accent,
            "avoid": "; ".join(self.avoid),
            "mood": self.mood,
        }

    def to_json(self) -> str:
        return json.dumps({k: getattr(self, k) for k in _FIELDS}, ensure_ascii=False)


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def build_dossier(incident: Incident, budget: int = DOSSIER_BUDGET, max_reports: int = MAX_REPORTS) -> str:
    """Plain-text dossier for the brief model. Report excerpts share whatever budget the facts leave."""
    lines = [
        f"INCIDENT {incident.incident_id} (date {incident.date})",
        f"Title: {incident.title}",
        f"Description: {_clean(incident.description)}",
    ]
    if _clean(incident.editor_notes):
        lines.append(f"Editor notes: {_clean(incident.editor_notes)}")
    for label, key in (
        ("Alleged developer of the AI system", "developer"),
        ("Alleged deployer of the AI system", "deployer"),
        ("Alleged harmed or nearly harmed parties", "harmed"),
        ("Implicated systems", "systems"),
    ):
        names = incident.entities.get(key) or []
        lines.append(f"{label}: {', '.join(names) if names else 'unknown'}")
    if incident.classifications:
        lines.append("Taxonomic classifications:")
        for cl in incident.classifications:
            attrs = [(k, v[:200]) for k, v in cl.attributes if k not in _SKIP_ATTRIBUTES][:20]
            if attrs:
                lines.append(f"  {cl.namespace}: " + "; ".join(f"{k} = {v}" for k, v in attrs))
    reports = incident.reports[:max_reports]
    lines.append(f"Reports ({len(incident.report_numbers)} total, {len(reports)} shown):")
    remaining = max(2_000, budget - len("\n".join(lines)))
    for index, report in enumerate(reports):
        share = remaining // (len(reports) - index)
        text = _clean(report.text)
        if len(text) > share:
            text = text[:share].rsplit(" ", 1)[0] + " […]"
        remaining -= len(text)
        published = str(report.date_published or "")[:10]
        lines.append(f"  [{report.report_number}] {report.title} — {report.source_domain}, published {published}")
        if text:
            lines.append(f"    {text}")
    return "\n".join(lines)


def fallback_brief(incident: Incident, model: str = "fallback") -> Brief:
    """Used when the brief model is unavailable, so a cover is still produced from the incident record."""
    words = _clean(incident.description or incident.title).split()
    scene = " ".join(words[:50])
    systems = incident.entities.get("systems") or incident.entities.get("developer") or []
    return Brief(
        scene=scene,
        subject=incident.title[:120],
        key_details=[f"the AI system involved: {', '.join(systems[:2])}" if systems else "the AI system involved",
                     "the people or things affected, shown generically"],
        accent="muted red marking the point of failure",
        avoid=["text or numbers", "logos", "identifiable faces", "graphic content"],
        mood="calm, factual",
        model=model,
        fallback=True,
    )


def write_brief(
    incident: Incident,
    *,
    api_key: str,
    instructions: str,
    model: str = DEFAULT_BRIEF_MODEL,
    timeout: int = 180,
    retries: int = 3,
    session: requests.Session | None = None,
) -> Brief:
    """Ask the brief model for the six fields. Raises BriefError after exhausting retries."""
    session = session or requests.Session()
    dossier = build_dossier(incident)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": dossier},
        ],
        "response_format": {"type": "json_object"},
        "reasoning": {"effort": "low"},
        "usage": {"include": True},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": APP_REFERER,
        "X-Title": APP_TITLE,
    }
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.post(OPENROUTER_CHAT_URL, json=payload, headers=headers, timeout=timeout)
            if resp.status_code != 200:
                raise BriefError(f"OpenRouter returned HTTP {resp.status_code}: {resp.text[:300]}")
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
            data = json.loads(content)
            brief = _parse(data, model)
            usage = body.get("usage") or {}
            brief.cost_usd = float(usage["cost"]) if usage.get("cost") is not None else None
            return brief
        except (requests.RequestException, BriefError, KeyError, ValueError, TypeError) as exc:
            last_error = exc
            log.warning("brief for incident %s failed (attempt %d/%d): %s", incident.incident_id, attempt, retries, exc)
            if attempt < retries:
                time.sleep(min(30, 3 * 2 ** (attempt - 1)))
    raise BriefError(f"Brief model failed after {retries} attempts: {last_error}")


def _parse(data: dict, model: str) -> Brief:
    if not isinstance(data, dict):
        raise ValueError("brief is not a JSON object")
    missing = [k for k in _FIELDS if not data.get(k)]
    if missing:
        raise ValueError(f"brief is missing {missing}")

    def as_list(value) -> list[str]:
        if isinstance(value, str):
            return [v.strip() for v in re.split(r"[;\n]+", value) if v.strip()]
        return [str(v).strip() for v in value if str(v).strip()]

    return Brief(
        scene=_clean(str(data["scene"])),
        subject=_clean(str(data["subject"])),
        key_details=as_list(data["key_details"]),
        accent=_clean(str(data["accent"])),
        avoid=as_list(data["avoid"]),
        mood=_clean(str(data["mood"])),
        model=model,
    )
