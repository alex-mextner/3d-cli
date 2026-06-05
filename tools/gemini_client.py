"""gemini_client.py — minimal Gemini REST client (stdlib only).

Purpose: drive the Gemini generateContent REST API directly. The Gemini CLI is
auth-blocked by location, so this talks to the public endpoint over urllib.

Accessed-via: imported by tools/vlm_judge.py and tools/gemini_arm.py; also runnable
standalone for a smoke test (`python tools/gemini_client.py [model] [prompt]`).

Invariants:
  * stdlib ONLY (urllib + json + base64 + os) — no pip deps, no new requirements.
  * API key is read at runtime from ExpenseSyncBot/.env (GEMINI_API_KEY=...), never
    hardcoded. Override with the GEMINI_API_KEY env var or GEMINI_ENV_FILE.
  * `system` is sent as the top-level `system_instruction` body field, NOT as a content
    part (Gemini ignores a system-as-part). `parts` are wrapped into contents[0].parts.
  * HTTP errors print status + response body (Google's error reason) before raising.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

# Default location of the .env that holds GEMINI_API_KEY (per task spec).
_DEFAULT_ENV_FILE = "/Users/ultra/xp/ExpenseSyncBot/.env"
_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def read_api_key() -> str:
    """Return the Gemini API key.

    Precedence: $GEMINI_API_KEY env var > the GEMINI_API_KEY line in the .env file
    ($GEMINI_ENV_FILE or the default ExpenseSyncBot/.env). Never hardcoded.
    """
    env = os.environ.get("GEMINI_API_KEY")
    if env:
        return env.strip()
    env_file = os.environ.get("GEMINI_ENV_FILE", _DEFAULT_ENV_FILE)
    try:
        with open(env_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("GEMINI_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError as exc:
        raise RuntimeError(f"cannot read GEMINI_API_KEY from {env_file}: {exc}") from exc
    raise RuntimeError(
        f"GEMINI_API_KEY not found (checked $GEMINI_API_KEY env and {env_file})"
    )


def image_part(path: str, mime_type: str | None = None) -> dict[str, Any]:
    """Build an inlineData part from an image file (base64-encoded, mime auto-detected)."""
    if mime_type is None:
        guessed, _ = mimetypes.guess_type(path)
        mime_type = guessed or "image/png"
    with open(path, "rb") as fh:
        data = base64.b64encode(fh.read()).decode("ascii")
    return {"inlineData": {"mimeType": mime_type, "data": data}}


def text_part(text: str) -> dict[str, Any]:
    """Build a text part."""
    return {"text": text}


def generate(
    model: str,
    parts: list[dict[str, Any]],
    system: str | None = None,
    *,
    generation_config: dict[str, Any] | None = None,
    timeout: float = 600.0,
    retries: int = 3,
) -> dict[str, Any]:
    """Call <model>:generateContent with `parts`; return {text, prompt_tokens, output_tokens}.

    `system`, when given, is sent as the top-level system_instruction (the correct place
    for system role — sending it as a content part is silently ignored by Gemini).
    On an HTTP error the status code + response body are printed to stderr (the body
    carries Google's real error reason: bad model, quota, key) then the error re-raised.
    """
    key = read_api_key()
    url = f"{_BASE}/{model}:generateContent?key={key}"

    body: dict[str, Any] = {"contents": [{"parts": parts}]}
    # Forbid function-calling: gemini-3-pro otherwise tries to emit a tool call (no tools are
    # declared) and returns finishReason=MALFORMED_FUNCTION_CALL with empty text. We only ever
    # want text/code back, so disable it explicitly for every call.
    body["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
    if system:
        body["system_instruction"] = {"parts": [{"text": system}]}
    if generation_config:
        body["generationConfig"] = generation_config

    raw = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=raw, headers={"Content-Type": "application/json"}, method="POST"
    )
    # gemini-3-pro with a big system prompt + images + thinking can take minutes; a single read
    # timeout must NOT kill the whole arm. Retry transient timeouts/network errors with backoff;
    # HTTP 4xx/5xx (a real API error) surfaces immediately.
    payload = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            try:
                err_body = exc.read().decode("utf-8")
            except Exception:  # noqa: BLE001 - best-effort body read
                err_body = "<no body>"
            print(f"[gemini_client] HTTP {exc.code} {exc.reason}\n{err_body}", file=sys.stderr)
            raise
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            print(
                f"[gemini_client] transient error (attempt {attempt}/{retries}): {reason}",
                file=sys.stderr,
            )
            if attempt == retries:
                raise
            time.sleep(5 * attempt)

    assert payload is not None  # loop either set payload (break) or raised
    # candidates / usageMetadata may be absent on a blocked or error response.
    text = ""
    candidates = payload.get("candidates") or []
    if candidates:
        content = candidates[0].get("content") or {}
        out_parts = content.get("parts") or []
        text = "".join(p.get("text", "") for p in out_parts if isinstance(p, dict))
    if not text:
        # Surface the reason a refusal/safety block returned no text.
        fb = payload.get("promptFeedback")
        if candidates and candidates[0].get("finishReason"):
            print(
                f"[gemini_client] empty text; finishReason={candidates[0].get('finishReason')}",
                file=sys.stderr,
            )
        elif fb:
            print(f"[gemini_client] empty text; promptFeedback={fb}", file=sys.stderr)

    usage = payload.get("usageMetadata") or {}
    return {
        "text": text,
        "prompt_tokens": int(usage.get("promptTokenCount", 0) or 0),
        "output_tokens": int(usage.get("candidatesTokenCount", 0) or 0),
    }


def list_models(timeout: float = 60.0) -> list[str]:
    """Return available model names (helper; not required for the harness)."""
    key = read_api_key()
    url = f"{_BASE}?key={key}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8") if exc.fp else "<no body>"
        print(f"[gemini_client] HTTP {exc.code} {exc.reason}\n{body}", file=sys.stderr)
        raise
    return [m.get("name", "") for m in payload.get("models", [])]


def _main(argv: list[str]) -> int:
    """Smoke test: `python tools/gemini_client.py [model] [prompt]` prints text + tokens."""
    model = argv[0] if argv else "gemini-2.5-flash"
    prompt = argv[1] if len(argv) > 1 else "reply OK"
    res = generate(model, [text_part(prompt)])
    print("TEXT:", res["text"].strip())
    print(f"prompt_tokens={res['prompt_tokens']} output_tokens={res['output_tokens']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
