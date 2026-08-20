# Plan: tiny tool, full punch

Status: plan, for discussion (2026-07-18)
Scope: what comes next for the hub after the July 2026 proposals
Constraint: every item respects the invariants in `AGENTS.md` (read-only hub,
git is the database, one canonical form, fail-closed build, minimal
dependencies). This file extends `docs/proposals/2026-07-hub-improvements.md`;
items already proposed or implemented from it are referenced, not repeated.

Progress (2026-08-20): phase 1 is implemented: repository CI,
`validate --json`, HTML escaping self-tests, and a monitored-project CI
template. Items 3.1 and 3.2 are documented.
Items 2.1 and 2.2 are implemented as one Health report.
Item 2.3 is implemented: item cards and JSON expose exact note references.

## The rule: mega simple, mega functional

The tool earns its keep by being small enough to audit in one sitting. Every
addition below passes three tests:

1. It reuses an existing pattern instead of inventing machinery.
2. It removes a failure mode agents or humans actually hit.
3. Its changed behavior is enforced in `self-test` and reflected in every
   public or copied surface it actually affects (`README.md`, schemas, example
   config, or the templates pack), in the same commit (per `AGENTS.md`).

Anything failing those tests goes to Non-goals, no matter how attractive.

## Phase 1 - close the enforcement loop (cheap, additive interfaces only)

### 1.1 CI for this repository

"Run self-test before every commit" is currently enforced by memory alone.
Add one workflow (`.github/workflows/self-test.yml` or the forge equivalent)
that installs `requirements.txt` and runs `python3 bin/hub.py self-test` on
push/PR. Test both Python 3.9 (the compatibility floor) and the current stable
Python so the documented support range is real rather than aspirational.

### 1.2 `validate --json`

The tool is LLM-first; its error report is not. Add a flag emitting
`[{file, rule, message}]` so agents parse failures instead of regexing
stdout. Internally, validators return one small structured diagnostic type;
the CLI has two renderers (existing human-readable text and JSON) rather than
trying to parse today's error strings after the fact.

The interface is explicit:

- `--json` writes only the JSON array to stdout, including `[]` on success;
- diagnostics go to stdout in JSON mode, never split across stdout/stderr;
- exit codes stay `0` for valid and `2` for contract/configuration errors;
- `rule` values are documented identifiers suitable for automation and stay
  stable within a schema version.

The default output remains byte-for-byte compatible where practical. This is
an additive CLI interface, not a backlog schema change.

### 1.3 Escaping self-test

The security posture says everything rendered goes through `h()`; self-test
never asserts it. Add cases: item title, note body, and docs title containing
`<script>` / quotes must come out escaped in rendered HTML. Guards the one
invariant a reviewer cannot eyeball in a diff.

### 1.4 CI template for monitored projects (carried over from proposals §5)

Ship a ready-made GitHub Actions workflow in `templates/` checking PRs that
touch the backlog or configured docs directory. Today the contract rests on
agent discipline until the fail-closed build catches a bad commit. It was
parked "until asked for" — the docs module has since made the contract bigger,
so this is asking for it.

`fmt` currently writes files and has no `--check` mode, so the workflow must
not silently repair a PR in the runner. Check out the hub into a disposable
subdirectory at an explicitly pinned tag or commit, install its requirements
into a venv, use the existing interface, and fail on the resulting project
diff:

```sh
python3 -m venv .llm-ops-hub/.venv
.llm-ops-hub/.venv/bin/python -m pip install -r .llm-ops-hub/requirements.txt
.llm-ops-hub/.venv/bin/python .llm-ops-hub/bin/hub.py fmt --backlog-dir docs/backlog
git diff --exit-code
.llm-ops-hub/.venv/bin/python .llm-ops-hub/bin/hub.py validate --backlog-dir docs/backlog
```

The workflow template must include the separate checkout step; the marked
customization points are the hub repository, its pinned ref, and the backlog
path. Never follow the hub's moving default branch in CI: contract enforcement
must not change underneath an otherwise unchanged project. Do not add
`fmt --check` unless a second consumer makes it worthwhile.

