# Proposal: hub improvements (July 2026)

Status: proposal, for discussion
Scope: GUI, human feedback loop, sync automation, PR view, misc ideas
Constraint: every design below respects the invariants in `AGENTS.md`
(read-only hub, git is the database, one canonical form, fail-closed build,
read-only tokens).

## 1. Human feedback loop (the big one)

Goal: a human browsing the hub can (a) leave guidance for agents on an item,
(b) change an item's priority, (c) archive an item — without the hub growing
a write path.

### Recommended mechanism: prefilled GitHub Issues + repo-side processing

The hub stays 100% read-only. Each item detail card gets action buttons that
are plain deep links to a prefilled GitHub issue in the monitored repo:

```
https://github.com/{github_repo}/issues/new
  ?title=[backlog-feedback] FEAT-20260703-xyz: change priority to now
  &labels=backlog-feedback
  &body=<structured body: item id, action, payload>
```

- **Auth is the human's own GitHub login** — the hub needs no token, no POST
  endpoint, no session. Invariant 1 and the read-only token posture hold.
- **Processing happens in the project repo**, where writes already live:
  either the next agent session picks up open `backlog-feedback` issues
  (add one paragraph to `templates/AGENTS.md`), or a GitHub Action applies
  mechanical changes (priority flip, archive) directly with `fmt`+`validate`
  in CI and closes the issue. Guidance-type feedback always goes through an
  agent, which turns it into a `notes` entry.
- **Audit trail for free**: the issue records who asked for what; the commit
  records what changed.

Three actions, one pattern:

| Button | Issue payload | Applied as |
| --- | --- | --- |
| "Add guidance" | free text | append `notes` entry with `author` |
| "Set priority: now/next/later" | target value | single-field edit + fmt |
| "Archive" | optional reason | `status: archived` + a dated note |

Alternative considered and rejected: GitHub web-edit deep links
(`/edit/{ref}/{path}`). Faster for the human, but hand-edited JSON almost
never survives byte-for-byte canonical-form validation, so every web edit
would trip the fail-closed build until someone runs `fmt`. Only viable if
the project repo runs an auto-fmt bot; keep as a phase-2 option.

Rejected outright: any hub-side mutation endpoint (violates invariants 1, 2
and the token posture).

### Schema changes needed (contract change, coordinate per AGENTS.md)

1. **`notes[].author`** (optional string, e.g. `"human:piotrek"`,
   `"agent:claude"`). Human guidance becomes a first-class, queryable part of
   the item; the hub renders human notes visually distinct so agents and
   humans both see "a person said this". Existing items stay valid (new
   optional field), so `schema_version` stays 1.
2. **`status: "archived"`** added to the enum. Archived = "keep for
   reference, not planned" — distinct from done (shipped) and dropped
   (deleted + done entry). One-field change, trivially revivable, works with
   the same feedback mechanism as priority. Hub: default backlog view filters
   it out, an "Archived" filter/tab shows them, `index.json` keeps them
   (status field already present). Agent guide: "pick next work" already
   selects `status == "open"`, so archived items are naturally skipped.
   - Alternative: an `archived/` directory parallel to `done/`. Cleaner
     "backlog dir holds open work only" story, but archiving/reviving becomes
     a file move and the feedback automation gets more complex. Not
     recommended for now.

## 2. GUI improvements

Quick wins (no contract changes):

- **Search the prose.** `data-search` on backlog rows covers only
  id/title/type/area/priority/status/risk/path — searching for a word that
  appears in `problem` or `scope` finds nothing. Include problem/scope/value
  text.
- **Staleness banner.** `build` fails closed, which is correct — but then the
  site silently serves stale data and nobody notices. Client-side: compare
  `generated_at` against now; if older than ~15 min (timer is 2 min), show a
  warning banner "hub may be stale — last successful build …". Zero new
  write paths.
- **PR pills become links.** `links.prs` renders as inert `PR #123` pills;
  when `github_repo` is set, link them to GitHub.
- **Filters in the URL.** Persist toolbar state as query params (hash already
  carries item id) so filtered views are shareable/bookmarkable.
