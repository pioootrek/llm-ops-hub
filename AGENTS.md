<!--
Operating guide for LLM agents and humans working on THIS repository (the
hub tool itself). The guide for projects whose backlog the hub monitors is
templates/AGENTS.md.
-->

# Backlog Ops Hub - Repository Guide

Status: active
Audience: LLM agents and humans changing the hub tool
Source of truth: operating rules and invariants for this codebase

## What this repository is

A generic, read-only backlog hub. Monitored projects keep their backlog as a
Git-backed JSON pseudo-database (one canonical JSON file per item, validated
against a JSON Schema, mutated only by commits to one configured ref). This
tool mirrors the project repo, validates the contract, and renders static
HTML for humans plus JSON for agents. User-facing documentation lives in
`README.md`; this file is the rulebook for changing the code.

## Single source of truth for instructions

- Every `AGENTS.md` is the only place its rules are written.
- Every `AGENTS.md` has a sibling `CLAUDE.md` whose entire content is one
  include line (`@AGENTS.md`) plus a do-not-duplicate note.
- When adding, moving, or deleting an `AGENTS.md`, make the matching
  `CLAUDE.md` change in the same commit.
- Never write rules into `CLAUDE.md`, `README.md`, or code comments that
  belong in an `AGENTS.md`; link instead of copying.

## Directory map

```
bin/hub.py       <- the whole tool: fmt/validate (agent-side, run inside the
                    monitored project checkout) and sync/build/serve
                    (hub-side, run on the worker against the bare mirror)
bin/sync_hub.sh  <- thin wrapper (sync + build) called by the systemd timer
schema/          <- bundled default JSON Schema for backlog items; a project
                    may override it with its own schema.json in backlog_dir
templates/       <- the pack dropped into a monitored project's backlog dir:
                    AGENTS.md (agent operating guide), CLAUDE.md (include),
                    example project config.json
systemd/         <- worker units: sync timer/service and LAN-only static HTTP
config.json      <- hub instance config for the current deployment (WinPath);
                    doubles as the reference example
```

Runtime state (`mirror.git/`, `cache/`, `public*`, `.venv/`) is gitignored
and must stay out of commits.

## Invariants - do not break these

1. **Read-only hub.** The hub never writes to a monitored project repo and
   has no mutation API. Backlog edits happen as commits in the project;
   "management" features must be designed as PR/commit generation in the
   project repo, never as hub-side state.
2. **Git is the database.** No hub-local record stores. Everything rendered
   comes from the mirror at the configured ref (plus GitHub PR metadata).
3. **One canonical serialization.** `canonical_json()` (UTF-8, 2-space
   indent, sorted keys, trailing newline) is the contract; `fmt` produces
   it, `validate` compares byte-for-byte. Never add a second formatting
   path.
4. **Contract = JSON Schema.** Structural rules belong in
   `schema/backlog-item.schema.json` (or the project's override), not in
   ad-hoc Python checks. Python-side checks are only for cross-file rules a
   schema cannot express (filename==id, type/prefix match, duplicates, area
   enum from project config, index freshness).
5. **Fail closed on build.** An invalid backlog must never render; `build`
   exits non-zero and leaves the previous release live.
6. **Single ref, no overlays.** The hub reads one configured `backlog_ref`.
   Do not reintroduce branch merging/overlay machinery without an explicit
   design decision.
7. **Ids are `TYPE-YYYYMMDD-slug`.** No counters, no allocation ledger;
   collisions are detected by `validate` and fixed by renaming a slug.
8. **Dependencies stay minimal.** Standard library plus `jsonschema` only.
   Python 3.9+ compatibility (no 3.10+-only syntax outside annotations).

## Development workflow

- Run `python3 bin/hub.py self-test` before every commit; extend the
  self-test when changing parser, validator, canonical form, or config
  resolution.
- For end-to-end checks, build a throwaway fixture project repo and point a
  scratch hub config at it (`repo_url` may be a local path); never test
  against a real monitored repo's remote.
- Contract changes (schema fields, id format, canonical form) are breaking
  for every monitored project: bump/consider `schema_version`, update
  `templates/AGENTS.md` and `README.md` in the same commit, and say so in
  the commit message.
- Keep `bin/hub.py` a single file until a real second consumer forces a
  split; keep diffs small and reviewable.
- Config knobs are documented in `README.md`; when adding one, update the
  README table, the example `config.json`, and `_resolve_hub_config()`
  defaults together.

## Security posture

- Generated sites can contain security-sensitive backlog items: deployments
  stay LAN-only; do not add features that publish output externally by
  default.
- The worker's `gh` token needs read scope only. The tool must never require
  write scopes.
- Treat monitored-repo content as untrusted input: everything rendered goes
  through HTML escaping (`h()`); keep it that way for any new render path.
