"""Secret redaction for anything that is logged, audited or shown from a tool.

Honest and small: it removes the *shapes* secrets commonly take (provider keys, bearer tokens,
``password=...`` pairs, private-key blocks) plus any exact secret value we already know from the
environment. It is a safety net, not a guarantee: a secret in an unrecognisable shape passes.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

__all__ = ["REDACTED", "env_secrets", "redact"]

REDACTED = "[redacted]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S
    ),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),  # OpenAI / OpenRouter / Anthropic style
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(
        r"(?<!\d)\d{8,10}:[A-Za-z0-9_-]{30,}"
    ),  # Telegram bot token (also inside /bot<token>/ URLs)
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWT
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
)
# `api_key=abc`, `"token": "abc"`, `PASSWORD: abc`: keep the name, drop the value.
_PAIR = re.compile(
    r"""(?ix)
    (?P<name>[\w.-]*(?:api[_-]?key|token|secret|passw(?:or)?d)[\w.-]*)
    (?P<sep>["']?\s*[:=]\s*["']?)
    (?P<value>[^\s"',;&]+)
    """
)

_SECRET_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD")
_MIN_SECRET_LENGTH = 8  # shorter values would redact ordinary words


def env_secrets(env: Mapping[str, str] | None = None) -> list[str]:
    """Values of environment variables that look like secrets, longest first."""
    source = os.environ if env is None else env
    values = {
        value
        for name, value in source.items()
        if len(value) >= _MIN_SECRET_LENGTH and any(m in name.upper() for m in _SECRET_ENV_MARKERS)
    }
    return sorted(values, key=len, reverse=True)


def redact(text: str, known: Iterable[str] = (), *, pairs: bool = True) -> str:
    """``text`` with known secret values and common secret shapes replaced.

    ``pairs=False`` skips the generic ``name=value`` rule. Use it for source code and tool
    output the model must still read: ``token = get_token()`` is not a leak.
    """
    for secret in known:
        if secret:
            text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    if not pairs:
        return text
    return _PAIR.sub(lambda m: f"{m['name']}{m['sep']}{REDACTED}", text)
