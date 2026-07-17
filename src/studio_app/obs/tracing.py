"""OTel/Langfuse client — fail-open (copy R-DI §14 `tracing.py:1-5,95-115`). Default OFF
(`STUDIO_ENABLE_TRACING=false`) — a tracing-only error must NEVER break the business path it
wraps.

`langfuse` is an OPTIONAL dependency (apps/studio pyproject `[project.optional-dependencies]
obs`) — imported lazily inside `get_langfuse()` so this module (and the default
`STUDIO_ENABLE_TRACING=false` path every other test runs under) never requires the package to be
installed. Any failure building the client (missing package, bad creds, network) is caught here
and turned into `None` — the caller always gets a value it can null-check, never an exception.

Divergence from R-DI note: R-DI's `TracingExtractor` wraps span-setup AND the business call in
ONE try/except (so any failure anywhere in the traced path re-executes the business call bare).
`traced_call()` here only wraps span SETUP in try/except; the business call itself runs outside
that guard so its own exceptions propagate normally instead of silently triggering a second,
duplicate business-logic execution — safer for a generic wrapper with unknown side effects.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from loguru import logger

from studio_app.settings import get_settings

_langfuse_singleton: Any | None = None
_langfuse_init_attempted = False


def get_langfuse() -> Any | None:
    """Lazy singleton; returns None when tracing is disabled, unconfigured, or client init fails
    for ANY reason (missing package, bad creds, network) — this function itself never raises."""
    global _langfuse_singleton, _langfuse_init_attempted
    settings = get_settings()
    if not settings.enable_tracing:
        return None
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    if _langfuse_init_attempted:
        return _langfuse_singleton
    _langfuse_init_attempted = True
    try:
        from langfuse import Langfuse  # type: ignore[import-not-found]

        _langfuse_singleton = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open: ANY init error must not break the caller
        logger.warning(f"[OBS] langfuse unavailable: {exc}")
        _langfuse_singleton = None
    return _langfuse_singleton


async def traced_call[T](name: str, fn: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Fail-open wrapper for an arbitrary async business call (precedent: R-DI §14
    `tracing.py:205-326` wraps "the Gemini call" the same way). Tracing disabled, unconfigured,
    or a span-setup failure all fall through to running `fn()` un-instrumented — `fn()`'s own
    exceptions are never caught here, they propagate to the caller unchanged."""
    client = get_langfuse()
    if client is None:
        return await fn()
    try:
        span = client.start_as_current_observation(as_type="span", name=name)
    except Exception as exc:  # noqa: BLE001 — span setup must never break the wrapped call
        logger.warning(f"[OBS] span '{name}' setup failed; call unaffected: {exc}")
        return await fn()
    with span:
        return await fn()
