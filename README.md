# OpenFDE

OpenFDE is an architecture-first coding environment. Point it at a repo, see the
code as a living canvas, select the part you want to change, describe the change
in plain English, and run an Agent Council that plans, implements, reviews the
actual diff, commits, and records the work — inside the permission boundaries you
set.

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
- Records the story in Timeline and Ledger; commits successful changes locally.
- Respects scope: **dotted = agent-editable**, **solid = locked** (protected files
  force an approval gate before anything touches them).

## Running the Council

Two settings, both from the command palette (`⌘K`):

1. **Pick the backend** — `⌘K` → **Agent Council (Architect → Sr Dev → Verifier)**.
2. **Assign providers** — `⌘K` → **Open Agent Settings**, then set each role
   (Architect / Senior Dev / Verifier).

### Claude Code (local CLI) — keyless

Set any role to provider **Claude Code (local CLI)**. OpenFDE drives your local
`claude` CLI headlessly — no copy-paste, no API key in OpenFDE. The Senior Dev
edits in-scope files (scope is enforced before and after the run, and pre-existing
dirty files are never reverted); the Architect and Verifier run as text roles on
the same CLI. Auth comes from your existing `claude` login, so on a Pro/Max plan
the whole council runs against your subscription with **zero keys** anywhere in
the project. Each stage is labeled with its provider in Review so the proof is
visible.

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

## Status

OpenFDE is early and moving fast. As of **0.3.0**, the full council can run
**keyless on your local Claude Code CLI** (Architect, Senior Dev, and Verifier),
with the agent **streamed live on the canvas** as it works and a **stop control**
to halt any run. The architecture-to-execution loop runs end-to-end: select scope,
describe intent, and the council implements, verifies against the real diff, and
commits — within the boundaries you draw.
