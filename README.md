# OpenFDE

OpenFDE is an architecture-first coding environment. Point it at a repo, see the code as a living canvas, describe a change, and run an agent council that plans, implements, verifies, records, and commits the work.

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
- Lets you select architecture and describe intent in the Work panel.
- Runs an Architect -> Senior Dev -> Verifier loop.
- Records the story in Timeline and Ledger.
- Commits successful changes locally.
- Supports an Echo mode for no-key demos.

## Demo Mode (no API key)

Two settings, in two places:

1. **Pick the execution backend** — open the command palette (`⌘K`) and choose
   **Agent Council** (Architect → Sr Dev → Verifier).
2. **Set the Senior Dev provider** — open **Agent Settings** (`⌘K` → "Open Agent
   Settings", or the settings button on the Agent tab) and set **Senior Dev →
   mode `API` → provider `Echo`**.

Now scan a repo, select a module, type a change in the Work panel, and press
Execute — the full Architect → Senior Dev → Verifier loop runs and commits,
offline. (Echo makes a deterministic in-scope edit; swap in a real provider
key for actual code generation.)

## Status

OpenFDE is early and moving fast. The current focus is making codebases understandable, editable, and reviewable through one coherent architecture-to-execution loop.
