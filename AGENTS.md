# AGENTS.md

This is an OpenCode **multi-agent orchestration template**, not an application. There is no build, test, lint, or run command; don't go looking for one.

## What this is

- `opencode.json` defines `orchestrator` (the `default_agent`) plus four subagents: `architect`, `implementer`, `reviewer`, `scout`. Their prompts are loaded from `prompts/*.md` via `{file:./prompts/...}`.
- Standard workflow: `architect` plans and writes `IMPLEMENTATION_PLAN.md` -> `implementer` reads it and implements -> `reviewer` reviews (findings ranked CRITICAL/HIGH/MEDIUM/LOW).

## Working here

- Sessions run as `orchestrator`: delegate to subagents and coordinate; do not edit application files yourself.
- Permission constraints that matter:
  - `architect`: may only edit `IMPLEMENTATION_PLAN.md` and `PROJECT_PLAN.md`; bash denied.
  - `implementer`: full edit + bash (the only agent that writes code).
  - `reviewer` / `scout`: cannot edit files.
- `IMPLEMENTATION_PLAN.md` is the durable handoff between agents; treat it as authoritative until superseded by a new plan written to the same file.
- Move small local changes straight to `implementer`; only use the architect path for substantial work.

## Secrets / gotchas

- `.secrets/openrouter-key` holds the OpenRouter API key (loaded by `opencode.json`). Never read, print, log, edit, or commit it.
- The template checkout is not a git repo, so `git diff` (expected by `prompts/reviewer.md`) will not work until a project is initialized.
- `opencode.json` is OpenCode's own config: edit it only when changing agents, models, prompts, or permissions. Agent behavior changes belong in `prompts/*.md`.