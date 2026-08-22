# Multi-project hub

Status: accepted for implementation
Date: 2026-08-22
Scope: monitor independent development projects in one read-only hub

## What a project means

A project is an independent development initiative with its own repository,
backlog, purpose, and release cycle. Projects may use unrelated languages,
toolchains, CI systems, and runtime environments. The hub does not execute any
of them. It reads the configured Git ref and backlog directory from a bare
mirror.

Deployment environments, branches, and modules inside one shared backlog are
not projects. Frontend and backend are separate projects only when they have
independent repositories and backlogs.

## Configuration

Hub config schema version 2 replaces the single `project` object with a
non-empty `projects` array. Every entry has a stable URL-safe `id`, display
name, repository URL, backlog ref, backlog directory, and optional GitHub
repository.

Schema version 1 remains supported without behavior or path changes. Migration
is manual because it only moves the existing `project` object into an array
and adds an `id`; monitored backlog data does not change.

## Runtime isolation

Each project owns these paths under `{root}/projects/<id>/`:

```text
mirror.git/
cache/
public_releases/
public -> public_releases/<release>
```

Projects never share refs, validation state, caches, or release switches. A
failed project build leaves its previous valid release live and does not stop
other projects from updating. The shared dashboard records the partial failure
instead of presenting the run as successful.

## Published interface

The shared site has this shape:

```text
public/
├── index.html
├── data/projects.json
└── projects/
    ├── <project-id>/
    └── <project-id>/
```

The root dashboard summarizes every available project and combines their
`priority: now` queues. Each project keeps its existing pages and
`data/index.json`. A header picker switches between all projects while keeping
the current core page type. Optional docs and instruction views return to the
destination project's dashboard because those modules may not be enabled
there.

Cross-project item IDs are interpreted as `(project_id, item_id)`. Dependencies
remain local to one project. A cross-project dependency contract is outside
this change.

## Commands

`sync` and `build` process every configured project. `--project <id>` limits
either command to one project. `serve` exposes the shared `public` tree.

The systemd wrapper needs no new orchestration layer because its existing
`sync` followed by `build` commands operate on the full configuration.

## Verification

Self-test covers version 1 compatibility, version 2 validation, isolated path
resolution, picker links, aggregate JSON, and publication symlinks. A
throwaway pair of local Git repositories exercises sync, build, selective
build, navigation, and failure isolation end to end.

## Non-goals

- executing project code or provisioning its development environment;
- merging branches or backlog directories into an overlay;
- changing monitored-project schemas;
- sharing state between independent projects;
- cross-project dependency semantics.
