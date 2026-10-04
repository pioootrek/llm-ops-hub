# LLM Ops Hub knowledge workflow

Status: active
Audience: humans and agents planning or recording work on LLM Ops Hub
Source of truth: Worktree Switcher Knowledge project `llm-ops-hub`

## Current work

Use the configured Worktree Switcher MCP connection or Knowledge GUI for
this project's tasks, discussions and durable agent memory. Before work, read
`knowledge_project` and `knowledge_tasks` with `projectId: "llm-ops-hub"`,
then the selected record, related discussion and relevant source provenance.
Use returned record IDs, not imported legacy IDs, for service operations.

Create tasks with `knowledge_create_task`, including the concrete problem,
expected value, bounded scope, validation and risk. Choose open work by
priority: `now`, then `next`, then `later`. Use `knowledge_update_task` with
the current revision to set `in_progress`, `blocked` with its unblock
condition, or `done` after verification. Append outcome, validation and limits
to the task description while preserving its existing content. If
`knowledge_relations` returns an existing linked discussion, append a reply
there as well. Read the saved task back before reporting completion. The
current MCP API cannot link an arbitrary new discussion to an existing task.

Append discussion replies rather than rewriting history. Reuse the same
idempotency key when retrying a write, and re-read after revision conflicts.
Save durable findings in Knowledge before compaction; check existing entries
before creating duplicates. Human decisions and imported author labels retain
their original meaning; agent proposals do not become approvals.

If Knowledge is unavailable, report the access or connection problem and pause
backlog writes. Credentials stay in private client configuration. Knowledge
operations do not require a managed development-server claim.

## Archived files and product contract

Existing JSON records and note payloads under this directory are the frozen
import source. Keep them unchanged. Hub rendering of this archive does not
include later Knowledge writes. There is no two-way synchronization, new
file-backed task creation or `DONE-*` file on completion. A future migration
back to files must preserve post-cutover Knowledge writes first.

The repository-root `AGENTS.md` still governs implementation. The Hub remains
a read-only Git-backed product for projects using that contract. Do not
change `templates/AGENTS.md`, schemas or generic onboarding merely because
the Hub's own backlog moved to Knowledge.

For repository documentation changes, run from the checkout root:

```bash
HUB_ROOT="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
"$HUB_ROOT/.venv/bin/python" bin/hub.py fmt --backlog-dir docs/backlog
"$HUB_ROOT/.venv/bin/python" bin/hub.py validate --backlog-dir docs/backlog
"$HUB_ROOT/.venv/bin/python" bin/hub.py self-test
```

The shared venv supplies dependencies; `bin/hub.py` is the current checkout's
implementation. `fmt` owns `index.json`; never edit it by hand or weaken
schemas. Preserve `CLAUDE.md` companions containing `@AGENTS.md`.

Human feedback issues labelled `backlog-feedback` remain input, including
issues opened from old Hub cards. Those cards and `data/index.json` show the
frozen Git snapshot: their open/stale status and promise of a repository
backlog commit do not describe current work. Do not select work from that
snapshot without reading Knowledge first.

For an instance that keeps displaying a migrated project, label its configured
display name as an archive with current work in Knowledge, and omit the
optional `github_repo` setting to stop offering new file-backlog feedback
actions. Rebuild the instance after that configuration change. Existing
feedback issues still follow the mapping below; do not erase historical open
statuses merely to make the archive look current.

To process feedback carrying a legacy `FEAT-*` or other imported ID, call
`knowledge_search` with `projectId: "llm-ops-hub"`, `query` set to that ID
and `includeInactive: true`. For a matching imported discussion, read it and
call `knowledge_relations` with its `recordId` and `recordKind: "thread"`
to find the related task. If this gives no unique task, search
`knowledge_tasks` with the archived item's title and `activeOnly: false`,
following pagination and checking candidate descriptions. Read the matched
task using its service record ID and current revision before writing.

The `legacyId` search filter currently works only for memories; it excludes
tasks and discussions. An empty result is not proof that a task is absent.
Report an unresolved or ambiguous match rather than creating a duplicate.
Record accepted changes and evidence in Knowledge, then reference the saved
record when closing the issue through the authorized workflow.

Never store secrets, credentials or private customer data in Knowledge or
repository documentation.
