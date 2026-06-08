"""Deterministic prompt → story metadata (Step 40 — Prompt Chapter Rail).

Turns a raw prompt episode into chip-/card-friendly metadata **without any LLM**:

  - ``title``    — a short 3–6 word story title (the rail chip label),
  - ``summary``  — a 1–2 sentence description (the episode card),
  - ``sequence`` — a monotonically-increasing per-repo number,
  - ``tag``      — ``P<sequence>`` (the OpenPM grouping tag).

The original ``prompt`` is always preserved verbatim elsewhere; this module only
*derives* a readable label from it. v1 is intentionally heuristic: strip obvious
meta prefixes ("please", "can you", "let's", "CC said", …), prefer the first
meaningful intent line (the line after a ``Goal:``/``Task:`` header when present),
and fall back to changed module/file names when the prompt has no usable text.
"""

import re

# Conversational / boilerplate prefixes to strip from the front of a line.
_META_PREFIXES = [
    "please", "can you", "could you", "would you", "will you", "can we", "could we",
    "let's", "lets", "let us", "i want to", "i'd like to", "i would like to",
    "i want", "i need to", "i need", "we should", "we need to", "we need",
    "go ahead and", "go ahead", "prompt cc", "cc said", "cc:", "claude", "hey",
    "okay", "ok", "so", "now", "just", "kindly", "next",
]
# Leading list/markdown noise: bullets, numbering, quotes, hashes.
_FILLER = re.compile(r"^[\s\-–—•*>#0-9.)\]]+")
# Lines that are pure boilerplate scaffolding (not the user's intent).
_SKIP_LINE = re.compile(
    r"^(you are\b|you'?re acting\b|acting as\b|read (the repo|first)\b|read\b|start with\b|"
    r"here'?s\b|begin\b|first,? read|the (cc|claude|sr dev|senior dev) prompt\b|"
    r"implementing the next\b|important — openfde\b|important - openfde\b|"
    r"yes[.,!]|okay[.,!]|ok[.,!]|sure[.,!]|thanks\b|i'?d switch\b)", re.I)
# Shell / status chatter — operational, never a product concept. Requires a flag/path
# arg (``\S*[-/]\S*``) so an English sentence that happens to start with a command word
# ("make the button async", "find the bug") is NOT misread as a shell command.
_SHELL_LINE = re.compile(
    r"^(curl|git|cd|python3?|pip3?|npm|npx|node|ls|grep|rg|cat|sed|awk|echo|mkdir|"
    r"rm|cp|mv|pkill|kill|nohup|chmod|export|brew|sudo|find|tail|head|touch|open|"
    r"code|ssh|scp|docker|make|pytest|eslint|vite)\b\s+\S*[-/]\S*", re.I)
# A heading whose content (or next line) is the real intent.
_GOAL_HEADER = re.compile(
    r"^\s*#*\s*(goal|task|objective|intent|product change|product correction|"
    r"product model|core behavior|core behaviour|the product change|summary|overview)\b"
    r"\s*[:.\-]?\s*(.*)$", re.I)
_PATH_TOKEN = re.compile(r"^[\w.@~+/-]+$")


def _looks_path(tok: str) -> bool:
    """A token that reads like a file path / filename (has a dot or slash)."""
    return bool(_PATH_TOKEN.fullmatch(tok)) and ("/" in tok or "." in tok)


def _is_filelist(s: str) -> bool:
    """A line that is just path/filename tokens (e.g. ``ROADMAP.md`` / ``openfde/x.py``)."""
    toks = [t.strip(",") for t in s.split() if t.strip(",")]
    return bool(toks) and all(_looks_path(t) for t in toks) and any(("/" in t or "." in t) for t in toks)


def _is_operational_line(s: str) -> bool:
    """A line that is shell/status chatter, a file list, a URL, or boilerplate scaffolding."""
    low = s.lower()
    return bool(_SKIP_LINE.match(s) or _SHELL_LINE.match(s) or _is_filelist(s)
                or "localhost:" in low or "127.0.0.1" in low)


