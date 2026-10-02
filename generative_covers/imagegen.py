"""Image generation through OpenRouter's Image API (https://openrouter.ai/docs/features/multimodal/image-generation)."""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import time
from dataclasses import dataclass

import requests

from .aiid import Incident

log = logging.getLogger(__name__)

OPENROUTER_IMAGES_URL = "https://openrouter.ai/api/v1/images"
DEFAULT_MODEL = "openai/gpt-5-image-mini"  # cheapest OpenAI image model on OpenRouter
DEFAULT_QUALITY = "high"
DEFAULT_ASPECT_RATIO = "3:2"  # closest supported ratio to the 16:9 crop the AIID displays
DEFAULT_BACKGROUND = "opaque"  # never ask for transparency; covers must sit on solid white
APP_REFERER = "https://github.com/responsible-ai-collaborative/generative-covers"
APP_TITLE = "AIID generative covers"

_RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


class ImageGenerationError(RuntimeError):
    pass


@dataclass
class GeneratedImage:
    data: bytes
    media_type: str
    model: str
    cost_usd: float | None
    prompt: str

    @property
    def extension(self) -> str:
        return {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(self.media_type, "png")


def load_prompt_template(path) -> str:
    text = open(path, encoding="utf-8").read().strip()
    if not text:
        raise ValueError(f"Prompt template {path} is empty")
    return text


def prompt_version(*templates: str) -> str:
    """Short fingerprint of the prompt text(s) so stored images can be traced to the prompt that made them."""
    digest = hashlib.sha1()
    for template in templates:
        digest.update(template.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:8]


def build_prompt(template: str, incident: Incident, fields: dict[str, str] | None = None) -> str:
    """Fill the stage-2 template. Plain replacement (not str.format) so braces in titles cannot break it.

    ``fields`` are the brief's scene/subject/key_details/accent/avoid/mood; the incident's own
    number/title/description placeholders stay available for simpler templates.
    """
    values = {
        "number": str(incident.incident_id),
        "title": incident.title,
        "description": incident.description or incident.title,
    }
    values.update(fields or {})
    prompt = template
    for key, value in values.items():
        prompt = prompt.replace("{" + key + "}", value)
    return prompt


def flatten_to_white(data: bytes) -> tuple[bytes, str]:
    """Composite any transparency onto solid white and return PNG bytes. Safe for already-opaque images."""
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        has_alpha = image.mode in ("RGBA", "LA", "P") and (image.mode != "P" or "transparency" in image.info)
        if has_alpha:
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            background.alpha_composite(rgba)
            result = background.convert("RGB")
        elif image.mode != "RGB":
            result = image.convert("RGB")
        else:
            return data, "image/png"
        out = io.BytesIO()
        result.save(out, format="PNG", optimize=True)
        return out.getvalue(), "image/png"


def generate_image(
    prompt: str,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    quality: str = DEFAULT_QUALITY,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    output_format: str = "png",
    background: str = DEFAULT_BACKGROUND,
    timeout: int = 300,
    retries: int = 3,
    session: requests.Session | None = None,
) -> GeneratedImage:
    """Generate one image and return its bytes. Retries transient OpenRouter errors."""
    session = session or requests.Session()
    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "quality": quality,
        "aspect_ratio": aspect_ratio,
        "output_format": output_format,
        "background": background,
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
            resp = session.post(OPENROUTER_IMAGES_URL, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_error = exc
            log.warning("OpenRouter request failed (attempt %d/%d): %s", attempt, retries, exc)
        else:
            if resp.status_code == 200:
                return _parse_response(resp.json(), model, prompt)
            body = resp.text[:500]
            last_error = ImageGenerationError(f"OpenRouter returned HTTP {resp.status_code}: {body}")
            if resp.status_code not in _RETRY_STATUSES:
                raise last_error
            log.warning("OpenRouter HTTP %s (attempt %d/%d): %s", resp.status_code, attempt, retries, body)
        if attempt < retries:
            time.sleep(min(60, 5 * 2 ** (attempt - 1)))
    raise ImageGenerationError(f"Image generation failed after {retries} attempts: {last_error}")


def _parse_response(body: dict, model: str, prompt: str) -> GeneratedImage:
    data = body.get("data") or []
    if not data or not data[0].get("b64_json"):
        raise ImageGenerationError(f"OpenRouter response contained no image: {str(body)[:500]}")
    image = data[0]
    usage = body.get("usage") or {}
    cost = usage.get("cost")
    raw = base64.b64decode(image["b64_json"])
    try:
        pixels, media_type = flatten_to_white(raw)
    except Exception as exc:  # a decode failure should not lose the image
        log.warning("Could not inspect image for transparency (%s); storing it as returned", exc)
        pixels, media_type = raw, image.get("media_type") or "image/png"
    return GeneratedImage(
        data=pixels,
        media_type=media_type,
        model=model,
        cost_usd=float(cost) if cost is not None else None,
        prompt=prompt,
    )
