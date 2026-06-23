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
import secrets

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


# ── Story-noise vocabulary: OS junk, demo planning, and change-based retitling ───
# These keep non-product work (a .DS_Store commit, "NanoGPT live demo" planning) out of
# the product Story while never touching real OpenFDE changes.

# OS / editor cruft — never product work, even when accidentally committed. Deliberately
# TINY: it must not swallow README, source, or real docs.
_JUNK_BASENAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".AppleDouble"}


def is_junk_path(path: str) -> bool:
    """True for OS/editor junk (``.DS_Store``, ``Thumbs.db``, …) by basename — local cruft
    that is never an OpenFDE product change, even if a commit swept it in."""
    base = str(path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return base in _JUNK_BASENAMES


# External demo subjects (other repos we record demos on) and demo-SEQUENCE language. A
# concept naming these is demo PLANNING, not an OpenFDE product concept.
_DEMO_TARGET_WORDS = ("nanogpt", "tailwind")
_DEMO_PLAN_MARKERS = (
    "live demo", "action demo", "action demos", "live run", "demo flow", "demo script",
    "walkthrough demo", "demo walkthrough", "self walkthrough", "feature walkthrough",
    "explanatory demo", "demo sequence", "two-minute", "two minute", "2-minute",
)


def is_demo_plan_concept(text: str) -> bool:
    """True when a phrase is demo PLANNING — an external demo target (NanoGPT, Tailwind) or
    demo-sequence/recording language (live demo, action demo, live run, walkthrough demo,
    self walkthrough, …). Such phrases stay in raw episode detail, never in Story concepts.

    Conservative on purpose: the phrase must name a demo target or a demo-sequence marker.
    The bare word "demo" does NOT qualify, so product copy ("Echo demo provider", "no-key
    demo") and real features survive."""
    low = (text or "").strip().lower()
    if not low:
        return False
    if any(w in low for w in _DEMO_TARGET_WORDS):
        return True
    if any(m in low for m in _DEMO_PLAN_MARKERS):
        return True
    return bool(re.search(r"\bdemo\s*(?:[0-9]|one|two|three)\b", low))


def is_demo_prompt(text: str) -> bool:
    """True when a PROMPT is about preparing/sequencing a demo (not implementing product).
    Used only to decide whether to title an episode by its code change instead of its words —
    never to hide a real commit. Needs a demo/walkthrough mention AND demo-prep context."""
    low = (text or "").lower()
    if not low.strip() or not any(h in low for h in ("demo", "walkthru", "walkthrough")):
        return False
    ctx = ("prep", "script", "two-minute", "2min", "2 min", "walkthr", "live", "record",
           "nanogpt", "tailwind", "explain", "narrative", "presentation", "slide",
           "demo 1", "demo 2", "demo 3", "demo1", "demo2", "demo3")
    return any(c in low for c in ctx)


def _clean_commit_subject(subject: str) -> str:
    """Drop a conventional-commit prefix (``openfde:`` / ``feat:`` / …) for use as a title."""
    s = (subject or "").strip()
    return re.sub(r"^(?:openfde|feat|fix|chore|docs|refactor|style|perf|test)\s*:\s*",
                  "", s, flags=re.I).strip()


def _dominant_component(files) -> str:
    """The most specific UI component/module name from changed paths — ``components/<Name>/``
    or a ``<Name>.(jsx|tsx|ts|js)`` basename — skipping generic shells (App/index/main)."""
    generic = {"app", "index", "main", "styles", "style", "globals"}
    for f in (files or []):
        f = str(f or "")
        m = re.search(r"/components/([^/]+)/", f) or re.search(r"/([A-Za-z0-9]+)\.[jt]sx?$", f)
        if m and m.group(1).lower() not in generic:
            return m.group(1)
    return ""


def product_title_from_change(files, commit_subject: str = ""):
    """A product ``(title, summary)`` derived from the CODE CHANGE — for an episode whose
    prompt is demo-ish but whose files/commit are a real OpenFDE change. Prefers a clean,
    non-demo commit subject; otherwise names the dominant component + change kind from the
    files. Returns ``None`` when there is no real source file to name (so callers keep the
    existing title)."""
    real = [f for f in (files or [])
            if f and not is_junk_path(f) and not str(f).lower().endswith(".md")]
    if not real:
        return None
    subj = _clean_commit_subject(commit_subject)
    if subj and not is_demo_plan_concept(subj) and not is_demo_prompt(subj):
        return _cap_title(subj), f"Changed {', '.join(real[:4])}."
    comp = _dominant_component(real)
    if not comp:
        return None                 # can't name the change specifically — keep the existing title
    kind = "layout" if any(str(f).lower().endswith((".css", ".scss")) for f in real) else "update"
    return _cap_title(f"{comp} {kind}"), f"Updated {comp} ({', '.join(real[:4])})."


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
    # Markdown code-fence openers: a prompt starting "```text" must not become a
    # title called "text" (the backtick strip in is_bad_title leaves the language token).
    "text", "bash", "python", "json", "diff", "shell", "sh", "console", "code",
    "markdown", "md", "yaml", "html", "css", "js", "jsx", "ts", "tsx",
}
# Leading boilerplate/operational phrasings a title must not start with.
_BAD_TITLE_LEAD = re.compile(
    r"^(here'?s|here is|you are|you'?re|read (the|first)|read\b|start with|important\b|"
    r"implementing the|first,? read|the (cc|claude|sr dev|senior dev) prompt|"
    r"restart the|let'?s\b|i'?d switch\b|acting as)\b", re.I)


