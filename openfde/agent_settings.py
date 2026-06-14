"""
openfde/agent_settings.py — role → provider configuration (Step 21).

OpenFDE lets the user decide who plays Architect, Senior Dev, and Verifier
before native agent conversations exist (Step 22). This module is the pure,
side-effect-free model for that configuration:

  - `normalize()` / `normalize_role()` — coerce any input into a safe internal
    shape (raw apiKey preserved, only here);
  - `merge()` — apply a full/partial update over stored settings, preserving a
    stored apiKey when the caller does not supply a new one;
  - `to_public()` — strip every raw apiKey and expose only `hasApiKey` +
    `maskedApiKey` for the frontend;
  - `check()` / `check_role()` — validate config *shape only* (no network);
  - `options()` — UI metadata (roles / modes / providers).

Secrets never leave `to_public()`. Nothing here logs, prints, or returns a raw
key. Persistence stores the internal (raw) shape under `.openfde/agent_settings.json`.
"""

import logging

logger = logging.getLogger("openfde.agent_settings")

# ─── Vocabulary ──────────────────────────────────────────────────────────── #

ROLES = ("architect", "senior_dev", "verifier")
PROVIDERS = (
    "codex-local",
    "claude-code-local",
    "echo",
    "openai-compatible",
    "anthropic",
    "openrouter",
    "ollama",
)
# Keyless local-CLI providers that drive a local coding app as a TEXT role
# (Architect / Verifier) — Day 3B. They use the app's own login, need no API key,
# and must never be Senior Dev (text-only, cannot edit the repo).
LOCAL_TEXT_PROVIDERS = ("codex-local",)
# Providers visible but not available yet (report supported:false from check()).
UNAVAILABLE_PROVIDERS = ()
# Providers that meaningfully accept a custom base URL.
BASE_URL_PROVIDERS = ("openai-compatible", "ollama", "openrouter")
# Providers that need no API key (offline demo / local CLI login). The transport
# is fully determined by the provider — there is no separate "mode" axis.
KEYLESS_PROVIDERS = ("echo", "claude-code-local", *LOCAL_TEXT_PROVIDERS)

# Old provider ids → current ids (silent migration of stored settings). The
# "-workflow" suffix is a fossil from the removed Mode axis.
PROVIDER_ALIASES = {"claude-code-workflow": "claude-code-local"}

_ROLE_LABELS = {"architect": "Architect", "senior_dev": "Senior Dev", "verifier": "Verifier"}
_PROVIDER_LABELS = {
    "codex-local": "Codex (local CLI)",
    "claude-code-local": "Claude Code (local CLI)",
    "echo": "Echo (offline demo)",
    "openai-compatible": "OpenAI (API)",
    "anthropic": "Anthropic (API)",
    "openrouter": "OpenRouter",
    "ollama": "Ollama",
}

# Field length caps (defensive — these are user-supplied strings).
_MAX_MODEL = 200
_MAX_BASEURL = 500
_MAX_KEY = 1000
_MAX_CUSTOM = 2000          # per-role custom instructions (additive taste, not permissions)


# ─── Helpers ─────────────────────────────────────────────────────────────── #

def _s(v, cap: int = 1000) -> str:
    """Coerce a value to a stripped, length-capped string."""
    return (v if isinstance(v, str) else "").strip()[:cap]


def mask_key(key: str) -> str:
    """Mask a secret for display — e.g. 'sk-...abcd'. Empty stays empty.

    Args:
        key: str — the raw secret.

    Returns:
        str — masked form safe to expose to the frontend.
    """
    k = key if isinstance(key, str) else ""
    if not k:
        return ""
    if len(k) <= 8:
        return "****"
    return f"{k[:3]}...{k[-4:]}"


# ─── Normalization ───────────────────────────────────────────────────────── #

