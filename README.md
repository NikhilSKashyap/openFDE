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
- Records the story in Timeline and Ledger; commits successful changes locally.
- Respects scope: **dotted = agent-editable**, **solid = locked** (protected files
  force an approval gate before anything touches them).

## Running the Council

Two settings, both from the command palette (`⌘K`):

1. **Pick the backend** — `⌘K` → **Agent Council (Architect → Sr Dev → Verifier)**.
2. **Assign providers** — `⌘K` → **Open Agent Settings**, then set each role
   (Architect / Senior Dev / Verifier).

### Real models

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

OpenFDE is early and moving fast. As of **0.2.0**, the architecture-to-execution
loop runs end-to-end with real models: select scope, describe intent, and the
council implements, verifies against the diff, and commits — within the
boundaries you draw.
