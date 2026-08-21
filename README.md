# LLM Ops Hub

A read-only hub for projects that keep their backlog in Git as JSON files. It
reads the repository through a bare mirror and produces static HTML for humans
and JSON for agents. It does not need a worktree and cannot write to the
monitored repository.

Project identity, repository location, backlog ref, and runtime paths come
from the instance configuration.

One hub instance monitors one project. Run another instance with its own
configuration and runtime paths for each additional project. Multi-project
support would require a separate design. The current configuration has no
multi-project mode.

## How it works

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
  consume the same file, so there is no second validator to keep in sync.
- **IDs need no counter.** `FEAT-20260703-salesforce-sync` = type prefix +
  creation date + slug. There is no allocation ledger. If two IDs collide,
  `validate` reports the duplicate and one slug must be renamed.
- **Two interfaces.** Agents edit JSON and run two commands. Humans use the
  rendered HTML.

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
`medium`/`high`. `notes` are append-only dated entries that replace comments.
Each note may have an `author` (`human:<name>` /
`agent:<name>`); human-authored notes render highlighted as direction for
agents.

An open item may optionally carry a time-bounded risk decision:

```json
"risk_acceptance": {
  "approved_by": "human:security-owner",
  "approved_on": "2026-07-31",
  "expires_on": "2026-08-14",
  "rationale": "Mitigation is scheduled after the pilot.",
  "scope": ["Only the report-only preview scan finding."]
}
```

The hub derives `active`, `expiring`, or `expired` from `expires_on`, exposes
that state in the backlog view, and never treats the item as completed. The
project repository remains the only place where the decision can be changed.

## Done entries

Completed work is the second record type: `done/DONE-YYYYMMDD-slug.json`,
validated against `schema/done-entry.schema.json` (project-overridable via
`done-schema.json`). Closing an item is one commit: delete the item file, add
a done entry carrying `item_id` (and optionally the full `item_snapshot`),
run `fmt` + `validate`. Each completed item has its own file. The rendered
site groups those files by month and area, so there is no growing log file to
split or archive.

## Agent notes

Agent notes hold findings that should survive context compaction and remain
available to later sessions or other models. Typical examples are audit
results, bugs found while doing unrelated work, and hard-won implementation
details. Each note is a directory:

```
notes/NOTE-20260705-auth-audit/
  note.json      <- manifest: the only validated, canonicalized file
  findings.md    <- everything else is free-form: markdown, JSON, images...
  login-bug.png
```

The manifest defines the note metadata: id == directory name, title, created,
author, status `active|archived`, plus optional `last_reviewed`, tags, and an
inline body. Extra fields are allowed. The bundled schema is
`schema/note.schema.json` and a
project can override it with `note-schema.json`. Payload files use the agent's
own format and are never rewritten by `fmt`; allowed types are text (`md txt json csv log`) and
images (`png jpg jpeg gif webp`), max 5 MB each. `index.json` lists each note's
id, title, tags, status, and files. Agents can check the index before opening
individual notes. The hub also renders notes on `notes.html`, with inline
text and images plus full-text search. Humans can request archival or deletion
through the feedback flow.

`last_reviewed` is an optional `YYYY-MM-DD` date. Set it when an active note's
facts have been checked again, and update it whenever those facts change.

The build links notes back to backlog items when a note manifest or text
payload contains the exact ID of an existing item. Images are not scanned, and
ID-shaped text that does not match a current item stays plain text. The links
appear on item cards and in `data/index.json` under `related_notes`.

## Docs pages (optional)

The optional docs module covers playbooks, contracts, and reference pages.
Every top-level `*.md` file in the configured docs directory starts with a
YAML frontmatter block. The values are parsed as a flat string dictionary and
validated against `schema/docs-header.schema.json` (project-overridable via
`docs-header-schema.json` in the backlog dir). Instruction files
(`AGENTS.md`, `CLAUDE.md`, `README.md`) and subdirectories are exempt.

```yaml
---
audience: "engineering agents changing org sync"
last_reviewed: "2026-07-09"
source_of_truth: "Kinde organization to Neon synchronization contract"
status: "active"
---
```

