# Plan: repository instruction explorer and change proposals

Status: phases 1-2 implemented; phase 3 patch export implemented; phase 4 planned
Date: 2026-08-21
Scope: repository-visible agent instructions for one monitored project
Constraints: the hub stays read-only, Git stays the database, generated pages
remain static, monitored-repository content is untrusted, and every displayed
claim is reproducible from the mirrored commit and a versioned agent profile.

## Decision summary

Evolve the existing repository instruction map into an **Instruction
Explorer**. A human selects a directory and an agent profile, sees the ordered
repository instruction sources that apply there, and can inspect their
effective combined text without losing source boundaries.

Editing uses a local browser draft. The hub never saves the draft to the
monitored repository and exposes no mutation API. Before export, the UI shows
the diff and the directories whose effective instruction chains would change.
The result can be copied or downloaded as a patch that a project-side workflow
can turn into a commit or pull request. A second machine-readable proposal
format is deferred until a concrete consumer justifies a versioned contract.

`AGENTS.md` as the source of truth with a sibling `CLAUDE.md` containing only
`@AGENTS.md` is the recommended architecture for projects adopting LLM Ops
Hub, including win-path ops. It is not a universal resolution rule in the hub.
Projects with independent `CLAUDE.md` content must remain supported.

## Product promise and terminology

The page answers:

> Which repository-visible instruction sources would this versioned profile
> discover for work rooted in this directory, and how would a proposed source
> edit change that result?

The page must not claim to show the complete runtime prompt. User, enterprise,
managed, system, skill, plugin, subagent, machine-local, configured fallback,
and repository-external sources may be unavailable to the hub. Profiles can
also differ in when they load nested instructions.

Use **profile**, not **agent**, for the selector. A profile is a versioned,
deterministic implementation of one agent runtime's repository discovery
rules, with an `as_of` date and a link to the vendor documentation used to
define it.

Use **effective repository instructions**, not **effective prompt**, for the
combined preview.

## Universal model and recommended convention

The core data model must not assume that `CLAUDE.md` aliases `AGENTS.md`.
Profiles independently discover sources and return a common report shape:

- selected work directory;
- ordered source records with path, kind, scope, provenance hash, and content
  availability;
- relationships such as `overrides`, `imports`, or `loads-on-demand`;
- findings and unresolved external sources;
- profile name, version, documentation URL, and `as_of` date.

The project's preferred architecture is expressed as an optional policy/lint:

- `AGENTS.md` is the single source of truth;
- each matching `CLAUDE.md` contains only `@AGENTS.md`;
- adding, moving, or deleting an `AGENTS.md` requires the sibling
  `CLAUDE.md` change.

The existing `instructions_lint_claude_include` setting is the first form of
that policy. A later configuration cleanup may give it a clearer policy name,
but must retain backward compatibility.

When the policy is enabled, the UI labels a conforming `CLAUDE.md` as a
bridge/import rather than an independent source. It reports deviations but
does not silently reinterpret or rewrite them. When the policy is disabled,
the Claude profile evaluates the repository according to its own rules.

## Target experience

Use a three-part desktop layout that collapses cleanly on narrow screens:

1. **Directory tree**
   - lazy disclosure of the configured repository subtree;
   - text filtering and a clear selected path;
   - badges for a local source, override, bridge/import, or finding;
   - only directories are primary navigation; files appear in the source
     inspector.
2. **Effective instructions**
   - profile selector and selected ref/commit always visible;
   - combined text in discovery/load order with persistent source boundaries;
   - each block labelled `inherited`, `local`, `override`, `imported`, or
     `loads on demand` where the profile can make that claim;
   - toggle between **Effective**, **Sources**, and **Changes**;
   - a delta view answering "what does this directory add or replace compared
     with its parent?".
3. **Source and diagnostics inspector**
   - source path, size, hash, scope, and profile relationship;
   - report findings and omitted content;
   - actions to edit an editable repository source or inspect it in the forge.

Selection state belongs in the URL: profile, directory, ref/commit, active
view, and optionally source path. A shared link must reconstruct the same
read-only view for the same rendered release.

The initial tree may cover the whole configured repository, but the existing
file-count and byte limits remain mandatory. Truncation is visible and never
silently produces a plausible-looking incomplete tree.

### Directory tree implementation decision

Implement the directory picker as a small repository-owned component using
server-rendered semantic HTML and vanilla JavaScript. Do not add React, a
frontend build pipeline, a tree-view package, or a runtime CDN dependency for
this feature. The hub already knows the complete bounded directory set at
build time, and the required interaction is narrower than a general-purpose
file manager.

The component is a single-select directory navigation tree, not a full file
tree. It shows directories as the primary nodes and may attach status badges
for local instructions, overrides, bridges/imports, and findings. It does not
provide drag-and-drop, rename, context menus, checkboxes, or multi-selection.

