"""Read incidents, reports, entities and classifications from an AI Incident Database snapshot.

Two sources, same tarball format (a mongodump of the ``aiidprod`` database):

* **Daily** (preferred): ``daily-DD.tar.bz2`` (DD = day of month, US Eastern) behind a base URL that is
  not published and must be supplied through ``AIID_DAILY_SNAPSHOT_URL``. Each file is overwritten a
  month later, so a file is only used when it was written within the last 24 hours.
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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
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


# --------------------------------------------------------------------------- daily source
DAILY_URL_ENV = "AIID_DAILY_SNAPSHOT_URL"
DAILY_MAX_AGE_HOURS = 24
_DAILY_TZ = ZoneInfo("America/New_York")  # the AIID names the file after the US Eastern calendar day


@dataclass
class DailyFeed:
    """Location of the rotating daily snapshots. The URL is a secret: never log or print it."""
    base_url: str
    max_age_hours: int = DAILY_MAX_AGE_HOURS

    @classmethod
    def from_env(cls) -> "DailyFeed | None":
        url = os.environ.get(DAILY_URL_ENV, "").strip()
        return cls(url.rstrip("/")) if url else None

    def url_for(self, key: str) -> str:
        return f"{self.base_url}/{key}"

    def candidate_keys(self, now: datetime | None = None) -> list[str]:
        """Today's and yesterday's file names in US Eastern and UTC; duplicates removed, order kept."""
        now = now or datetime.now(timezone.utc)
        days = []
        for tz in (_DAILY_TZ, timezone.utc):
            local = now.astimezone(tz)
            days += [local.strftime("%d"), (local - timedelta(days=1)).strftime("%d")]
        seen: list[str] = []
        for day in days:
            key = f"{DAILY_PREFIX}{day}.tar.bz2"
            if key not in seen:
                seen.append(key)
        return seen


@dataclass
class SnapshotRef:
    source: str
    key: str
    modified: str = ""   # YYYY-MM-DD the file was written
    size: int = 0
    written_at: datetime | None = None

    @property
    def cache_id(self) -> str:
        """Stable id for caching: a daily file keeps its name but changes content monthly."""
        stem = self.key.removesuffix(".tar.bz2")
        return f"{stem}-{self.modified.replace('-', '')}" if self.modified else stem


def _head(feed: DailyFeed, key: str, session: requests.Session, timeout: int = 60) -> SnapshotRef | None:
    try:
        resp = session.head(feed.url_for(key), timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        log.warning("HEAD %s failed: %s", key, type(exc).__name__)
        return None
    if resp.status_code != 200 or not resp.headers.get("Last-Modified"):
        return None
    written = parsedate_to_datetime(resp.headers["Last-Modified"]).astimezone(timezone.utc)
    return SnapshotRef("daily", key, written.strftime("%Y-%m-%d"), int(resp.headers.get("Content-Length") or 0), written)


def latest_daily_ref(feed: DailyFeed, session: requests.Session | None = None,
                     now: datetime | None = None) -> SnapshotRef | None:
    """The freshest daily file written within the allowed age, or None when none qualifies.

    Files are overwritten a month later, so a name alone says nothing about the content's age; only the
    Last-Modified header does. Yesterday's file is also considered in case today's has not landed yet.
    """
    session = session or requests.Session()
    now = now or datetime.now(timezone.utc)
    refs = [r for r in (_head(feed, key, session) for key in feed.candidate_keys(now)) if r]
    if not refs:
        log.warning("No daily snapshot files were reachable")
        return None
    newest = max(refs, key=lambda r: r.written_at)
    age = now - newest.written_at
    if age > timedelta(hours=feed.max_age_hours):
        log.warning("Newest daily snapshot %s was written %.1f hours ago (limit %d h); treating it as stale",
                    newest.key, age.total_seconds() / 3600, feed.max_age_hours)
        return None
    log.info("Daily snapshot %s written %s (%.1f hours ago)", newest.key,
             newest.written_at.strftime("%Y-%m-%d %H:%M UTC"), age.total_seconds() / 3600)
    return newest


def daily_ref(feed: DailyFeed, key: str, session: requests.Session | None = None) -> SnapshotRef:
    """Reference to one specific daily file (used when a run pins a snapshot); freshness is enforced."""
    ref = _head(feed, key, session or requests.Session())
    if ref is None:
        raise RuntimeError(f"Daily snapshot {key} is not available")
    age = datetime.now(timezone.utc) - ref.written_at
    if age > timedelta(hours=feed.max_age_hours):
        raise RuntimeError(f"Daily snapshot {key} was written {age.total_seconds() / 3600:.1f} hours ago; "
                           f"refusing a file older than {feed.max_age_hours} hours")
    return ref


def download_daily(feed: DailyFeed, ref: SnapshotRef, cache_dir: Path, session: requests.Session | None = None,
                   timeout: int = 600) -> Path:
    session = session or requests.Session()
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{ref.cache_id}.tar.bz2"
    if target.exists() and target.stat().st_size > 0:
        log.info("Using cached snapshot %s", target)
        return target
    for stale in cache_dir.glob(f"{DAILY_PREFIX}*.tar.bz2"):
        stale.unlink()  # a daily file is only useful for a day; keep the cache dir small
    log.info("Downloading daily snapshot %s (%s, %.1f MB)", ref.key, ref.modified, ref.size / 1e6)
    tmp = target.with_suffix(".part")
    with session.get(feed.url_for(ref.key), stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    if ref.size and tmp.stat().st_size != ref.size:
        tmp.unlink()
        raise RuntimeError(f"Daily snapshot {ref.key} download was incomplete")
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
def latest_ref(feed: DailyFeed | None = None, session: requests.Session | None = None) -> SnapshotRef:
    """Prefer a fresh daily snapshot when the feed is configured; otherwise the public weekly one."""
    session = session or requests.Session()
    if feed is not None:
        ref = latest_daily_ref(feed, session)
        if ref is not None:
            return ref
        log.warning("Falling back to the public weekly snapshot")
    else:
        log.warning("%s is not set; using the public weekly snapshot", DAILY_URL_ENV)
    return latest_weekly_ref(session)


def fetch_snapshot(cache_dir: Path, feed: DailyFeed | None = None, ref: SnapshotRef | None = None,
                   session: requests.Session | None = None) -> Snapshot:
    session = session or requests.Session()
    ref = ref or latest_ref(feed, session)
    if ref.source == "daily":
        if feed is None:
            raise RuntimeError(f"A daily snapshot was requested but {DAILY_URL_ENV} is not set")
        path = download_daily(feed, ref, cache_dir, session)
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