# Smart quotes → ASCII, so matching is robust to curly apostrophes ("Here's" vs "Here's").
_SMART_QUOTES = str.maketrans({
    "‘": "'", "’": "'", "‛": "'", "ʼ": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
})


def _normalize_quotes(s: str) -> str:
    return (s or "").translate(_SMART_QUOTES)


def is_bad_title(title: str) -> bool:
    """True when a title is operational/meta and must not become a concept or chip label.

    Catches bare acks ("yes"/"ok"), boilerplate ("here's the CC prompt", "read the repo",
    "you are implementing"), the machine directive, shell commands, and file-list titles
    (``ROADMAP.md`` / ``openfde/server.py``, with or without backticks). Smart quotes are
    normalized first so a curly apostrophe ("Here's") is caught the same as a straight one.
    """
    t = _normalize_quotes(title or "").strip().strip("`\"' ").strip()
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


def operational_title(episode: dict) -> str:
    """A clean, neutral chip/card title for a wrapper/meta prompt episode — one whose own
    visible text is just boilerplate ("Here's the Claude Code prompt: …") with no product
    intent of its own. Names the agent when detectable. The raw prompt is preserved as
    evidence elsewhere (Full prompt); this only labels the surface so the rail/card read clean.
    """
    blob = _normalize_quotes((episode.get("title") or "") + " "
                             + (episode.get("prompt") or "")[:300]).lower()
    if "codex" in blob and "claude" not in blob:
        return "Codex Implementation Prompt"
    if "claude" in blob or "cc prompt" in blob:
        return "Claude Code Implementation Prompt"
    return "Implementation Prompt"


def _distill_title(line: str) -> str:
    """Trim an intent line into a concept: drop a leading 'implement'/'move … to' verb and a
    trailing version marker, so 'Implement LLM Story Summarizer v1' → 'LLM Story Summarizer'."""
    s = (line or "").strip()
    s2 = re.sub(r"^(please\s+)?(design and implement|implement the|implement|build out)\s+", "", s, flags=re.I)
    s2 = re.sub(r"^move\s+(from\s+.+?\s+)?to\s+", "", s2, flags=re.I)
    s2 = re.sub(r"\s+v\d+(\.\d+)?\b\.?$", "", s2, flags=re.I)
    s2 = s2.strip(" :–—-")
    return s2 if len(s2) >= 3 else s


