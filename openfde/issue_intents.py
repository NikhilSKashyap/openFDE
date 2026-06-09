"""
openfde/issue_intents.py — GitHub Issues as durable intent (v1).

A GitHub Issue is *intent before the episode*: it enters OpenPM as a To Do card
(``intentSource.provider == "github"``), waits for real work, and only becomes Story
memory when an episode/commit actually lands — imported issues never pollute the
prompt story by themselves. The chain this module starts:

    GitHub Issue → intent (this module) → OpenPM card → prompt episode
        (``episode.intentSource``) → commit evidence → Story beat

v1 is deliberately local and deterministic: issues arrive via the ``gh`` CLI (already
authenticated for local dev) or as raw issue JSON — no OAuth app, no webhooks, no
background sync. Import is explicit and idempotent: re-importing refreshes the issue
surface (title/state/labels) but preserves the card's column and verification, and a
closed issue keeps its card (state shows CLOSED; nothing is auto-deleted).

Pure helpers + two thin ``gh`` runners (injectable for tests; no network of our own).
"""

import json
import re
import secrets
import subprocess

# gh CLI JSON fields we ask for — kept to what the card needs (no comments/assignees).
GH_ISSUE_FIELDS = "number,title,url,state,labels,body"
_ISSUE_URL_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)", re.I)
_MAX_DESC = 160
_GH_TIMEOUT = 20


def intent_id(url: str, number) -> str:
    """Stable intent id: ``github:<owner>/<repo>#<n>`` from the issue URL, else
    ``github:#<n>`` (raw imports without a URL still dedupe within the repo)."""
    m = _ISSUE_URL_RE.search(url or "")
    if m:
        return f"github:{m.group(1)}/{m.group(2)}#{m.group(3)}"
    return f"github:#{int(number)}"


def normalize_issue(payload) -> dict:
    """Normalize a gh-CLI / GitHub-API issue object into the intent shape.

    Accepts ``labels`` as ``[{"name": ...}]`` (gh/API) or plain strings, ``url`` or
    ``html_url``, and a numeric-ish ``number``. State is uppercased (OPEN/CLOSED).

    Args:
        payload: dict — raw issue JSON.

    Returns:
        dict — {provider, issueNumber, url, title, state, labels, body}.

    Raises:
        ValueError — payload isn't an object, has no numeric number, or no title.
    """
    if not isinstance(payload, dict):
        raise ValueError("issue payload must be a JSON object")
    try:
        number = int(payload.get("number"))
    except (TypeError, ValueError):
        raise ValueError("issue payload needs a numeric 'number'")
    title = (payload.get("title") or "").strip()
    if not title:
        raise ValueError("issue payload needs a 'title'")
    labels = []
    for lb in payload.get("labels") or []:
        name = lb.get("name") if isinstance(lb, dict) else lb
        name = str(name).strip() if name else ""
        if name:
            labels.append(name)
    return {
        "provider": "github",
        "issueNumber": number,
        "url": (payload.get("url") or payload.get("html_url") or "").strip(),
        "title": title,
        "state": str(payload.get("state") or "OPEN").strip().upper(),
        "labels": labels,
        "body": (payload.get("body") or "").strip(),
    }


def intent_task_fields(intent: dict) -> dict:
    """The OpenPM card surface for an intent — planned work, not evidence.

    Card lands in **To Do** with verification pending (it hasn't been built); the
    description is the issue body's first line (capped) so the card stays scannable.
    ``intentSource`` carries the durable metadata the UI badges read; ``intentId`` is
    the dedupe key for repeated imports.
    """
    first = (intent.get("body") or "").strip().splitlines()
    desc = first[0].strip() if first else ""
    if len(desc) > _MAX_DESC:
        desc = desc[: _MAX_DESC - 1] + "…"
    return {
        "title": intent["title"],
        "description": desc,
        "column": "todo",
        "verificationStatus": "pending",
        "source": "github-issue",
        "linkedBoxIds": [],
        "intentId": intent_id(intent.get("url"), intent["issueNumber"]),
        "intentSource": {k: intent[k] for k in
                         ("provider", "issueNumber", "url", "title", "state", "labels")},
    }


def _task_intent_id(task: dict):
    """A task's intent identity: explicit ``intentId``, else derived from its
    ``intentSource`` (older/hand-made cards), else None."""
    if not isinstance(task, dict):
        return None
    if task.get("intentId"):
        return task["intentId"]
    src = task.get("intentSource") or {}
    if src.get("provider") == "github" and src.get("issueNumber") is not None:
        try:
            return intent_id(src.get("url"), src["issueNumber"])
        except (TypeError, ValueError):
            return None
    return None


def upsert_intent_task(tasks: list, intent: dict, *, make_id=None) -> tuple:
    """Create or refresh the OpenPM card for an intent. Idempotent by intent id.

    On re-import the issue surface is refreshed — card title, ``intentSource``
    (state/labels/url), and the description when the issue has one — while the
    card's **column, verification, links, and id are preserved** (a card the user
    moved to Doing stays in Doing; a closed issue keeps its card with state CLOSED).

    Args:
        tasks: list[dict] — current OpenPM task list (not mutated).
        intent: dict — normalized issue (see :func:`normalize_issue`).
        make_id: optional () -> str — task id factory (tests).

    Returns:
        (tasks, task, created) — new list, the card, and whether it was created.
    """
    tasks = list(tasks or [])
    fields = intent_task_fields(intent)
    iid = fields["intentId"]
    for i, t in enumerate(tasks):
        if _task_intent_id(t) != iid:
            continue
        updated = {**t, "title": fields["title"],
                   "description": fields["description"] or t.get("description") or "",
                   "intentId": iid, "intentSource": fields["intentSource"],
                   "source": t.get("source") or "github-issue"}
        tasks[i] = updated
        return tasks, updated, False
    task = {"id": make_id() if make_id else "task_" + secrets.token_hex(6), **fields}
    tasks.append(task)
    return tasks, task, True


# ── gh CLI (local dev path — no OAuth, no API client of our own) ─────────────

def _run_gh(args: list, cwd: str, runner=None) -> str:
    """Run ``gh <args>`` in the repo and return stdout; raise on failure.

    Raises:
        FileNotFoundError — gh CLI not installed.
        RuntimeError — gh exited non-zero (not authenticated, no such issue, …).
    """
    run = runner or subprocess.run
    proc = run(["gh"] + list(args), cwd=cwd, capture_output=True, text=True,
               timeout=_GH_TIMEOUT)
    if getattr(proc, "returncode", 1) != 0:
        raise RuntimeError(((proc.stderr or "") or "gh failed").strip()[:300])
    return proc.stdout or ""


def gh_issue_view(number: int, cwd: str, runner=None) -> dict:
    """``gh issue view <n> --json …`` → normalized intent."""
    out = _run_gh(["issue", "view", str(int(number)), "--json", GH_ISSUE_FIELDS], cwd, runner)
    return normalize_issue(json.loads(out))


def gh_issue_list(cwd: str, limit: int = 30, runner=None) -> list:
    """``gh issue list --json …`` (open issues) → normalized intents."""
    out = _run_gh(["issue", "list", "--json", GH_ISSUE_FIELDS, "--limit", str(int(limit))],
                  cwd, runner)
    return [normalize_issue(x) for x in json.loads(out)]
