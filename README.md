# OpenFDE

**FDE — Forward-Deployed Engineer.** A great FDE embeds with you, works inside
your boundaries, tests what they ship, files the PR with a writeup, and leaves a
record of what was tried, dropped, and delivered. OpenFDE is the open layer that
makes **any coding agent behave like one** — Claude Code, Codex, a future fleet:
point it at a repo and every agent's work becomes scoped, verified, shippable,
and remembered.

It is **not an IDE** — there's no editor inside, and it doesn't replace yours.
It sits *above* editors and agents as the **system of record for agent-built
software**: see the code as a living canvas, select the part you want changed,
describe it in English, and run an Agent Council that plans, implements, reviews
the actual diff, commits, and records the work — inside the permission
boundaries you set.

It also **remembers**. Prompts run through the council, OpenFDE wrappers, or
passive Claude Code/Codex capture become *episodes*, committed with their own
attributed scope and woven into a replayable **Story** of how the codebase came to be.
Codex can handle the thinking roles, Claude Code can handle the coding role, and
OpenFDE keeps the work visible, scoped, reviewed, and narrated.

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

- Turns modules, files, functions, and dataflow into an interactive canvas.
- Lets you select architecture and describe intent in one progressive Work panel.
- Runs an **Architect → Senior Dev → Verifier** council:
  - **Architect** writes a scoped implementation brief.
  - **Senior Dev** edits only the files you marked agent-editable.
  - **Verifier** reads the *actual diff* and gates the commit — if it fails, the
    Senior Dev is reprompted once; if it still fails, nothing lands.
- Shows the real change **inline in Review** (colored diff) the moment it commits.
- **Streams the agent live**: each node glows as the council reads and writes it
  (active → next → queued → done / failed), so you watch the work move through the
  canvas instead of staring at a spinner. Stop a run mid-flight at any time.
- **Watches any edit as it happens** — yours, the council's, or another agent's — by glowing
  the touched nodes, then surfaces **what changed** (files, affected concepts, diff)
  once the work settles.
- **Remembers the path**: council prompts, OpenFDE wrapper prompts, and passive
  Claude Code/Codex prompts become episodes, auto-committed with attributed scope and
  replayable as a **Story** and an **OpenPM** board (below).
- Respects scope: **dotted = agent-editable**, **solid = locked** (protected files
  force an approval gate before anything touches them). The toolbar draws both box
  styles plus arrows — the **Arrow** tool inherits the source box's style, the
  **Solid arrow** tool always draws solid.

## Running the Council

Two settings, both from the command palette (`⌘K`):

1. **Pick the backend** — `⌘K` → **Agent Council (Architect → Sr Dev → Verifier)**.
2. **Assign providers** — `⌘K` → **Open Agent Settings**, then set each role
   (Architect / Senior Dev / Verifier).

### Codex (local CLI) — keyless thinking roles

Set **Architect** and/or **Verifier** to provider **Codex (local CLI)**.
OpenFDE uses your existing Codex login for text-only reasoning: the Architect
writes the scoped implementation brief, and the Verifier reviews the actual
worktree diff before anything lands. No OpenFDE API key is needed, and each role
is labeled in Review so the proof is visible.

### Claude Code (local CLI) — keyless coding role

Set any role to provider **Claude Code (local CLI)**. OpenFDE drives your local
`claude` CLI headlessly — no copy-paste, no API key in OpenFDE. It is the
preferred **Senior Dev** backend for code edits: scope is enforced before and
after the run, pre-existing dirty files are never reverted, and read/write
progress streams onto the canvas as file glow. Auth comes from your existing
`claude` login, so on a Pro/Max plan the coding role runs against your
subscription with **zero keys** anywhere in the project.

### Real models (API)

Set each role to mode **API**, provider **Anthropic**, with a model id (e.g.
`claude-sonnet-4-5`) and key. The council calls the model in-process, edits
in-scope files, the Verifier reviews the real worktree diff, and the accepted
result commits through the gated path. Keys live only in
`.openfde/agent_settings.json` (gitignored) — they are never logged or committed.

### No-key demo (Echo)

Set **Senior Dev** to mode **API**, provider **Echo**. Echo makes a deterministic
in-scope edit so you can watch the whole loop run offline. Swap in a real provider
key for actual code generation.

## The Development Story

OpenFDE doesn't just run changes — it remembers how the codebase got here.

- **Prompts become episodes.** Council runs, `openfde cc` / `openfde codex`
  wrappers, and **passive Claude Code + Codex capture** create prompt episodes. When the
  work completes, OpenFDE commits the files attributable to that episode — clustered into
  one commit per logical change — and links the commits back to the prompt. A manual
  **Land** stays available as a fallback when the scope is ambiguous.
