"""
openfde/codex_local_runner.py — drive the local Codex CLI as a text role (Day 3B).

Slice 1 of local-app orchestration: OpenFDE shells out to the user's local
``codex`` CLI in non-interactive ``exec`` mode so Codex can play the **Architect**
(writes the implementation brief) and the **Verifier** (reviews the actual diff
and returns pass/fail/summary) — without the user copy-pasting between Codex and
Claude Code. The Senior Dev stays on the Claude Code backend for now.

Text-only by construction (the roles must never mutate the repo):
  - ``codex exec`` runs with ``-s read-only`` — the model may READ the repo for
    context, but the sandbox blocks every write/edit/shell-mutation;
  - the working root is pinned with ``-C <repo>`` and the session is ``--ephemeral``
    (no session files persisted);
  - as defense-in-depth we snapshot the repo's dirty set before/after and FAIL the
    run if anything changed — we never auto-revert (a read-only role should not have
    written anything; if it somehow did, we surface it rather than risk data loss).

No secrets are passed or logged: Codex authenticates via its own local login
(``CODEX_HOME``), inherited through the normal environment. We never print env values.

Two layers:
  - ``run_codex_local(...) -> dict`` — structured result {ok, text, error, returncode,
    stderr, touched}; surfaces clear provider/timeout/nonzero errors (testable).
  - ``run_codex_local_text(...) -> str`` — thin wrapper returning the text on success
    or "" on failure (logging the clear error), matching ``run_claude_code_text`` so
    the council's ``_text_role`` dispatch stays uniform and degrades gracefully to the
    deterministic role.
"""

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("openfde.codex_local_runner")

_DEFAULT_TIMEOUT = 180
_GIT_TIMEOUT = 30
# Codex.app ships the CLI here; it is usually NOT on PATH, so we fall back to it.
_CODEX_APP_BIN = "/Applications/Codex.app/Contents/Resources/codex"


def _norm(p: str) -> str:
    s = (p or "").strip().strip('"')
    return s[2:] if s.startswith("./") else s


def resolve_codex_bin(codex_bin: str = None) -> str:
    """Resolve the codex binary: explicit path → PATH → Codex.app bundle → None.

    Args:
        codex_bin: str | None — explicit binary path, or None to auto-resolve.

    Returns:
        str | None — a usable codex binary path, or None if none is found.
    """
    # An explicit binary is honored only if it exists — we never silently fall
    # back to a *different* binary than the caller named.
    if codex_bin:
        return codex_bin if Path(codex_bin).exists() else None
    found = shutil.which("codex")
    if found:
        return found
    if Path(_CODEX_APP_BIN).exists():
        return _CODEX_APP_BIN
    return None


def cli_available(codex_bin: str = None) -> bool:
    """Return whether the local Codex CLI can be resolved.

    Args:
        codex_bin: str | None — explicit binary path, or None to search.

    Returns:
        bool — True if a codex binary is resolvable.
    """
    return bool(resolve_codex_bin(codex_bin))


def _git(args: list, root: Path, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=str(root), shell=False,
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr=str(exc))


def _dirty_set(root: Path) -> set:
    """Repo-relative paths that differ from HEAD or are untracked (the dirty set).

    OpenFDE-owned metadata under ``.openfde/`` is excluded: the watcher rewrites it
    during a run, so it must never trip the runner's scope/conflict guards."""
    out = set()
    r1 = _git(["diff", "--name-only", "HEAD"], root)
    for ln in (r1.stdout or "").splitlines():
        if ln.strip():
            out.add(_norm(ln.strip()))
    r2 = _git(["ls-files", "--others", "--exclude-standard"], root)
    for ln in (r2.stdout or "").splitlines():
        if ln.strip():
            out.add(_norm(ln.strip()))
    return {p for p in out if not (p == ".openfde" or p.startswith(".openfde/"))}