Build on nested `ul`/`li` semantics enhanced with the ARIA tree pattern. The
minimum keyboard contract is:

- `Up` and `Down` move focus through visible nodes;
- `Right` expands a closed directory or moves to its first child;
- `Left` collapses an open directory or moves to its parent;
- `Home` and `End` move to the first and last visible nodes;
- `Enter` or `Space` selects the focused directory;
- one roving `tabindex="0"` keeps the tree to a single tab stop.

Focus and selection are separate states. Arrow navigation must not repeatedly
replace the selected directory, effective-instructions panel, or URL. Clicking
a directory label or pressing `Enter`/`Space` selects it. Clicking its
disclosure control only expands or collapses it. Focus and selection require
visibly distinct styling.

Directory filtering keeps matching nodes and their ancestors visible, expands
the matching paths temporarily, and restores the previous expansion state
when cleared. Loading a deep link expands the selected directory's ancestor
chain. The first implementation renders the bounded tree without
virtualization; revisit that only with measured evidence that the configured
limits produce an unusable DOM or interaction latency.

Use the current WAI-ARIA tree-view pattern and GitHub's documented accessible
repository-tree behavior as implementation references, then verify the actual
component with keyboard-only interaction and at least one desktop screen
reader. Documentation examples are guidance, not proof that the resulting
component is accessible.

## Local draft editing

Editing always starts from a concrete source block, never from the anonymous
combined output. The editor records:

- repository path;
- base commit and base content hash;
- original content;
- draft content;
- selected profile and work directory used for preview.

The first implementation keeps the draft in page memory. Reloading or leaving
the page warns about unsaved draft content. Browser persistence may be added
later only as an explicit opt-in because instruction files can contain
security-sensitive operational information and stale drafts are easy to apply
against the wrong commit.

The editor must support creating a missing bridge/source file as a proposed
addition and deleting a source as a proposed deletion, but destructive-looking
actions remain drafts until exported and applied outside the hub.

Markdown rendering is optional for the first implementation. Accurate plain
text, a readable diff, and correct source provenance matter more than a rich
editor.

## Impact preview

For every draft, recompute the selected profile report against an in-memory
overlay of the mirrored commit. Do not mutate the mirror or write an overlay
to hub state.

Show:

- the effective instruction diff for the selected directory;
- the set and count of directories whose source chain or effective content
  changes;
- whether the source becomes newly applicable, stops applying, changes an
  import relationship, or replaces another source;
- new or resolved findings;
- base commit/hash mismatches if the live release changed while the draft was
  open.

Impact is structural and deterministic. The hub may detect exact duplicate
lines, broken imports, missing bridge files, cycles, size limits, and source
selection changes. It must not ask an LLM to decide whether two natural
language rules conflict or which instruction will win semantically.

For large affected subtrees, show a summary first and allow the directory list
to expand. Never duplicate full combined content for every descendant in the
generated JSON or HTML.

## Patch and pull-request proposal flow

The read-only boundary divides **proposal creation** from **repository
mutation**.

The browser can produce:

1. a standards-compatible unified diff with paths, additions/deletions, and
   the base commit recorded alongside it;
2. a machine-readable change proposal containing base commit, edited files,
   before/after hashes, profile, selected directory, impact summary, and the
   human's optional rationale;
3. a copy/download action that works without GitHub;
4. when `project.github_repo` is configured, a forge handoff using the human's
   own authentication.

The static hub must not embed a write credential, accept a callback token, or
POST repository changes. A prefilled issue or copied proposal may ask a
project-side agent/automation to apply the patch, validate it, and open a pull
request. If a direct "Create PR" experience is later required, specify a
separate authenticated project-side component; do not quietly turn the static
hub server into that component.

Large patches must not be placed in a URL. For the initial GitHub handoff, use
a short prefilled issue plus an explicit copy/download step for the full
proposal, or provide a local checkout command that consumes the exported
proposal. The exact transport is a decision gate before phase 3.

Applying an exported proposal must reject a base commit/hash mismatch unless
the human explicitly rebases and reviews the resulting diff.

## Profiles

### Codex repository profile

Build on the current `codex-repository-v1` implementation:

- preserve root-to-directory discovery order;
- preserve `AGENTS.override.md` before `AGENTS.md` at each level;
- continue ignoring symlinks and unsafe paths;
- keep configured fallback filenames outside the profile until their
  repository-visible configuration and provenance are explicitly modeled;
- add source content to the effective browser view without publishing copied
  combined text in `data/index.json`.

The version changes when observable resolution behavior changes. Documentation
refreshes that confirm unchanged behavior update `as_of` only.

### Claude Code repository profile

Add only after rechecking current vendor documentation and specifying at least:

- `CLAUDE.md`, `.claude/CLAUDE.md`, and repository-local
  `CLAUDE.local.md` discovery;