- **Passively captures Claude Code and Codex today.** OpenFDE tails both agents' session
  transcripts as you work — no wrapper — so the story keeps building on changes the council
  didn't run. Capture is **forward-only** (baselined at startup; it never replays history).
  Claude Code is cwd-agnostic (its tool calls carry clean file paths); **Codex attribution
  is dirty-set / quiet-window based** — strongest when Codex runs in the watched repo. Other
  agents are still watched via file changes, and the wrappers remain. Historical import of
  old Claude/Codex/Cursor logs is future work.
- **Story tab — a product memory with a decision lifecycle.** Concepts derived
  from your prompts land in five lanes: **Now** (the latest beat's build
  direction), **Next** (queued — including explicit "Next:" marks), **Watch**
  (interesting, not committed), **Deferred** (parked, with the revisit trigger
  when you wrote one — "until passive capture lands"), and **Abandoned**. Each
  concept links back to its prompts, commits, and files. Press **Tell** to replay
  the work as a narrative storyboard: mainline episodes move across a center
  spine, real exploratory episodes branch forward as smooth paths, and local
  decisions orbit their source episode as compact halo chips (`watch`,
  `deferred`, `✕ dropped`). Between mainline beats, an **evidence ladder** rides
  the spine: trust above (`tests ✓ · PR #2`) and mechanics below
  (`commit abc1234 · 3 files`). Clicking any episode, halo, receipt, PR, commit,
  or file opens an inline drawer without leaving Story. The Story is **immutable
  by design** — a derived, read-only record, so a team always sees the same
  history; an **Events** toggle exposes the raw event-log layer underneath.
- **OpenPM.** Landed prompts surface as Done cards on a Kanban board, tagged and
  grouped by their episode, so the board mirrors the same prompt → commit story.
- **GitHub Issues become durable intent.** Import an issue (the board's
  **⊕ Issue** input, or `POST /api/issues/github/import`) and it becomes a
  **To Do** card carrying the issue badge, state, and labels — intent *before*
  the episode. Re-import is idempotent (refreshes state, preserves your board),
  closed issues keep their card, and an episode started from an issue carries
  `intentSource` so commits trace back to the ticket. Issues never enter the
  Story until an episode actually lands work. v1 rides the local `gh` CLI — no
  OAuth, no webhooks.
- **Verify Gate — receipts before landing.** Before OpenFDE lands an episode it
  runs the repo's local checks (auto-discovered: `unittest` when `tests/`
  exists, `npm run lint` when the frontend defines it — or an explicit
  `.openfde/verify.json`) and stores the evidence on the episode: command,
  status, one-line summary, output tail, timing. **A failed required check
  blocks auto-land**; an explicit user Land stays the escape hatch with the
  failure recorded. No checks configured → explicit *skipped* evidence, never
  silent success. Evidence shows as per-check rows on the episode card and as
  `tests ✓ / lint ✓ / verify failed` badges on OpenPM cards.
- **Ready to ship — a deterministic PR verdict.** Every episode carries a
  readiness verdict computed from evidence and policy, never LLM judgment:
  **ready** (landed commits, episode files clean, checks passed, commit not
  already on the remote base, `gh` present — each shown as a ✓ receipt),
  **blocked** (each blocker named, with its next action — unrelated in-progress
  files don't block: a PR branches from the landed commit), or **created**
  (the PR link). The episode card shows the verdict as a shipping panel; the
  **Create Pull Request** button is enabled only when the gate says ready.
- **Land as PR — the PR description is the episode's story.** One click turns a
  landed episode into a branch (`openfde/p42-slug`) and a GitHub pull request
  whose body is the captured story: summary, linked issue, commits with their
  titles, changed files, and the Verify Gate receipts. Idempotent, guarded
  against no-diff PRs (commits already on the base are refused), local `gh`
  only. PR creation is **manual today**; a Claude-Code-style **Manual / Auto
  ship toggle** is the next step — in Auto, the same deterministic gate ships
  the PR the moment an episode becomes ready.

## Status

OpenFDE is early and moving fast. As of **0.4.5**, the local-first council can
run with **Codex local CLI for Architect/Verifier** and **Claude Code local CLI
for Senior Dev**, with coding activity **streamed live on the canvas** and a
**stop control** to halt any run. The architecture-to-execution loop runs
end-to-end: select scope, describe intent, and the council implements, verifies
against the real diff, and commits — within the boundaries you draw. Beyond
execution, OpenFDE keeps a **development memory**: prompts are captured as
episodes from the council, OpenFDE wrappers, or passive Claude Code/Codex
capture, auto-committed with attributed scope, and replayable as a visual
**Story**: episode movement on the spine, explorations as branches, decisions as
halos, and verification/commit/PR receipts in the drawer. The delivery chain now
closes the loop end-to-end — *issue → prompt
episode → verification receipts → clustered commits → ready-to-ship verdict →
pull request whose description is the story* — dogfooded on this repository
(its first PRs were created by OpenFDE, from episodes, through that exact
chain). Hardened by use: atomic single-writer persistence, a per-repo instance
lock, session-aware capture that doesn't split long agent turns, and
cross-process episode dedup.
