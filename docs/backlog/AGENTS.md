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
condition, or `done` after verification. Record outcome and evidence in a
linked discussion and read the saved task back before reporting completion.

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

For repository documentation changes, run the canonical Hub `fmt` and
`validate` commands against this archive. Use the repository venv or the
configured Hub venv in an isolated worktree. `fmt` owns `index.json`; never
edit it by hand or weaken schemas. Run the root guide's `self-test` before
committing, and preserve `CLAUDE.md` companions containing `@AGENTS.md`.

Human feedback issues labelled `backlog-feedback` remain input. Record
accepted task changes and evidence in Knowledge, then reference the resulting
record when closing the issue through the authorized workflow.

Never store secrets, credentials or private customer data in Knowledge or
repository documentation.
