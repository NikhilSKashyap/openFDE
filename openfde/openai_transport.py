"""
openfde/openai_transport.py — OpenAI-compatible chat transport (Step 29 Slice 1).

Dependency-free (urllib). Produces a callable with the SAME interface as
anthropic_transport so it drops into the agent loop / council: it takes the
runner request shape and returns the runner response shape (Anthropic-ish
{stop_reason, content:[blocks]}), translating to/from the OpenAI Chat Completions
format in between.

Works against any OpenAI-compatible endpoint (OpenAI, OpenRouter, Together,
local servers, …) via `base_url` from the role settings. Used for the Architect
and Verifier roles (text in → text out). The key is passed in by the caller and
never logged.

The translation helpers are pure and unit-testable; only `make_transport` does
network I/O.
"""

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger("openfde.openai_transport")

_DEFAULT_BASE = "https://api.openai.com"
_TIMEOUT = 120


def _block_text(block) -> str:
    """Flatten a single Anthropic-style content block to text (best-effort)."""
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        t = block.get("type")
        if t == "text":
            return block.get("text", "")
        if t == "tool_result":
            c = block.get("content", "")
            return c if isinstance(c, str) else json.dumps(c)
        if t == "tool_use":
            return f"[tool {block.get('name')} {json.dumps(block.get('input', {}))}]"
    return ""


def to_chat_messages(system: str, messages: list) -> list:
    """Translate (system + runner messages) into OpenAI chat messages.

    Runner messages are Anthropic-shaped: role in {user, assistant} with content
    that is a string or a list of blocks. For the text roles (Architect/Verifier)
    we flatten block lists to text.

    Returns:
        list[dict] — [{role, content}], roles in {system, user, assistant}.
    """
    out = []
    if system:
        out.append({"role": "system", "content": system})
    for m in (messages or []):
        role = m.get("role", "user")
        if role not in ("user", "assistant", "system"):
            role = "user"
        content = m.get("content", "")
        if isinstance(content, list):
            content = "\n".join(t for t in (_block_text(b) for b in content) if t).strip()
        out.append({"role": role, "content": content if isinstance(content, str) else str(content)})
    return out


def from_chat_response(payload: dict) -> dict:
    """Translate an OpenAI chat completion into the runner response shape.

    Returns:
        dict — {stop_reason, content:[{type:'text', text}]}.
    """
    choices = payload.get("choices") or []
    msg = (choices[0].get("message") if choices else {}) or {}
    text = msg.get("content") or ""
    finish = (choices[0].get("finish_reason") if choices else "") or ""
    return {"stop_reason": finish, "content": [{"type": "text", "text": text}]}


def make_transport(api_key: str, base_url: str = "", *, timeout: int = _TIMEOUT):
    """Build an OpenAI-compatible transport callable bound to a key + base URL.

    Args:
        api_key: str — provider API key (never logged).
        base_url: str — optional override (defaults to api.openai.com). Point this
            at OpenRouter / a local server / etc. via role settings.
        timeout: int — request timeout in seconds.

    Returns:
        callable — request(dict) -> response(dict). Raises RuntimeError on
                   transport/HTTP failure (the loop catches and fails closed).
    """
    base = (base_url or _DEFAULT_BASE).rstrip("/")
    url = base + "/v1/chat/completions"

    def transport(req: dict) -> dict:
        body = {
            "model": req["model"],
            "max_tokens": req.get("max_tokens", 4096),
            "messages": to_chat_messages(req.get("system", ""), req.get("messages", [])),
        }
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST", headers={
            "content-type": "application/json",
            "authorization": f"Bearer {api_key}",
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
            raise RuntimeError(f"OpenAI API HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"OpenAI API unreachable: {exc}") from None

        return from_chat_response(payload)

    return transport


def complete(transport, *, model: str, system: str, user: str, max_tokens: int = 4096) -> str:
    """One-shot text completion helper for text roles (Architect/Verifier).

    Args:
        transport: callable — a make_transport() result (or an injected fake).
        model: str — provider model id.
        system: str — system prompt.
        user: str — the user message.
        max_tokens: int — cap.

    Returns:
        str — the assistant's text (empty on no content).
    """
    resp = transport({
        "model": model, "system": system, "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user}],
    })
    blocks = resp.get("content", []) if isinstance(resp, dict) else []
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