The header has sorted keys and double-quoted values. `fmt` canonicalizes the
header without touching the Markdown body. `status` must be one of
`active | reference | superseded | archived`, `audience` required,
`last_reviewed` required for `active`/`reference`, `superseded_by` required
non-empty for `superseded`. Extra string keys are allowed. `validate` and
`build` fail closed on a missing or non-contract header, exactly as for
backlog records.

The module is enabled by the **project's** `config.json` (in the backlog
dir), so agent-side `fmt`/`validate` and the hub build cannot disagree
about whether docs are part of the contract:

| Project config key | Meaning | Default |
| --- | --- | --- |
| `docs_dir` | repo-root-relative docs directory; non-empty enables the module | unset (disabled) |
| `docs_index_file` | discovery index page checked for a link to every other page | unset (check skipped) |
| `docs_stale_days` | review-staleness threshold for the health report (`0` disables) | `60` |

Backlog and note health use two more project settings:

| Project config key | Meaning | Default |
| --- | --- | --- |
| `health_stale_days` | inactivity threshold for `in-progress`, `blocked`, and `priority: now` items (`0` disables age findings) | `45` |
| `notes_stale_days` | review threshold for active agent notes (`0` disables) | `90` |

The build writes `health.html`, adds a Health card to the dashboard, and puts
the same findings in the `health` block of `data/index.json`. Backlog findings
include items with no notes even when age checks are disabled. Health findings
do not block a release.

When enabled, the build renders a **Docs health** page (plus a dashboard
card and a `docs` block in `data/index.json` for agents). It reports status
counts, overdue reviews, pages missing from the index file, and dead relative
links. Stale reviews and dead links do not block a release. Broken headers do.

## Repository instruction map (optional)

The instruction map answers a narrow debugging question: which versioned
repository instruction sources apply to work in a selected directory? Set
`instructions_dir` in the project's backlog `config.json` to a repo-relative
subtree, or to `.` for the whole repository. Leaving it unset or empty keeps
the feature off.

The first profile is `codex-repository-v1`, checked against the
[Codex `AGENTS.md` discovery rules](https://developers.openai.com/codex/guides/agents-md)
on 2026-08-20. It walks from the repository root to the selected directory,
choosing `AGENTS.override.md` before `AGENTS.md` in each directory and showing
the resulting sources in discovery order. Configured fallback filenames are
not part of this fixed profile.

This is not a reconstruction of an agent's prompt. Global, user, enterprise,
skill, plugin, subagent, system-prompt, configured fallback, and external
sources remain outside the hub's view. The report does not expand imports or
guess which natural-language rule wins.

| Project config key | Meaning | Default |
| --- | --- | --- |
| `instructions_dir` | repo-root-relative subtree to map; `.` maps the repository and a non-empty value enables the page | unset (disabled) |
| `instructions_max_files` | maximum repository files used to build the directory map | `5000` |
| `instructions_max_file_bytes` | maximum bytes published from one instruction source | `65536` |
| `instructions_max_total_bytes` | maximum instruction bytes published in one report | `524288` |
| `instructions_lint_claude_include` | report deviations from this project's `CLAUDE.md` = `@AGENTS.md` convention | `false` |

The scanner reads the mirrored Git tree at the configured ref and never
follows symlinks. File-count and byte limits produce visible findings instead
of silently changing the report. Findings are advisory and do not block a
valid backlog release. `instructions.html` provides a searchable, keyboard-
navigable directory tree. The selected directory can be viewed as effective
repository instructions with visible source boundaries, an ordered provenance
list, or a source delta from its parent directory. Directory and view selection
are stored in URL query parameters for shareable deep links. Escaped plain-text
source previews are emitted once and moved into the selected effective view in
the browser. `data/index.json` contains ordered paths, provenance hashes, limit
findings, and no instruction text.

A source can be edited or proposed for deletion as a single-file local draft;
when the selected directory has no local instruction candidate, a new
`AGENTS.md` can be proposed there. Draft text stays in page memory and is never
sent to the hub or written to the monitored repository. The browser shows a
line diff, the effective-instruction diff for the selected directory, and every
mapped directory affected by the operation. Deleting an override previews a
bounded, published `AGENTS.md` fallback when one exists. Shadowed candidates are
therefore included in the HTML report once, but instruction text remains absent
from `data/index.json`.

Draft actions are offered only when the published source is valid UTF-8 and
uses one line-ending style. CRLF and CR sources are edited with browser-normalized
line breaks, then restored to their original style for impact calculation and
patch export. Invalid UTF-8 and mixed-line-ending sources remain visible but
read-only, with a report finding explaining why.

The editor compares the draft's base commit and source hash (or the expected
absence of a newly proposed path) with the current `data/index.json` when
possible, warns if the published release changed, and warns before leaving with
an unsaved operation. Reloading or discarding loses the draft.

