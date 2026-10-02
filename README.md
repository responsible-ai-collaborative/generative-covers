# generative-covers

Candidate cover images for [AI Incident Database](https://incidentdatabase.ai) (AIID) incidents whose
reports have no image. A daily GitHub Actions job looks at the most recent incidents in the latest
database snapshot, writes a short visual brief for each one from everything the database knows about it,
asks an OpenAI image model (through OpenRouter) for a flat, Tufte-inspired pictogram, stores the result
in Cloudinary under the incident number, and publishes a gallery where editors can preview the images
and copy their URLs.

**Gallery:** https://responsible-ai-collaborative.github.io/generative-covers/

## How it works

1. **Read the newest snapshot.** The job prefers the private *daily* MongoDB dump that the AIID writes
   to Cloudflare R2 every morning (`daily-DD.tar.bz2`, overwritten monthly). When the R2 settings are
   missing it falls back to the public *weekly* snapshot listed at
   https://incidentdatabase.ai/research/snapshots/ and says so in the job summary. (The GraphQL API only
   accepts browser origins, so snapshots are the supported route for automation.)
2. **Look only at the most recent incidents.** By default the 10 highest incident IDs are in scope. An
   incident qualifies when none of its reports has a real `cloudinary_id`/`image_url` (the AIID
   `placeholder.svg` does not count). Incidents that already have a candidate cover in Cloudinary are
   skipped, so each incident is generated once and a day with no new incidents generates nothing. The job
   never walks back through the historical backlog unless asked to (`recent_window` = 0).
3. **Write a visual brief** (stage 1). A dossier with the title, description, editor notes, named
   developer/deployer/harmed-party/implicated-system entities, taxonomy classifications (MIT, CSET, GMF)
   and up to 24,000 characters of report text goes to a text model, `openai/gpt-5-mini` by default, with
   the instructions in [`brief_prompt.txt`](brief_prompt.txt). It answers with JSON: scene, subject, key
   details, accent colour, things to avoid, mood. The rules there keep text, logos, real likenesses and
   anything explicit out of the picture, and turn sensitive incidents into restrained symbols so the image
   model's safety filter accepts them. If the text model fails, a plain brief built from the title and
   description is used instead and the run report says so.
4. **Generate the image** (stage 2). The brief's fields are dropped into the fixed design brief in
   [`prompt.txt`](prompt.txt) and sent to OpenRouter's Image API. Default model `openai/gpt-5-image-mini`,
   `high` quality, 3:2 aspect ratio (crops well to the 16:9 frame the AIID uses), opaque background. Any
   transparency that comes back is flattened onto white before upload.
5. **Store.** The PNG is uploaded to Cloudinary as `generative-covers/incident-<number>`, tagged
   `generative-cover` and `incident-<number>`, with the title, models, quality, prompt version, the
   brief's scene text and the generation time stored as contextual metadata.
6. **Publish.** A manifest of everything in the Cloudinary folder is written to `site/manifest.json` and
   the static gallery in [`site/`](site) is deployed to GitHub Pages. The manifest also marks incidents
   that have since received a real report image.

Everything lives in the workflow [`.github/workflows/generate-covers.yml`](.github/workflows/generate-covers.yml)
and the Python package [`generative_covers/`](generative_covers).

## Setup

Repository **secrets** (Settings → Secrets and variables → Actions):

| Secret | Purpose |
| --- | --- |
| `OPENROUTER_API_KEY` | OpenRouter key used for both the brief model and the image model |
| `CLOUDINARY_API_KEY` | Cloudinary API key. On Cloudinary's Roles and Permissions system the key needs the **Master Admin** role (Console Settings → API Keys), otherwise uploads fail with `missing permissions (actions=["create"])` |
| `CLOUDINARY_API_SECRET` | Cloudinary API secret |
| `CLOUDFLARE_R2_ACCESS_KEY_ID` | R2 access key with **read** access to the daily snapshot bucket |
| `CLOUDFLARE_R2_SECRET_ACCESS_KEY` | Its secret |

