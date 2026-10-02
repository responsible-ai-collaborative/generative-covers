"""Image generation through OpenRouter's Image API (https://openrouter.ai/docs/features/multimodal/image-generation)."""

from __future__ import annotations

import base64
import hashlib
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


def prompt_version(template: str) -> str:
    """Short fingerprint of the template so stored images can be traced to the prompt that made them."""
    return hashlib.sha1(template.encode("utf-8")).hexdigest()[:8]


def build_prompt(template: str, incident: Incident) -> str:
    # Plain replacement (not str.format) so braces inside titles/descriptions cannot break the prompt.
    description = incident.description or incident.title
    return (
        template.replace("{number}", str(incident.incident_id))
        .replace("{title}", incident.title)
        .replace("{description}", description)
    )


def generate_image(
    prompt: str,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    quality: str = DEFAULT_QUALITY,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    output_format: str = "png",
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
    return GeneratedImage(
        data=base64.b64decode(image["b64_json"]),
        media_type=image.get("media_type") or "image/png",
        model=model,
        cost_usd=float(cost) if cost is not None else None,
        prompt=prompt,
    )