def _strip_meta(s: str) -> str:
    """Strip leading list noise + conversational prefixes from one line."""
    s = _FILLER.sub("", (s or "")).strip()
    low = s.lower()
    changed = True
    while changed:
        changed = False
        for p in _META_PREFIXES:
            if low == p or low.startswith(p + " ") or low.startswith(p + ","):
                s = s[len(p):].lstrip(" ,:-").strip()
                low = s.lower()
                changed = True
    return s


def _meaningful_lines(prompt: str) -> list:
    """Lines that read like user intent: meta-stripped, boilerplate/headers removed.

    A ``Goal:``/``Task:`` header contributes its inline content (if any) but is not
    itself emitted, so the very next real line leads. Scaffolding lines ("You are
    implementing…", "Read the repo…") are dropped.
    """
    out = []
    for raw in (prompt or "").splitlines():
        m = _GOAL_HEADER.match(raw)
        if m:
            inline = _strip_meta(m.group(2))
            if len(inline) >= 3 and not _is_operational_line(inline):
                out.append(inline)
            continue
        s = _strip_meta(raw)
        if len(s) >= 3 and not _is_operational_line(s):
            out.append(s)
    return out


# Bare acknowledgements / non-actionable replies — operational, not product intent.
_ACK_WORDS = {
    "yes", "ya", "yeah", "yep", "yup", "ok", "okay", "k", "sure", "no", "nope", "n",
    "done", "thanks", "thank you", "ty", "lgtm", "ship it", "go", "go ahead", "go for it",
    "sounds good", "great", "nice", "cool", "perfect", "right", "correct", "agreed",
}


def is_operational(prompt: str) -> bool:
    """True when a prompt has no product/build intent — only shell/status chatter, file
    lists, boilerplate, or a bare acknowledgement ("yes", "ok"). Such episodes are flagged
    ``signal: operational`` and kept out of Story's active concepts. (The LLM summarizer
    catches the subtler operational phrasings deterministic can't, e.g. "restart the server".)
    """
    lines = _meaningful_lines(prompt)
    if not lines:
        return True
    joined = " ".join(lines).strip().lower().strip(" .!?,")
    return joined in _ACK_WORDS


def _cap_title(t: str, n: int = 46) -> str:
    t = (t or "").strip().rstrip(".,;:—–-")
    if len(t) > n:
        t = t[:n - 1].rstrip() + "…"
    return t


# Generic / non-product titles that should never head a Story concept or a chip.
_GENERIC_TITLE = {
    "yes", "ya", "yeah", "ok", "okay", "k", "sure", "no", "nope", "done", "thanks",
    "here", "prompt", "change", "update", "fix", "this", "that", "it", "n/a", "none",
    "stuff", "thing", "things", "the prompt", "the cc prompt", "wip", "todo", "test",
}
# Leading boilerplate/operational phrasings a title must not start with.
_BAD_TITLE_LEAD = re.compile(
    r"^(here'?s|here is|you are|you'?re|read (the|first)|read\b|start with|important\b|"
    r"implementing the|first,? read|the (cc|claude|sr dev|senior dev) prompt|"
    r"restart the|let'?s\b|i'?d switch\b|acting as)\b", re.I)


def is_bad_title(title: str) -> bool:
    """True when a title is operational/meta and must not become a concept or chip label.

    Catches bare acks ("yes"/"ok"), boilerplate ("here's the CC prompt", "read the repo",
    "you are implementing"), the machine directive, shell commands, and file-list titles
    (``ROADMAP.md`` / ``openfde/server.py``, with or without backticks).
    """
    t = (title or "").strip().strip("`\"' ").strip()
    if len(t) < 2:
        return True
    low = t.lower().strip(" .!?,:")
    if low in _GENERIC_TITLE:
        return True
    if "openfde owns version control" in low:
        return True
    if _BAD_TITLE_LEAD.match(t):
        return True
    if _SHELL_LINE.match(t) or _is_filelist(t):
        return True
    return False


