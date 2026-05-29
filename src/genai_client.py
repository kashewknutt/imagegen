from __future__ import annotations

import os
import ast
import base64
import datetime as _dt
from io import BytesIO
from pathlib import Path
from typing import Iterable

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from PIL import Image
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from .rate_limit import RateLimiter
from .semaphore import acquire_dir_semaphore, release_token


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, genai_errors.ClientError):
        code = getattr(exc, "status_code", None)
        # 429: quota/rate limit; 5xx: transient server issues
        return code in {429, 500, 502, 503, 504}
    if isinstance(exc, genai_errors.ServerError):
        return True
    # Network flakiness / connection resets / TLS EOFs.
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    return False


def _ensure_image_model_name(model: str) -> None:
    m = (model or "").strip().lower()
    if not m:
        raise RuntimeError("Config error: `model` is empty.")
    # Accept either Model Garden resource names or short model IDs, but require an image-capable model.
    # Examples:
    # - publishers/google/models/gemini-2.5-flash-image
    # - gemini-2.5-flash-image
    # - gemini-3-pro-image-preview
    # - gemini-3.1-flash-image-preview
    if "image" not in m:
        raise RuntimeError(
            "Config error: `model` must be an image-generation Gemini model (must contain 'image'), "
            "e.g. `gemini-3-pro-image-preview` or `gemini-2.5-flash-image`."
        )


def _coerce_inline_data_to_bytes(data: object) -> bytes | None:
    if data is None:
        return None
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        s = data.strip()
        # Some debug/serialized forms look like "b'\\x89PNG...'"
        if (s.startswith("b'") and s.endswith("'")) or (s.startswith('b"') and s.endswith('"')):
            try:
                v = ast.literal_eval(s)
                if isinstance(v, (bytes, bytearray)):
                    return bytes(v)
            except Exception:
                pass
        # If it starts like a bytes literal but looks truncated, try a best-effort parse.
        if s.startswith("b'") or s.startswith('b"'):
            try:
                # ensure we have a closing quote
                quote = "'" if s.startswith("b'") else '"'
                if not s.endswith(quote):
                    s2 = s + quote
                else:
                    s2 = s
                v = ast.literal_eval(s2)
                if isinstance(v, (bytes, bytearray)):
                    return bytes(v)
            except Exception:
                pass
        # Otherwise, try base64
        try:
            return base64.b64decode(s, validate=False)
        except Exception:
            return None
    return None


def _get_inline_data_data(part: object) -> object:
    # Supports both SDK Part objects and plain dicts (as seen in some debug dumps).
    try:
        inline = getattr(part, "inline_data", None)
        if inline is not None:
            return getattr(inline, "data", None)
    except Exception:
        pass
    if isinstance(part, dict):
        inline = part.get("inline_data")
        if isinstance(inline, dict):
            return inline.get("data")
    return None


def _extract_parts_debug(parts: list[object]) -> list[dict]:
    out: list[dict] = []
    try:
        for i, p in enumerate(parts[:10]):
            raw = _get_inline_data_data(p)
            out.append(
                {
                    "idx": i,
                    "type": type(p).__name__,
                    "has_inline_data_attr": hasattr(p, "inline_data"),
                    "inline_data_data_type": type(raw).__name__ if raw is not None else None,
                    "inline_data_data_len": len(raw) if isinstance(raw, (bytes, bytearray, str)) else None,
                    "text_present": bool(getattr(p, "text", None)) if not isinstance(p, dict) else bool(p.get("text")),
                }
            )
    except Exception as e:
        out.append({"error": str(e)})
    return out


def _candidate_parts(response: object) -> list[object]:
    """
    Prefer the canonical response shape described in the docs:
      response.candidates[0].content.parts
    Do NOT rely on response.parts, which can differ by SDK version and may be an iterator.
    """
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        c0 = candidates[0]
        content = getattr(c0, "content", None)
        if content is not None:
            parts = getattr(content, "parts", None) or []
            return list(parts)
    return []


