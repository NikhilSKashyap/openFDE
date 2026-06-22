"""
openfde/episode_llm_summary.py — LLM Story Summarizer v1 (local CLI, deterministic fallback).

The deterministic summarizer (``episode_summary``) hit its ceiling: it strips obvious
noise but still surfaces literal prompt text. This adds a **best-effort LLM upgrade** that
rewrites a captured prompt into *what the user was trying to build* — a short product title,
a 1–2 sentence summary, and ``storyFacts`` (concepts / decisions / deferred / abandoned /
operational) that drive the Story lanes.

Principles:
  - **No external API by default.** Provider order: deterministic fallback → **Codex local
    CLI** → **Claude Code local CLI** → configured API (only if already enabled — not v1).
    Both CLI providers reuse the repo's read-only text roles (no repo mutation, no GUI app).
  - **Deterministic is always assigned first** and is the guaranteed fallback; the LLM is a
    best-effort overlay applied off the request path (a background tick).
  - **Cached.** Each episode carries a ``summaryFingerprint`` (prompt+files+commits); the LLM
    is attempted at most once per fingerprint (``summaryLlmTried``) — never on every page load.
  - **Strict JSON.** The model must return one JSON object; anything unparseable, a title
    that is a file list / shell command / generic "yes", or output worse than deterministic
    is rejected and we keep deterministic.
  - **Capture-safe.** Summarizer prompts carry ``[OpenFDE internal summarizer]`` so passive
    prompt capture drops them — an internal call must never become a prompt episode.
"""

import hashlib
import json
import logging
import os
import re

logger = logging.getLogger("openfde.episode_llm_summary")

# Marker prefixed to every summarizer prompt so prompt_capture can drop it (an internal
# summarizer call must never become a captured episode).
INTERNAL_MARKER = "[OpenFDE internal summarizer]"

_SYSTEM = (
    "You are OpenFDE's story summarizer. Given a captured coding prompt and its context, "
    "describe WHAT THE USER WAS TRYING TO BUILD — the product/architecture concept — NOT the "
    "literal wrapper text, shell commands, file lists, or chit-chat.\n\n"
    "Output STRICT JSON ONLY — one object, no prose, no markdown fences:\n"
    '{"title":"3-6 word product title","summary":"1-2 sentences","concepts":["..."],'
    '"decisions":["..."],"deferred":["..."],"abandoned":["..."],"operational":false,'
    '"confidence":0.0}\n\n'
    "Rules:\n"
    "- title: 3-6 words, Title Case, under 60 chars, a product concept. NEVER a filename, a "
    "shell command, or a bare 'yes/ok/here'.\n"
    "- summary: 1-2 sentences, under 300 chars.\n"
    "- concepts/decisions/deferred/abandoned: short noun phrases, max 6 each, [] if none.\n"
    "- operational: true when the prompt is just status/debug/chatter/commands/file-lists/"
    "acknowledgement (e.g. 'yes', 'curl ...', 'read these files', 'restart the server', a "
    "pasted file list). Operational episodes are hidden from the story.\n"
    "- confidence: 0.0-1.0.\n"
    "Return ONLY the JSON object."
)

_GENERIC_TITLES = {"yes", "ok", "okay", "sure", "no", "done", "here", "prompt", "change",
                   "update", "fix", "the prompt", "this", "n/a", "none"}


def fingerprint(episode: dict) -> str:
    """Stable hash of the inputs a summary depends on (prompt + files + commits)."""
    raw = "\x1e".join([
        (episode.get("prompt") or ""),
        "\x1f".join(episode.get("files") or []),
        "\x1f".join(episode.get("commitShas") or []),
    ])
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _clean_str(v) -> str:
    """Trim a model string: strip markdown fences/quotes/backticks and collapse space."""
    if not isinstance(v, str):
        return ""
    s = v.strip().strip("`").strip()
    s = re.sub(r"^#+\s*", "", s)                 # leading markdown heading
    s = s.strip('"').strip("'").strip()
    return re.sub(r"\s+", " ", s).strip()


