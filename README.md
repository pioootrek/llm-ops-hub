# Backlog Ops Hub

A generic, read-only hub for **backlog-as-code**: the monitored project keeps
its backlog as a Git-backed JSON pseudo-database, and this tool renders it as
static HTML for humans and JSON for agents - from outside the project
checkout, through a bare mirror, with no worktrees and no write path.

Currently deployed for WinPath; intended to go open source once the contract
stabilizes.

## The model in one minute

- **Git is the database.** One backlog item = one canonical JSON file in the
  project repo (default `docs/backlog/<type>/<ID>.json`). Git history is the
  audit log; `git revert` is the undo.
- **One writable ref.** Backlog changes are commits to one configured branch
  (`backlog_ref`, default `main`). No branch merging, no overlay machinery,
  no hub-side state. Pull requests are an optional project policy, not a
  tool concept.
- **One canonical serialization.** UTF-8, 2-space indent, sorted keys,
  trailing newline. `fmt` produces it; `validate` compares byte-for-byte, so
  "looks right" and "is right" are the same thing.
- **Contract = JSON Schema.** Structure, enums, and the mandatory risk
  assessment live in a JSON Schema (2020-12). The tool bundles a default
  (`schema/backlog-item.schema.json`); a project can override it by placing
  its own `schema.json` in the backlog directory. Project CI and this tool
  consume the same file - no duplicated validators.
- **Coordination-free ids.** `FEAT-20260703-salesforce-sync` = type prefix +
  creation date + slug. No counters, no allocation ledger; in the unlikely
  collision `validate` reports the duplicate and the fix is renaming a slug.
- **LLM-first, human-friendly.** Agents read/write raw JSON and run two
  commands; humans read the rendered HTML.

## A backlog item

```json
{
  "area": "api",
  "created": "2026-07-03",
  "id": "SEC-20260703-rate-limit-exports",
  "links": {
    "prs": [],
    "related_ids": ["FEAT-20260703-csv-export"]
  },
  "notes": [
    { "date": "2026-07-03", "text": "Decision state: proposed." }
  ],
  "priority": "now",
  "problem": ["Export endpoints can be hammered without limits."],
  "risk": {
    "dimensions": ["availability"],
    "level": "low"
  },
  "schema_version": 1,
  "scope": ["Token-bucket limiter on export routes."],
  "source": "security review 2026-07-03",
  "status": "open",
  "title": "Rate-limit export endpoints",
  "type": "security",
  "validation": ["Limiter unit test: burst allowed, flood blocked."],
  "value": ["Bounded load from a single client."]
}
```

Types: `feature` (FEAT), `fix` (FIX), `rework` (RWK), `security` (SEC).
Status: `open`, `in-progress`, `blocked`, or `archived` (kept for reference,
not planned; hidden from the default backlog view). Prose is arrays of
paragraphs (diff-friendly). `risk` is mandatory; `rollback` is required for
`medium`/`high`. `notes` are append-only dated entries - the structured
replacement for comments - with an optional `author` (`human:<name>` /
`agent:<name>`); human-authored notes render highlighted as direction for
agents.

## Done entries

Completed work is the second record type: `done/DONE-YYYYMMDD-slug.json`,
validated against `schema/done-entry.schema.json` (project-overridable via
`done-schema.json`). Closing an item is one commit: delete the item file, add
a done entry carrying `item_id` (and optionally the full `item_snapshot`),
run `fmt` + `validate`. Because storage is one flat file per entry, there is
never a giant done log to archive - grouping (by month, by area) happens in
the rendered site, not on disk.

## Human feedback

The hub is read-only, but when `project.github_repo` is set, each rendered
item carries three feedback buttons - **Add guidance**, **Change priority**,
**Archive** - that open a prefilled GitHub issue (label `backlog-feedback`)
with a machine-readable body. The human authenticates with their own GitHub
login; the hub still needs no write token. Applying the issue is a normal
backlog commit in the project repo, made by the next agent session (the
workflow is in `templates/AGENTS.md`) or by project-side automation.