- **Dashboard cards link somewhere.** "Backlog", "Risk", "Done" cards become
  links to the corresponding (pre-filtered) pages; add a "top of queue" list
  (open items with priority `now`) directly on the dashboard.
- **Dark mode.** CSS is `color-scheme: light` only; add a
  `prefers-color-scheme: dark` variant.
- **Item age.** Show "created N days ago" and flag stale items (old, no
  recent notes) in the table.
- **Done page filters.** Filter by month/area in addition to the text search;
  link `item_id` pills to the item snapshot when present.
- **Feedback buttons** from section 1 (guidance / priority / archive) on the
  item detail card — pure links, so they belong to this bucket too.

## 3. Sync automation

A systemd timer already syncs+builds every 2 minutes, so "automatic sync"
exists; the actual problems are cost and observability:

- **Skip no-op builds.** `build` currently renders a brand-new immutable
  release every 2 minutes even when nothing changed — release-dir churn and
  disk growth. Record the (commit, tool version, config hash) of the last
  successful build; if unchanged, exit 0 without rendering.
- **Prune releases.** Keep the last N (e.g. 20) release dirs; delete older
  ones after the symlink flip. (New config knob → README + example +
  `_resolve_hub_config()` together, per AGENTS.md.)
- **Surface failures.** Log-only failure of a fail-closed build is invisible.
  Combined with the staleness banner (section 2) this may be enough; if not,
  a `status.json` written on every attempt (success or failure) is the next
  step — needs a small design decision because releases are immutable.
- **Push-based sync is not worth it here.** The worker is LAN-only, so
  GitHub webhooks can't reach it without opening a tunnel; 2-minute polling
  of a bare mirror fetch is cheap. Revisit only if latency ever matters.

## 4. The PRs page: why is it there, and should it stay?

Honest answer: in its current form it earns little. It lists *all* open PRs
of the monitored repo — GitHub's own PR list does that better, and the page
costs a `gh` dependency plus a token on the worker.

The only PR information GitHub *doesn't* give you is the join against the
backlog: which items have in-flight work. Two sensible directions:

- **Repurpose (recommended):** match open PRs to items via `links.prs` and
  id mentions in branch names/titles; render an "in flight" badge on matched
  backlog rows and show only matched PRs (plus unmatched count) on the
  dashboard. If feedback goes the issue route (section 1), the same `gh`
  call can list open `backlog-feedback` issues on the dashboard — that makes
  the GitHub integration pull its weight.
- **Or drop it:** delete `prs.html` + `load_prs()`, drop the `gh`
  dependency entirely. Simpler tool, one less token on the worker.

Keeping it as-is is the one option that makes no sense.

## 5. Other ideas (unordered, for later)

- **Multi-project hub.** Config takes a list of projects; one worker renders
  N backlogs under one site with a project switcher. Biggest structural
  change; do it before open-sourcing, since single-project is baked into
  config resolution and rendering.
- **CI template for monitored projects.** Ship a GitHub Actions workflow in
  `templates/` that runs `fmt --check`+`validate` on PRs touching the
  backlog dir — enforces the contract before the hub ever sees a bad commit,
  and is a prerequisite for web-edit-style feedback.
- **Per-item JSON endpoints.** `data/items/<id>.json` next to the bulk
  `data/index.json`, so agents can fetch one item cheaply.
- **Atom/RSS feed** of new items and done entries — cheap to render, nice
  for humans who won't open the hub daily.
- **Item history view.** The mirror has full git history; render "priority
  changed 2026-07-01, status → in-progress 2026-07-04" per item from
  `git log` on the item file.
- **Throughput on the dashboard.** Done entries per month, median item age —
  trivial to compute at build time.

## Suggested order

1. GUI quick wins + skip-no-op-build + release pruning (small, no contract
   impact, immediate value).
2. Feedback loop: schema (`notes[].author`, `status: archived`), issue deep
   links, `templates/AGENTS.md` update — one coordinated contract change.
3. PR page repurpose (or removal) — decide after 2, since the issue-based
   feedback reuses the same `gh` surface.
4. Everything in section 5 stays parked until asked for.