def parse_summary_json(text: str):
    """Best-effort extraction of the JSON object from a model's text output (or None)."""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    start = t.find("{")
    end = t.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(t[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        # Tolerate trailing junk: shrink from the last brace inward.
        for e in range(end, start, -1):
            if t[e] == "}":
                try:
                    return json.loads(t[start:e + 1])
                except (json.JSONDecodeError, ValueError):
                    continue
    return None


def validate(obj: dict, det_title: str = "") -> dict:
    """Validate + clean a parsed summary object; return the cleaned dict or None if rejected."""
    if not isinstance(obj, dict):
        return None
    from openfde.episode_summary import is_bad_title
    title = _clean_str(obj.get("title"))
    if not title or len(title) > 60 or is_bad_title(title):
        return None
    summary = _clean_str(obj.get("summary"))
    if len(summary) > 300:
        summary = summary[:299].rstrip() + "…"

    def _arr(key):
        v = obj.get(key)
        if not isinstance(v, list):
            return []
        out = []
        for x in v:
            c = _clean_str(x)
            if c and c not in out:
                out.append(c)
        return out[:6]

    conf = obj.get("confidence", 0.0)
    conf = float(conf) if isinstance(conf, (int, float)) else 0.0
    return {
        "title": title, "summary": summary or title,
        "concepts": _arr("concepts"), "decisions": _arr("decisions"),
        "deferred": _arr("deferred"), "abandoned": _arr("abandoned"),
        "operational": bool(obj.get("operational")),
        "confidence": max(0.0, min(1.0, conf)),
    }


def _build_input(episode: dict) -> str:
    """A compact summarizer input — context, not the whole world (no giant patches)."""
    parts = [
        f"KIND: {episode.get('kind') or '?'} | SOURCE: {episode.get('source') or '?'} | "
        f"STATUS: {episode.get('status') or '?'}",
        f"DETERMINISTIC_TITLE: {episode.get('title') or ''}",
        f"DETERMINISTIC_SUMMARY: {episode.get('summary') or ''}",
    ]
    prompt = (episode.get("prompt") or "").strip()
    if prompt:
        parts.append("PROMPT:\n" + (prompt[:2000] + (" …" if len(prompt) > 2000 else "")))
    files = episode.get("files") or []
    if files:
        parts.append("CHANGED_FILES:\n" + "\n".join("- " + f for f in files[:20]))
    shas = episode.get("commitShas") or []
    if shas:
        parts.append(f"COMMITS: {len(shas)} landed")
    return "\n\n".join(parts)


def _default_invoke(provider: str, system: str, user: str, timeout: int) -> str:
    """Dispatch to a local CLI text role (read-only, no repo mutation, no GUI app)."""
    os.environ.setdefault("OPENFDE_INTERNAL", "1")
    os.environ.setdefault("OPENFDE_SUMMARIZER", "1")
    if provider == "codex-local":
        from openfde.codex_local_runner import run_codex_local_text
        return run_codex_local_text(system=system, user=user, cwd=None, timeout=timeout) or ""
    if provider == "claude-local":
        from openfde.claude_code_runner import run_claude_code_text
        return run_claude_code_text(system=system, user=user, cwd=None, timeout=timeout) or ""
    return ""


def available_providers() -> list:
    """Detect local CLI providers in priority order (Codex, then Claude). [] when disabled."""
    if os.environ.get("OPENFDE_LLM_SUMMARY") == "0":
        return []
    out = []
    try:
        from openfde import codex_local_runner as cx
        if cx.cli_available():
            out.append("codex-local")
    except Exception:  # noqa: BLE001
        pass
    try:
        from openfde import claude_code_runner as cc
        if cc.cli_available():
            out.append("claude-local")
    except Exception:  # noqa: BLE001
        pass
    return out


def summarize_episode(episode: dict, *, invoke=None, providers=None, timeout: int = 30) -> dict:
    """Try the LLM providers in order; return a cleaned summary dict (+ summarySource) or None.

    Args:
        episode: dict — the episode to summarize.
        invoke: callable(provider, system, user, timeout) -> str — CLI dispatcher (injectable
            for tests; defaults to the local Codex/Claude text roles).
        providers: list[str] | None — provider order (defaults to detected CLIs).
        timeout: int — per-call wall-clock seconds.

    Returns:
        dict | None — {title, summary, concepts, decisions, deferred, abandoned, operational,
                       confidence, summarySource}, or None if every provider fails/rejects.
    """
    invoke = invoke or _default_invoke
    provs = providers if providers is not None else available_providers()
    if not provs:
        return None
    user = INTERNAL_MARKER + "\n\n" + _build_input(episode)
    for prov in provs:
        try:
            text = invoke(prov, _SYSTEM, user, timeout)
        except Exception:  # noqa: BLE001 — a bad provider must not raise
            logger.debug("summarizer provider %s raised", prov, exc_info=True)
            text = ""
        clean = validate(parse_summary_json(text) or {}, episode.get("title") or "")
        if clean:
            clean["summarySource"] = prov
            return clean
    return None


def deterministic_story_facts(episode: dict) -> dict:
    """Story facts from the deterministic signal extraction (the guaranteed fallback)."""
    from openfde.episode_summary import is_operational, is_intent_graph_episode
    from openfde.prompt_story import _signals
    # A Sketch-First intent run is a product build by construction — its step text ("read the
    # data") trips the operational heuristics, so never let the deterministic pass bury it.
    operational = (False if is_intent_graph_episode(episode)
                   else bool(episode.get("signal") == "operational"
                             or is_operational(episode.get("prompt") or "")))
    deferred, abandoned = [], []
    # storyFacts carry only the strong lanes; next/watch (and the deferred revisit
    # trigger) are re-derived from the raw text by build_prompt_graph at read time.
    for phrase, kind, _trigger in _signals(episode):
        if kind == "abandoned":
            abandoned.append(phrase)
        elif kind == "deferred":
            deferred.append(phrase)
    title = (episode.get("title") or "").strip()
    return {
        "concepts": [] if operational else ([title] if title else []),
        "decisions": [],
        "deferred": deferred[:6],
        "abandoned": abandoned[:6],
        "operational": operational,
    }


def wants_llm(episode: dict) -> bool:
    """True when an episode is eligible for an LLM upgrade (deterministic, not yet tried,
    and not already classified operational by the deterministic pass)."""
    return (episode.get("summarySource") in (None, "deterministic")
            and not episode.get("summaryLlmTried")
            and episode.get("signal") != "operational")


def enrich(episode: dict, *, invoke=None, providers=None, timeout: int = 30, allow_llm: bool = True) -> bool:
    """Ensure an episode carries story metadata; mutate in place. Returns True if changed.

    Deterministic facts are assigned immediately (cheap). When ``allow_llm`` and the episode
    still needs it, one LLM attempt (per fingerprint) may upgrade the title/summary/storyFacts.
    """
    from openfde.episode_summary import (is_bad_title, derive_title_summary, is_operational,
                                          operational_title)
    changed = False

    # 0) Repair existing bad titles ("Yes", "`ROADMAP.md`", "Here's the CC prompt", …).
    # A stored bad title means stale metadata: re-derive deterministically (preferring the
    # prompt's Goal/Product-Change heading), and drop the summary cache so storyFacts re-derive
    # and the LLM re-attempts a real title. When the re-derive is STILL bad — a wrapper/meta
    # prompt whose own text is boilerplate ("Here's the Claude Code prompt: …") — show a clean
    # neutral operational label + summary instead of leaking the raw line, and keep the episode
    # operational (out of Story). Never touches episodeId / sequence / tag / commitShas / files /
    # the original prompt (the raw text stays as Full-prompt evidence).
    if is_bad_title(episode.get("title") or ""):
        t, s = derive_title_summary(episode.get("prompt") or "", episode.get("files"))
        if is_bad_title(t):
            t = operational_title(episode)
            s = "Captured implementation prompt."
            episode["signal"] = "operational"
        else:
            episode["signal"] = "operational" if is_operational(episode.get("prompt") or "") else "product"
        episode["title"], episode["summary"] = t, s
        for k in ("summaryFingerprint", "storyFacts", "summaryLlmTried", "summarySource", "summaryConfidence"):
            episode.pop(k, None)
        changed = True

    fp = fingerprint(episode)
    fresh = (episode.get("summaryFingerprint") == fp)

    # 1) Deterministic facts — guaranteed, refreshed when the fingerprint changes.
    if not fresh or not episode.get("storyFacts"):
        episode["storyFacts"] = deterministic_story_facts(episode)
        if not fresh:
            episode["summarySource"] = "deterministic"
            episode["summaryConfidence"] = 0.3
            episode["summaryLlmTried"] = False
        else:
            episode.setdefault("summarySource", "deterministic")
            episode.setdefault("summaryConfidence", 0.3)
        episode["summaryFingerprint"] = fp
        changed = True

    # 2) LLM upgrade — best-effort, at most once per fingerprint, off the request path.
    if allow_llm and wants_llm(episode):
        episode["summaryLlmTried"] = True          # mark attempted regardless of outcome
        changed = True
        clean = summarize_episode(episode, invoke=invoke, providers=providers, timeout=timeout)
        if clean:
            episode["title"] = clean["title"] or episode.get("title")
            episode["summary"] = clean["summary"] or episode.get("summary")
            episode["storyFacts"] = {
                "concepts": clean["concepts"] or ([] if clean["operational"] else [episode["title"]]),
                "decisions": clean["decisions"], "deferred": clean["deferred"],
                "abandoned": clean["abandoned"], "operational": clean["operational"],
            }
            episode["summarySource"] = clean["summarySource"]
            episode["summaryConfidence"] = clean["confidence"]
            episode["signal"] = "operational" if clean["operational"] else "product"
            logger.info("LLM-summarized %s via %s → %r", episode.get("episodeId"), clean["summarySource"], clean["title"])

    # 3) Sketch-First law (authoritative, after every other pass): an intent-graph run is a
    # PRODUCT build. Its step text trips the operational/scaffolding heuristics, so the
    # deterministic pass mislabels it operational + "Update <scope>". Enforce product here so
    # neither that fallback nor a stray LLM verdict can bury the run; replace ONLY a generic
    # deterministic title (a real LLM upgrade is kept).
    from openfde.episode_summary import intent_title_summary
    intent = intent_title_summary(episode)
    if intent:
        if episode.get("signal") != "product":
            episode["signal"] = "product"
            changed = True
        sf = episode.get("storyFacts")
        if isinstance(sf, dict) and sf.get("operational"):
            sf["operational"] = False
            if not sf.get("concepts"):
                sf["concepts"] = [episode.get("title") or intent[0]]
            changed = True
        if episode.get("summarySource") in (None, "deterministic"):
            t, s = intent
            if episode.get("title") != t:
                episode["title"] = t
                changed = True
            if episode.get("summary") != s:
                episode["summary"] = s
                changed = True
    return changed


def ensure_facts(persistence, *, allow_llm: bool = False, providers=None, invoke=None,
                 timeout: int = 30, limit=None) -> list:
    """Assign story metadata to episodes that need it; persist if changed; return episodes.

    Request path uses ``allow_llm=False`` (deterministic only — no subprocess). The background
    tick uses ``allow_llm=True, limit=1`` to upgrade one eligible episode per cycle.
    """
    eps = persistence.load_episodes()
    changed_ids = set()
    spent = 0
    for ep in eps:
        do_llm = allow_llm and (limit is None or spent < limit) and wants_llm(ep)
        if enrich(ep, invoke=invoke, providers=providers, timeout=timeout, allow_llm=do_llm):
            changed_ids.add(ep.get("episodeId"))
        if do_llm:
            spent += 1
    if not changed_ids:
        return eps
    # Merge-by-field at write time. The LLM call above takes SECONDS — between
    # our load and now, OTHER writers may have attached evidence (a Reproduce
    # run's verify receipts), files, or repair artifacts. A stale full-list
    # write here clobbered exactly that once. The summarizer owns ONLY its
    # narrative fields; everything else is taken fresh from the store.
    _OWNED = ("sequence", "tag", "title", "summary", "storyFacts",
              "summarySource", "summaryConfidence", "summaryFingerprint",
              "summaryLlmTried", "signal")
    by_id = {e.get("episodeId"): e for e in eps}
    fresh = persistence.load_episodes()
    for f in fresh:
        src = by_id.get(f.get("episodeId"))
        if src is not None and f.get("episodeId") in changed_ids:
            for k in _OWNED:
                if k in src:
                    f[k] = src[k]
    persistence._write_json(persistence.episodes_path, fresh)
    return fresh


# ── Change clustering (multi-commit Auto-Land) ──────────────────────────────
# Group an episode's changed files into 1–N LOGICAL commits — "prompt → episode →
# logical changes → one commit each → one OpenPM task each". Local LLM first, with a
# deterministic by-scope fallback that always covers every file exactly once.
_MAX_CLUSTERS = 8

_CLUSTER_SYSTEM = (
    "You are OpenFDE's change clusterer. Given a captured coding prompt and the list of files it "
    "changed, group those files into 1-N LOGICAL changes — each a coherent unit that should be ONE "
    "git commit (a feature and its tests, a fix, a refactor). A logical change may span directories. "
    "Every listed file must appear in exactly one group.\n\n"
    "Output STRICT JSON ONLY — one object, no prose, no markdown fences:\n"
    '{"commits":[{"title":"3-6 word Title Case concept","message":"imperative subject under 70 chars",'
    '"files":["repo/rel/path", ...]}, ...]}\n\n'
    "Rules:\n"
    "- Group by INTENT, not by folder. Keep tightly-coupled files together (an impl + its test).\n"
    "- Prefer FEWER, meaningful commits; never one-per-file unless the files are truly unrelated.\n"
    "- title: 3-6 words, Title Case, a product concept (never a filename or 'misc').\n"
    "- message: conventional imperative subject, no trailing period, under 70 chars.\n"
    "- files: repo-relative, ONLY from the provided list, each file in exactly one commit.\n"
    "Return ONLY the JSON object."
)


def _build_cluster_input(episode: dict, files: list, diff_stat: str = "") -> str:
    parts = [
        "PROMPT:\n" + (episode.get("prompt") or "")[:1500],
        "CHANGED_FILES:\n" + "\n".join("- " + f for f in files[:60]),
    ]
    if diff_stat:
        parts.append("DIFFSTAT:\n" + diff_stat[:1500])
    return "\n\n".join(parts)


def _commit_subject(message, title: str) -> str:
    """A clean ``openfde: <subject>`` commit subject from a cluster message (falling back to its
    title when the message is missing/noisy)."""
    from openfde.episode_summary import is_bad_title
    msg = _clean_str(message)
    if not msg or is_bad_title(msg):
        msg = title
    msg = re.sub(r"^openfde:\s*", "", msg, flags=re.I).strip() or "change"
    return ("openfde: " + msg)[:78]


def _validate_clusters(obj, files: list):
    """Validate an LLM cluster object against the scoped file set: every file ends up in exactly
    one commit (leftovers → a final 'Misc Changes' commit); hallucinated/duplicate files are
    dropped. Returns the cluster list, or None to signal the deterministic fallback."""
    if not isinstance(obj, dict):
        return None
    commits = obj.get("commits")
    if not isinstance(commits, list) or not commits:
        return None
    from openfde.episode_summary import is_bad_title
    fileset, seen, out = set(files), set(), []
    for c in commits:
        if not isinstance(c, dict):
            continue
        cf = [f for f in (c.get("files") or [])
              if isinstance(f, str) and f in fileset and f not in seen]
        if not cf:
            continue
        seen.update(cf)
        title = _clean_str(c.get("title"))
        if not title or is_bad_title(title):
            title = "Change"
        out.append({"title": title[:48], "message": _commit_subject(c.get("message"), title),
                    "files": sorted(cf)})
    if not out:
        return None
    leftover = sorted(fileset - seen)
    if leftover:
        out.append({"title": "Misc Changes", "message": "openfde: misc changes", "files": leftover})
    return out[:_MAX_CLUSTERS]


def _deterministic_clusters(episode: dict, files: list) -> list:
    """Fallback grouping: one commit per top-level scope (``openfde`` / ``frontend`` / ``tests`` …),
    titled from the episode. Deterministic, no model, always covers every file."""
    from openfde.episode_summary import is_bad_title
    base = (episode.get("title") or "").strip()
    if not base or is_bad_title(base):
        base = "Update"
    groups: dict = {}
    for f in files:
        seg = f.split("/")
        groups.setdefault(seg[0] if len(seg) > 1 else ".", []).append(f)
    multi = len(groups) > 1
    out = []
    for scope in sorted(groups):
        label = scope if scope != "." else "root"
        title = (f"{base} · {label}" if multi else base)[:48]
        msg = (f"openfde: {base} ({label})" if multi else f"openfde: {base}")[:78]
        out.append({"title": title, "message": msg, "files": sorted(groups[scope])})
    return out[:_MAX_CLUSTERS]


def cluster_changes(episode: dict, files, *, invoke=None, providers=None, timeout: int = 30,
                    diff_stat: str = "") -> list:
    """Group an episode's changed ``files`` into logical commits: ``[{title, message, files}]``.

    Local LLM first (best-effort, strict JSON via the summarizer machinery), deterministic
    by-scope fallback otherwise. Every input file appears in exactly one returned commit (capped
    at ``_MAX_CLUSTERS``). Drives multi-commit Auto-Land — one commit (and one OpenPM task) per
    logical change. ``providers=[]`` forces the deterministic path (the fast, no-subprocess mode
    callers use when they can't offload the LLM).
    """
    files = [f for f in (files or []) if f]
    if not files:
        return []
    if len(files) == 1:
        from openfde.episode_summary import is_bad_title
        t = (episode.get("title") or "").strip()
        if not t or is_bad_title(t):
            t = "Update"
        return [{"title": t[:48], "message": _commit_subject(t, t), "files": list(files)}]
    provs = providers if providers is not None else available_providers()
    if provs:
        user = INTERNAL_MARKER + "\n\n" + _build_cluster_input(episode, files, diff_stat)
        inv = invoke or _default_invoke
        for prov in provs:
            try:
                text = inv(prov, _CLUSTER_SYSTEM, user, timeout)
            except Exception:  # noqa: BLE001 — a bad provider must not raise
                logger.debug("clusterer provider %s raised", prov, exc_info=True)
                text = ""
            clusters = _validate_clusters(parse_summary_json(text), files)
            if clusters:
                return clusters
    return _deterministic_clusters(episode, files)