## Quickstart

### In the monitored project (agents and humans editing the backlog)

```bash
# after any edit under docs/backlog/:
python3 bin/hub.py fmt      --backlog-dir docs/backlog
python3 bin/hub.py validate --backlog-dir docs/backlog   # must exit 0 before commit
git commit ...
```

`fmt` canonicalizes files and regenerates `index.json` (the generated fast
scan surface - never hand-edited). The full agent workflow (when to write
what, lifecycle, query patterns) is `templates/AGENTS.md`, which you drop
into the project's backlog directory together with `templates/CLAUDE.md` and
a project `config.json` (areas enum).

### On the hub host (rendering and serving)

```bash
pip install -r requirements.txt      # jsonschema; Python 3.9+
python3 bin/hub.py self-test         # no config or network needed
python3 bin/hub.py sync              # clone/fetch the bare mirror
python3 bin/hub.py build             # render a static release, flip the symlink
python3 bin/hub.py serve             # optional stdlib static server
```

`build` validates everything first and **refuses to render an invalid
backlog** - the previous release stays live. Output: `index.html`
(dashboard), `backlog.html` (table + item cards), `done.html` (completed work
grouped by month), optional `prs.html`, and `data/index.json` for agents
(items + done + ref + commit + generated_at).

## Configuration

Hub instance config is JSON, resolved from `--config`, then the
`BACKLOG_HUB_CONFIG` env var, then `config.json` next to the tool, then
`~/winpath-hub/config.json`. This repo's
[config.example.json](config.example.json) is the reference example; copy it
to `config.json` for a local or deployment-specific instance.

| Key | Meaning | Default |
| --- | --- | --- |
| `project.repo_url` | remote of the monitored repo (mirror source) | required |
| `project.backlog_ref` | the single writable backlog branch | `main` |
| `project.backlog_dir` | backlog path inside the repo | `docs/backlog` |
| `project.github_repo` | `owner/repo` for the PRs page; omit to skip it (no `gh` needed) | unset |
| `project.name` | display name in the rendered site | `Backlog` |
| `paths.root` | runtime root | `~/winpath-hub` |
| `paths.mirror` / `cache` / `releases` / `public` | each path individually overridable; `{root}` placeholder supported | `{root}/...` |
| `server.host` / `server.port` | for `hub.py serve` | `127.0.0.1` / `8080` |
| `build.releases_keep` | how many release directories to keep after a successful build (`0` = keep all) | `20` |

## Repository layout

```
bin/hub.py       the whole tool (fmt / validate / sync / build / serve / self-test)
bin/sync_hub.sh  sync + build wrapper used by the systemd timer
schema/          bundled default JSON Schemas (backlog items, done entries)
templates/       pack for monitored projects: AGENTS.md, CLAUDE.md, config.json
systemd/         worker units: sync timer/service, LAN-only static HTTP service
config.example.json
                 reference hub instance config; local config.json is ignored
AGENTS.md        rulebook for changing this codebase (single source of truth)
```

Instruction files follow one rule everywhere: **`AGENTS.md` is the single
source of truth; every `CLAUDE.md` is only an `@AGENTS.md` include.** That
applies to this repo and to the template pack shipped to projects.

## Deployment notes

- The systemd timer runs `bin/sync_hub.sh` every 2 minutes; the HTTP service
  serves `public/` LAN-only. Generated sites can include security-sensitive
  backlog items - keep them LAN-only.
- `gh` (read scope) is needed only when `project.github_repo` is set.
- Releases are immutable directories under `public_releases/`; `public` is a
  symlink flipped atomically after a successful build. `build` skips rendering
  when nothing changed since the last successful build (same commit, PR data,
  tool, and config — use `build --force` to override) and prunes old release
  directories down to `build.releases_keep`.

## Status

- No open-source license chosen yet; rebranding happens at open-sourcing
  time.
- The migration of WinPath's legacy markdown backlog to this contract is
  tracked separately.
