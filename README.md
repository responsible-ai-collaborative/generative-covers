# generative-covers

Candidate cover images for [AI Incident Database](https://incidentdatabase.ai) (AIID) incidents whose
reports have no image. A scheduled GitHub Actions job finds those incidents in the latest public
snapshot of the database, asks a low-cost OpenAI image model (through OpenRouter) for a simple,
Tufte-inspired illustration of each one, stores the result in Cloudinary under the incident number, and
publishes a gallery where editors can preview the images and copy their URLs.

**Gallery:** https://responsible-ai-collaborative.github.io/generative-covers/

## How it works

1. **Find incidents without images.** The job downloads the newest weekly MongoDB snapshot from
   https://incidentdatabase.ai/research/snapshots/ (the GraphQL API only accepts browser origins) and
   reads `incidents` and `reports`. An incident qualifies when none of its reports has a real
   `cloudinary_id`/`image_url` (the AIID `placeholder.svg` does not count). Incidents with no reports at
   all qualify too.
2. **Skip incidents that already have a cover.** Cloudinary is listed under the configured folder, so
   each incident is generated once. Re-running is cheap and idempotent.
3. **Generate.** For every remaining incident (newest first, up to the per-run limit) the prompt in
   [`prompt.txt`](prompt.txt) is filled with the incident number, title and description and sent to
   OpenRouter's Image API. The default model is `openai/gpt-5-image-mini` at `low` quality and a 3:2
   aspect ratio, which costs about $0.0035 per image and crops well to the 16:9 frame the AIID uses.
4. **Store.** The PNG is uploaded to Cloudinary as `generative-covers/incident-<number>`, tagged
   `generative-cover` and `incident-<number>`, with the title, model, quality, prompt version and
   generation time stored as contextual metadata.
5. **Publish.** A manifest of everything in the Cloudinary folder is written to `site/manifest.json` and
   the static gallery in [`site/`](site) is deployed to GitHub Pages. The manifest also marks incidents
   that have since received a real report image, so editors can focus on the ones still missing one.

Everything lives in the workflow [`.github/workflows/generate-covers.yml`](.github/workflows/generate-covers.yml)
and the Python package [`generative_covers/`](generative_covers).

## Setup

Repository **secrets** (Settings → Secrets and variables → Actions):

| Secret | Purpose |
| --- | --- |
| `OPENROUTER_API_KEY` | OpenRouter key used for image generation |
| `CLOUDINARY_API_KEY` | Cloudinary API key. On Cloudinary's Roles and Permissions system the key needs the **Master Admin** role (Console Settings → API Keys), otherwise uploads fail with `missing permissions (actions=["create"])` |
| `CLOUDINARY_API_SECRET` | Cloudinary API secret |

Repository **variables** (Settings → Secrets and variables → Actions → Variables):

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLOUDINARY_CLOUD_NAME` | *(required)* | Cloud name of the Cloudinary product environment that receives the images. A variable rather than a secret because it is public and would otherwise be masked in job summaries |
| `CLOUDINARY_FOLDER` | `generative-covers` | Folder (public ID prefix) for the covers |
| `IMAGE_MODEL` | `openai/gpt-5-image-mini` | Any OpenRouter model that outputs images |
| `CONCURRENCY` | `4` | Parallel generations per run |

GitHub Pages must be set to **Source: GitHub Actions** (Settings → Pages). The workflow deploys to the
`github-pages` environment.

## Running

- **Daily:** the `schedule` trigger runs at 06:17 UTC. Each run generates up to 100 new covers
  (newest incidents first) and republishes the gallery, so a backlog drains over a few days.
- **Manually:** Actions → *Generate incident covers* → *Run workflow*. Inputs:
  - `max_images` – cap for this run (`-1` for no cap; the whole current backlog of ~740 incidents costs
    roughly $3 at `low` quality).
  - `incident_ids` – comma-separated incident numbers to process instead of the whole backlog.
  - `force` – regenerate even if a cover exists (combine with `incident_ids`; overwrites the asset).
  - `dry_run` – only list what would be generated. Costs nothing.
  - `quality` – `low` (default), `medium` or `high`.
- **On push to `main`** touching `site/`, the code or the workflow, only the gallery is republished.

Each run writes a summary table (incident, title, status, link) to the job summary, uploads
`results.json` as an artifact, and emits a warning annotation for any incident that failed. Failed
incidents are simply retried on the next run. The job exits non-zero only when every attempted
generation failed or when configuration is missing.

## Using a cover on the AIID

Open the gallery, search by incident number or title, click an image to preview it, then:

- **Copy URL** gives the Cloudinary delivery URL, e.g.
  `https://res.cloudinary.com/<cloud>/image/upload/v…/generative-covers/incident-1714.png`.
  Paste it into the report's image URL field in the AIID incident editor. The AIID derives its own
  `cloudinary_id` (`reports/<url without scheme>`) and auto-uploads the file into its own account, exactly
  as it does for images from news sites.
- **Copy ID** gives the Cloudinary public ID (`generative-covers/incident-1714`), useful when working
  directly inside the Cloudinary account that holds the covers.

The toggle *Only incidents still without a report image* hides covers for incidents that have gained a
real image since generation.

## Local development

```bash
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt   # or python -m venv + pip
source .venv/bin/activate

# What would be generated? (downloads ~110 MB snapshot into .cache/ once per week)
python -m generative_covers generate --dry-run --max-images 10

# Generate a single incident locally without touching Cloudinary
OPENROUTER_API_KEY=... python -m generative_covers generate --no-upload --output-dir output --incident-ids 1714

# Full run against Cloudinary
export OPENROUTER_API_KEY=... CLOUDINARY_CLOUD_NAME=... CLOUDINARY_API_KEY=... CLOUDINARY_API_SECRET=...
python -m generative_covers generate --max-images 5

# Build the gallery manifest and serve the site
python -m generative_covers manifest
python -m http.server --directory site 8000   # http://localhost:8000
```

`python -m generative_covers --help` and `python -m generative_covers generate --help` list every
option. All options can also be supplied as environment variables (`MAX_IMAGES`, `INCIDENT_IDS`,
`FORCE`, `DRY_RUN`, `IMAGE_MODEL`, `IMAGE_QUALITY`, `IMAGE_ASPECT_RATIO`, `CONCURRENCY`, `ORDER`,
`CLOUDINARY_FOLDER`, `SNAPSHOT_CACHE_DIR`, `SNAPSHOT_KEY`, `OUTPUT_DIR`, `NO_UPLOAD`, `RESULTS_FILE`),
which is how the workflow passes its inputs through.

## Tuning the prompt

Edit [`prompt.txt`](prompt.txt). The placeholders `{number}`, `{title}` and `{description}` are
substituted per incident. Each uploaded image records a short hash of the template as
`prompt_version` in its Cloudinary context, so covers made with an older prompt can be identified and
regenerated with `incident_ids` + `force`.

A note from testing: at `low` quality the model tends to render the incident title inside the image and
misspells it. If that proves distracting, appending a sentence such as "Do not include any text or
lettering." to the prompt, or switching `quality` to `medium`, are the two cheapest fixes.

## Cloudinary layout

| Item | Value |
| --- | --- |
| Public ID | `generative-covers/incident-<incident_id>` |
| Tags | `generative-cover`, `incident-<incident_id>` |
| Context | `incident_id`, `title`, `model`, `quality`, `prompt_version`, `generated_at` |

The gallery only lists assets whose public ID matches `…/incident-<number>` inside the folder, so other
files in the same folder are ignored.

## Troubleshooting

- **`cloud_name mismatch`** – the API key belongs to a different Cloudinary product environment than
  `CLOUDINARY_CLOUD_NAME`. Copy the cloud name from the Cloudinary console dashboard of the environment
  the key came from.
- **`Request forbidden due to missing permissions (actions=["create"])`** – the API key can read but not
  upload. Give it the Master Admin role in Console Settings → API Keys, or use the environment's root API key.
- **Gallery shows "The listing could not be built"** – the publish job could not reach Cloudinary;
  the message names the cause. The job still deploys so the problem is visible.
- **Nothing generated** – all incidents without images already have covers, or `max_images` was 0.
  Run with `dry_run` to see the candidate list.
- **OpenRouter errors** – transient 429/5xx responses are retried three times with backoff; the key's
  spending limit and balance are visible at https://openrouter.ai/settings/keys.

## License

Apache 2.0, see [LICENSE](LICENSE).