def commit_display(episode_title: str, episode_summary: str, commit_summary: str) -> tuple:
    """Clean (title, summary) for an episode's commit card — the OpenPM/evidence display.

    Prefers the already-cleaned **episode** title/summary so a card reads like the prompt
    concept (``LLM Story Summarizer``), and never the noisy raw commit subject
    (``openfde: Here's the CC prompt``). Falls back to the de-``openfde:``-ed commit summary
    only when it's clean; ultimately ``"Landed change"``. The commit SHA + files stay as
    separate evidence on the card.

    Args:
        episode_title: str — the owning episode's cleaned title.
        episode_summary: str — the owning episode's cleaned 1–2 sentence summary.
        commit_summary: str — the raw commit subject.

    Returns:
        (str, str) — (displayTitle, displaySummary).
    """
    clean = re.sub(r"^openfde:\s*", "", (commit_summary or "").strip()).strip()
    et = (episode_title or "").strip()
    if et and not is_bad_title(et):
        title = et
    elif clean and not is_bad_title(clean):
        title = clean
    else:
        title = "Landed change"
    summary = (episode_summary or "").strip()
    if not summary and clean and not is_bad_title(clean):
        summary = clean
    return title, summary


def repair_episode_tasks(tasks, episodes):
    """Heal persisted OpenPM episode-commit cards whose stored title is noisy/meta.

    The durable counterpart to the reducer's `SYNC_EPISODE_COMMITS` self-heal: a card that
    represents an episode commit (``source == "openfde-episode"`` or carrying ``episodeId`` +
    ``commitSha``) and whose title is bad (``is_bad_title``) is rewritten from its owning
    episode's cleaned title/summary — so stale text can't be hydrated back into the UI. Clean
    cards and non-episode tasks are untouched; identity fields (``commitSha``/``shortSha``/
    ``files``/``episodeId``) are preserved. Idempotent.

    Args:
        tasks: list[dict] — persisted OpenPM tasks.
        episodes: list[dict] — episodes (to look up the owning one by id, else by commit sha).

    Returns:
        (list[dict], bool) — (possibly-repaired tasks, changed?).
    """
    if not isinstance(tasks, list):
        return tasks, False
    by_id, by_sha = {}, {}
    for e in (episodes or []):
        if isinstance(e, dict) and e.get("episodeId"):
            by_id[e["episodeId"]] = e
            for sha in (e.get("commitShas") or []):
                by_sha[sha] = e
    changed = False
    out = []
    for t in tasks:
        if not isinstance(t, dict):
            out.append(t)
            continue
        # Intent-graph cards carry a DELIBERATE step title ("read the data", "clean the data" —
        # which trip is_bad_title's `read the …` heuristic). Never rewrite them from commit text.
        if t.get("source") == "intent-graph":
            out.append(t)
            continue
        is_ep_card = (t.get("source") == "openfde-episode") or (t.get("episodeId") and t.get("commitSha"))
        if not is_ep_card or not is_bad_title(t.get("title") or ""):
            out.append(t)
            continue
        ep = by_id.get(t.get("episodeId")) or by_sha.get(t.get("commitSha")) or {}
        dtitle, dsummary = commit_display(ep.get("title"), ep.get("summary"), t.get("title"))
        nt = dict(t)
        nt["title"] = dtitle
        if not nt.get("description") and dsummary:
            nt["description"] = dsummary
        if not nt.get("episodeTag") and ep.get("tag"):
            nt["episodeTag"] = ep["tag"]
        if not nt.get("promptTitle") and ep.get("title"):
            nt["promptTitle"] = ep["title"]
        if not nt.get("sequence") and ep.get("sequence"):
            nt["sequence"] = ep["sequence"]
        if nt != t:
            changed = True
        out.append(nt)
    return out, changed


def reconcile_task_status(tasks, episodes):
    """Make OpenPM cards mirror their episode's CURRENT verification — one source
    of truth, no split-brain badges.

    The episode's ``verify.status`` (and landed state) is authoritative; a card
    linked to it must not show a stale FAILED while the episode reads passed (or
    vice-versa). Landed episodes → Done/passed. Otherwise the card's
    ``verificationStatus`` mirrors the episode's verify result, and a card still
    in To Do / Doing when a result exists is promoted to Testing. Cards the user
    already pushed to Done, and non-episode cards, are left alone.

    Args:
        tasks: list[dict] — persisted OpenPM tasks.
        episodes: list[dict] — episodes (source of truth).

    Returns:
        bool — whether any task changed (caller persists).
    """
    if not isinstance(tasks, list):
        return False
    by_id = {e["episodeId"]: e for e in (episodes or [])
             if isinstance(e, dict) and e.get("episodeId")}
    changed = False
    for t in tasks:
        if not isinstance(t, dict) or not t.get("episodeId"):
            continue
        ep = by_id.get(t["episodeId"])
        if not ep:
            continue
        if ep.get("status") == "landed":
            if t.get("column") != "done" or t.get("verificationStatus") != "passed":
                t["column"] = "done"
                t["verificationStatus"] = "passed"
                changed = True
            continue
        if t.get("column") == "done":
            continue                      # the user shipped it — leave it
        vs = (ep.get("verify") or {}).get("status")
        if vs not in ("passed", "failed"):
            continue
        want = "passed" if vs == "passed" else "failed"
        if t.get("verificationStatus") != want:
            t["verificationStatus"] = want
            if t.get("column") in ("todo", "doing"):
                t["column"] = "testing"   # a result exists → at least Testing
            changed = True
    return changed