def _distill_title(line: str) -> str:
    """Trim an intent line into a concept: drop a leading 'implement'/'move … to' verb and a
    trailing version marker, so 'Implement LLM Story Summarizer v1' → 'LLM Story Summarizer'."""
    s = (line or "").strip()
    s2 = re.sub(r"^(please\s+)?(design and implement|implement the|implement|build out)\s+", "", s, flags=re.I)
    s2 = re.sub(r"^move\s+(from\s+.+?\s+)?to\s+", "", s2, flags=re.I)
    s2 = re.sub(r"\s+v\d+(\.\d+)?\b\.?$", "", s2, flags=re.I)
    s2 = s2.strip(" :–—-")
    return s2 if len(s2) >= 3 else s


def _scope_names(files) -> list:
    """Up to two distinct top-level dir/module scopes from changed paths."""
    names = []
    for p in (files or []):
        if not p:
            continue
        seg = p.split("/")
        names.append(seg[0] if len(seg) > 1 else seg[0])
    return list(dict.fromkeys(names))[:2]


def derive_title_summary(prompt: str, files=None):
    """Return ``(title, summary)`` for a prompt episode (deterministic).

    Args:
        prompt: str — the original user prompt (may be empty).
        files: list[str] | None — changed repo-relative paths (title/summary fallback).

    Returns:
        (str, str) — a compact story title and a 1–2 sentence summary.
    """
    lines = _meaningful_lines(prompt)
    base = lines[0] if lines else ""
    if not base:
        names = _scope_names(files)
        if names:
            t = "Update " + ", ".join(names)
            return _cap_title(t), f"Changes under {', '.join(names)}."
        return "Prompt", "A captured prompt — no description yet."

    # Title: first ~7 words of the lead intent *sentence*, distilled (drop a leading
    # "Implement"/"Move … to" verb + a trailing "v1") so "Implement LLM Story Summarizer
    # v1" → "LLM Story Summarizer", capped, sentence-cased.
    title_base = _distill_title(re.split(r"(?<=[.!?])\s+", base)[0])
    title = _cap_title(" ".join(title_base.split()[:7]))
    title = (title[:1].upper() + title[1:]) if title else title

    # Summary: first 1–2 sentences of the meaningful body (boilerplate skipped).
    text = re.sub(r"\s+", " ", " ".join(lines)).strip() or base
    parts = re.split(r"(?<=[.!?])\s+", text)
    summary = " ".join(parts[:2]).strip()
    if len(summary) > 200:
        summary = summary[:199].rstrip() + "…"
    if summary and summary[-1] not in ".!?…":
        summary += "."
    summary = (summary[:1].upper() + summary[1:]) if summary else summary
    return title, summary


def enrich_episode(ep: dict, max_seq: int) -> int:
    """Assign ``sequence``/``tag``/``title``/``summary`` in place when missing.

    Idempotent: fields already present are never overwritten (so a landed episode
    keeps its number/label forever). ``sequence`` is only assigned when absent, by
    bumping ``max_seq`` — the caller threads the running maximum across a batch so
    numbers stay unique and monotonically increasing per repo.

    Args:
        ep: dict — the episode (mutated in place).
        max_seq: int — the highest sequence seen so far.

    Returns:
        int — the (possibly incremented) running maximum sequence.
    """
    if not ep.get("sequence"):
        max_seq += 1
        ep["sequence"] = max_seq
    if not ep.get("tag"):
        ep["tag"] = f"P{ep['sequence']}"
    if not ep.get("title") or not ep.get("summary"):
        title, summary = derive_title_summary(ep.get("prompt") or "", ep.get("files"))
        if not ep.get("title"):
            ep["title"] = title
        if not ep.get("summary"):
            ep["summary"] = summary
    if not ep.get("signal"):
        # Product/build prompt vs operational chatter (shell/status/file-list) — the
        # Story keeps operational episodes out of the active concepts.
        ep["signal"] = "operational" if is_operational(ep.get("prompt") or "") else "product"
    return max_seq
