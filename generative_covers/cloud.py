"""Store generated covers in Cloudinary, one asset per incident.

Naming scheme: ``<folder>/incident-<incident_id>`` (folder defaults to ``generative-covers``),
tagged ``generative-cover`` and ``incident-<incident_id>`` and carrying contextual metadata
(incident title, model, quality, prompt version, generation time).
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import cloudinary
import cloudinary.api
import cloudinary.uploader

from .aiid import Incident
from .imagegen import GeneratedImage

log = logging.getLogger(__name__)

DEFAULT_FOLDER = "generative-covers"
TAG = "generative-cover"
_PUBLIC_ID_RE = re.compile(r"(?:^|/)incident-(\d+)$")


def configure(cloud_name: str | None, api_key: str | None, api_secret: str | None) -> None:
    missing = [
        name
        for name, value in (
            ("CLOUDINARY_CLOUD_NAME", cloud_name),
            ("CLOUDINARY_API_KEY", api_key),
            ("CLOUDINARY_API_SECRET", api_secret),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"Cloudinary is not configured; missing {', '.join(missing)}")
    cloudinary.config(cloud_name=cloud_name, api_key=api_key, api_secret=api_secret, secure=True)


def ping() -> None:
    """Fail fast with a readable error when the cloud name or credentials are wrong."""
    try:
        cloudinary.api.ping()
    except cloudinary.exceptions.Error as exc:
        raise RuntimeError(
            f"Cloudinary rejected the credentials for cloud '{cloudinary.config().cloud_name}': {exc}"
        ) from exc


def public_id_for(incident_id: int, folder: str = DEFAULT_FOLDER) -> str:
    return f"{folder.strip('/')}/incident-{incident_id}"


def incident_id_from_public_id(public_id: str) -> int | None:
    match = _PUBLIC_ID_RE.search(public_id)
    return int(match.group(1)) if match else None


@dataclass
class Cover:
    incident_id: int
    public_id: str
    url: str
    width: int | None = None
    height: int | None = None
    bytes: int | None = None
    format: str | None = None
    created_at: str | None = None
    version: int | None = None
    context: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_resource(cls, res: dict) -> "Cover | None":
        incident_id = incident_id_from_public_id(res.get("public_id", ""))
        if incident_id is None:
            return None
        context = res.get("context") or {}
        if isinstance(context, dict) and "custom" in context:
            context = context["custom"] or {}
        return cls(
            incident_id=incident_id,
            public_id=res["public_id"],
            url=res.get("secure_url") or res.get("url", ""),
            width=res.get("width"),
            height=res.get("height"),
            bytes=res.get("bytes"),
            format=res.get("format"),
            created_at=res.get("created_at"),
            version=res.get("version"),
            context=dict(context) if isinstance(context, dict) else {},
            tags=list(res.get("tags") or []),
        )


def existing_covers(folder: str = DEFAULT_FOLDER) -> dict[int, Cover]:
    """List every cover already stored under the folder, keyed by incident id."""
    covers: dict[int, Cover] = {}
    cursor = None
    prefix = folder.strip("/") + "/"
    while True:
        resp = cloudinary.api.resources(
            type="upload",
            resource_type="image",
            prefix=prefix,
            max_results=500,
            context=True,
            tags=True,
            next_cursor=cursor,
        )
        for res in resp.get("resources", []):
            cover = Cover.from_resource(res)
            if cover is not None:
                covers[cover.incident_id] = cover
        cursor = resp.get("next_cursor")
        if not cursor:
            break
    log.info("Cloudinary already holds %d covers under %s", len(covers), prefix)
    return covers


def upload_cover(
    image: GeneratedImage,
    incident: Incident,
    *,
    folder: str = DEFAULT_FOLDER,
    quality: str,
    prompt_version: str,
    overwrite: bool = False,
    extra_context: dict[str, str] | None = None,
) -> Cover:
    """Upload an image for the incident. Existing assets are kept unless overwrite=True."""
    folder = folder.strip("/")
    filename = f"incident-{incident.incident_id}.{image.extension}"
    context = {
        "incident_id": str(incident.incident_id),
        "title": incident.title[:500],
        "model": image.model,
        "quality": quality,
        "prompt_version": prompt_version,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    for key, value in (extra_context or {}).items():
        if value:
            context[key] = str(value)[:500]
    # `folder` + a short public_id yields "<folder>/incident-N" in both fixed and dynamic
    # folder modes, and files the asset under that folder in the Media Library.
    result = cloudinary.uploader.upload(
        io.BytesIO(image.data),
        filename=filename,
        folder=folder,
        public_id=f"incident-{incident.incident_id}",
        resource_type="image",
        overwrite=overwrite,
        invalidate=overwrite,
        unique_filename=False,
        use_filename=False,
        tags=[TAG, f"incident-{incident.incident_id}"],
        context=context,
    )
    if result.get("existing"):
        log.info("Incident %s already had a cover; kept the existing asset", incident.incident_id)
    cover = Cover.from_resource(result)
    if cover is None:  # should not happen, but keep the pipeline informative
        raise RuntimeError(f"Unexpected Cloudinary response for incident {incident.incident_id}: {result}")
    if not cover.context:
        cover.context = context
    return cover
