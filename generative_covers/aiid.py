"""Read incidents and reports from the public AI Incident Database research snapshots.

The AIID publishes a weekly MongoDB dump at https://incidentdatabase.ai/research/snapshots/.
The GraphQL API is restricted to browser origins, so the snapshot is the supported way for
automation to see the whole database.
"""

from __future__ import annotations

import logging
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

_BACKUP_KEY_RE = re.compile(r"backup-\d{14}\.tar\.bz2")


@dataclass
class Report:
    report_number: int
    title: str = ""
    image_url: str = ""
    cloudinary_id: str = ""

    def has_image(self) -> bool:
        """True when the report carries a real image (the AIID placeholder does not count)."""
        for value in (self.cloudinary_id, self.image_url):
            value = (value or "").strip()
            if value and "placeholder" not in value.lower():
                return True
        return False


@dataclass
class Incident:
    incident_id: int
    title: str
    description: str
    date: str = ""
    report_numbers: list[int] = field(default_factory=list)
    reports: list[Report] = field(default_factory=list)

    @property
    def url(self) -> str:
        return INCIDENT_URL.format(incident_id=self.incident_id)

    def has_report_image(self) -> bool:
        return any(r.has_image() for r in self.reports)


@dataclass
class Snapshot:
    key: str
    incidents: list[Incident]

    @property
    def by_id(self) -> dict[int, Incident]:
        return {i.incident_id: i for i in self.incidents}

    def incidents_without_report_images(self) -> list[Incident]:
        return [i for i in self.incidents if not i.has_report_image()]


def latest_snapshot_key(session: requests.Session | None = None, timeout: int = 60) -> str:
    """Return the object key (file name) of the newest weekly backup."""
    session = session or requests.Session()
    try:
        resp = session.get(SNAPSHOT_INDEX_URL, timeout=timeout)
        resp.raise_for_status()
        backups = resp.json()["result"]["pageContext"]["backups"]
        keys = [b["Key"] for b in backups if _BACKUP_KEY_RE.fullmatch(b.get("Key", ""))]
        if keys:
            return max(keys)  # keys embed a timestamp, so lexical max == newest
    except Exception as exc:  # fall back to scraping the HTML page
        log.warning("Snapshot index JSON unavailable (%s); falling back to the HTML page", exc)
    resp = session.get(SNAPSHOT_PAGE_URL, timeout=timeout)
    resp.raise_for_status()
    keys = set(_BACKUP_KEY_RE.findall(resp.text))
    if not keys:
        raise RuntimeError("Could not find any backup links on the AIID snapshots page")
    return max(keys)


def download_snapshot(key: str, cache_dir: Path, session: requests.Session | None = None,
                      timeout: int = 600) -> Path:
    """Download the snapshot tarball into cache_dir (skipped when already present)."""
    session = session or requests.Session()
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / key
    if target.exists() and target.stat().st_size > 0:
        log.info("Using cached snapshot %s", target)
        return target
    url = SNAPSHOT_BUCKET_URL + key
    log.info("Downloading snapshot %s", url)
    tmp = target.with_suffix(".part")
    with session.get(url, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    tmp.replace(target)
    log.info("Downloaded %.1f MB", target.stat().st_size / 1e6)
    return target


def _iter_bson(fileobj) -> Iterator[dict]:
    yield from bson.decode_file_iter(fileobj)


def load_snapshot(path: Path, key: str | None = None) -> Snapshot:
    """Parse incidents and reports out of a snapshot tarball without unpacking it to disk."""
    incidents_raw: list[dict] = []
    reports: dict[int, Report] = {}
    found = set()
    # Stream mode ("r|bz2") reads members sequentially, which is far faster than seeking in bz2.
    with tarfile.open(path, mode="r|bz2") as tar:
        for member in tar:
            name = member.name
            if name.endswith("/aiidprod/incidents.bson"):
                fh = tar.extractfile(member)
                incidents_raw = list(_iter_bson(fh))
                found.add("incidents")
            elif name.endswith("/aiidprod/reports.bson"):
                fh = tar.extractfile(member)
                for doc in _iter_bson(fh):
                    number = doc.get("report_number")
                    if number is None:
                        continue
                    reports[int(number)] = Report(
                        report_number=int(number),
                        title=doc.get("title") or "",
                        image_url=doc.get("image_url") or "",
                        cloudinary_id=doc.get("cloudinary_id") or "",
                    )
                found.add("reports")
            if found == {"incidents", "reports"}:
                break
    missing = {"incidents", "reports"} - found
    if missing:
        raise RuntimeError(f"Snapshot {path} is missing collections: {sorted(missing)}")

    incidents: list[Incident] = []
    for doc in incidents_raw:
        if doc.get("incident_id") is None:
            continue
        numbers = [int(n) for n in (doc.get("reports") or [])]
        incidents.append(
            Incident(
                incident_id=int(doc["incident_id"]),
                title=(doc.get("title") or "").strip(),
                description=(doc.get("description") or "").strip(),
                date=str(doc.get("date") or ""),
                report_numbers=numbers,
                reports=[reports[n] for n in numbers if n in reports],
            )
        )
    incidents.sort(key=lambda i: i.incident_id)
    log.info("Snapshot has %d incidents and %d reports", len(incidents), len(reports))
    return Snapshot(key=key or path.name, incidents=incidents)


def fetch_snapshot(cache_dir: Path, key: str | None = None,
                   session: requests.Session | None = None) -> Snapshot:
    """Resolve the newest snapshot key (unless given), download it, and parse it."""
    session = session or requests.Session()
    key = key or latest_snapshot_key(session)
    path = download_snapshot(key, cache_dir, session)
    return load_snapshot(path, key)


def pick(incidents: Iterable[Incident], ids: Iterable[int]) -> list[Incident]:
    wanted = set(ids)
    return [i for i in incidents if i.incident_id in wanted]
