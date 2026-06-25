<p align="center">
  <img src="frontend/public/banner.svg" alt="OpenFDE, the orange box for coding with agents" width="100%" />
</p>

# OpenFDE

**The orange box for coding with agents.**

[Watch the demo](https://x.com/nikhilskashyap/status/2064387404807680007?s=20)

Flight recorders are painted orange so they can be found later. OpenFDE brings
that idea to software work. It records how a codebase got built: prompts, file
changes, tasks, commits, checks, PRs, decisions, and the story they add up to
later.

**FDE means Forward-Deployed Engineer.** A good FDE embeds with a team, works
inside its boundaries, tests what they ship, files the PR with context, and
leaves behind a record of what was tried, dropped, and delivered. OpenFDE is the
open layer that helps any coding agent behave more like that.

It is not an editor, and it does not replace yours. Keep using Claude Code,
Codex, Cursor, VS Code, or a plain terminal. OpenFDE sits above that workflow. It
shows the repo as a living canvas, lets you scope what can change, can run an
Agent Council when you want it to, reviews the real diff, commits the work, and
keeps the record.

It also remembers. Prompts from the council, OpenFDE wrappers, and passive
Claude Code/Codex capture become episodes. Each episode gets attributed scope,
commits, receipts, and a place in the replayable **Story**. Codex can handle
thinking roles, Claude Code can handle coding, and OpenFDE keeps the work
visible, scoped, reviewed, and understandable.

## The cockpit and orange box theory

Agentic coding is a black box today. Prompts vanish into terminal scrollback,
agents commit without context, and the *why* disappears when the session closes.
The code remains; the reasoning does not. Six months later, or six minutes
later when several agents are building in parallel, nobody can say what was
tried, what was dropped, or why the surviving path won.

The theory is simple: coding agents are the engines, your imagination is the
wings, and you are still the pilot. OpenFDE is the cockpit and the orange box.
While the work is happening, it gives you the controls and instruments: scope,
live file glow, diffs, tasks, checks, and shipping gates. After the work lands,
it gives you the recorder: the prompt, the files, the commits, the receipts,
and the story.

When agents do more of the building, the missing artifact is no longer just the
code. It is the record of how the code came to be. Whoever has that record has
the best chance of understanding the system later. So the record needs a
recorder, built with the same instincts as a flight recorder:

1. **Always on.** It watches file activity from any editor or agent. For Claude
   Code and Codex it can also capture prompts passively; wrappers cover the
   explicit OpenFDE path. A record you have to remember to keep is a record you
   will not have.
2. **Evidence-grade.** Receipts, not vibes: every episode carries its prompt,
   files, commits, check results, and pull request. A failed test is recorded
   as honestly as a passing one.
3. **Immutable.** The story is derived testimony. Nobody can drag history into
   a nicer shape. A team reads one record, and the dropped paths stay visible
   as dropped paths.
4. **Readable in ten seconds.** A log is not enough. The telemetry renders as a
   story: beats on a spine, explorations branching out, decisions sitting next
   to the work that created them.

In the UI the theory is a color: **violet is intent flowing in; orange is the
record flowing out.** In the fleet era, where one architect might direct many
agents, the record is the only thing that scales trust. You build; the narrative
is free.

## Quick Start

```bash
# 1. Install the Python package (provides the `openfde` CLI)
pip install -e .

# 2. Build the frontend (the canvas UI is served from frontend/dist, which is
#    not checked in)
cd frontend && npm install && npm run build && cd ..

# 3. Point OpenFDE at any repo
python3 -m openfde watch /path/to/repo --port 7420
```

Then open:

```text
http://127.0.0.1:7420
```

## What It Does

- Turns modules, files, functions, and dataflow into an interactive canvas for
  **Python and JS/TS** out of the box. It also maps **HTML entrypoints to the JS
  modules they load**, so web and WebXR repos read like apps instead of loose
  files. All of this is regex/AST-based today, with no language servers to install.
- Lets you select architecture and describe intent in one progressive Work panel.
- Runs an **Architect → Senior Dev → Verifier** council:
  - **Architect** writes a scoped implementation brief.
  - **Senior Dev** edits only the files you marked agent-editable.
  - **Verifier** reads the *actual diff* and gates the commit. If it fails, the
    Senior Dev is reprompted once; if it still fails, nothing lands.
- Shows the real change **inline in Review** (colored diff) the moment it commits.
- **Streams the agent live**: each node glows as the council reads and writes it
  (active, next, queued, done, failed), so you watch work move through the canvas
  instead of staring at a spinner. Stop a run mid-flight at any time.
- **Watches any edit as it happens**: yours, the council's, or another agent's.
  It glows the touched nodes, then surfaces **what changed** once the work
  settles: files, affected concepts, and diff.
- **Remembers the path**: council prompts, OpenFDE wrapper prompts, and passive
  Claude Code/Codex prompts become episodes, auto-committed with attributed scope and
  replayable as a **Story** and an **OpenPM** board (below).
- Respects scope: **dotted = agent-editable**, **solid = locked** (protected files
  force an approval gate before anything touches them). The toolbar draws both box
  styles plus arrows. The **Arrow** tool inherits the source box's style; the
  **Solid arrow** tool always draws solid.
- **Raise an OpenFDE issue** from the top bar. Describe a bug, rough edge, or
  idea, and the Architect drafts it from your words plus light app context. Repo
  paths, tests, and code are scrubbed. You review the draft before anything posts.
- **A capability registry — and a real plugin runtime** (`⌘K → Plugins`). Built-in
  language packs (Python, JS/TS), **suggested** domain packs, repo-local manifests
  (`.openfde/plugins/{id}.json` — a JSON file; **no code is downloaded or run**), and
  packs shipped **outside core** as pip-installed Python packages (entry points) all
  appear through one contract. Capabilities — `architecture`, `test_detector`,
  `failure_parser`, `domain_summary` — are real runtime **hooks**: a pack's runtime loads
  **lazily** (only when a repo matches and a capability is asked), and wired product
  paths consume those hooks with a safe fallback. So JS/TS assimilation, test discovery, and
  failure parsing run through the pack's trusted runtime — with optional **tree-sitter**
  for precise JS/TS parsing, else the built-in regex. The **WebXR pack** marks WebXR files
  on the canvas + Explorer with honest badges — **XR API**, **Three**, **R3F**, **Scene**,
  **Shader**, **3D asset** — and groups scene assets (models / shaders / textures) so they
  read as a few nodes, not a hairball (architecture hints only — no runtime or test lens).
  Safety is fixed: a repo-local manifest can **never** import code, and external packs are
  trusted only once pip-installed. No marketplace yet — OpenFDE never downloads or installs packs.

## Fix a real issue, end to end

OpenFDE 0.6 can run the forward-deployed loop on repos it watches. Import a
GitHub issue, reproduce it, fix it, and take it to a reviewable PR with proof at
every step:

1. **Import the issue** (OpenPM → paste an issue number). It becomes a To-Do card with
   the issue as its durable intent.
2. **Reproduce.** OpenFDE triages the issue, locates the code it names, **drafts a
   failing test that proves the bug, and runs it**. It keeps the test only if it
   actually reproduces. If the issue is stale or already fixed, it reverts the test
   and says so.
   On a repo with no test setup it pins a pytest check (`.openfde/verify.json`) and
   creates the test, so a test-less repo is no longer a dead end.
3. **Show the failure flow.** The canvas dims to a single causal path: *test -> the
   function that failed*. The failing function gets a red ring. Left-click a node to focus
   its arrows; **right-click -> open it in an editor** (the failing function and the test
   side by side, the relevant line highlighted).
4. **Fix it in place.** Edit in the hatch and Save, or run the Senior Dev. The change
   renders **inline in the editor for the file it touched**. Review it, then re-run
   checks.
5. **Verify, land, PR.** Checks go green, the OpenPM card flips to passed, and the
   episode becomes landable. Land commits **only the fix and its test**. OpenFDE
   keeps its own metadata out of your tree through `.git/info/exclude`, never your
   `.gitignore`. Readiness is honest: no green check, no "ready for PR."

Proven live on foreign Python repos. **Repro-and-fix is Python/pytest today.** The
architecture canvas, Watch glow, Review, and **failure lens** also work on **JS/TS**
repos: regex-based assimilation, Vitest/Jest/Playwright failure parsing, and
HTML/web-app entrypoint mapping. That means web and WebXR repos can show the path
from an HTML page to the JS module it loads. Tree-sitter precision, JS/TS repro
drafting, Go/Rust, and big-repo scale are next. See `ROADMAP.md`.

## Running the Council

Two settings, both from the command palette (`⌘K`):

1. **Pick the backend**: `⌘K` → **Agent Council (Architect → Sr Dev → Verifier)**.
2. **Assign providers**: `⌘K` → **Open Agent Settings**, then set each role
   (Architect / Senior Dev / Verifier).

The **autonomous council relay** records durable handoff deliveries between
roles, exposes pending work through `openfde council status --role codex|claude`,
and treats native wakeup as best-effort only — durable delivery, not the wakeup,
is the source of truth. See `AGENTS.md` for the full protocol. _Verify:_ running
`openfde council status --role codex` (or `--role claude`) should print a
`▶ Resume council handoff ...` / inbox banner when work is pending, or
`No active council handoff.` on a clean checkout. If a provider call fails, the
run blocks before the next role and the error is surfaced as a visible receipt
rather than treated as that role's output.

A single autonomous council run is recorded as one parent episode. Inside that
episode, the role turns stay ordered as user → Architect → Senior Dev → Verifier.
OpenPM tracks the phase cards against that same parent episode, so the council
timeline and OpenPM work view describe one shared lifecycle.

### Codex (local CLI): keyless thinking roles

Set **Architect** and/or **Verifier** to provider **Codex (local CLI)**.
OpenFDE uses your existing Codex login for text-only reasoning: the Architect
writes the scoped implementation brief, and the Verifier reviews the actual
worktree diff before anything lands. No OpenFDE API key is needed, and each role
is labeled in Review so the proof is visible.

### Claude Code (local CLI): keyless coding role

Set any role to provider **Claude Code (local CLI)**. OpenFDE drives your local
`claude` CLI headlessly. There is no copy-paste and no API key in OpenFDE. It
is the preferred **Senior Dev** backend for code edits: scope is enforced before
and after the run, pre-existing dirty files are never reverted, and read/write
progress streams onto the canvas as file glow. Auth comes from your existing
`claude` login. On a Pro/Max plan the coding role runs against your subscription,
with no OpenFDE API key in the project.

### Real models (API)

Set each role to mode **API**, provider **Anthropic**, with a model id (e.g.
`claude-sonnet-4-5`) and key. The council calls the model in-process, edits
in-scope files, the Verifier reviews the real worktree diff, and the accepted
result commits through the gated path. Keys live only in
`.openfde/agent_settings.json` (gitignored). They are never logged or committed.

### No-key demo (Echo)

Set **Senior Dev** to mode **API**, provider **Echo**. Echo makes a deterministic
in-scope edit so you can watch the whole loop run offline. Swap in a real provider
key for actual code generation.

## The Development Story

OpenFDE doesn't just run changes. It remembers how the codebase got here.

- **Prompts become episodes.** Council runs, `openfde cc` / `openfde codex`
  wrappers, and **passive Claude Code + Codex capture** create prompt episodes. When the
  work completes, OpenFDE commits the files attributable to that episode, clustered into
  one commit per logical change, and links the commits back to the prompt. A manual
  **Land** stays available as a fallback when the scope is ambiguous.
- **Passively captures Claude Code and Codex today.** OpenFDE tails both agents' session
  transcripts as you work, with no wrapper, so the story keeps building on
  changes the council did not run. Capture is **forward-only** (baselined at
  startup; it never replays history). Claude Code is cwd-agnostic (its tool
  calls carry clean file paths); **Codex attribution is dirty-set /
  quiet-window based**, strongest when Codex runs in the watched repo. Other
  agents are still watched via file changes, and the wrappers remain. Historical import of
  old Claude/Codex/Cursor logs is future work.
- **Story tab: a product memory with a decision lifecycle.** Concepts derived
  from your prompts land in five lanes: **Now** (the latest beat's build
  direction), **Next** (queued, including explicit "Next:" marks), **Watch**
  (interesting, not committed), **Deferred** (parked, with the revisit trigger
  when you wrote one, such as "until passive capture lands"), and
  **Abandoned**. Each concept links back to its prompts, commits, and files.
  Press **Tell** to replay the work as a narrative storyboard: mainline episodes
  move across a center spine, real exploratory episodes branch forward as
  smooth paths, and local decisions orbit their source episode as compact halo
  chips (`watch`,
  `deferred`, `✕ dropped`). Between mainline beats, an **evidence ladder** rides
  the spine: trust above (`tests ✓ · PR #2`) and mechanics below
  (`commit abc1234 · 3 files`). Clicking any episode, halo, receipt, PR, commit,
  or file opens an inline drawer without leaving Story. The Story is **immutable
  by design**: a derived, read-only record, so a team always sees the same
  history; an **Events** toggle exposes the raw event-log layer underneath.
- **OpenPM.** Landed prompts surface as Done cards on a Kanban board, tagged and
  grouped by their episode, so the board mirrors the same prompt → commit story.
- **GitHub Issues become durable intent.** Import an issue (the board's
  **⊕ Issue** input, or `POST /api/issues/github/import`) and it becomes a
  **To Do** card carrying the issue badge, state, and labels. This is intent
  *before* the episode. Re-import is idempotent (refreshes state, preserves your
  board), closed issues keep their card, and an episode started from an issue
  carries `intentSource` so commits trace back to the ticket. Issues never enter
  the Story until an episode actually lands work. v1 rides the local `gh` CLI:
  no OAuth, no webhooks.
- **Verify Gate: receipts before landing.** Before OpenFDE lands an episode it
  runs the repo's local checks (auto-discovered: `unittest` when `tests/`
  exists, `npm run lint` when the frontend defines it, or an explicit
  `.openfde/verify.json`) and stores the evidence on the episode: command,
  status, one-line summary, output tail, timing. **A failed required check
  blocks auto-land**; an explicit user Land stays the escape hatch with the
  failure recorded. No checks configured → explicit *skipped* evidence, never
  silent success. Evidence shows as per-check rows on the episode card and as
  `tests ✓ / lint ✓ / verify failed` badges on OpenPM cards.
