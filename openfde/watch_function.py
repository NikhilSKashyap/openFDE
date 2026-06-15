"""
openfde/watch_function.py — infer *which function* an external edit touched.

The "Watch Any Agent" loop (``fs_watch``) glows the canvas whenever any editor writes a
repo file. By default the glow lands on the file box; when we can pin the edit to a single
function we glow that instead — a far more specific "here's what's happening right now".

Both helpers are pure and deterministic so they can be unit-tested without git or a repo:

  - ``changed_line_numbers(diff_text)`` — the new-file line numbers added/modified in a
    unified ``git diff`` (post-image side).
  - ``infer_changed_function(changed_lines, fns)`` — the enclosing function for those lines,
    using only each function's *start* line (the ArchGraph gives no end line). This mirrors
    ``architect._js_flows.owner_at``: the function with the greatest start line <= a changed
    line owns it, implicitly bounded by the next function's start.

The server wires these into a ``resolve_function(rel)`` closure (git diff + the cached
ArchGraph) and hands it to ``fs_watch.watch_loop``; the frontend turns the returned name into
``box:function:<path>:<name>`` and pulses it. No file or repo names are hardcoded here.
"""

import re

# Matches a unified-diff hunk header and captures the new-file start line:
#   @@ -<old>[,<n>] +<new>[,<n>] @@[ optional section heading]
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def changed_line_numbers(diff_text: str) -> list:
    """New-file line numbers of every added/modified line in a unified diff.

    Walks each hunk body tracking the post-image (new file) line counter: context lines
    (' ') advance it, added lines ('+') are recorded and advance it, removed lines ('-')
    do not (they don't exist in the new file). The file headers ('--- '/'+++ ') sit before
    the first ``@@`` so the counter is still unset there and they're ignored. Returns a
    sorted, de-duplicated list. Empty for an empty/unparseable diff (e.g. a new untracked
    file with no diff) — the caller then falls back to the file-level glow.
    """
    out = set()
    new_ln = None
    for raw in (diff_text or "").splitlines():
        m = _HUNK.match(raw)
        if m:
            new_ln = int(m.group(1))
            continue
        if new_ln is None:
            continue                       # pre-hunk header lines (diff/index/---/+++)
        if raw.startswith("+"):
            out.add(new_ln)
            new_ln += 1
        elif raw.startswith("-"):
            continue                       # removed — absent from the new file
        elif raw.startswith("\\"):
            continue                       # "\ No newline at end of file"
        else:
            new_ln += 1                    # context line
    return sorted(out)


def infer_changed_function(changed_lines, fns) -> str:
    """Name of the function that encloses the most changed lines, or ``None``.

    Args:
        changed_lines: iterable of 1-based new-file line numbers (from ``changed_line_numbers``).
        fns: this file's functions, each a dict with at least ``name`` and ``line`` (start).
            Extra keys are ignored, so an ArchGraph function dict can be passed directly.

    Enclosing rule (same as ``architect._js_flows.owner_at``): functions sorted by start line;
    a changed line belongs to the function with the greatest start line <= it. The ArchGraph
    has no end line, so a function's span is implicitly [its start, the next function's start);
    lines before the first function (module-level) belong to nothing. The winner is the function
    owning the most changed lines; ties break toward the earlier (smaller start line) function
    for determinism. Returns ``None`` when nothing is known or no line maps to a function.
    """
    if not changed_lines or not fns:
        return None
    ordered = sorted(
        (f for f in fns if isinstance(f.get("line"), int)),
        key=lambda f: f["line"],
    )
    if not ordered:
        return None
    counts = {}
    for line in changed_lines:
        owner = None
        for f in ordered:
            if f["line"] <= line:
                owner = f
            else:
                break
        if owner is not None:
            counts[owner["name"]] = counts.get(owner["name"], 0) + 1
    if not counts:
        return None
    start_by_name = {f["name"]: f["line"] for f in ordered}
    # Most lines wins; on a tie prefer the earlier function (smaller start line).
    return max(counts, key=lambda n: (counts[n], -start_by_name[n]))
