# WinPath Ops Hub

Private prototype for a read-only Git/YAML operations hub used by WinPath agents
and humans.

The hub treats Git as the source of truth and YAML as the LLM-friendly record
contract. It renders static HTML for humans and JSON for agents.

## Current MVP

- Reads local YAML records from `data/backlog/**` and `data/done/**`.
- Reads `docs/done.md` from `origin/staging` through a bare mirror.
- Reads open pull request metadata through GitHub CLI.
- Generates static HTML into `public`.
- Serves the generated site on a LAN-only host.

## Runtime Layout

On a worker host:

```text
~/winpath-hub/
  mirror.git/          # bare mirror of the product repo, not committed here
  data/                # YAML pseudo-database records
  cache/               # generated JSON cache
  public/              # symlink to latest static release
  public_releases/     # generated releases
  bin/                 # build and sync scripts
```

## Build

```bash
python3 bin/build_hub.py --self-test
bin/sync_hub.sh
```

Environment variables:

- `WINPATH_HUB_ROOT`: runtime root, defaults to `~/winpath-hub`.

## GitHub CLI

Open PR metadata requires `gh` authenticated on the worker:

```bash
gh auth login --hostname github.com --git-protocol https --web
```

The product repo mirror still uses SSH remotes.

## Notes

- This is WinPath-specific for now.
- The intended direction is a generic open-source agent operations hub after
  the YAML contract and branch overlay behavior stabilize.
- No open-source license has been chosen yet.