Every non-empty operation produces a standard unified diff with three lines of
context. Its header records the base commit and either the source SHA-256 or
that the path was absent. **Copy patch** uses the Clipboard API with a legacy
copy fallback; **Download patch** creates a local `.patch` file in the browser.
Both actions are disabled when the published base is stale. The patch contains
no credential. Export is also blocked when draft content exceeds the configured
`instructions_max_file_bytes` limit. Applying remains an explicit project-side
action:

```sh
# First compare HEAD with the patch's "# base-commit:" header.
git apply --check instruction-*.patch
git apply instruction-*.patch
# Then run the monitored project's documented fmt/validate commands.
```

The base header is review metadata; `git apply` does not enforce it. A
machine-readable proposal format is intentionally deferred until a concrete
consumer needs one.

## Human feedback

The hub is read-only, but when `project.github_repo` is set, each rendered
item carries three feedback buttons: **Add guidance**, **Change priority**,
and **Archive**. Each opens a prefilled GitHub issue (label `backlog-feedback`)
with a machine-readable body. The human authenticates with their own GitHub
login; the hub still needs no write token. Applying the issue is a normal
backlog commit in the project repo, made by the next agent session (the
workflow is in `templates/AGENTS.md`) or by project-side automation. Until
applied, open feedback issues are listed on the hub dashboard.

Without `project.github_repo`, item and note cards show the same payload in a
read-only text field. The copy button uses the Clipboard API when the browser
allows it. On LAN HTTP or after a clipboard error, the field stays visible and
its text is selected for manual copying. Paste the payload into the project's
tracker or a commit message. The hub still does not accept writes.

## Installation

Setup has two locations. Add the backlog contract and agent instructions to
the monitored repository, then run the hub worker on a machine in your LAN.

### 1. Wire up the monitored project

On any machine that edits the backlog (developer laptops, agent runners),
clone this repo once and install the single dependency into a local
virtualenv. This keeps the tool's dependency out of the system Python on
machines that juggle many projects (the dedicated LAN worker in section 3
is the one place a system-wide install is acceptable):

```bash
git clone <this-repo-url> ~/tools/llm-ops-hub && cd ~/tools/llm-ops-hub
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python bin/hub.py self-test
```

Then, inside the project repo:

```bash
mkdir -p docs/backlog/feature docs/backlog/fix docs/backlog/rework \
         docs/backlog/security docs/backlog/done docs/backlog/notes
cp ~/tools/llm-ops-hub/templates/AGENTS.md \
   ~/tools/llm-ops-hub/templates/CLAUDE.md \
   ~/tools/llm-ops-hub/templates/config.json docs/backlog/
$EDITOR docs/backlog/config.json     # set this project's areas enum
~/tools/llm-ops-hub/.venv/bin/python ~/tools/llm-ops-hub/bin/hub.py fmt      --backlog-dir docs/backlog
~/tools/llm-ops-hub/.venv/bin/python ~/tools/llm-ops-hub/bin/hub.py validate --backlog-dir docs/backlog
git add docs/backlog && git commit -m "Adopt backlog-as-code"
```