Repository **variables**:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLOUDINARY_CLOUD_NAME` | *(required)* | Cloud name of the Cloudinary product environment that receives the images. A variable rather than a secret because it is public and would otherwise be masked in job summaries |
| `CLOUDFLARE_R2_ACCOUNT_ID` | *(required for daily snapshots)* | Cloudflare account that owns the snapshot buckets |
| `CLOUDFLARE_R2_DAILY_BUCKET_NAME` | *(required for daily snapshots)* | Private bucket holding `daily-DD.tar.bz2` |
| `CLOUDINARY_FOLDER` | `generative-covers` | Folder (public ID prefix) for the covers |
| `IMAGE_MODEL` | `openai/gpt-5-image-mini` | Any OpenRouter model that outputs images |
| `BRIEF_MODEL` | `openai/gpt-5-mini` | Any OpenRouter chat model with JSON output |
| `CONCURRENCY` | `4` | Parallel generations per run |

GitHub Pages must be set to **Source: GitHub Actions** (Settings → Pages). The workflow deploys to the
`github-pages` environment.

## Running

- **Daily:** the `schedule` trigger runs at 09:17 UTC, two hours after the AIID daily snapshot is
  written. It generates covers for incidents among the 10 most recent that lack a report image and do
  not have a cover yet, then republishes the gallery. No new incidents, no images.
- **Manually:** Actions → *Generate incident covers* → *Run workflow*. Inputs:
  - `recent_window` – how many of the most recent incident IDs to consider (default 10; `0` means every
    incident, which at `high` quality costs about $0.05 per incident without an image).
  - `incident_ids` – comma-separated incident numbers to process instead; overrides the window.
  - `force` – regenerate even if a cover exists (combine with `incident_ids`; overwrites the asset).
  - `dry_run` – only list what would be generated. Costs nothing.
  - `quality` – `low`, `medium` or `high` (default).
- **On push to `main`** touching `site/`, the code or the workflow, only the gallery is republished.

Each run writes a summary table (incident, title, status, link) to the job summary, uploads
`results.json` (including every brief) as an artifact, and emits a warning annotation for any incident
that failed or needed the fallback brief. Failed incidents are retried on the next run while they stay in
the window. The job exits non-zero only when every attempted generation failed or when configuration is
missing.

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

# What would be generated? (uses the public weekly snapshot unless the CLOUDFLARE_R2_* variables are set)
python -m generative_covers generate --dry-run

# Generate one incident locally without touching Cloudinary; writes the image, brief and prompt to output/
OPENROUTER_API_KEY=... python -m generative_covers generate --no-upload --output-dir output --incident-ids 1714

# Full run against Cloudinary and the daily snapshot
export OPENROUTER_API_KEY=... CLOUDINARY_CLOUD_NAME=... CLOUDINARY_API_KEY=... CLOUDINARY_API_SECRET=...
export CLOUDFLARE_R2_ACCOUNT_ID=... CLOUDFLARE_R2_DAILY_BUCKET_NAME=... CLOUDFLARE_R2_ACCESS_KEY_ID=... CLOUDFLARE_R2_SECRET_ACCESS_KEY=...
python -m generative_covers generate

# Build the gallery manifest and serve the site
python -m generative_covers manifest
python -m http.server --directory site 8000   # http://localhost:8000
```

`python -m generative_covers generate --help` lists every option. All options can also be supplied as
environment variables (`RECENT_WINDOW`, `MAX_IMAGES`, `INCIDENT_IDS`, `FORCE`, `DRY_RUN`, `IMAGE_MODEL`,
`BRIEF_MODEL`, `IMAGE_QUALITY`, `IMAGE_ASPECT_RATIO`, `CONCURRENCY`, `CLOUDINARY_FOLDER`,
`SNAPSHOT_CACHE_DIR`, `SNAPSHOT_KEY`, `OUTPUT_DIR`, `NO_UPLOAD`, `RESULTS_FILE`), which is how the
workflow passes its inputs through.

## Tuning the prompts

- [`brief_prompt.txt`](brief_prompt.txt) tells the text model how to turn an incident into a drawable
  scene: at most three elements, no words, anonymous people, sensitive material rendered symbolically.
- [`prompt.txt`](prompt.txt) is the design brief the image model receives. `{scene}`, `{subject}`,
  `{key_details}`, `{accent}`, `{avoid}` and `{mood}` come from the brief; `{number}`, `{title}` and
  `{description}` are also available. It follows the ordering the GPT Image prompting guides recommend:
  deliverable and purpose, scene and subject, key details, style system, background, exclusions last.

Each uploaded image records a short hash of both templates as `prompt_version` in its Cloudinary
context, so covers made with an older prompt can be identified and regenerated with `incident_ids` +
`force`.

## Cloudinary layout

| Item | Value |
| --- | --- |
| Public ID | `generative-covers/incident-<incident_id>` |
| Tags | `generative-cover`, `incident-<incident_id>` |
| Context | `incident_id`, `title`, `model`, `brief_model`, `quality`, `prompt_version`, `scene`, `generated_at`, `brief_fallback` (only when the text model failed) |

The gallery only lists assets whose public ID matches `…/incident-<number>` inside the folder, so other
files in the same folder are ignored.

## Costs

Measured with the defaults: the brief costs about $0.0016 and the image about $0.05, so roughly $0.052
per cover and about 45 seconds each. A day with three new image-less incidents costs about 15 cents.
The OpenRouter key's spending limit is visible at https://openrouter.ai/settings/keys.

## Troubleshooting

- **`cloud_name mismatch`** – the API key belongs to a different Cloudinary product environment than
  `CLOUDINARY_CLOUD_NAME`. Copy the cloud name from the Cloudinary console dashboard of the environment
  the key came from.
- **`Request forbidden due to missing permissions (actions=["create"])`** – the API key can read but not
  upload. Give it the Master Admin role in Console Settings → API Keys, or use the environment's root API key.
- **"Using the public weekly snapshot"** warning – one of the four `CLOUDFLARE_R2_*` settings is missing
  or the key cannot list the bucket. The job still works, up to a week behind.
- **Gallery shows "The listing could not be built"** – the publish job could not reach Cloudinary;
  the message names the cause. The job still deploys so the problem is visible.
- **Nothing generated** – no incident among the most recent ones lacks an image, or they all have covers.
  Run with `dry_run` to see the candidate list, or widen `recent_window`.
- **`rejected by the safety system`** – OpenAI's image model refused the prompt. The brief rules make this
  rare; when it happens the incident is logged as failed and retried on the next run while it is in scope.
- **OpenRouter errors** – transient 429/5xx responses are retried with backoff.

## License

Apache 2.0, see [LICENSE](LICENSE).