def repair_task_commit_shas(tasks, episodes):
    """Heal OpenPM cards whose stored ``commitSha`` is no longer claimed by their owning episode.

    Episode ``commitShas`` is the source of truth: a reconcile can move a commit to its correct
    episode (or drop it), leaving a card pointing at a stale sha (e.g. a card still showing a commit
    its episode no longer lists). When a card references an episode (by ``episodeId``) that the store
    knows but that does NOT list the card's commit, the card adopts the episode's current commit
    (only when the episode has exactly one — unambiguous) or drops the stale sha entirely. A card
    whose episode is absent from the store is left untouched (a load-order blip must never destroy
    data); non-commit cards are untouched. Idempotent — a healed card matches its episode, so a
    re-run is a no-op. General — keyed on episode truth, never on a specific prompt/commit.

    Args:
        tasks: list[dict] — persisted OpenPM tasks.
        episodes: list[dict] — episodes (source of truth).

    Returns:
        (list[dict], bool) — (possibly-repaired tasks, changed?).
    """
    if not isinstance(tasks, list):
        return tasks, False
    shas_by_ep = {e["episodeId"]: list(e.get("commitShas") or [])
                  for e in (episodes or []) if isinstance(e, dict) and e.get("episodeId")}
    changed = False
    out = []
    for t in tasks:
        if not isinstance(t, dict):
            out.append(t)
            continue
        # Intent-graph step cards OWN their run's commit (set by sync_intent_tasks). The
        # episode-mismatch heuristic must never null their sha — that's a receipt the run authored,
        # and reconcile_intent_task_receipts heals it from episode truth instead.
        if t.get("source") == "intent-graph":
            out.append(t)
            continue
        sha, eid = t.get("commitSha"), t.get("episodeId")
        if not sha or eid not in shas_by_ep or sha in shas_by_ep[eid]:
            out.append(t)                 # no commit, unknown episode, or already valid → leave it
            continue
        ep_shas = shas_by_ep[eid]
        new_sha = ep_shas[0] if len(ep_shas) == 1 else None
        out.append({**t, "commitSha": new_sha, "shortSha": (new_sha[:7] if new_sha else None)})
        changed = True
    return out, changed


def reconcile_intent_tasks(tasks, episodes):
    """Protect an intent-graph run's OpenPM receipts across UI hydration/persistence.

    For an intent-graph episode, the FIVE step cards (``source == "intent-graph"``) ARE the
    operational source of truth. This does two things, both keyed generically on the episode (never a
    specific demo), and both idempotent:

      1. **Heal receipts.** A frontend hydrate→PUT round-trip can drop a step card's ``files`` /
         ``commitSha``. Restore them from episode truth — per-step ``files`` from the episode's
         ``intentSource.steps`` (matched by the card's linked box id) and ``commitSha`` from the
         episode's single landed commit — so opening OpenPM can never erase what the run produced.
      2. **Drop the duplicate.** Remove any ``source == "openfde-episode"`` card for an episode that
         already has intent-graph step cards — the step cards cover that landed commit, so the extra
         episode/commit card is noise (the regression). Episodes with NO step cards keep their
         episode-card behaviour untouched.

    Args:
        tasks: list[dict] — persisted OpenPM tasks.
        episodes: list[dict] — episodes (source of truth).

    Returns:
        (list[dict], bool) — (possibly-repaired tasks, changed?).
    """
    if not isinstance(tasks, list):
        return tasks, False
    by_ep = {e["episodeId"]: e for e in (episodes or [])
             if isinstance(e, dict) and e.get("episodeId")}
    # Episodes that already have intent-graph step cards — their landed commit is covered by them.
    intent_eps = {t.get("episodeId") for t in tasks
                  if isinstance(t, dict) and t.get("source") == "intent-graph" and t.get("episodeId")}
    changed = False
    out = []
    for t in tasks:
        if not isinstance(t, dict):
            out.append(t)
            continue
        # (2) drop a redundant episode/commit card duplicating an intent-graph episode.
        if t.get("source") == "openfde-episode" and t.get("episodeId") in intent_eps:
            changed = True
            continue
        # (1) heal step-card receipts from episode truth.
        if t.get("source") == "intent-graph":
            ep = by_ep.get(t.get("episodeId"))
            if ep:
                t = dict(t)
                shas = ep.get("commitShas") or []
                if not t.get("commitSha") and len(shas) == 1:
                    t["commitSha"] = shas[0]
                    t["shortSha"] = shas[0][:7]
                    changed = True
                if not (t.get("files") or []):
                    box = (t.get("linkedBoxIds") or [None])[0]
                    steps = (ep.get("intentSource") or {}).get("steps") or []
                    files = next((s.get("files") for s in steps
                                  if s.get("boxId") == box and s.get("files")), None)
                    if files:
                        t["files"] = list(files)
                        changed = True
        out.append(t)
    return out, changed