To put the project's living docs (playbooks, contracts,
reference pages) under the same enforcement, add the docs-module keys to
`docs/backlog/config.json` (`"docs_dir": "docs"`, optionally
`docs_index_file` / `docs_stale_days`; see [Docs pages](#docs-pages-optional)),
give every top-level page of that directory a frontmatter header, and re-run
`fmt` + `validate`. The same two commands gate docs headers from then on,
and the hub build renders the Docs health page.

The copied `docs/backlog/AGENTS.md` is the operating guide agents load when
working inside the backlog directory (record contract, lifecycle, query
patterns); adjust its marked spots before committing. The regular workflow is
short: edit files under `docs/backlog/`, run `fmt`, run `validate` and require
exit code 0, then commit to the backlog branch. `fmt` canonicalizes files and
regenerates `index.json`. Do not edit the index by hand.

#### Check backlog pull requests in CI

GitHub projects can copy the bundled workflow:

```bash
mkdir -p .github/workflows
cp ~/tools/llm-ops-hub/templates/github-actions/backlog.yml \
   .github/workflows/backlog.yml
```

Edit the copied file before committing it. Replace `<hub-ref>` with a released
tag or full commit SHA, change the hub repository if you use a fork, and adjust
every `docs/backlog` path if the project uses another `backlog_dir`. If the docs
module is enabled, add its directory to the workflow's `paths` filter.

The job runs only on matching pull requests. It checks out the pinned hub into
`.llm-ops-hub`, installs its dependency in a temporary virtualenv, runs `fmt`,
and fails if formatting changed the project. It then runs `validate`. Keep the
hub ref pinned so an unchanged project does not pick up a new contract without
an explicit update.

### 2. Onboard the project's agents

Agents will not look into `docs/backlog/` on their own. The project's root
instruction file has to send them there. Paste the following into the
project's root `AGENTS.md` (or `CLAUDE.md`, if that is the file your agents
load), and fill in the two placeholders:

```markdown
## Backlog

This project keeps its backlog as code: one canonical JSON file per item
under `docs/backlog/`, rendered elsewhere by a read-only hub. The operating
guide is `docs/backlog/AGENTS.md`. Read it before adding, updating, or
closing backlog items. The short version:

- Backlog changes are ordinary commits to `<backlog-branch>`; there is no
  other write path. Never edit `docs/backlog/index.json` by hand.
- After ANY edit under `docs/backlog/`, run
  `<path-to-hub>/.venv/bin/python <path-to-hub>/bin/hub.py fmt --backlog-dir docs/backlog`,
  then the same command with `validate`. It must exit 0 before you commit.
- Pick up work from items with `status: open`, highest priority first
  (`now` > `next` > `later`); read the full item before starting, and treat
  human-authored notes (`"author": "human:..."`) as direction.
- When you finish work, close the loop in one commit: delete the item file
  and add a `done/` entry (see the guide).
- Durable findings (audit results, bugs spotted in passing, gotchas) belong
  in `docs/backlog/notes/` as agent notes, shared across sessions
  and models. One note = one directory (`note.json` manifest + any files:
  markdown, JSON, screenshots). Check `index.json` for relevant notes before
  starting non-trivial work; persist what matters before compacting.
- Open GitHub issues labeled `backlog-feedback` are human instructions for
  the backlog. Apply them as described in the guide, then close them.
- (Only if the docs module is enabled) editing any top-level page of the
  configured docs directory counts as a backlog-contract edit too: the page
  must keep its YAML frontmatter header, and the same fmt+validate commands
  must exit 0 before you commit.
```

Reword freely. Keep the pointer to
`docs/backlog/AGENTS.md`, the fmt+validate rule, the agent-notes bullet
(otherwise agents will miss their shared memory), and the
`backlog-feedback` bullet (drop that one if the project is not on GitHub).
If the project follows the `AGENTS.md`-plus-`CLAUDE.md`-include convention,
the snippet goes into `AGENTS.md` only. When several repositories on one
machine share the hub, pin the tool path behind an environment variable in
the snippet (for example `LLM_OPS_HUB_DIR`, defaulting to a sibling
checkout) so agents do not hard-code a machine-specific location.

For automation, `validate` also accepts `--json`. It writes only a JSON array
to stdout: `[]` on success, or objects with stable `file`, `rule`, and
`message` fields on failure. Its exit codes remain `0` for a valid contract
and `2` for validation or configuration errors; the default human-readable
output is unchanged.

Stable rule identifiers in schema version 1 are `schema`, `json.invalid`,
`canonical.form`, `path.mismatch`, `id.duplicate`, `item.id_prefix`,
`item.area`, `done.id_date`, `note.layout`, `note.file_type`,
`note.file_size`, `note.manifest_missing`, `note.id_directory`,
`note.id_date`, `docs.empty`, `docs.frontmatter.invalid`,
`note.last_reviewed`, `docs.frontmatter.canonical`, `docs.last_reviewed`,
`index.missing`, `index.stale`, and `configuration`.

### 3. Stand up the hub worker

On the LAN machine that renders and serves the site:

```bash
git clone <this-repo-url> && cd llm-ops-hub
python3 -m pip install -r requirements.txt   # jsonschema; Python 3.9+
python3 bin/hub.py self-test                 # no config or network needed
cp config.example.json config.json && $EDITOR config.json
python3 bin/hub.py sync                      # clone/fetch the bare mirror
python3 bin/hub.py build                     # render a release, flip the symlink
python3 bin/hub.py serve                     # or install the systemd units
```

For unattended operation, adjust the paths/user inside the `systemd/` units
and install them: the timer runs sync+build every 2 minutes, the HTTP service
serves the site LAN-only.

If `project.github_repo` is set, enable the feedback loop:

```bash
gh auth login    # a token with read scope is enough
gh label create backlog-feedback --repo <owner/repo> \
  --description "Backlog feedback filed from the hub"
```

The label must exist up front. GitHub silently drops unknown labels from
prefilled issue links.

`build` validates everything first and **refuses to render an invalid
backlog**. The previous release stays live. Output: `index.html` (dashboard,
including open feedback issues), `backlog.html` (table + item cards),
`notes.html` (agent notes: browse/search, archive/delete via feedback
issues), `done.html` (completed work grouped by month), `health.html`
(report-only backlog and note findings), and
`data/index.json` for agents (keys: `backlog` with the full items,
`done`, `notes`, `health`, `related_notes`, `feedback_issues`,
`feedback_error`, `ref`, `commit`, `generated_at`).

## Configuration

Hub instance config is JSON, resolved from `--config`, then the
`LLM_OPS_HUB_CONFIG` env var, then `config.json` next to the tool, then
`~/.local/share/llm-ops-hub/config.json`. This repo's
[config.example.json](config.example.json) is the reference example; copy it
to `config.json` for a local or deployment-specific instance.

| Key | Meaning | Default |
| --- | --- | --- |
| `project.repo_url` | remote of the monitored repo (mirror source) | required |
| `project.backlog_ref` | the single writable backlog branch | `main` |
| `project.backlog_dir` | backlog path inside the repo | `docs/backlog` |
| `project.github_repo` | `owner/repo` enabling the feedback loop (buttons on item cards, open `backlog-feedback` issues on the dashboard); omit to skip it (no `gh` needed) | unset |
| `project.name` | display name in the rendered site | `Backlog` |
| `paths.root` | runtime root | `~/.local/share/llm-ops-hub` |
| `paths.mirror` / `cache` / `releases` / `public` | each path individually overridable; `{root}` placeholder supported | `{root}/...` |
| `server.host` / `server.port` | for `hub.py serve` | `127.0.0.1` / `8080` |
| `build.releases_keep` | how many release directories to keep after a successful build (`0` = keep all) | `20` |

## Repository layout

```
bin/hub.py       the whole tool (fmt / validate / sync / build / serve / self-test)
bin/sync_hub.sh  sync + build wrapper used by the systemd timer
schema/          bundled default JSON Schemas (backlog items, done entries, agent notes, docs headers)
templates/       pack for monitored projects: instructions, config, CI workflow
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
  backlog items. Keep them LAN-only.
- `gh` (read scope) is needed only when `project.github_repo` is set.
- Releases are immutable directories under `public_releases/`; `public` is a
  symlink flipped atomically after a successful build. `build` skips rendering
  when nothing changed since the last successful build. It compares the commit,
  feedback issues, tool, and config. Use `build --force` to override the check.
  Old release directories are pruned down to `build.releases_keep`.
- `public/heartbeat.json` is the one mutable file in a release. It is
  rewritten on every successful build attempt, including skipped builds. The
  rendered pages show a staleness warning when the heartbeat is old, which
  means the pipeline is down or failing. Old content alone does not trigger
  the warning because a quiet backlog is not a failure.

## Status

- Released under the MIT License; see [LICENSE](LICENSE).
- The repository and bundled examples are project-agnostic.