def _build_cmd(codex_bin: str, repo_root, model, out_path: str) -> list:
    """Compose the non-interactive, read-only `codex exec` command.

    Prompt is fed on stdin (``exec -`` reads it), so giant prompts/diffs never hit
    argv length limits or shell escaping. ``-o`` captures only the final message.
    """
    cmd = [
        codex_bin, "exec", "-",          # read the prompt from stdin
        "-s", "read-only",                # sandbox: may read, never write/mutate
        "--skip-git-repo-check",          # don't refuse outside-of-git contexts
        "--color", "never",               # clean machine output
        "--ephemeral",                    # do not persist session files
        "-o", out_path,                   # write ONLY the last agent message here
    ]
    if repo_root:
        cmd += ["-C", str(repo_root)]
    if model:
        cmd += ["-m", str(model)]
    return cmd


def run_codex_local(*, system: str, user: str, model: str = None,
                    timeout: int = _DEFAULT_TIMEOUT, cwd=None, codex_bin: str = None) -> dict:
    """Run the local Codex CLI as a pure TEXT role and return a structured result.

    Codex receives the role system prompt + the user payload (intent/scope for the
    Architect, or brief + actual diff for the Verifier) as a single combined prompt,
    runs read-only against ``cwd`` (the repo root), and its final message is returned
    as ``text``. The repo is never mutated (read-only sandbox + pre/post dirty check).

    Args:
        system: str — the role's system prompt (Architect or Verifier instructions).
        user:   str — the user payload (context; for the Verifier, includes the diff).
        model:  str | None — optional model override (else Codex's configured default).
        timeout: int — wall-clock seconds before the run is aborted.
        cwd:    Path | str | None — repo root (working root + read context).
        codex_bin: str | None — explicit binary path (else auto-resolve).

    Returns:
        dict — {ok, text, error, returncode, stderr, touched}.
    """
    resolved = resolve_codex_bin(codex_bin)
    if not resolved:
        return _err("Codex CLI not found. Install Codex (or set the codex binary on "
                    "PATH) to use the Codex Local provider for Architect/Verifier.")

    root = Path(cwd) if cwd else None
    pre_dirty = _dirty_set(root) if root else set()

    prompt = f"{(system or '').strip()}\n\n---\n\n{(user or '').strip()}".strip()

    out_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
            out_path = tf.name
        cmd = _build_cmd(resolved, root, model, out_path)
        try:
            proc = subprocess.run(
                cmd, input=prompt, cwd=(str(root) if root else None), shell=False,
                capture_output=True, text=True, timeout=timeout, env=_child_env())
        except subprocess.TimeoutExpired:
            logger.error("Codex local text role timed out after %ss", timeout)
            return _err(f"Codex CLI timed out after {timeout}s.")
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("Codex local text role failed to launch: %s", exc)
            return _err(f"Codex CLI failed to launch: {exc}")

        stdout, stderr = (proc.stdout or ""), (proc.stderr or "")
        if proc.returncode != 0:
            detail = (stderr.strip() or stdout.strip() or "no output")[:300]
            logger.error("Codex local text role exited %s: %s", proc.returncode, detail)
            return _err(f"Codex CLI exited {proc.returncode}: {detail}",
                        returncode=proc.returncode, stderr=stderr[:1000])

        # Prefer the -o last-message file; fall back to stdout if it is empty.
        text = ""
        try:
            text = Path(out_path).read_text().strip()
        except OSError:
            text = ""
        if not text:
            text = stdout.strip()

        # Defense-in-depth: a read-only role must not have changed the tree.
        touched = sorted(_dirty_set(root) - pre_dirty) if root else []
        if touched:
            logger.error("Codex local text role unexpectedly changed files: %s", touched)
            return _err("Codex (text role) unexpectedly modified the working tree "
                        f"(left intact): {', '.join(touched)}.", touched=touched)

        if not text:
            return _err("Codex CLI returned no text output.", returncode=0)
        return {"ok": True, "text": text, "error": None,
                "returncode": 0, "stderr": stderr[:1000], "touched": []}
    finally:
        if out_path:
            try:
                os.unlink(out_path)
            except OSError:
                pass