# Receipt fields an intent run (or a land) authored onto an OpenPM card — never let a client copy
# that is missing them overwrite a server copy that has them.
_RECEIPT_FIELDS = ("files", "commitSha", "shortSha", "episodeId", "linkedBoxIds",
                   "intentKey", "source", "verificationStatus", "column")


def _empty_receipt(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _task_identity(t):
    """A task's STABLE identity for matching it across a hydrate/persist round-trip: its
    ``intentKey`` (intent-graph step), else its ``commitSha`` (episode/commit card), else its
    ``id``. Generic — independent of any domain/title/path."""
    if not isinstance(t, dict):
        return None
    if t.get("intentKey"):
        return ("intent", t["intentKey"])
    if t.get("commitSha"):
        return ("commit", t["commitSha"])
    if t.get("id"):
        return ("id", t["id"])
    return None


def merge_tasks_preserving_receipts(incoming, existing):
    """Merge a client's PUT task list onto the server's BY STABLE IDENTITY, never letting a client
    copy with MISSING receipt fields clobber receipts the server already holds.

    For each incoming task matched to a server task (by ``intentKey``, else ``commitSha``, else
    ``id``), any receipt field (:data:`_RECEIPT_FIELDS`) that is absent/empty on the incoming copy is
    filled from the server copy. Tasks the client added (no server match) are kept as-is; tasks the
    client dropped are honoured as deletions; a non-empty field the client changed (a real edit —
    e.g. dragging a card to another column) is preserved, since the guard only fills EMPTY fields.

    Generic and idempotent: keyed on identity + field-emptiness, with no domain specifics — works
    for any intent graph (support inbox, insurance, hotel booking, data pipeline, …).

    Args:
        incoming: list[dict] — the client's PUT body.
        existing: list[dict] — the server's current task list.

    Returns:
        list — the merged list (same shape/order as ``incoming``).
    """
    if not isinstance(incoming, list):
        return incoming
    by_ident = {}
    for t in (existing or []):
        k = _task_identity(t)
        if k is not None:
            by_ident[k] = t
    out = []
    for t in incoming:
        srv = by_ident.get(_task_identity(t)) if isinstance(t, dict) else None
        if srv:
            t = dict(t)
            for f in _RECEIPT_FIELDS:
                if _empty_receipt(t.get(f)) and not _empty_receipt(srv.get(f)):
                    t[f] = srv.get(f)
        out.append(t)
    return out


def sync_intent_tasks(tasks, *, episode_id, run_id, tag, steps,
                      committed=False, awaiting_review=False, failed=False, commit_sha=None):
    """Server-durable OpenPM cards for an intent-graph run — the SOURCE OF TRUTH (tasks.json),
    which the frontend reducer mirrors. One card per intent step, idempotent by
    ``<episodeId|runId>:<boxId>``, so re-running the same episode UPDATES the cards in place and
    never duplicates. Column follows the lifecycle: committed → done (passed); awaiting review or
    FAILED → testing (failed verification); otherwise doing. Order is the steps' graph order.

    Args:
        tasks: list[dict] — current task list.
        episode_id: str — the run's episode id (the stable idempotency key).
        run_id: str — run id (fallback key when no episode yet).
        tag: str — sketch label shown on the cards (episode tag).
        steps: list[dict] — selected intent steps ({boxId, title, files?}), in graph order.
        committed / awaiting_review / failed: bool — the run outcome.
        commit_sha: str | None — the landed commit.

    Returns:
        (list[dict], bool) — (possibly-updated tasks, changed?).
    """
    if not isinstance(tasks, list) or not steps:
        return tasks, False
    key = episode_id or run_id or ""
    column = "done" if committed else ("testing" if (awaiting_review or failed) else "doing")
    vstatus = "passed" if committed else ("failed" if failed else "pending")
    by_ident = {t.get("intentKey"): i for i, t in enumerate(tasks)
                if isinstance(t, dict) and t.get("intentKey")}
    out = list(tasks)
    changed = False
    for s in steps:
        box_id = s.get("boxId")
        if not box_id:
            continue
        ident = f"{key}:{box_id}"
        fields = {
            "title": s.get("title") or "intent step", "files": list(s.get("files") or []),
            "linkedBoxIds": [box_id], "column": column, "verificationStatus": vstatus,
            "episodeId": episode_id or None, "commitSha": commit_sha,
            "episodeTag": tag, "promptTitle": tag, "promptLabel": tag,
            "source": "intent-graph", "intentKey": ident,
        }
        if ident in by_ident:
            idx = by_ident[ident]
            merged = {**out[idx], **fields}
            if merged != out[idx]:
                out[idx] = merged
                changed = True
        else:
            out.append({"id": "task_" + secrets.token_hex(5), "description": "", **fields})
            changed = True
    return out, changed


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


def is_intent_graph_episode(episode: dict) -> bool:
    """True when an episode was born from a Sketch-First intent graph (a user-drawn sketch the
    Council ran). Such an episode is a PRODUCT build by construction — never operational."""
    return (isinstance(episode, dict)
            and (episode.get("intentSource") or {}).get("kind") == "intent-graph")


def intent_title_summary(episode: dict):
    """``(title, summary)`` for an intent-run episode, derived from the STRUCTURED intent steps
    rather than the flattened ``Intent: …`` prompt.

    Why not reuse ``derive_title_summary``: a sketch step like *"read the data"* begins with a
    scaffolding word (``read``) that the shell/boilerplate heuristics strip, so the flattened
    prompt collapses to nothing and the episode is mislabelled operational + "Update <scope>".
    The steps are clean structured data, so we title from them directly and keep the run product.

    Returns ``None`` when the episode did not come from an intent graph.
    """
    if not is_intent_graph_episode(episode):
        return None
    src = episode.get("intentSource") or {}
    steps = [s.strip() for s in (_strip_meta(st.get("title") or "")
                                 for st in (src.get("steps") or []) if st.get("title")) if s.strip()]
    if steps:
        flow = " → ".join(steps)
        title = _cap_title(flow, 58)
        title = (title[:1].upper() + title[1:]) if title else title
        summary = "Built from a sketch: " + ", ".join(steps) + "."
        return title, summary
    ref = (src.get("ref") or "").strip()
    if ref:
        return _cap_title(ref), f"Built from a sketch: {ref}."
    return "Sketch-First intent run", "Built from a sketch on the canvas."


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
    # Sketch-First intent runs title from their steps and are product by construction (see
    # intent_title_summary); everything else uses the deterministic prompt/file derivation.
    intent = intent_title_summary(ep)
    if not ep.get("title") or not ep.get("summary"):
        title, summary = intent or derive_title_summary(ep.get("prompt") or "", ep.get("files"))
        if not ep.get("title"):
            ep["title"] = title
        if not ep.get("summary"):
            ep["summary"] = summary
    if not ep.get("signal"):
        # Product/build prompt vs operational chatter (shell/status/file-list) — the
        # Story keeps operational episodes out of the active concepts. An intent run is product.
        ep["signal"] = "product" if intent else (
            "operational" if is_operational(ep.get("prompt") or "") else "product")
    return max_seq
