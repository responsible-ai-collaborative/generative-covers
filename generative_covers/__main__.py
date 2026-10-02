"""Command line entry point: ``python -m generative_covers <command>``.

Commands
--------
snapshot-key   Print a cache id for the newest AIID snapshot (daily if R2 is configured, else weekly).
generate       Generate covers for the most recent incidents that have no report image, and upload them.
manifest       Write site/manifest.json from the covers stored in Cloudinary.

Configuration comes from flags, falling back to environment variables (see README).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from . import aiid, brief as briefing, cloud, imagegen
from .manifest import build_manifest

log = logging.getLogger("generative_covers")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DIR = REPO_ROOT / ".cache" / "aiid-snapshot"
DEFAULT_PROMPT_FILE = REPO_ROOT / "prompt.txt"
DEFAULT_BRIEF_PROMPT_FILE = REPO_ROOT / "brief_prompt.txt"
DEFAULT_MANIFEST = REPO_ROOT / "site" / "manifest.json"
DEFAULT_RECENT_WINDOW = 10


# --------------------------------------------------------------------------- helpers
def env(name: str, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def env_flag(name: str) -> bool:
    return str(env(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int | None) -> int | None:
    value = env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {value!r}")


def parse_ids(text: str | None) -> list[int]:
    if not text:
        return []
    return [int(t) for t in re.split(r"[\s,;]+", text.strip()) if t]


def step_summary(markdown: str) -> None:
    """Append to the GitHub Actions job summary when running in CI; otherwise print it."""
    path = env("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown.rstrip() + "\n\n")
    else:
        print(markdown, file=sys.stderr)


def warn(message: str) -> None:
    log.warning(message)
    if env("GITHUB_ACTIONS"):
        print(f"::warning::{message}")


def configure_cloudinary() -> str:
    cloud_name = env("CLOUDINARY_CLOUD_NAME")
    cloud.configure(cloud_name, env("CLOUDINARY_API_KEY"), env("CLOUDINARY_API_SECRET"))
    cloud.ping()
    return cloud_name


def resolve_ref(args, r2: aiid.R2Config | None) -> aiid.SnapshotRef:
    key = args.snapshot_key
    if not key:
        return aiid.latest_ref(r2)
    if key.startswith(aiid.DAILY_PREFIX):
        if r2 is None:
            raise SystemExit(f"{key} is a daily snapshot but R2 is not configured")
        return aiid.daily_ref(r2, key)
    return aiid.SnapshotRef("weekly", key)


def get_snapshot(args) -> aiid.Snapshot:
    r2 = aiid.R2Config.from_env()
    ref = resolve_ref(args, r2)
    snapshot = aiid.fetch_snapshot(Path(args.cache_dir), r2=r2, ref=ref)
    if snapshot.source != "daily":
        warn("Using the public weekly snapshot; set the CLOUDFLARE_R2_* settings to read the private daily one")
    return snapshot


def _md(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


# --------------------------------------------------------------------------- commands
def cmd_snapshot_key(args) -> int:
    r2 = aiid.R2Config.from_env()
    print(aiid.latest_ref(r2).cache_id)
    return 0


def cmd_generate(args) -> int:
    template = imagegen.load_prompt_template(args.prompt_file)
    brief_instructions = imagegen.load_prompt_template(args.brief_prompt_file)
    version = imagegen.prompt_version(template, brief_instructions)
    output_dir = Path(args.output_dir) if args.output_dir else None
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    api_key = env("OPENROUTER_API_KEY")
    if not api_key and not args.dry_run:
        raise SystemExit("OPENROUTER_API_KEY is not set")

    folder = args.folder
    cloudinary_ready = False
    if not args.no_upload:
        try:
            configure_cloudinary()
            cloudinary_ready = True
        except (ValueError, RuntimeError) as exc:
            if not args.dry_run:
                raise SystemExit(str(exc))
            warn(f"Cloudinary unavailable, so the dry run assumes no covers exist yet: {exc}")

    snapshot = get_snapshot(args)

    # Which incidents are in scope? Explicit ids win; otherwise only the most recent window, so the
    # job follows new incidents and never walks back through the historical backlog.
    if args.incident_ids:
        wanted = parse_ids(args.incident_ids)
        scope = aiid.pick(snapshot.incidents, wanted)
        unknown = sorted(set(wanted) - {c.incident_id for c in scope})
        if unknown:
            warn(f"Incident ids not present in snapshot {snapshot.label}: {unknown}")
        scope_label = f"{len(scope)} requested incidents"
    elif args.recent is not None and args.recent > 0:
        scope = snapshot.most_recent(args.recent)
        ids = [i.incident_id for i in scope]
        scope_label = f"the {len(scope)} most recent incidents ({min(ids)}–{max(ids)})" if ids else "no incidents"
    else:
        scope = list(snapshot.incidents)
        scope_label = f"all {len(scope)} incidents"

    candidates = [i for i in scope if not i.has_report_image()]
    existing = cloud.existing_covers(folder) if cloudinary_ready else {}
    already = [c for c in candidates if c.incident_id in existing]
    todo = [c for c in candidates if args.force or c.incident_id not in existing]
    todo.sort(key=lambda i: i.incident_id, reverse=True)
    deferred = 0
    if args.max_images is not None and args.max_images >= 0:
        deferred = max(0, len(todo) - args.max_images)
        todo = todo[: args.max_images]

    context_line = (
        f"Snapshot `{snapshot.label}` · scope: {scope_label} · {len(candidates)} without a report image · "
        f"{len(already)} already have a cover" + (f" · {deferred} deferred by the per-run cap" if deferred else "")
    )
    log.info("%s · %d to generate", context_line.replace("`", ""), len(todo))

    if args.dry_run:
        rows = "\n".join(f"| {i.incident_id} | {_md(i.title)} |" for i in todo) or "| – | nothing to do |"
        step_summary(f"## Dry run: {len(todo)} covers would be generated\n\n{context_line}\n\n"
                     f"| Incident | Title |\n|---|---|\n{rows}")
        return 0

    if not todo:
        step_summary(f"## Nothing to generate\n\n{context_line}. Every incident in scope that lacks a report "
                     f"image already has a candidate cover.")
        return 0

    def process(incident: aiid.Incident) -> dict:
        started = time.time()
        session = requests.Session()
        try:
            visual = briefing.write_brief(incident, api_key=api_key, instructions=brief_instructions,
                                          model=args.brief_model, session=session)
        except briefing.BriefError as exc:
            warn(f"incident {incident.incident_id}: brief model failed, using the fallback brief ({str(exc)[:200]})")
            visual = briefing.fallback_brief(incident)
        prompt = imagegen.build_prompt(template, incident, visual.as_fields())
        image = imagegen.generate_image(prompt, api_key=api_key, model=args.model, quality=args.quality,
                                        aspect_ratio=args.aspect_ratio, session=session)
        result = {
            "incident_id": incident.incident_id, "title": incident.title, "status": "ok",
            "brief": json.loads(visual.to_json()), "brief_model": visual.model, "brief_fallback": visual.fallback,
            "brief_cost_usd": visual.cost_usd, "image_cost_usd": image.cost_usd,
            "cost_usd": (visual.cost_usd or 0) + (image.cost_usd or 0), "model": image.model, "seconds": None,
        }
        if output_dir:
            stem = output_dir / f"incident-{incident.incident_id}"
            stem.with_suffix(f".{image.extension}").write_bytes(image.data)
            stem.with_suffix(".brief.json").write_text(visual.to_json(), encoding="utf-8")
            stem.with_suffix(".prompt.txt").write_text(prompt, encoding="utf-8")
            result["file"] = str(stem.with_suffix(f".{image.extension}"))
        if not args.no_upload:
            cover = cloud.upload_cover(
                image, incident, folder=folder, quality=args.quality, prompt_version=version,
                overwrite=args.force,
                extra_context={"brief_model": visual.model, "scene": visual.scene,
                               "brief_fallback": "true" if visual.fallback else ""},
            )
            result.update(public_id=cover.public_id, url=cover.url)
        result["seconds"] = round(time.time() - started, 1)
        return result

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {pool.submit(process, inc): inc for inc in todo}
        for future in as_completed(futures):
            incident = futures[future]
            try:
                result = future.result()
                log.info("incident %s ok (%ss, $%.4f) %s", result["incident_id"], result["seconds"],
                         result["cost_usd"], result.get("url") or result.get("file", ""))
            except Exception as exc:  # keep going; one failure must not sink the batch
                result = {"incident_id": incident.incident_id, "title": incident.title,
                          "status": "error", "error": str(exc)[:1000]}
                warn(f"incident {incident.incident_id} failed: {str(exc)[:300]}")
            results.append(result)

    results.sort(key=lambda r: r["incident_id"], reverse=True)
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] != "ok"]
    cost = sum(r.get("cost_usd") or 0 for r in ok)

    if args.results_file:
        Path(args.results_file).write_text(json.dumps({
            "snapshot": snapshot.label, "model": args.model, "brief_model": args.brief_model,
            "quality": args.quality, "prompt_version": version, "generated": len(ok), "failed": len(failed),
            "cost_usd": round(cost, 4), "results": results,
        }, indent=2), encoding="utf-8")

    def row(r: dict) -> str:
        if r["status"] == "ok":
            link = f"[image]({r['url']})" if r.get("url") else r.get("file", "")
            note = " (fallback brief)" if r.get("brief_fallback") else ""
            return f"| {r['incident_id']} | {_md(r['title'])} | ok{note} | {link} |"
        return f"| {r['incident_id']} | {_md(r['title'])} | **failed** | {_md(r.get('error', ''))[:160]} |"

    step_summary(
        f"## Generated {len(ok)} covers" + (f", {len(failed)} failed" if failed else "") + "\n\n"
        f"{context_line} · brief model `{args.brief_model}` · image model `{args.model}` (quality {args.quality}) · "
        f"estimated cost ${cost:.3f}\n\n"
        "| Incident | Title | Status | Result |\n|---|---|---|---|\n" + "\n".join(row(r) for r in results)
    )
    log.info("done: %d generated, %d failed, estimated cost $%.3f", len(ok), len(failed), cost)
    if failed and not ok:
        return 2  # every attempt failed: something systemic is wrong
    return 0


def cmd_manifest(args) -> int:
    error = None
    covers: dict[int, cloud.Cover] = {}
    cloud_name = env("CLOUDINARY_CLOUD_NAME") or ""
    try:
        cloud_name = configure_cloudinary()
        covers = cloud.existing_covers(args.folder)
    except Exception as exc:  # publish an empty gallery that names the problem instead of failing silently
        error = f"Cloudinary unavailable: {exc}"
        warn(error)
    snapshot = None
    if not args.no_snapshot:
        try:
            snapshot = get_snapshot(args)
        except Exception as exc:
            warn(f"Snapshot unavailable; manifest will not flag incidents that gained images: {exc}")
    manifest = build_manifest(covers, snapshot, cloud_name=cloud_name, folder=args.folder, error=error)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote %s with %d covers", out, manifest["cover_count"])
    step_summary(
        f"## Gallery manifest\n\n{manifest['cover_count']} covers listed from `{cloud_name}/{args.folder}`"
        + (f" · snapshot `{snapshot.label}` · {manifest['incidents_without_report_images']} incidents "
           f"without a report image overall" if snapshot else "")
        + (f"\n\n> **Warning:** {error}" if error else "")
    )
    return 0


# --------------------------------------------------------------------------- argparse
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m generative_covers", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--folder", default=env("CLOUDINARY_FOLDER", cloud.DEFAULT_FOLDER),
                       help="Cloudinary folder holding the covers (env CLOUDINARY_FOLDER)")
        p.add_argument("--cache-dir", default=env("SNAPSHOT_CACHE_DIR", str(DEFAULT_CACHE_DIR)),
                       help="where snapshot tarballs are cached (env SNAPSHOT_CACHE_DIR)")
        p.add_argument("--snapshot-key", default=env("SNAPSHOT_KEY"),
                       help="pin a snapshot file (daily-DD.tar.bz2 or backup-….tar.bz2) instead of the newest (env SNAPSHOT_KEY)")

    p = sub.add_parser("snapshot-key", help="print a cache id for the newest snapshot")
    p.set_defaults(func=cmd_snapshot_key)

    p = sub.add_parser("generate", help="generate and upload covers")
    common(p)
    p.add_argument("--recent", type=int, default=env_int("RECENT_WINDOW", DEFAULT_RECENT_WINDOW),
                   help="only consider the N most recent incident ids; 0 or negative means all incidents "
                        f"(env RECENT_WINDOW, default {DEFAULT_RECENT_WINDOW})")
    p.add_argument("--max-images", type=int, default=env_int("MAX_IMAGES", -1),
                   help="cap on new images per run; negative means no cap (env MAX_IMAGES, default no cap)")
    p.add_argument("--incident-ids", default=env("INCIDENT_IDS"),
                   help="comma-separated incident ids to process instead of the recent window")
    p.add_argument("--force", action="store_true", default=env_flag("FORCE"),
                   help="regenerate and overwrite covers that already exist (env FORCE=true)")
    p.add_argument("--dry-run", action="store_true", default=env_flag("DRY_RUN"),
                   help="only report what would be generated (env DRY_RUN=true)")
    p.add_argument("--model", default=env("IMAGE_MODEL", imagegen.DEFAULT_MODEL),
                   help="OpenRouter image model (env IMAGE_MODEL)")
    p.add_argument("--brief-model", default=env("BRIEF_MODEL", briefing.DEFAULT_BRIEF_MODEL),
                   help="OpenRouter text model that writes the visual brief (env BRIEF_MODEL)")
    p.add_argument("--quality", default=env("IMAGE_QUALITY", imagegen.DEFAULT_QUALITY),
                   choices=["auto", "low", "medium", "high"], help="(env IMAGE_QUALITY)")
    p.add_argument("--aspect-ratio", default=env("IMAGE_ASPECT_RATIO", imagegen.DEFAULT_ASPECT_RATIO),
                   help="(env IMAGE_ASPECT_RATIO)")
    p.add_argument("--concurrency", type=int, default=env_int("CONCURRENCY", 4),
                   help="parallel generations (env CONCURRENCY, default 4)")
    p.add_argument("--prompt-file", default=env("PROMPT_FILE", str(DEFAULT_PROMPT_FILE)),
                   help="stage-2 image prompt template")
    p.add_argument("--brief-prompt-file", default=env("BRIEF_PROMPT_FILE", str(DEFAULT_BRIEF_PROMPT_FILE)),
                   help="stage-1 instructions for the brief model")
    p.add_argument("--output-dir", default=env("OUTPUT_DIR"),
                   help="also save generated images, briefs and prompts to this directory")
    p.add_argument("--no-upload", action="store_true", default=env_flag("NO_UPLOAD"),
                   help="skip Cloudinary entirely (use with --output-dir for local experiments)")
    p.add_argument("--results-file", default=env("RESULTS_FILE"), help="write a JSON run report here")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("manifest", help="write the gallery manifest from Cloudinary")
    common(p)
    p.add_argument("--out", default=str(DEFAULT_MANIFEST))
    p.add_argument("--no-snapshot", action="store_true",
                   help="do not consult the AIID snapshot (faster; omits 'still missing' flags)")
    p.set_defaults(func=cmd_manifest)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # The Cloudinary SDK shares one small connection pool; with concurrent uploads urllib3 logs a
    # harmless "connection pool is full" warning for every discarded connection. Keep it quiet.
    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
    for noisy in ("boto3", "botocore", "s3transfer", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if getattr(args, "no_upload", False) and not getattr(args, "output_dir", None) \
            and not getattr(args, "dry_run", False):
        raise SystemExit("--no-upload needs --output-dir, otherwise generated images would be discarded")
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