def run_codex_local_text(*, system: str, user: str, model: str = None,
                         timeout: int = _DEFAULT_TIMEOUT, cwd=None, codex_bin: str = None) -> str:
    """Text wrapper over ``run_codex_local`` matching ``run_claude_code_text``.

    Returns the model's text on success, or "" on any failure (logging the clear
    error) so the council's ``_text_role`` dispatch degrades to the deterministic
    role instead of raising.

    Returns:
        str — Codex's text response, or "" on any failure.
    """
    res = run_codex_local(system=system, user=user, model=model,
                          timeout=timeout, cwd=cwd, codex_bin=codex_bin)
    if not res.get("ok"):
        logger.error("Codex local text role failed: %s", res.get("error"))
        return ""
    return res.get("text") or ""


# Prepended to the editing prompt: OpenFDE owns version control, Codex only edits.
_NO_COMMIT_DIRECTIVE = (
    "IMPORTANT — OpenFDE owns version control. EDIT files only. Do NOT run "
    "`git add`, `git commit`, `git push`, or stage/commit. Leave all changes in the "
    "working tree; OpenFDE reviews them and lands the commit.\n\n"
)


def run_codex_local_edit(*, repo_root, prompt: str, model: str = None,
                         timeout: int = _DEFAULT_TIMEOUT, codex_bin: str = None) -> dict:
    """Run the local Codex CLI **as an editor** (`openfde codex`) — repo-wide.

    Uses Codex's ``workspace-write`` sandbox (the local CLI supports it) so Codex
    edits files in the repo, with the no-git directive keeping it from committing.
    Touched files come from the git dirty-set diff (before vs after). Never commits;
    fails honestly (no fake edits) if Codex is missing or the run errors.

    Args:
        repo_root: Path | str — repository root (cwd + workspace-write boundary).
        prompt: str — the user's prompt (no-commit directive prepended).
        model: str | None — optional model override.
        timeout: int — wall-clock seconds.
        codex_bin: str | None — explicit binary (else auto-resolve).

    Returns:
        dict — {ok: bool, touched: [str], error: str|None, summary: str}.
    """
    resolved = resolve_codex_bin(codex_bin)
    if not resolved:
        return {"ok": False, "touched": [], "summary": "",
                "error": "Codex CLI not found. Install Codex or set the codex binary on PATH."}
    root = Path(repo_root)
    pre_dirty = _dirty_set(root)

    cmd = [resolved, "exec", "-", "-s", "workspace-write",
           "--skip-git-repo-check", "--color", "never", "--ephemeral", "-C", str(root)]
    if model:
        cmd += ["-m", str(model)]
    full_prompt = _NO_COMMIT_DIRECTIVE + (prompt or "")

    error, summary = None, ""
    try:
        proc = subprocess.run(cmd, input=full_prompt, cwd=str(root), shell=False,
                              capture_output=True, text=True, timeout=timeout, env=_child_env())
        summary = (proc.stdout or "").strip()[-2000:]
        if proc.returncode != 0:
            detail = ((proc.stderr or "").strip() or summary or "no output")[:300]
            error = f"Codex CLI exited {proc.returncode}: {detail}"
    except subprocess.TimeoutExpired:
        error = f"Codex CLI timed out after {timeout}s."
        logger.error("openfde codex: timed out (%ss)", timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        error = f"Codex CLI failed to launch: {exc}"
        logger.error("openfde codex: launch failed: %s", exc)

    touched = sorted(_dirty_set(root) - pre_dirty)
    # A clean non-zero exit with no edits is an honest failure (no faked success).
    return {"ok": error is None, "touched": touched, "summary": summary, "error": error}


def _child_env() -> dict:
    """Environment for the spawned `codex` process. Codex authenticates via its own
    local login (CODEX_HOME), inherited here; we never log env values."""
    return dict(os.environ)


def _err(msg: str, *, returncode: int = 1, stderr: str = "", touched=None) -> dict:
    return {"ok": False, "text": "", "error": msg, "returncode": returncode,
            "stderr": stderr, "touched": touched or []}