- **Ready to ship: a deterministic PR verdict.** Every episode carries a
  readiness verdict computed from evidence and policy, never LLM judgment:
  **ready** (landed commits, episode files clean, checks passed, commit not
  already on the remote base, `gh` present, each shown as a ✓ receipt),
  **blocked** (each blocker named, with its next action; unrelated in-progress
  files don't block: a PR branches from the landed commit), or **created**
  (the PR link). The episode card shows the verdict as a shipping panel; the
  **Create Pull Request** button is enabled only when the gate says ready.
- **Land as PR: the PR description is the episode's story.** One click turns a
  landed episode into a branch (`openfde/p42-slug`) and a GitHub pull request
  whose body is the captured story: summary, linked issue, commits with their
  titles, changed files, and the Verify Gate receipts. Idempotent, guarded
  against no-diff PRs (commits already on the base are refused), local `gh`
  only. PR creation is **manual today**; a Claude-Code-style **Manual / Auto
  ship toggle** is the next step. In Auto, the same deterministic gate ships
  the PR the moment an episode becomes ready.

## Status

OpenFDE is early and moving fast. As of **0.6.0**, the architecture canvas, Watch,
Review, and **failure lens** span **Python and JS/TS**. JS/TS support uses regex/AST
assimilation (optional **tree-sitter** for precise parsing), Vitest/Jest/Playwright
failure parsing, and HTML/web-app entrypoint mapping for web and WebXR repos. A
**capability registry with a real plugin runtime** (`⌘K → Plugins`) lets built-in,
repo-local, and pip-installed packs provide lazily-loaded capability hooks that core
uses in the wired product paths, with a safe fallback. The **WebXR pack** marks WebXR files with
honest badges (XR API / Three / R3F / Scene / Shader / 3D asset) and groups scene assets, and
an **additive focused path** (`POST /api/focus/neighborhood`) returns an issue/failure
neighborhood + scoped-verify selection for large repos — backend groundwork toward O(issue), not
yet full focused rendering. Repro-and-fix is still Python/pytest.

The local-first council can run with **Codex local CLI for Architect/Verifier**
and **Claude Code local CLI for Senior Dev**. Coding activity streams live on the
canvas, and a stop control can halt a run. The main loop works now: select scope,
describe intent, let the council implement, verify the real diff, then commit
inside the boundaries you drew.

OpenFDE also keeps a development memory. Prompts become episodes through the
council, OpenFDE wrappers, or passive Claude Code/Codex capture. Episodes are
auto-committed with attributed scope and replayable as a visual **Story**:
movement on the spine, explorations as branches, decisions as halos, and
verification/commit/PR receipts in the drawer.

The delivery chain is now tested on this repo: issue, prompt episode,
verification receipts, clustered commits, ready-to-ship verdict, and a PR whose
description is the story. The boring reliability work is also in place: atomic
single-writer persistence, a per-repo instance lock, session-aware capture that
does not split long agent turns, and cross-process episode dedup.
