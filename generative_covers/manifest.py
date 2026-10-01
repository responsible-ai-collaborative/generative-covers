"""Build the JSON manifest consumed by the gallery web app in ./site."""

from __future__ import annotations

from datetime import datetime, timezone

from .aiid import INCIDENT_URL, Snapshot
from .cloud import Cover


def build_manifest(
    covers: dict[int, Cover],
    snapshot: Snapshot | None,
    *,
    cloud_name: str,
    folder: str,
    error: str | None = None,
) -> dict:
    by_id = snapshot.by_id if snapshot else {}
    items = []
    for incident_id in sorted(covers, reverse=True):
        cover = covers[incident_id]
        incident = by_id.get(incident_id)
        ctx = cover.context or {}
        items.append(
            {
                "incident_id": incident_id,
                "incident_url": INCIDENT_URL.format(incident_id=incident_id),
                "title": ctx.get("title") or (incident.title if incident else ""),
                "incident_date": incident.date if incident else None,
                "public_id": cover.public_id,
                "url": cover.url,
                "format": cover.format,
                "width": cover.width,
                "height": cover.height,
                "bytes": cover.bytes,
                "version": cover.version,
                "created_at": cover.created_at,
                "model": ctx.get("model"),
                "quality": ctx.get("quality"),
                "prompt_version": ctx.get("prompt_version"),
                "incident_in_snapshot": incident is not None,
                # None when the snapshot was unavailable; True means an editor has since added a real image
                "incident_has_report_image": incident.has_report_image() if incident else None,
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cloud_name": cloud_name,
        "folder": folder,
        "snapshot_key": snapshot.key if snapshot else None,
        "incident_count": len(snapshot.incidents) if snapshot else None,
        "incidents_without_report_images": (
            len(snapshot.incidents_without_report_images()) if snapshot else None
        ),
        "cover_count": len(items),
        # set when Cloudinary could not be reached; the gallery shows it instead of an empty page
        "error": error,
        "items": items,
    }
