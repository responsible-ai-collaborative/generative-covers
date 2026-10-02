"""Read incidents, reports, entities and classifications from an AI Incident Database snapshot.

Two sources, same tarball format (a mongodump of the ``aiidprod`` database):

* **Daily** (preferred): ``daily-DD.tar.bz2`` in a private Cloudflare R2 bucket, written every day
  around 07:27 UTC and overwritten monthly. Needs the R2 account id, bucket name and an access key.
* **Weekly** (fallback): ``backup-YYYYMMDDhhmmss.tar.bz2`` in the public bucket listed on
  https://incidentdatabase.ai/research/snapshots/.

The GraphQL API is restricted to browser origins, so snapshots are the supported way for automation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

import bson
import requests

log = logging.getLogger(__name__)

SNAPSHOT_INDEX_URL = "https://incidentdatabase.ai/page-data/research/snapshots/page-data.json"
SNAPSHOT_PAGE_URL = "https://incidentdatabase.ai/research/snapshots/"
SNAPSHOT_BUCKET_URL = "https://pub-72b2b2fc36ec423189843747af98f80e.r2.dev/"
INCIDENT_URL = "https://incidentdatabase.ai/cite/{incident_id}/"
DAILY_PREFIX = "daily-"

_BACKUP_KEY_RE = re.compile(r"backup-\d{14}\.tar\.bz2")
_ENTITY_FIELDS = {
    "developer": "Alleged developer of AI system",
    "deployer": "Alleged deployer of AI system",
    "harmed": "Alleged harmed or nearly harmed parties",
    "systems": "implicated_systems",
}
# Per-annotator working copies of the CSET taxonomy duplicate the published CSETv1 record.
_SKIP_NAMESPACES = {"CSETv1_Annotator-1", "CSETv1_Annotator-2", "CSETv1_Annotator-3"}


# --------------------------------------------------------------------------- data model
@dataclass
class Report:
    report_number: int
    title: str = ""
    image_url: str = ""
    cloudinary_id: str = ""
    text: str = ""
    source_domain: str = ""
    date_published: str = ""
    url: str = ""

    def has_image(self) -> bool:
        """True when the report carries a real image (the AIID placeholder does not count)."""
        for value in (self.cloudinary_id, self.image_url):
            value = (value or "").strip()
            if value and "placeholder" not in value.lower():
                return True
        return False


@dataclass
class Classification:
    namespace: str
    attributes: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Incident:
    incident_id: int
    title: str
    description: str
    date: str = ""
    editor_notes: str = ""
    report_numbers: list[int] = field(default_factory=list)
    reports: list[Report] = field(default_factory=list)
    entities: dict[str, list[str]] = field(default_factory=dict)
    classifications: list[Classification] = field(default_factory=list)

    @property
    def url(self) -> str:
        return INCIDENT_URL.format(incident_id=self.incident_id)

    def has_report_image(self) -> bool:
        return any(r.has_image() for r in self.reports)


@dataclass
class Snapshot:
    key: str                 # file name in the bucket
    incidents: list[Incident]
    source: str = "weekly"   # "daily" or "weekly"
    modified: str = ""       # ISO date the file was written (dailies are overwritten monthly)

    @property
    def label(self) -> str:
        return f"{self.key} ({self.source}, {self.modified})" if self.modified else self.key

    @property
    def by_id(self) -> dict[int, Incident]:
        return {i.incident_id: i for i in self.incidents}

    def incidents_without_report_images(self) -> list[Incident]:
        return [i for i in self.incidents if not i.has_report_image()]

    def most_recent(self, count: int) -> list[Incident]:
        """The ``count`` highest incident ids, newest first."""
        return sorted(self.incidents, key=lambda i: i.incident_id, reverse=True)[:count]


# --------------------------------------------------------------------------- daily source (R2)
@dataclass
class R2Config:
    account_id: str
    bucket: str
    access_key_id: str
    secret_access_key: str

    @classmethod
    def from_env(cls) -> "R2Config | None":
        values = [os.environ.get(name, "").strip() for name in (
            "CLOUDFLARE_R2_ACCOUNT_ID", "CLOUDFLARE_R2_DAILY_BUCKET_NAME",
            "CLOUDFLARE_R2_ACCESS_KEY_ID", "CLOUDFLARE_R2_SECRET_ACCESS_KEY")]
        if all(values):
            return cls(*values)
        if any(values):
            missing = [n for n, v in zip(("CLOUDFLARE_R2_ACCOUNT_ID", "CLOUDFLARE_R2_DAILY_BUCKET_NAME",
                                          "CLOUDFLARE_R2_ACCESS_KEY_ID", "CLOUDFLARE_R2_SECRET_ACCESS_KEY"), values) if not v]
            log.warning("Daily snapshot bucket is only partly configured; missing %s", ", ".join(missing))
        return None

    def client(self):
        import boto3  # imported lazily so the weekly fallback works without it
        from botocore.config import Config
        return boto3.client(
            "s3",
            endpoint_url=f"https://{self.account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 4}),
        )


@dataclass
class SnapshotRef:
    source: str
    key: str
    modified: str = ""   # YYYY-MM-DD
    size: int = 0

    @property
    def cache_id(self) -> str:
        """Stable id for caching: a daily file keeps its name but changes content monthly."""
        stem = self.key.removesuffix(".tar.bz2")
        return f"{stem}-{self.modified.replace('-', '')}" if self.modified else stem


def latest_daily_ref(r2: R2Config) -> SnapshotRef:
    client = r2.client()
    newest = None
    token = None
    while True:
        kwargs = {"Bucket": r2.bucket, "Prefix": DAILY_PREFIX}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".tar.bz2") and (newest is None or obj["LastModified"] > newest["LastModified"]):
                newest = obj
        token = page.get("NextContinuationToken")
        if not token:
            break
    if newest is None:
        raise RuntimeError(f"No {DAILY_PREFIX}*.tar.bz2 objects found in R2 bucket {r2.bucket}")
    return SnapshotRef("daily", newest["Key"], newest["LastModified"].strftime("%Y-%m-%d"), int(newest.get("Size", 0)))


def daily_ref(r2: R2Config, key: str) -> SnapshotRef:
    """Reference to one specific daily file (used when a run pins a snapshot)."""
    head = r2.client().head_object(Bucket=r2.bucket, Key=key)
    return SnapshotRef("daily", key, head["LastModified"].strftime("%Y-%m-%d"), int(head.get("ContentLength", 0)))


def download_daily(r2: R2Config, ref: SnapshotRef, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{ref.cache_id}.tar.bz2"
    if target.exists() and target.stat().st_size > 0:
        log.info("Using cached snapshot %s", target)
        return target
    for stale in cache_dir.glob("daily-*.tar.bz2"):
        stale.unlink()  # a daily file is only useful for a day; keep the cache dir small
    log.info("Downloading daily snapshot %s (%s, %.1f MB) from R2", ref.key, ref.modified, ref.size / 1e6)
    tmp = target.with_suffix(".part")
    r2.client().download_file(r2.bucket, ref.key, str(tmp))
    tmp.replace(target)
    return target


# --------------------------------------------------------------------------- weekly source (public)
def latest_weekly_ref(session: requests.Session | None = None, timeout: int = 60) -> SnapshotRef:
    """Newest weekly backup listed on the public snapshots page."""
    session = session or requests.Session()
    try:
        resp = session.get(SNAPSHOT_INDEX_URL, timeout=timeout)
        resp.raise_for_status()
        backups = resp.json()["result"]["pageContext"]["backups"]
        backups = [b for b in backups if _BACKUP_KEY_RE.fullmatch(b.get("Key", ""))]
        if backups:
            newest = max(backups, key=lambda b: b["Key"])  # keys embed a timestamp
            return SnapshotRef("weekly", newest["Key"], str(newest.get("LastModified", ""))[:10], int(newest.get("Size", 0)))
    except Exception as exc:  # fall back to scraping the HTML page
        log.warning("Snapshot index JSON unavailable (%s); falling back to the HTML page", exc)
    resp = session.get(SNAPSHOT_PAGE_URL, timeout=timeout)
    resp.raise_for_status()
    keys = set(_BACKUP_KEY_RE.findall(resp.text))
    if not keys:
        raise RuntimeError("Could not find any backup links on the AIID snapshots page")
    return SnapshotRef("weekly", max(keys))


def download_weekly(ref: SnapshotRef, cache_dir: Path, session: requests.Session | None = None,
                    timeout: int = 600) -> Path:
    session = session or requests.Session()
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / ref.key
    if target.exists() and target.stat().st_size > 0:
        log.info("Using cached snapshot %s", target)
        return target
    url = SNAPSHOT_BUCKET_URL + ref.key
    log.info("Downloading weekly snapshot %s", url)
    tmp = target.with_suffix(".part")
    with session.get(url, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    tmp.replace(target)
    log.info("Downloaded %.1f MB", target.stat().st_size / 1e6)
    return target


# --------------------------------------------------------------------------- orchestration
def latest_ref(r2: R2Config | None = None, session: requests.Session | None = None) -> SnapshotRef:
    """Prefer the private daily snapshot when R2 is configured; otherwise the public weekly one."""
    if r2 is not None:
        return latest_daily_ref(r2)
    log.warning("R2 daily snapshot not configured; using the public weekly snapshot instead")
    return latest_weekly_ref(session)


def fetch_snapshot(cache_dir: Path, r2: R2Config | None = None, ref: SnapshotRef | None = None,
                   session: requests.Session | None = None) -> Snapshot:
    ref = ref or latest_ref(r2, session)
    if ref.source == "daily":
        if r2 is None:
            raise RuntimeError("A daily snapshot was requested but R2 is not configured")
        path = download_daily(r2, ref, cache_dir)
    else:
        path = download_weekly(ref, cache_dir, session)
    snapshot = load_snapshot(path, ref.key)
    snapshot.source, snapshot.modified = ref.source, ref.modified
    return snapshot


# --------------------------------------------------------------------------- parsing
def _iter_bson(fileobj) -> Iterator[dict]:
    yield from bson.decode_file_iter(fileobj)


def _decode_value(value) -> str | None:
    """Turn a classification ``value_json`` into a short string, or None when it carries no information."""
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return None
    if isinstance(value, bool) or value in (None, "", [], {}):
        return None
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, list) and all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in value):
        return ", ".join(str(v) for v in value)
    return None  # nested structures (snippets, entity tables) are too noisy for a brief


def load_snapshot(path: Path, key: str | None = None) -> Snapshot:
    """Parse the collections we need out of a snapshot tarball without unpacking it to disk."""
    incidents_raw: list[dict] = []
    reports: dict[int, Report] = {}
    entity_names: dict[str, str] = {}
    classifications_raw: list[dict] = []
    wanted = {
        "/aiidprod/incidents.bson": "incidents",
        "/aiidprod/reports.bson": "reports",
        "/aiidprod/entities.bson": "entities",
        "/aiidprod/classifications.bson": "classifications",
    }
    found: set[str] = set()
    # Stream mode ("r|bz2") reads members sequentially, which is far faster than seeking in bz2.
    with tarfile.open(path, mode="r|bz2") as tar:
        for member in tar:
            name = next((v for suffix, v in wanted.items() if member.name.endswith(suffix)), None)
            if name is None:
                continue
            fh = tar.extractfile(member)
            if name == "incidents":
                incidents_raw = list(_iter_bson(fh))
            elif name == "reports":
                for doc in _iter_bson(fh):
                    number = doc.get("report_number")
                    if number is None:
                        continue
                    reports[int(number)] = Report(
                        report_number=int(number),
                        title=doc.get("title") or "",
                        image_url=doc.get("image_url") or "",
                        cloudinary_id=doc.get("cloudinary_id") or "",
                        text=doc.get("plain_text") or doc.get("text") or "",
                        source_domain=doc.get("source_domain") or "",
                        date_published=str(doc.get("date_published") or "")[:10],
                        url=doc.get("url") or "",
                    )
            elif name == "entities":
                entity_names = {doc["entity_id"]: doc.get("name") or doc["entity_id"]
                                for doc in _iter_bson(fh) if doc.get("entity_id")}
            elif name == "classifications":
                classifications_raw = list(_iter_bson(fh))
            found.add(name)
            if found == set(wanted.values()):
                break
    missing = {"incidents", "reports"} - found
    if missing:
        raise RuntimeError(f"Snapshot {path} is missing collections: {sorted(missing)}")
    for optional in ("entities", "classifications"):
        if optional not in found:
            log.warning("Snapshot %s has no %s collection; briefs will be thinner", path.name, optional)

    by_incident: dict[int, list[Classification]] = {}
    for doc in classifications_raw:
        namespace = doc.get("namespace") or ""
        if namespace in _SKIP_NAMESPACES or doc.get("publish") is False:
            continue
        attributes = []
        for attr in doc.get("attributes") or []:
            value = _decode_value(attr.get("value_json"))
            if value:
                attributes.append((attr.get("short_name") or "", value))
        if not attributes:
            continue
        for incident_id in doc.get("incidents") or []:
            by_incident.setdefault(int(incident_id), []).append(Classification(namespace, attributes))

    incidents: list[Incident] = []
    for doc in incidents_raw:
        if doc.get("incident_id") is None:
            continue
        incident_id = int(doc["incident_id"])
        numbers = [int(n) for n in (doc.get("reports") or [])]
        incidents.append(
            Incident(
                incident_id=incident_id,
                title=(doc.get("title") or "").strip(),
                description=(doc.get("description") or "").strip(),
                date=str(doc.get("date") or ""),
                editor_notes=(doc.get("editor_notes") or "").strip(),
                report_numbers=numbers,
                reports=[reports[n] for n in numbers if n in reports],
                entities={role: [entity_names.get(e, e) for e in (doc.get(field_name) or [])]
                          for role, field_name in _ENTITY_FIELDS.items()},
                classifications=by_incident.get(incident_id, []),
            )
        )
    incidents.sort(key=lambda i: i.incident_id)
    log.info("Snapshot has %d incidents, %d reports, %d entities, %d classified incidents",
             len(incidents), len(reports), len(entity_names), len(by_incident))
    return Snapshot(key=key or path.name, incidents=incidents)


def pick(incidents: Iterable[Incident], ids: Iterable[int]) -> list[Incident]:
    wanted = set(ids)
    return [i for i in incidents if i.incident_id in wanted]
