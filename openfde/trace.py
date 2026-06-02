"""
openfde/trace.py — trace payload summarization, redaction, and hashing (Step 17).

Trace events can carry arbitrary input / output / intermediate payloads. Before
anything is stored or shown, payloads are:
  - redacted   — likely secrets (tokens, passwords, API keys, auth headers) are
                 replaced with "[redacted]".
  - summarized — long strings, big lists, and deep objects are capped to a
                 small, display-safe preview.
  - hashed     — anything truncated carries a short content hash so the full
                 value is still identifiable / comparable without storing it.

This keeps enough for debugging without flooding the UI or persisting secrets.
"""

import hashlib
import json
import logging

logger = logging.getLogger("openfde.trace")

# ─── Tunables ─────────────────────────────────────────────────────────────── #

_MAX_STR: int = 240        # chars kept in a string preview
_MAX_LIST: int = 20        # items kept from a list
_MAX_KEYS: int = 30        # keys kept from an object
_MAX_DEPTH: int = 4        # nesting depth before collapsing

# Keys whose values are always redacted (case/sep-insensitive exact match).
_REDACT_KEYS: frozenset = frozenset({
    "token", "access_token", "refresh_token", "id_token", "api_key", "apikey",
    "authorization", "auth", "password", "passwd", "secret", "client_secret",
    "credential", "credentials", "private_key", "secret_key", "session",
    "cookie", "set_cookie", "ssn", "card", "card_number", "cvv",
})
# Substrings that, if present in a key, force redaction.
_REDACT_SUBSTRINGS: tuple = ("token", "secret", "password", "passwd", "api_key", "apikey", "auth")


# ─── Helpers ──────────────────────────────────────────────────────────────── #

def _norm_key(key: str) -> str:
    return str(key).lower().replace("-", "_").replace(" ", "_")


def is_secret_key(key) -> bool:
    """Return True if a dict key likely holds a secret.

    Args:
        key: any — dict key.

    Returns:
        bool — True when the value should be redacted.
    """
    k = _norm_key(key)
    if k in _REDACT_KEYS:
        return True
    return any(sub in k for sub in _REDACT_SUBSTRINGS)


def hash_value(value) -> str:
    """Return a short, stable content hash for a value.

    Args:
        value: any — JSON-serialisable value (falls back to str()).

    Returns:
        str — "sha256:<first 16 hex chars>".
    """
    try:
        data = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        data = str(value)
    return "sha256:" + hashlib.sha256(data.encode("utf-8", "replace")).hexdigest()[:16]


def summarize_payload(value, depth: int = 0):
    """Summarize an arbitrary payload into a display-safe, redacted form.

    Long strings, big lists, and deep objects are capped; anything truncated
    carries a content hash. Secret-looking dict keys are redacted.

    Args:
        value: any — the payload to summarize.
        depth: int — current recursion depth (internal).

    Returns:
        A JSON-safe summary: a scalar, a capped list, or a dict that may include
        envelope fields like {"_truncated", "_len", "_hash", "_omitted"}.
    """
    # Depth guard
    if depth >= _MAX_DEPTH:
        if isinstance(value, (dict, list)):
            return {"_collapsed": True, "_kind": type(value).__name__, "_hash": hash_value(value)}
        # fall through for scalars at max depth

    # Scalars
    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        if len(value) <= _MAX_STR:
            return value
        return {
            "_kind": "string",
            "_preview": value[:_MAX_STR],
            "_len": len(value),
            "_truncated": True,
            "_hash": hash_value(value),
        }

    # Lists / tuples
    if isinstance(value, (list, tuple)):
        items = list(value)
        kept = [summarize_payload(v, depth + 1) for v in items[:_MAX_LIST]]
        if len(items) <= _MAX_LIST:
            return kept
        return {
            "_kind": "list",
            "_items": kept,
            "_len": len(items),
            "_omitted": len(items) - _MAX_LIST,
            "_truncated": True,
            "_hash": hash_value(items),
        }

    # Dicts
    if isinstance(value, dict):
        out = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _MAX_KEYS:
                out["_omitted"] = len(value) - _MAX_KEYS
                out["_truncated"] = True
                out["_hash"] = hash_value({str(kk): "…" for kk in value})
                break
            if is_secret_key(k):
                out[str(k)] = "[redacted]"
            else:
                out[str(k)] = summarize_payload(v, depth + 1)
        return out

    # Unknown / non-serialisable
    text = str(value)
    if len(text) <= _MAX_STR:
        return {"_kind": type(value).__name__, "_repr": text}
    return {
        "_kind": type(value).__name__,
        "_preview": text[:_MAX_STR],
        "_len": len(text),
        "_truncated": True,
        "_hash": hash_value(text),
    }


def summarize_trace_event(event: dict) -> dict:
    """Return a copy of a trace event with input/output/error payloads summarized.

    Args:
        event: dict — raw trace event; payload fields: input, output, error,
                      intermediate.

    Returns:
        dict — copy with those fields summarized + redacted.
    """
    out = dict(event)
    for field in ("input", "output", "intermediate", "error"):
        if field in out and out[field] is not None:
            # error may be a plain string; summarize handles both.
            out[field] = summarize_payload(out[field])
    return out
