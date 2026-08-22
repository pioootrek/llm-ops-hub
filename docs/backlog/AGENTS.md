# LLM Ops Hub backlog guide

Status: active
Audience: humans and agents planning or recording work on LLM Ops Hub
Source of truth: workflow and operating rules for `docs/backlog`

This directory is the Git-backed backlog for LLM Ops Hub itself. The running
hub reads it from the `main` ref through its mirror. It never writes changes
back to this repository.

The repository-root `AGENTS.md` still governs implementation work. This file
adds the rules for backlog records.

## Workflow

One open item lives in `feature/`, `fix/`, `rework/`, or `security/`. Completed
work lives in `done/`; durable agent findings live under `notes/`.

After every backlog edit, run:

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
"$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/bin/hub.py" fmt --backlog-dir "$REPO_ROOT/docs/backlog"
"$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/bin/hub.py" validate --backlog-dir "$REPO_ROOT/docs/backlog"
```

`fmt` is the only formatting path and regenerates `index.json`. Never edit
`index.json` by hand. The bundled schemas define the record contract; do not
copy or weaken them locally merely to make an item pass.

## Records

- Open item IDs are `TYPE-YYYYMMDD-slug`, with `FEAT`, `FIX`, `RWK`, or `SEC`
  matching the containing directory.
- Set `status` to `in-progress` when work starts. Use `blocked` only with a
  dated note that names the blocker.
- Append dated notes instead of rewriting earlier decisions or progress.
- When work finishes, delete the open item and add a
  `done/DONE-YYYYMMDD-slug.json` record in the same commit.
- Keep every item concrete: describe the problem, expected value, bounded
  scope, validation evidence, and risk.
- Use `priority: now` only for work that should be picked up immediately.
- Never put secrets, credentials, or private machine paths in backlog records
  or agent notes. The hub renders this content for its configured LAN users.

Feedback issues labelled `backlog-feedback` are input, not mutations. Apply a
valid request in a normal backlog commit, run `fmt` and `validate`, then close
the issue with the resulting commit or PR.