## Phase 2 - health and discovery (all follow the `docs_health` pattern)

`docs_health()` already proves the pattern: compute findings at build time,
render report-only, never block a release. Apply it three more times. Health
checks stay deterministic: they inspect dates, statuses, and exact identifiers;
they do not infer meaning from prose.

### 2.1 Backlog health page

The stale-item pill (`STALE_AFTER_DAYS`) flags age only. A health report goes
further: `in-progress` without activity for N days, `blocked` with no notes or
without activity for N days, `priority: now` without activity for N days, and
items with zero notes since creation. "Last activity" has one definition: the
latest of `created` and all item-note dates. No prose inspection and no Git
history dependency.

This is a report-only page + dashboard card + a `health` block in
`data/index.json` for agents. Replace the hard-coded `STALE_AFTER_DAYS` with
`health_stale_days` from the monitored project's `config.json`, so the existing
pill and the health report cannot disagree. Resolve it through a small
`health_settings()` path analogous to `docs_settings()` — not through
`_resolve_hub_config()`, which belongs to the hub instance configuration.
Update the project-config table in README and `templates/config.json` in the
same commit. `0` disables age-based findings but not structural findings such
as "zero notes".

### 2.2 Note staleness

Notes have `status: archived` but nothing says "this may no longer be true".
Add an optional `last_reviewed` date to the note contract. Freshness is the
latest of `created` and `last_reviewed`; existing notes therefore remain valid
without migration, while re-verifying an old note can reset its age without
recreating it or consulting Git history. Updating a note's factual content
must also update `last_reviewed`.

Active notes older than N days land on the health page as "re-verify". Use a
separate project-config knob, `notes_stale_days` (`0` disables), because
backlog cadence and durable agent-memory cadence are different. Update the
bundled note schema, `templates/AGENTS.md`, README, and `templates/config.json`
together; treat the new optional field as an additive contract change and add
self-test coverage for old notes, reviewed notes, and the disabled threshold.
Notes rot is a when, not an if — have this before open-sourcing.

### 2.3 Related notes on item cards (no contract change)

Renderer scans note manifests and allowed text payloads for exact mentions of
IDs that exist in the validated item set, then adds a "notes mentioning this
item" section to the item card. Do not scan images and do not turn arbitrary
ID-shaped text into links. Deduplicate by `(note_id, item_id)` and expose the
same relation in `data/index.json`. Zero schema impact, and the item↔note link
stops living only in agents' heads.

### 2.4 Feedback without GitHub

The human→agent channel exists only when `github_repo` is set; a non-GitHub
project loses it entirely. Minimal invariant-clean fill: show the same
machine-readable feedback body in a selectable field and add a "copy feedback
payload" button. Use the Clipboard API where the browser permits it, but keep
the field visible and selectable as the fallback: the hub is intentionally
served over LAN HTTP, where a secure clipboard context is not guaranteed. The
human pastes the payload into whatever tracker or commit message the project
uses. No write path, no new dependency.

### 2.5 Repository instruction sources map

Add an opt-in, report-only tree view for answering: "which repository
instruction files apply to work in this directory?" This is a debugging aid
for humans, not an authoritative reconstruction of an agent's full runtime
prompt. The page must state that user, enterprise, skill, plugin, subagent, and
system-prompt layers are outside the hub's view.

The MVP supports only a versioned `Codex` profile: select a directory and list
the applicable `AGENTS.md` files from repository root to that directory in
precedence order, with provenance links. Render each source once as escaped
plain text; the directory view references sources rather than duplicating every
combined chain. `data/index.json` exposes ordered paths and content hashes, not
copied instruction text. Do not use an LLM to infer semantic conflicts or claim
that one natural-language instruction overrides another.

Enable the feature through project config, resolved by an
`instructions_settings()` path analogous to `docs_settings()`, because scanning
instruction files expands the content published beyond `backlog_dir`. A future
`Claude` profile requires a fresh check against vendor documentation and must
be described as repository instruction sources discoverable for a path, not
"what Claude loads". The project's `CLAUDE.md` equals `@AGENTS.md` convention
belongs in optional report-only lint findings, not in profile resolution.

