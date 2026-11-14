"""Model selection for the news bots — one place to change the model.

Why this exists (2026-11-14): the four bot scripts each hard-coded
``claude-sonnet-4-6``. Moving to a newer model meant editing four files, and a
model the API key cannot use would have silenced the bots. Now:

* ``NEWS_MODEL`` / ``NEWS_EFFORT`` (env, or the defaults below) pick the model
  and effort level for every call.
* If the API rejects the primary model or the effort setting (HTTP 400 / 403 /
  404 — unknown model, no access, unsupported parameter), the call is retried
  once on ``NEWS_FALLBACK_MODEL`` with no effort setting, and a warning is
  logged. The bot keeps posting; the log says which model actually ran.
* Thinking models count their thinking toward ``max_tokens`` (a hard cap on
  thinking + text), so callers pass the *text* budget they need and this module
  adds headroom for thinking.

Transient errors (429 / 5xx / network) are not retried here on purpose — a
different model would not fix them, and the callers already handle failures.
"""
from __future__ import annotations

import logging
import os

import anthropic

log = logging.getLogger("llm")

MODEL = os.environ.get("NEWS_MODEL") or "claude-fable-5-2"
EFFORT = os.environ.get("NEWS_EFFORT") or "medium"
FALLBACK_MODEL = os.environ.get("NEWS_FALLBACK_MODEL") or "claude-sonnet-4-6"

# Extra output room for thinking, added on top of the caller's text budget.
THINKING_HEADROOM = 6000
_REJECTED = (400, 403, 404)


def create(client: anthropic.Anthropic, *, max_tokens: int, system: str, messages: list):
    """``client.messages.create`` with the configured model, effort and fallback."""
    try:
        return client.messages.create(
            model=MODEL,
            max_tokens=max_tokens + THINKING_HEADROOM,
            system=system,
            messages=messages,
            # extra_body keeps this working on SDK versions that predate the
            # output_config parameter.
            extra_body={"output_config": {"effort": EFFORT}},
        )
    except anthropic.APIStatusError as e:
        if getattr(e, "status_code", None) not in _REJECTED or FALLBACK_MODEL == MODEL:
            raise
        log.warning(
            "Model %s (effort %s) was rejected with HTTP %s — falling back to %s for this call.",
            MODEL, EFFORT, e.status_code, FALLBACK_MODEL,
        )
        return client.messages.create(
            model=FALLBACK_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )


def text_of(response) -> str:
    """The reply text, skipping thinking blocks."""
    return next((b.text for b in response.content if b.type == "text"), "")