def normalize_role(cfg) -> dict:
    """Coerce a single role config into the safe internal shape (raw key kept).

    Args:
        cfg: any — candidate role config.

    Returns:
        dict — {provider, model, baseUrl, apiKey, enabled}. (Any legacy ``mode``
        field is ignored — transport is determined entirely by the provider.)
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    provider = cfg.get("provider")
    provider = PROVIDER_ALIASES.get(provider, provider)  # migrate legacy ids
    if provider not in PROVIDERS:
        provider = "claude-code-local"
    return {
        "provider": provider,
        "model": _s(cfg.get("model"), _MAX_MODEL),
        "baseUrl": _s(cfg.get("baseUrl"), _MAX_BASEURL),
        "apiKey": _s(cfg.get("apiKey"), _MAX_KEY),
        "enabled": bool(cfg.get("enabled", True)),
        # Additive per-role instructions (Council chat). Tunes taste/style only — it
        # is layered AFTER OpenFDE's fixed read-only role contract, never overrides it.
        "customPrompt": _s(cfg.get("customPrompt"), _MAX_CUSTOM),
    }


def normalize(settings) -> dict:
    """Coerce a full settings map into all three roles, each normalized.

    Args:
        settings: any — candidate settings map.

    Returns:
        dict — {architect, senior_dev, verifier} internal role configs.
    """
    settings = settings if isinstance(settings, dict) else {}
    return {role: normalize_role(settings.get(role)) for role in ROLES}


def default_settings() -> dict:
    """Return the default settings: every role runs via Claude Code workflow.

    Returns:
        dict — normalized default settings (no secrets).
    """
    return normalize({})


def merge(existing, incoming) -> dict:
    """Apply a full/partial update over existing settings, preserving secrets.

    For each role present in ``incoming``, known fields overwrite the stored
    role. The apiKey is preserved unless the caller supplies a non-empty
    ``apiKey`` (replace) or ``clearApiKey: true`` (wipe). Public-only fields such
    as ``maskedApiKey`` / ``hasApiKey`` are ignored, so a sanitized payload can
    be safely round-tripped without ever overwriting the stored key.

    Args:
        existing: any — current stored settings.
        incoming: any — full or partial update.

    Returns:
        dict — merged, normalized internal settings.
    """
    base = normalize(existing)
    incoming = incoming if isinstance(incoming, dict) else {}
    out = {}
    for role in ROLES:
        cur = dict(base[role])
        inc = incoming.get(role)
        if not isinstance(inc, dict):
            out[role] = cur
            continue
        for field in ("provider", "model", "baseUrl", "enabled", "customPrompt"):
            if field in inc:
                cur[field] = inc[field]
        if inc.get("clearApiKey"):
            cur["apiKey"] = ""
        elif _s(inc.get("apiKey"), _MAX_KEY):
            cur["apiKey"] = inc["apiKey"]
        # else: keep the stored key (sanitized round-trip / unchanged field)
        out[role] = normalize_role(cur)
    return out


# ─── Public projection (secrets stripped) ────────────────────────────────── #

def to_public(settings) -> dict:
    """Project settings to a secret-free shape for the frontend.

    Args:
        settings: any — internal settings (may contain raw keys).

    Returns:
        dict — same roles, each with hasApiKey + maskedApiKey and NO apiKey.
    """
    s = normalize(settings)
    out = {}
    for role in ROLES:
        c = s[role]
        out[role] = {
            "provider": c["provider"],
            "model": c["model"],
            "baseUrl": c["baseUrl"],
            "enabled": c["enabled"],
            "customPrompt": c["customPrompt"],     # not a secret — round-trips to the UI
            "hasApiKey": bool(c["apiKey"]),
            "maskedApiKey": mask_key(c["apiKey"]),
        }
    return out


# ─── Validation (shape only — never a network call) ──────────────────────── #

def check_role(role: str, cfg: dict) -> dict:
    """Validate one role's config shape. Pure: no network, no side effects.

    Args:
        role: str — role id.
        cfg: dict — internal (normalized) role config; a stored apiKey counts.

    Returns:
        dict — {role, label, supported, ok, configured, status, message}.
    """
    cfg = normalize_role(cfg)
    provider = cfg["provider"]
    has_key = bool(cfg["apiKey"])
    label = _ROLE_LABELS.get(role, role)
    pl = _PROVIDER_LABELS.get(provider, provider)

    def result(supported, ok, configured, status, message):
        return {
            "role": role, "label": label, "supported": supported, "ok": ok,
            "configured": configured, "status": status, "message": message,
        }

    # Validity is now a pure function of the provider (no separate "mode" axis).

    # Any provider parked as not-yet-available is visible but reports unavailable.
    if provider in UNAVAILABLE_PROVIDERS:
        return result(False, False, False, "unavailable",
                      f"{pl} is not available yet (planned for a later step).")

    if not cfg["enabled"]:
        return result(True, True, False, "disabled", f"{label} is disabled.")

    # Keyless local-CLI text roles (Codex Local) — Architect / Verifier only.
    if provider in LOCAL_TEXT_PROVIDERS:
        if role == "senior_dev":
            return result(True, False, False, "error",
                          f"{pl} is a text-only role (Architect/Verifier). Senior Dev needs "
                          "Claude Code, Anthropic, or Echo.")
        return result(True, True, True, "configured",
                      f"{pl} — local CLI text role, no API key needed.")

    # Claude Code (local CLI) — keyless, runs on the user's login.
    if provider == "claude-code-local":
        return result(True, True, True, "configured",
                      f"{pl} — runs on your Claude login, no API key needed.")

    # Echo — offline deterministic demo, no key or model.
    if provider == "echo":
        return result(True, True, True, "configured",
                      "Echo — offline demo, no key or model needed.")

    # Everything else is a hosted API provider: needs a model and a key.
    if not cfg["model"]:
        return result(True, False, False, "missing", f"{pl} needs a model id.")
    if not has_key:
        return result(True, False, False, "missing",
                      f"{pl} needs an API key (enter one or keep the stored key).")
    return result(True, True, True, "configured", f"{pl} ready.")


def check(settings, role: str = None) -> dict:
    """Validate settings shape for one role or all roles.

    Args:
        settings: any — internal settings (stored keys honored).
        role: str | None — a single role to check, or None for all.

    Returns:
        dict — {ok: bool, roles: {role: check_role(...)}}.
    """
    s = normalize(settings)
    targets = [role] if role in ROLES else list(ROLES)
    results = {r: check_role(r, s[r]) for r in targets}
    return {"ok": all(r["ok"] for r in results.values()), "roles": results}


# ─── UI metadata ─────────────────────────────────────────────────────────── #

def options() -> dict:
    """Return roles / providers metadata for rendering the settings UI.

    Returns:
        dict — {roles, providers} each a list of {id, label, ...}. (Transport is
        determined by the provider; there is no separate "mode" axis.)
    """
    return {
        "roles": [{"id": r, "label": _ROLE_LABELS[r]} for r in ROLES],
        "providers": [
            {
                "id": p, "label": _PROVIDER_LABELS[p],
                # Both local-CLI providers are kind 'local'; 'textOnly' tells them
                # apart (Codex = text-only Architect/Verifier; Claude Code can edit).
                "kind": ("local" if p in ("codex-local", "claude-code-local") else "api"),
                "supportsBaseUrl": p in BASE_URL_PROVIDERS,
                "keyless": p in KEYLESS_PROVIDERS,
                "textOnly": p in LOCAL_TEXT_PROVIDERS,
                "available": p not in UNAVAILABLE_PROVIDERS,
            }
            for p in PROVIDERS
        ],
    }