Treat monitored-repository paths and file contents as untrusted input. Never
follow symlinks or read outside the mirrored commit; cap file count, bytes per
file, total bytes, and any future include depth. Limit violations produce
visible truncation findings, never silent output changes. Escaping self-tests
from phase 1.3 are a prerequisite. Add fixture coverage for nested scopes,
sibling isolation, case-sensitive names, symlinks, traversal attempts, source
ordering, size limits, and convention-lint findings before exposing the UI.

**Design review outcome (Codex + Claude Opus, 2026-08-11): conditional go.**
The failure mode is real: reviewers and operators need to explain why an agent
behaved differently in one subtree, and today the applicable instruction
sources are easy to miss. The feature is valuable only while it preserves the
hub's strongest property: every claim shown in the UI is deterministic and
verifiable from the mirrored Git commit.

Accepted for the first implementation:

- a human-facing map of repository-visible instruction sources;
- explicit source order, scope, provenance, and content hashes;
- a Codex ancestry profile whose rules carry an "as of" date;
- optional lint findings for this project's `AGENTS.md` / `CLAUDE.md` pairing;
- opt-in project configuration and strict publication limits;
- escaped plain-text previews, with each source stored and rendered once.

Deferred until independently specified and checked against current vendor
documentation:

- a Claude profile, because Claude Code can load project instructions lazily
  and can include user-level, enterprise, imported, skill, plugin, subagent,
  and system-prompt layers that the hub cannot observe;
- import expansion, including `@AGENTS.md`, because repository-external imports
  must remain unresolved and visibly marked as outside the hub's view;
- any claim that the output equals the complete context seen by an agent.

Rejected for this feature: semantic conflict resolution by an LLM, silently
following symlinks, publishing combined instruction text in `data/index.json`,
enabling repository-wide instruction discovery by default, or blocking a valid
backlog build because an advisory instruction-lint finding exists.

## Phase 3 - document boundaries before open-sourcing

### 3.1 Multi-project — name it, don't build it

Already parked in proposals §5. State "one instance = one project" plainly in
README now so nobody assumes otherwise. If multi-project support is ever
accepted, it gets a separate design proposal before code changes; this plan
does not reserve an abstraction for it.

### 3.2 Contract migration story

`schema_version: 1` is everywhere; nothing says what 2 means. Add one paragraph
to AGENTS.md: a bump is an explicit breaking-contract decision and must declare
either a `hub.py migrate` path or a no-migration break with manual steps. Do not
build migration machinery speculatively, but this rule must exist before the
first version bump, independent of the open-source date.

## Definition of done for every item

An item is complete only when:

1. `python3 bin/hub.py self-test` passes and covers the new failure mode.
2. The default human CLI/UI behavior remains compatible unless the item
   explicitly declares a change.
3. README, `config.example.json`, and the templates pack are updated where
   their public or copied contract changes — not mechanically when irrelevant.
4. A throwaway fixture project covers end-to-end behavior for changes spanning
   project config, validation, and build rendering.
5. Generated output still contains only escaped untrusted content and an
   invalid backlog still leaves the previous release live.

## Non-goals (attractive, rejected)

- **MCP server** — CLI + canonical JSON is already agent-native; MCP adds a
  surface and a dependency for no new capability.
- **Semantic search over notes** — grep + `index.json` covers discovery;
  embeddings mean dependencies and non-deterministic results.
- **Any edit button in the UI** — violates invariant 1, full stop.
- **Per-item JSON endpoints, Atom feed, item history view, throughput
  stats** — parked in proposals §5 "until asked for"; still not asked for.

## Suggested order

1. 1.1 + 1.3: protect the repository and the rendering boundary first.
2. 1.2: introduce structured diagnostics as a focused internal refactor.
3. 1.4: publish the monitored-project CI template against the settled CLI.
4. 3.1 + 3.2: document current boundaries; both are cheap and no-regret.
5. 2.1 + 2.2: one health page, two explicitly configured data sources.
6. 2.3 and 2.4 independently, based on observed demand.
