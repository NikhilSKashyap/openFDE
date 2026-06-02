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
MODES = ("workflow", "api", "local_bridge", "disabled")
PROVIDERS = (
    "claude-code-workflow",
    "echo",
    "openai-compatible",
    "anthropic",
    "openrouter",
    "ollama",
    "custom",
    "codex-local",
    "claude-code-local",
)
# Providers that will eventually shell out to a local CLI bridge — not available
# in this step. They report supported:false from check().
LOCAL_BRIDGE_PROVIDERS = ("codex-local", "claude-code-local")
# Providers that meaningfully accept a custom base URL.
BASE_URL_PROVIDERS = ("openai-compatible", "custom", "ollama", "openrouter")
# Providers that need no API key (offline / local demo).
KEYLESS_PROVIDERS = ("echo",)

_ROLE_LABELS = {"architect": "Architect", "senior_dev": "Senior Dev", "verifier": "Verifier"}
_PROVIDER_LABELS = {
    "claude-code-workflow": "Claude Code Workflow",
    "echo": "Echo (offline demo)",
    "openai-compatible": "OpenAI-compatible",
    "anthropic": "Anthropic",
    "openrouter": "OpenRouter",
    "ollama": "Ollama",
    "custom": "Custom (OpenAI-compatible)",
    "codex-local": "Codex (local bridge)",
    "claude-code-local": "Claude Code (local bridge)",
}
_MODE_LABELS = {"workflow": "Workflow", "api": "API", "local_bridge": "Local bridge", "disabled": "Disabled"}

# Field length caps (defensive — these are user-supplied strings).
_MAX_MODEL = 200
_MAX_BASEURL = 500
_MAX_KEY = 1000


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
        dict — {mode, provider, model, baseUrl, apiKey, enabled}.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    mode = cfg.get("mode")
    if mode not in MODES:
        mode = "workflow"
    provider = cfg.get("provider")
    if provider not in PROVIDERS:
        provider = "claude-code-workflow"
    return {
        "mode": mode,
        "provider": provider,
        "model": _s(cfg.get("model"), _MAX_MODEL),
        "baseUrl": _s(cfg.get("baseUrl"), _MAX_BASEURL),
        "apiKey": _s(cfg.get("apiKey"), _MAX_KEY),
        "enabled": bool(cfg.get("enabled", True)),
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
        for field in ("mode", "provider", "model", "baseUrl", "enabled"):
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
            "mode": c["mode"],
            "provider": c["provider"],
            "model": c["model"],
            "baseUrl": c["baseUrl"],
            "enabled": c["enabled"],
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
    mode, provider = cfg["mode"], cfg["provider"]
    has_key = bool(cfg["apiKey"])
    label = _ROLE_LABELS.get(role, role)

    def result(supported, ok, configured, status, message):
        return {
            "role": role, "label": label, "supported": supported, "ok": ok,
            "configured": configured, "status": status, "message": message,
        }

    # Local bridges (by mode or provider) are visible but not available yet.
    if mode == "local_bridge" or provider in LOCAL_BRIDGE_PROVIDERS:
        pl = _PROVIDER_LABELS.get(provider, provider)
        return result(False, False, False, "unavailable",
                      f"{pl} local bridge is not available yet (planned for a later step).")

    if mode == "disabled" or not cfg["enabled"]:
        return result(True, True, False, "disabled", f"{label} is disabled.")

    if mode == "workflow":
        if provider != "claude-code-workflow":
            return result(True, False, False, "error",
                          "Workflow mode requires the claude-code-workflow provider.")
        return result(True, True, True, "configured", "Runs via Claude Code workflow.")

    if mode == "api":
        if provider == "echo":
            return result(True, True, True, "configured",
                          "Echo provider — offline demo, no key or model needed.")
        if provider in ("claude-code-workflow", ""):
            return result(True, False, False, "error",
                          "API mode requires an API provider (Anthropic, OpenAI-compatible, …).")
        if not cfg["model"]:
            return result(True, False, False, "missing", "Model is required for API mode.")
        if not has_key:
            return result(True, False, False, "missing",
                          "API key is required (enter one or keep the stored key).")
        return result(True, True, True, "configured", f"{_PROVIDER_LABELS.get(provider, provider)} ready.")

    return result(True, False, False, "error", f"Unknown mode '{mode}'.")


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
    """Return roles / modes / providers metadata for rendering the settings UI.

    Returns:
        dict — {roles, modes, providers} each a list of {id, label, ...}.
    """
    return {
        "roles": [{"id": r, "label": _ROLE_LABELS[r]} for r in ROLES],
        "modes": [{"id": m, "label": _MODE_LABELS[m]} for m in MODES],
        "providers": [
            {
                "id": p, "label": _PROVIDER_LABELS[p],
                "kind": ("local_bridge" if p in LOCAL_BRIDGE_PROVIDERS
                         else "workflow" if p == "claude-code-workflow" else "api"),
                "supportsBaseUrl": p in BASE_URL_PROVIDERS,
                "keyless": p in KEYLESS_PROVIDERS,
                "available": p not in LOCAL_BRIDGE_PROVIDERS,
            }
            for p in PROVIDERS
        ],
    }