class GenAiImageClient:
    def __init__(
        self,
        model: str,
        min_seconds_between_requests: float,
        *,
        semaphore_dir: str | None = None,
        max_inflight_generations: int = 4,
    ) -> None:
        self.model = model
        _ensure_image_model_name(model)

        api_key = (os.getenv("GOOGLE_API_KEY") or "").strip()
        if not api_key:
            # Some setups use GEMINI_API_KEY; accept it as fallback.
            api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("Dev API mode requires GOOGLE_API_KEY (or GEMINI_API_KEY) in the environment.")

        # Gemini Developer API / AI Studio only.
        # Use v1beta for Nano Banana image features and config fields per docs.
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(api_version="v1beta"),
        )
        self.mode = "devapi"
        self._rl = RateLimiter(min_seconds_between_requests)
        self._semaphore_dir = semaphore_dir
        self._max_inflight = int(max_inflight_generations or 0)

    def list_models(self) -> list[dict]:
        models_out: list[dict] = []
        try:
            for m in self._client.models.list():
                try:
                    models_out.append(m.model_dump())
                except Exception:
                    models_out.append(getattr(m, "__dict__", {"repr": repr(m)}))
        except Exception as e:
            models_out.append({"error": str(e), "error_type": type(e).__name__, "error_repr": repr(e)})
        return models_out

    def generate_image_with_meta(
        self,
        reference_rgb: Image.Image | list[Image.Image],
        prompt: str,
        aspect_ratio: str | None = None,
    ) -> tuple[Image.Image, dict]:
        """
        Returns (image, meta) where meta includes response_id/model_version/usage_metadata when available.
        """
        started_at = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        try:
            img = self.generate_image(reference_rgb, prompt, aspect_ratio=aspect_ratio)
            meta = {
                "started_at": started_at,
                "response_id": getattr(self, "last_response_id", None),
                "model_version": getattr(self, "last_model_version", None),
                "usage_metadata": getattr(self, "last_usage_metadata", None),
            }
            return img, meta
        except Exception:
            meta = {
                "started_at": started_at,
                "response_id": getattr(self, "last_response_id", None),
                "model_version": getattr(self, "last_model_version", None),
                "usage_metadata": getattr(self, "last_usage_metadata", None),
                "error": getattr(self, "last_error_message", None),
            }
            raise

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential_jitter(initial=2, max=120),
        stop=stop_after_attempt(10),
        reraise=True,
    )
    def generate_image(self, reference_rgb: Image.Image | list[Image.Image], prompt: str, aspect_ratio: str | None = None) -> Image.Image:
        token = None
        if self._semaphore_dir and self._max_inflight > 0:
            token = acquire_dir_semaphore(
                Path(self._semaphore_dir),
                name="gen",
                slots=self._max_inflight,
                timeout_seconds=600,
            )
        try:
            self._rl.wait()
            # v1beta supports response_modalities; request image explicitly.
            cfg = types.GenerateContentConfig(response_modalities=[types.Modality.TEXT, types.Modality.IMAGE])
            if aspect_ratio:
                cfg.image_config = types.ImageConfig(aspect_ratio=aspect_ratio)

            # Follow official docs payload ordering:
            # - image generation/editing: contents=[prompt, image1, image2, ...]
            # For Dev API, passing PIL.Image directly is supported.
            if isinstance(reference_rgb, list):
                imgs = [img for img in reference_rgb if isinstance(img, Image.Image)]
                if not imgs:
                    raise RuntimeError("No reference images provided.")
                contents = [prompt, *imgs]
            else:
                contents = [prompt, reference_rgb]

            response = self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=cfg,
            )

            self.last_error_message = ""

            # Always parse from candidates[0].content.parts (canonical).
            parts_list = _candidate_parts(response)
            if not parts_list:
                # Fallback only if candidates absent.
                parts_list = list(_iter_parts(response))

            # Keep a structured debug payload for UI inspection on failures.
            try:
                self.last_raw_response = getattr(response, "model_dump", lambda: None)()  # type: ignore[attr-defined]
            except Exception:
                try:
                    self.last_raw_response = response.__dict__
                except Exception:
                    self.last_raw_response = str(response)
            # Also keep lightweight diagnostics about the first inline_data we see.
            self.last_inline_data_debug = None
            self.last_parts_debug = _extract_parts_debug(parts_list)
            try:
                for p in parts_list:
                    raw = _get_inline_data_data(p)
                    if raw is None:
                        continue
                    raw_type = type(raw).__name__
                    raw_len = None
                    raw_head = None
                    if isinstance(raw, (bytes, bytearray)):
                        raw_len = len(raw)
                        raw_head = bytes(raw[:32]).hex()
                    elif isinstance(raw, str):
                        raw_len = len(raw)
                        raw_head = raw[:80]
                    self.last_inline_data_debug = {"type": raw_type, "len": raw_len, "head": raw_head}
                    break
            except Exception:
                self.last_inline_data_debug = {"error": "failed to inspect inline_data"}

            # Capture identifiers/usage for cost tracking.
            try:
                self.last_response_id = getattr(response, "response_id", None)
            except Exception:
                self.last_response_id = None
            try:
                self.last_model_version = getattr(response, "model_version", None)
            except Exception:
                self.last_model_version = None
            try:
                usage = getattr(response, "usage_metadata", None)
                self.last_usage_metadata = getattr(usage, "model_dump", lambda: usage)() if usage is not None else None
            except Exception:
                self.last_usage_metadata = None

            text_parts: list[str] = []
            for part in parts_list:
                # Preferred: use the SDK helper when possible (matches docs).
                try:
                    if getattr(part, "inline_data", None) is not None and hasattr(part, "as_image"):
                        img = part.as_image()
                        if img is not None:
                            if img.mode != "RGB":
                                img = img.convert("RGB")
                            return img
                except Exception:
                    pass

                # Fallback: decode inline_data.data manually (handles debug/serialized forms).
                raw = _get_inline_data_data(part)
                if raw is not None:
                    data = _coerce_inline_data_to_bytes(raw)
                    if data:
                        try:
                            img = Image.open(BytesIO(data))
                            if img.mode != "RGB":
                                img = img.convert("RGB")
                            return img
                        except Exception:
                            pass
                txt = getattr(part, "text", None)
                if isinstance(txt, str) and txt.strip():
                    text_parts.append(txt.strip())

            finish_reason = None
            try:
                candidates = getattr(response, "candidates", None) or []
                if candidates:
                    finish_reason = getattr(candidates[0], "finish_reason", None)
            except Exception:
                finish_reason = None

            snippet = (" | ".join(text_parts))[:500]
            msg = f"Model response did not contain an image. finish_reason={finish_reason!r} text={snippet!r}"
            self.last_error_message = msg
            raise RuntimeError(msg)
        finally:
            if token is not None:
                release_token(token)


def _iter_parts(response: object) -> Iterable[object]:
    # The SDK exposes a convenience `response.parts` in many cases,
    # but we support the candidate structure too.
    if hasattr(response, "parts") and response.parts:
        return response.parts
    candidates = getattr(response, "candidates", None) or []
    for c in candidates:
        content = getattr(c, "content", None)
        parts = getattr(content, "parts", None) or []
        for p in parts:
            yield p
    return []