- ancestor load order and nested sources that load on demand;
- repository-local `@` imports with depth and cycle limits;
- `.claude/rules/`, including path-scoped rules;
- configured exclusions only when their configuration is visible in the
  mirrored repository;
- external, user, managed, auto-memory, and machine-local sources as visibly
  unresolved/out of scope.

An `@AGENTS.md` import is a normal Claude import. The recommended bridge policy
adds lint and clearer presentation; it does not replace Claude resolution.

## Delivery phases

### Phase 1 - explorer UX on the existing Codex report

Implementation status: complete in the 2026-08-21 working change. Keyboard and
browser accessibility-tree behavior were verified; hands-on screen-reader
verification remains a release check rather than an implementation claim.

- replace the directory select with a searchable, collapsible tree;
- add the effective/source/delta views with visible source boundaries;
- deep-link selection state;
- preserve current limits, escaping, profile warnings, and JSON minimization;
- add self-test fixtures for tree selection, source order, sibling isolation,
  and URL state.

### Phase 2 - local draft and deterministic impact preview

- edit one existing source in page memory; **implemented 2026-08-21**
- recompute Codex chains using an in-memory overlay; **implemented for edits,
  additions, deletions, and override fallback selection**
- show source diff, selected-directory effective diff, and affected subtree;
  **implemented for all three operations**
- detect stale base commit/hash; **implemented through a no-cache comparison
  with the currently published `data/index.json`**
- support proposed add/delete after single-file editing is verified;
  **implemented, including shadowed `AGENTS.md` fallback after override
  deletion**
- add escaping and adversarial-content tests for every draft-rendering path.
  **implemented with text-only DOM construction and an end-to-end malicious
  draft fixture**

### Phase 3 - portable change proposal

- export/copy a unified diff; **implemented with copy and local `.patch`
  download, including base commit/hash metadata and stale-base blocking**
- define and validate a versioned proposal schema; **deferred until a concrete
  machine consumer exists**
- add a non-GitHub application path; **implemented through standard
  `git apply --check` / `git apply` workflow documentation**
- choose and implement the GitHub handoff without adding hub credentials or a
  mutation endpoint;
- document project-side validation before commit/PR creation; **implemented
  for the local patch path; forge handoff remains**

### Phase 4 - Claude profile and bridge-policy presentation

- implement the versioned Claude repository profile after a documentation
  refresh;
- expand safe repository-local imports and rules;
- distinguish startup sources from load-on-demand sources;
- present conforming `CLAUDE.md -> @AGENTS.md` bridges compactly;
- prove independent `CLAUDE.md` content works when the policy is disabled.

Claude may move earlier if a real monitored project needs it, but it must not
delay the simpler Codex explorer and draft loop.

## Decision gates

Resolve these before implementing the named phase:

1. **Phase 2:** whether a single browser draft may touch one source only or
   multiple sources. Default to one source; multi-file edits materially
   complicate impact display and patch review.
2. **Phase 3:** the first GitHub transport: issue handoff, local checkout
   command, or a separately authorized PR component. Default to issue plus
   portable proposal because it preserves the current deployment model.
3. **Phase 3:** proposal JSON schema and maximum exported content size. JSON is
   now deferred until a concrete consumer exists; the configured
   `instructions_max_file_bytes` limit constrains the initial single-file patch
   workflow.
4. **Phase 4:** exact Claude profile surface after checking current vendor
   behavior, including path-scoped rules and repository-visible exclusions.

## Definition of done

Each phase is complete only when:

1. `python3 bin/hub.py self-test` passes with new behavior and failure modes
   covered.
2. Every report and impact claim is derived from the immutable mirrored commit,
   the explicit draft overlay, and a named versioned profile.
3. No instruction content reaches `data/index.json` unless a separate security
   review explicitly changes the existing minimization decision.
4. Untrusted paths and content remain bounded and escaped in source, effective,
   diff, finding, and proposal views.
5. The hub has no repository write credential, mutation API, or hub-local
   record store.
6. README and configuration examples describe new public knobs and limitations
   in the same change that introduces them.
7. A throwaway monitored repository proves the behavior introduced by that
   phase end to end: nested scopes and overrides in phase 1; stale drafts and
   affected-directory calculation in phase 2; patch export in phase 3; and
   the enabled and disabled bridge policy in phase 4.

## Non-goals

- reconstructing or displaying the complete runtime/system prompt;
- importing user, enterprise, managed, skill, plugin, subagent, auto-memory,
  or repository-external instructions;
- semantic conflict resolution by an LLM;
- silently following symlinks or external imports;
- editing anonymous combined output;
- storing drafts or applied changes in hub state;
- giving the static hub a repository write token;
- making the `CLAUDE.md = @AGENTS.md` convention mandatory for every monitored
  project.
