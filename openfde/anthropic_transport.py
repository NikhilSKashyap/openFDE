"""
openfde/anthropic_transport.py — real Anthropic Messages API transport (Step 22a).

Dependency-free (urllib) so OpenFDE pulls in no SDK. Produces a callable that the
agent runner drives: it takes the runner's request shape and returns the runner's
response shape, both of which already match the Anthropic Messages API 1:1.

This is the only module that talks to a provider network endpoint, and only when
a role is configured `api` / `anthropic` with a stored key. The key is passed in
by the caller (loaded from .openfde/agent_settings.json) and never logged here.
"""

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger("openfde.anthropic_transport")

_DEFAULT_BASE = "https://api.anthropic.com"
_API_VERSION = "2023-06-01"
_TIMEOUT = 120


def make_transport(api_key: str, base_url: str = "", *, version: str = _API_VERSION,
                   timeout: int = _TIMEOUT):
    """Build a transport callable bound to an API key (and optional base URL).

    Args:
        api_key: str — Anthropic API key (never logged).
        base_url: str — optional override (defaults to api.anthropic.com).
        version: str — anthropic-version header.
        timeout: int — request timeout in seconds.

    Returns:
        callable — request(dict) -> response(dict). Raises RuntimeError on
                   transport/HTTP failure (the runner catches and fails closed).
    """
    base = (base_url or _DEFAULT_BASE).rstrip("/")
    url = base + "/v1/messages"

    def transport(req: dict) -> dict:
        body = {
            "model": req["model"],
            "max_tokens": req.get("max_tokens", 4096),
            "system": req.get("system", ""),
            "messages": req.get("messages", []),
            "tools": req.get("tools", []),
        }
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST", headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": version,
        })
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                payload = json.loads(resp.read() or "{}")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise RuntimeError(f"Anthropic API HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Anthropic API unreachable: {exc}") from None

        return {
            "stop_reason": payload.get("stop_reason", ""),
            "content": payload.get("content", []),
        }

    return transport
