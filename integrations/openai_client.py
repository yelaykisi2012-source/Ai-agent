"""One place that builds the OpenAI client.

The client is created lazily and only when a key exists, so importing the
application never requires credentials and a missing key produces a clean 503
instead of a crash at import time.
"""

from __future__ import annotations

import logging

from openai import AsyncOpenAI

from config import Settings

logger = logging.getLogger(__name__)


def build_async_client(settings: Settings) -> AsyncOpenAI | None:
    """Return a configured async client, or None when no key is set.

    The SDK's own retry loop is disabled: the brain implements explicit
    exponential backoff, and the speech engine uses a small bounded retry.
    """
    key = settings.openai_key
    if not key:
        return None
    return AsyncOpenAI(
        api_key=key,
        timeout=settings.openai_timeout_seconds,
        max_retries=0,
    )
