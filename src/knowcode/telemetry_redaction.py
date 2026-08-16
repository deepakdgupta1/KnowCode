"""Recursive secret redaction and size bounds for telemetry values.

This is the *second* privacy control, not the first. The first one is
:mod:`knowcode.telemetry_policy`: a field a caller invents never reaches disk
because the field is dropped, so a secret inside it is dropped with it.
Redaction exists because an allowlisted field can still carry a credential —
an MCP client picks its own ``tool_name``, and a user's own question is the
most common place a pasted key shows up.

Neither control is a guarantee that unknown secret formats are caught; ADR 5
is explicit that redaction is defense in depth and omission is the default.
What this module does guarantee is a bound: every string is truncated, every
container is bounded in depth and width, and no value type outside the JSON
scalars survives.
"""

from __future__ import annotations

import re
from typing import Any, Final, Mapping, Pattern, Sequence

#: Replacement for a matched credential.
REDACTED: Final = "[REDACTED]"
#: Marker appended to a string that hit the length bound.
TRUNCATED: Final = "...[truncated]"
#: Placeholder for a container nested past :data:`MAX_DEPTH`.
DEPTH_LIMIT: Final = "[TRUNCATED_DEPTH]"
#: Placeholder for a value whose type telemetry does not serialize.
UNSUPPORTED: Final = "[UNSUPPORTED_TYPE]"

#: Per-string character bound. Telemetry fields are labels and enums; anything
#: longer is either a mistake or a payload, and neither belongs on disk.
MAX_STRING_CHARS: Final = 256
#: Nesting bound. Telemetry events are flat by policy; this bounds the walk.
MAX_DEPTH: Final = 4
#: Width bound for lists and mappings.
MAX_ITEMS: Final = 32
#: Key-name bound, applied before a mapping key is used.
MAX_KEY_CHARS: Final = 64

#: Ordered (pattern, replacement) rules. Order matters: the structural rules
#: (credentials in a URL, an assignment, an authorization scheme) run before
#: the vendor-prefix rules so the surrounding context is preserved in the
#: output, which is what makes a redacted record still readable.
_RULES: Final[Sequence[tuple[Pattern[str], str]]] = (
    # PEM private key blocks, header and body alike.
    (
        re.compile(
            r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END[A-Z ]*PRIVATE KEY-----)?"
        ),
        REDACTED,
    ),
    # scheme://user:password@host
    (
        re.compile(r"\b([a-z][a-z0-9+.\-]*://)[^\s/:@]+:[^\s/@]+@"),
        r"\1" + REDACTED + "@",
    ),
    # Authorization schemes.
    (re.compile(r"\b(Bearer|Basic|Token)\s+[A-Za-z0-9._\-+/=]{8,}"), r"\1 " + REDACTED),
    # name = value assignments for credential-ish names.
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?key|secret[_-]?key|secret|token|password|passwd|pwd"
            r"|authorization|credential|private[_-]?key|key)\b(\s*[:=]\s*)"
            r"[\"']?[^\s\"',;]{4,}",
        ),
        r"\1\2" + REDACTED,
    ),
    # JSON Web Tokens.
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}"),
        REDACTED,
    ),
    # Vendor-prefixed keys.
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{10,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), REDACTED),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{12,}"), REDACTED),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), REDACTED),
    # Long opaque tokens: mixed letters and digits, no separators, base64-ish.
    # The lookaheads keep ordinary prose and long lowercase words intact.
    (
        re.compile(
            r"(?<![A-Za-z0-9+/=_\-])"
            r"(?=[A-Za-z0-9+/=_\-]*[A-Za-z])(?=[A-Za-z0-9+/=_\-]*\d)"
            r"[A-Za-z0-9+/=_\-]{40,}"
            r"(?![A-Za-z0-9+/=_\-])"
        ),
        REDACTED,
    ),
)


def contains_secret(text: str) -> bool:
    """Whether any redaction rule matches ``text``.

    Exposed for tests and for callers that want to refuse a value outright
    rather than store a redacted form of it.
    """
    return any(pattern.search(text) for pattern, _ in _RULES)


def redact_text(text: str) -> str:
    """Apply every rule, then bound the length."""
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    if len(text) > MAX_STRING_CHARS:
        text = text[:MAX_STRING_CHARS] + TRUNCATED
    return text


def redact(value: Any, *, depth: int = 0) -> Any:
    """Return a redacted, bounded copy of ``value``.

    Scalars pass through untouched; strings are scrubbed and truncated;
    mappings and sequences are walked to :data:`MAX_DEPTH` and
    :data:`MAX_ITEMS`. Anything else becomes :data:`UNSUPPORTED` rather than
    being stringified, because ``repr()`` of a caller's object is exactly the
    kind of unreviewed text this module exists to keep off disk.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, Mapping):
        if depth >= MAX_DEPTH:
            return DEPTH_LIMIT
        return {
            redact_text(str(key))[:MAX_KEY_CHARS]: redact(item, depth=depth + 1)
            for key, item in list(value.items())[:MAX_ITEMS]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        if depth >= MAX_DEPTH:
            return DEPTH_LIMIT
        return [redact(item, depth=depth + 1) for item in list(value)[:MAX_ITEMS]]
    return UNSUPPORTED
