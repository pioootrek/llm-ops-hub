#!/usr/bin/env python3
"""
Generic read-only backlog hub.

Git is the source of truth; backlog items are canonical JSON records living in
the monitored project repo (one file per item). This tool formats/validates
those records in a working checkout, mirrors the project repo, and renders a
static HTML + JSON site for humans and agents.

Subcommands:
  fmt        canonicalize item files and regenerate index.json (working copy)
  validate   schema + cross-file + canonical-form checks (working copy)
  sync       clone/fetch the bare mirror of the project repo
  build      render a static release from the mirror at the configured ref
  serve      serve the generated site (stdlib http.server)
  self-test  run built-in contract tests, no config or network needed
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import hashlib
import html
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import jsonschema

TOOL_ROOT = Path(__file__).resolve().parent.parent
BUNDLED_SCHEMA = TOOL_ROOT / "schema" / "backlog-item.schema.json"
BUNDLED_DONE_SCHEMA = TOOL_ROOT / "schema" / "done-entry.schema.json"

TYPE_PREFIX = {"feature": "FEAT", "fix": "FIX", "rework": "RWK", "security": "SEC"}
PRIORITY_ORDER = {"now": 0, "next": 1, "later": 2}
INDEX_FILE = "index.json"
PROJECT_SCHEMA_FILE = "schema.json"
PROJECT_DONE_SCHEMA_FILE = "done-schema.json"
PROJECT_CONFIG_FILE = "config.json"
DONE_SUBDIR = "done"


class ContractError(Exception):
    pass


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------------------
# Canonical JSON

def canonical_json(data: Any) -> str:
    """The one canonical serialization: UTF-8, 2-space indent, sorted keys,
    trailing newline. Validation compares bytes against this exact form."""
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def parse_json(label: str, text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{label}: invalid JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Hub config

def load_hub_config(explicit: str | None) -> dict[str, Any]:
    candidates = [
        Path(explicit).expanduser() if explicit else None,
        Path(os.environ["BACKLOG_HUB_CONFIG"]).expanduser() if os.environ.get("BACKLOG_HUB_CONFIG") else None,
        TOOL_ROOT / "config.json",
        Path.home() / "winpath-hub" / "config.json",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            raw = parse_json(str(candidate), candidate.read_text(encoding="utf-8"))
            return _resolve_hub_config(candidate, raw)
    raise ConfigError("no config file found (use --config or BACKLOG_HUB_CONFIG)")


def _resolve_hub_config(path: Path, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ConfigError(f"{path}: expected a mapping with schema_version 1")
    project = raw.get("project")
    if not isinstance(project, dict) or not str(project.get("repo_url", "")).strip():
        raise ConfigError(f"{path}: project.repo_url is required")

    paths_raw = raw.get("paths") if isinstance(raw.get("paths"), dict) else {}
    root = Path(str(paths_raw.get("root", "~/winpath-hub"))).expanduser()

    def resolve(key: str, default: str) -> Path:
        value = str(paths_raw.get(key, default)).replace("{root}", str(root))
        return Path(value).expanduser()

    server = raw.get("server") if isinstance(raw.get("server"), dict) else {}
    build = raw.get("build") if isinstance(raw.get("build"), dict) else {}
    return {
        "config_path": path,
        "project": {
            "name": str(project.get("name", "Backlog")).strip() or "Backlog",
            "repo_url": str(project["repo_url"]).strip(),
            "github_repo": str(project.get("github_repo", "")).strip() or None,
            "backlog_ref": str(project.get("backlog_ref", "main")).strip() or "main",
            "backlog_dir": str(project.get("backlog_dir", "docs/backlog")).strip().strip("/"),
        },
        "paths": {
            "root": root,
            "mirror": resolve("mirror", "{root}/mirror.git"),
            "cache": resolve("cache", "{root}/cache"),
            "releases": resolve("releases", "{root}/public_releases"),
            "public": resolve("public", "{root}/public"),
        },
        "server": {
            "host": str(server.get("host", "127.0.0.1")),
            "port": int(server.get("port", 8080)),
        },
        "build": {
            "releases_keep": max(0, int(build.get("releases_keep", 20))),
        },
    }


# ---------------------------------------------------------------------------
# Item sources: working tree or bare mirror

def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


class WorktreeSource:
    """Reads a backlog directory from the local filesystem (fmt/validate)."""

    def __init__(self, backlog_dir: Path):
        self.backlog_dir = backlog_dir
        self.label = str(backlog_dir)

    def list_files(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.backlog_dir))
            for p in self.backlog_dir.rglob("*.json")
            if p.is_file()
        )

    def read(self, rel_path: str) -> str:
        return (self.backlog_dir / rel_path).read_text(encoding="utf-8")


class MirrorSource:
    """Reads a backlog directory from a bare mirror at a ref (build)."""

    def __init__(self, mirror: Path, ref: str, backlog_dir: str):
        self.mirror = mirror
        self.ref = ref
        self.backlog_dir = backlog_dir
        self.label = f"{ref}:{backlog_dir}"

    def resolve_commit(self) -> str | None:
        result = run(["git", f"--git-dir={self.mirror}", "rev-parse", "--verify", "--quiet", self.ref])
        return result.stdout.strip() or None

    def list_files(self) -> list[str]:
        result = run(
            ["git", f"--git-dir={self.mirror}", "ls-tree", "-r", "--name-only", self.ref, "--", self.backlog_dir]
        )
        if result.returncode != 0:
            raise ContractError(f"could not list {self.backlog_dir} at {self.ref}: {result.stderr.strip()}")
        prefix = f"{self.backlog_dir}/"
        return sorted(
            p[len(prefix):]
            for p in result.stdout.splitlines()
            if p.startswith(prefix) and p.endswith(".json")
        )

    def read(self, rel_path: str) -> str:
        result = run(["git", f"--git-dir={self.mirror}", "show", f"{self.ref}:{self.backlog_dir}/{rel_path}"])
        if result.returncode != 0:
            raise ContractError(f"could not read {rel_path} at {self.ref}")
        return result.stdout


# ---------------------------------------------------------------------------
# Contract: schema + cross-file rules

def load_schema(source) -> dict[str, Any]:
    """Project-owned schema.json wins over the bundled default."""
    if PROJECT_SCHEMA_FILE in source.list_files():
        return parse_json(f"{source.label}/{PROJECT_SCHEMA_FILE}", source.read(PROJECT_SCHEMA_FILE))
    return parse_json(str(BUNDLED_SCHEMA), BUNDLED_SCHEMA.read_text(encoding="utf-8"))


def load_done_schema(source) -> dict[str, Any]:
    """Project-owned done-schema.json wins over the bundled default."""
    if PROJECT_DONE_SCHEMA_FILE in source.list_files():
        return parse_json(f"{source.label}/{PROJECT_DONE_SCHEMA_FILE}", source.read(PROJECT_DONE_SCHEMA_FILE))
    return parse_json(str(BUNDLED_DONE_SCHEMA), BUNDLED_DONE_SCHEMA.read_text(encoding="utf-8"))


def load_project_config(source) -> dict[str, Any]:
    if PROJECT_CONFIG_FILE in source.list_files():
        data = parse_json(f"{source.label}/{PROJECT_CONFIG_FILE}", source.read(PROJECT_CONFIG_FILE))
        if isinstance(data, dict):
            return data
    return {}


def item_files(source) -> list[str]:
    reserved = {INDEX_FILE, PROJECT_SCHEMA_FILE, PROJECT_DONE_SCHEMA_FILE, PROJECT_CONFIG_FILE}
    return [
        p for p in source.list_files()
        if p not in reserved and not p.startswith(DONE_SUBDIR + "/")
    ]


def done_files(source) -> list[str]:
    return [p for p in source.list_files() if p.startswith(DONE_SUBDIR + "/")]


def validate_items(source, schema: dict[str, Any], project_cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Returns (parsed items sorted for display, error messages)."""
    validator = jsonschema.Draft202012Validator(schema)
    areas = project_cfg.get("areas")
    errors: list[str] = []
    items: list[dict[str, Any]] = []
    seen_ids: dict[str, str] = {}

    for rel_path in item_files(source):
        label = f"{source.label}/{rel_path}"
        try:
            raw = source.read(rel_path)
            data = parse_json(label, raw)
        except ContractError as exc:
            errors.append(str(exc))
            continue

        schema_errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
        if schema_errors:
            for err in schema_errors:
                where = "/".join(str(p) for p in err.path) or "(root)"
                errors.append(f"{label}: {where}: {err.message}")
            continue

        item_id = data["id"]
        item_type = data["type"]
        expected_rel = f"{item_type}/{item_id}.json"
        if rel_path != expected_rel:
            errors.append(f"{label}: file must be at {expected_rel} (id and type define the path)")
        if not item_id.startswith(TYPE_PREFIX[item_type] + "-"):
            errors.append(f"{label}: id prefix must be {TYPE_PREFIX[item_type]} for type {item_type}")
        if isinstance(areas, list) and areas and data["area"] not in areas:
            errors.append(f"{label}: area {data['area']!r} not in project config areas")
        if item_id in seen_ids:
            errors.append(f"{label}: duplicate id {item_id} (also in {seen_ids[item_id]})")
        else:
            seen_ids[item_id] = rel_path

        if raw != canonical_json(data):
            errors.append(f"{label}: not in canonical form (run: hub.py fmt)")

        data["_path"] = rel_path
        items.append(data)

    items.sort(key=lambda i: (PRIORITY_ORDER[i["priority"]], i["type"], i["id"]))
    return items, errors


def validate_done_entries(source, schema: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Returns (done entries newest first, error messages)."""
    validator = jsonschema.Draft202012Validator(schema)
    errors: list[str] = []
    entries: list[dict[str, Any]] = []
    seen_ids: dict[str, str] = {}

    for rel_path in done_files(source):
        label = f"{source.label}/{rel_path}"
        try:
            raw = source.read(rel_path)
            data = parse_json(label, raw)
        except ContractError as exc:
            errors.append(str(exc))
            continue

        schema_errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
        if schema_errors:
            for err in schema_errors:
                where = "/".join(str(p) for p in err.path) or "(root)"
                errors.append(f"{label}: {where}: {err.message}")
            continue

        entry_id = data["id"]
        expected_rel = f"{DONE_SUBDIR}/{entry_id}.json"
        if rel_path != expected_rel:
            errors.append(f"{label}: file must be at {expected_rel} (id defines the path)")
        if entry_id[5:13] != data["date"].replace("-", ""):
            errors.append(f"{label}: id date part must match date {data['date']}")
        if entry_id in seen_ids:
            errors.append(f"{label}: duplicate id {entry_id} (also in {seen_ids[entry_id]})")
        else:
            seen_ids[entry_id] = rel_path

        if raw != canonical_json(data):
            errors.append(f"{label}: not in canonical form (run: hub.py fmt)")

        data["_path"] = rel_path
        entries.append(data)

    entries.sort(key=lambda e: (e["date"], e["id"]), reverse=True)
    return entries, errors


def build_index(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "generated_by": "hub.py fmt",
        "items": [
            {
                "area": i["area"],
                "id": i["id"],
                "path": i["_path"],
                "priority": i["priority"],
                "risk_level": i["risk"]["level"],
                "status": i["status"],
                "title": i["title"],
                "type": i["type"],
            }
            for i in items
        ],
        "schema_version": 1,
    }


def validate_index(source, items: list[dict[str, Any]]) -> list[str]:
    expected = canonical_json(build_index(items))
    if INDEX_FILE not in source.list_files():
        return [f"{source.label}/{INDEX_FILE}: missing (run: hub.py fmt)"]
    if source.read(INDEX_FILE) != expected:
        return [f"{source.label}/{INDEX_FILE}: stale (run: hub.py fmt)"]
    return []


# ---------------------------------------------------------------------------
# Commands: fmt / validate (working copy)

def _worktree(backlog_dir: str) -> WorktreeSource:
    path = Path(backlog_dir).expanduser()
    if not path.is_dir():
        raise ContractError(f"backlog directory not found: {path}")
    return WorktreeSource(path)


def cmd_fmt(args: argparse.Namespace) -> int:
    source = _worktree(args.backlog_dir)
    changed = 0
    for rel_path in item_files(source) + done_files(source):
        label = f"{source.label}/{rel_path}"
        raw = source.read(rel_path)
        formatted = canonical_json(parse_json(label, raw))
        if raw != formatted:
            (source.backlog_dir / rel_path).write_text(formatted, encoding="utf-8")
            changed += 1
            print(f"formatted {rel_path}")

    schema = load_schema(source)
    project_cfg = load_project_config(source)
    items, errors = validate_items(source, schema, project_cfg)
    _, done_errors = validate_done_entries(source, load_done_schema(source))
    errors += done_errors
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print("fmt: canonicalized files, but contract errors remain (see above)", file=sys.stderr)
        return 2

    index_text = canonical_json(build_index(items))
    index_path = source.backlog_dir / INDEX_FILE
    if not index_path.exists() or index_path.read_text(encoding="utf-8") != index_text:
        index_path.write_text(index_text, encoding="utf-8")
        print(f"regenerated {INDEX_FILE}")
    print(f"fmt: {changed} file(s) rewritten, {len(items)} item(s) indexed")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    source = _worktree(args.backlog_dir)
    schema = load_schema(source)
    project_cfg = load_project_config(source)
    items, errors = validate_items(source, schema, project_cfg)
    done_entries, done_errors = validate_done_entries(source, load_done_schema(source))
    errors += done_errors
    errors += validate_index(source, items)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print(f"validate: {len(errors)} error(s)", file=sys.stderr)
        return 2
    print(f"validate: OK ({len(items)} items, {len(done_entries)} done entries)")
    return 0


# ---------------------------------------------------------------------------
# Commands: sync / build / serve (hub host)

def cmd_sync(args: argparse.Namespace) -> int:
    cfg = load_hub_config(args.config)
    mirror = cfg["paths"]["mirror"]
    repo_url = cfg["project"]["repo_url"]
    mirror.parent.mkdir(parents=True, exist_ok=True)
    if not mirror.exists():
        result = run(["git", "clone", "--mirror", repo_url, str(mirror)])
    else:
        result = run(["git", f"--git-dir={mirror}", "fetch", "--prune", "origin", "+refs/heads/*:refs/heads/*"])
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        return 1
    print(f"sync: mirror up to date at {mirror}")
    return 0


def gh_json(args: list[str]) -> tuple[list[dict[str, Any]], str | None]:
    if shutil.which("gh") is None:
        return [], "gh is not installed"
    result = run(["gh", *args])
    if result.returncode != 0:
        return [], result.stderr.strip() or f"gh {args[0]} {args[1]} failed"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as exc:
        return [], f"gh returned invalid JSON: {exc}"


def load_prs(github_repo: str | None) -> tuple[list[dict[str, Any]], str | None]:
    if not github_repo:
        return [], None
    return gh_json(
        ["pr", "list", "--repo", github_repo, "--state", "open",
         "--json", "number,title,headRefName,baseRefName,isDraft,updatedAt,url", "--limit", "100"]
    )


def load_feedback_issues(github_repo: str | None) -> tuple[list[dict[str, Any]], str | None]:
    if not github_repo:
        return [], None
    return gh_json(
        ["issue", "list", "--repo", github_repo, "--state", "open",
         "--label", FEEDBACK_LABEL, "--json", "number,title,url,updatedAt", "--limit", "100"]
    )


def match_prs(
    items: list[dict[str, Any]], prs: list[dict[str, Any]]
) -> tuple[dict[str, list[dict[str, Any]]], dict[int, list[str]]]:
    """Join open PRs to backlog items via links.prs plus item-id mentions in
    PR branch names and titles. Returns (item_id -> PRs, pr_number -> item ids)."""
    by_number = {pr["number"]: pr for pr in prs}
    item_prs: dict[str, list[dict[str, Any]]] = {}
    pr_items: dict[int, list[str]] = {number: [] for number in by_number}
    for item in items:
        matched: dict[int, dict[str, Any]] = {}
        for number in (item.get("links") or {}).get("prs", []):
            if number in by_number:
                matched[number] = by_number[number]
        needle = item["id"].lower()
        for pr in prs:
            haystack = f"{pr.get('headRefName', '')} {pr.get('title', '')}".lower()
            if needle in haystack:
                matched[pr["number"]] = pr
        if matched:
            item_prs[item["id"]] = [matched[number] for number in sorted(matched)]
            for number in matched:
                pr_items[number].append(item["id"])
    return item_prs, pr_items


def build_state_key(project: dict[str, Any], commit: str, github: dict[str, Any]) -> str:
    """Everything a release depends on: repo state, GitHub data, tool, project
    config. If this key is unchanged since the last successful build, rendering
    again would produce an identical release."""
    return canonical_json({
        "commit": commit,
        "github": github,
        "project": project,
        "tool": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    })


def prune_releases(releases_dir: Path, current: Path, keep: int) -> None:
    if keep <= 0:
        return
    releases = sorted(p for p in releases_dir.iterdir() if p.is_dir())
    for old in releases[:-keep]:
        if old != current:
            shutil.rmtree(old, ignore_errors=True)


def cmd_build(args: argparse.Namespace) -> int:
    cfg = load_hub_config(args.config)
    project = cfg["project"]
    paths = cfg["paths"]

    if not paths["mirror"].exists():
        print("build: mirror is not initialized (run: hub.py sync)", file=sys.stderr)
        return 1
    source = MirrorSource(paths["mirror"], project["backlog_ref"], project["backlog_dir"])
    commit = source.resolve_commit()
    if commit is None:
        print(f"build: ref {project['backlog_ref']} not found in mirror", file=sys.stderr)
        return 1

    prs, pr_error = load_prs(project["github_repo"])
    issues, issue_error = load_feedback_issues(project["github_repo"])
    github = {"issue_error": issue_error, "issues": issues, "pr_error": pr_error, "prs": prs}

    state_key = build_state_key(project, commit, github)
    state_path = cfg["paths"]["cache"] / "last_build.json"
    if (
        not args.force
        and state_path.is_file()
        and state_path.read_text(encoding="utf-8") == state_key
        and paths["public"].exists()
    ):
        print(f"build: no changes at {project['backlog_ref']} ({commit[:10]}); keeping current release")
        return 0

    schema = load_schema(source)
    project_cfg = load_project_config(source)
    items, errors = validate_items(source, schema, project_cfg)
    done_entries, done_errors = validate_done_entries(source, load_done_schema(source))
    errors += done_errors
    errors += validate_index(source, items)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print("build: refusing to render an invalid backlog; previous release stays live", file=sys.stderr)
        return 2

    paths["cache"].mkdir(parents=True, exist_ok=True)
    paths["releases"].mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    release = paths["releases"] / stamp
    if release.exists():
        release = paths["releases"] / f"{stamp}-{os.getpid()}"

    render_site(cfg, items, done_entries, commit, github, release)

    tmp_link = paths["root"] / "public.next"
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(release, target_is_directory=True)
    os.replace(tmp_link, paths["public"])
    state_path.write_text(state_key, encoding="utf-8")
    prune_releases(paths["releases"], release, cfg["build"]["releases_keep"])
    print(f"build: {len(items)} items at {project['backlog_ref']} ({commit[:10]}) -> {paths['public']}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = load_hub_config(args.config)
    public = cfg["paths"]["public"]
    if not public.exists():
        print("serve: nothing built yet (run: hub.py build)", file=sys.stderr)
        return 1
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(public))
    address = (cfg["server"]["host"], cfg["server"]["port"])
    print(f"serve: http://{address[0]}:{address[1]} -> {public}")
    http.server.ThreadingHTTPServer(address, handler).serve_forever()
    return 0


# ---------------------------------------------------------------------------
# Rendering

def h(value: Any) -> str:
    return html.escape(str(value), quote=True)


def paragraphs(values: list[str]) -> str:
    return "".join(f"<p>{h(p)}</p>" for p in values)


STALE_AFTER_MINUTES = 15

STALE_SCRIPT = """
<script>
(function () {
  const banner = document.getElementById("stale-banner");
  const generated = Date.parse(banner ? banner.dataset.generatedAt : "");
  if (!banner || !isFinite(generated)) return;
  const limit = Number(banner.dataset.staleAfter);
  function check() {
    const minutes = Math.round((Date.now() - generated) / 60000);
    if (minutes < limit) return;
    banner.textContent = "This snapshot is " + minutes + " minutes old — sync or build may be failing.";
    banner.hidden = false;
  }
  check();
  setInterval(check, 60000);
})();
</script>
"""


def page(project_name: str, title: str, body: str, *, active: str, has_prs: bool, generated_at: str = "", css_version: str = "") -> str:
    links = [
        ("home", "index.html", "Dashboard"),
        ("backlog", "backlog.html", "Backlog"),
        ("done", "done.html", "Done"),
    ]
    if has_prs:
        links.append(("prs", "prs.html", "PRs"))
    links.append(("data", "data/index.json", "JSON"))
    nav = "".join(
        f'<a class="{"active" if key == active else ""}" href="{href}">{label}</a>'
        for key, href, label in links
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h(title)} · {h(project_name)} Backlog Hub</title>
<link rel="stylesheet" href="assets/styles.css{h(css_version)}">
</head>
<body>
<header>
  <div><strong>{h(project_name)} Backlog Hub</strong><span>read-only · git is the source of truth</span></div>
  <nav>{nav}</nav>
</header>
<main>
<div id="stale-banner" class="warn" data-generated-at="{h(generated_at)}" data-stale-after="{STALE_AFTER_MINUTES}" hidden></div>
{body}</main>
{STALE_SCRIPT}</body>
</html>
"""


def card(title: str, content: str, meta: str = "") -> str:
    meta_html = f'<div class="meta">{meta}</div>' if meta else ""
    return f'<article class="card"><h2>{h(title)}</h2>{meta_html}{content}</article>'


CSS = (
    ':root{color-scheme:light dark;--background:#fafafa;--foreground:#09090b;--card:#fff;--muted:#71717a;'
    '--muted-bg:#f4f4f5;--border:#e4e4e7;--ring:#18181b;--accent:#f4f4f5;--accent-fg:#18181b;'
    '--surface:#fff;--surface-hover:#f4f4f5;--label:#52525b;--link:#0f766e;--row-hover:#f8fafc;'
    '--on-strong:#fff;--header-bg:rgba(250,250,250,.92);'
    '--warn-border:#f59e0b;--warn-bg:#fffbeb;--warn-fg:#92400e;'
    '--risk-high-border:#fecaca;--risk-high-bg:#fef2f2;--risk-high-fg:#991b1b;'
    '--risk-medium-border:#fde68a;--risk-medium-bg:#fffbeb;--risk-medium-fg:#92400e;'
    '--risk-low-border:#bbf7d0;--risk-low-bg:#f0fdf4;--risk-low-fg:#166534;'
    '--radius:8px;--shadow:0 1px 2px rgba(24,24,27,.04)}'
    '@media(prefers-color-scheme:dark){:root{--background:#0c0c0e;--foreground:#fafafa;--card:#161618;--muted:#a1a1aa;'
    '--muted-bg:#232326;--border:#2e2e33;--ring:#d4d4d8;--accent:#232326;--accent-fg:#fafafa;'
    '--surface:#1c1c1f;--surface-hover:#28282c;--label:#a1a1aa;--link:#2dd4bf;--row-hover:#1f1f23;'
    '--on-strong:#0c0c0e;--header-bg:rgba(12,12,14,.92);'
    '--warn-border:#b45309;--warn-bg:#2a2010;--warn-fg:#fcd34d;'
    '--risk-high-border:#7f1d1d;--risk-high-bg:#2a1215;--risk-high-fg:#fca5a5;'
    '--risk-medium-border:#78350f;--risk-medium-bg:#271d0b;--risk-medium-fg:#fcd34d;'
    '--risk-low-border:#14532d;--risk-low-bg:#0f2018;--risk-low-fg:#86efac;'
    '--shadow:0 1px 2px rgba(0,0,0,.4)}}'
    '*{box-sizing:border-box}'
    'body{margin:0;background:var(--background);color:var(--foreground);font-family:-apple-system,BlinkMacSystemFont,'
    '"Segoe UI",sans-serif;line-height:1.45;-webkit-font-smoothing:antialiased}'
    'header{position:sticky;top:0;z-index:20;background:var(--header-bg);backdrop-filter:saturate(180%) blur(12px);'
    'border-bottom:1px solid var(--border);padding:12px 24px;display:flex;justify-content:space-between;'
    'gap:16px;align-items:center}header strong{font-size:14px;font-weight:650}header span{display:block;color:var(--muted);font-size:12px}'
    'nav{display:flex;gap:6px;flex-wrap:wrap}nav a{color:var(--foreground);text-decoration:none;border:1px solid transparent;'
    'padding:6px 10px;border-radius:6px;background:transparent;font-size:13px;font-weight:500}'
    'nav a:hover{background:var(--accent)}nav a.active{background:var(--foreground);color:var(--on-strong);border-color:var(--foreground)}'
    'main{max-width:1320px;margin:0 auto;padding:24px}h1{font-size:28px;line-height:1.15;margin:0;font-weight:700}'
    '.page-heading{display:flex;justify-content:space-between;gap:18px;align-items:flex-end;margin:2px 0 18px}'
    '.page-heading p{margin:6px 0 0;max-width:760px}.heading-meta{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}'
    '.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}'
    '.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px;margin:0 0 14px;box-shadow:var(--shadow)}'
    '.card h2{font-size:17px;line-height:1.28;margin:0 0 10px;font-weight:650}.card p{margin:9px 0}'
    '.meta,.muted{color:var(--muted);font-size:13px}code{border:1px solid var(--border);border-radius:6px;background:var(--muted-bg);'
    'padding:1px 5px;font-size:.92em}.pill{display:inline-flex;align-items:center;border:1px solid var(--border);background:var(--surface);'
    'border-radius:6px;padding:2px 7px;margin:2px;font-size:12px;font-weight:500;line-height:1.45}'
    'a.pill{color:inherit;text-decoration:none}a.pill:hover{border-color:var(--ring)}'
    '.pill.stale{border-color:var(--warn-border);background:var(--warn-bg);color:var(--warn-fg)}'
    '.pill.in-flight{border-color:var(--link);color:var(--link)}'
    '.button{display:inline-flex;align-items:center;justify-content:center;height:32px;border:1px solid var(--border);'
    'border-radius:6px;background:var(--surface);color:var(--foreground);padding:0 10px;font:inherit;font-size:13px;font-weight:500;cursor:pointer;text-decoration:none}'
    '.button:hover{background:var(--surface-hover)}.button.primary{background:var(--foreground);color:var(--on-strong);border-color:var(--foreground)}.button:focus-visible,'
    '.control input:focus,.control select:focus{outline:2px solid var(--ring);outline-offset:2px}'
    '.toolbar{display:grid;grid-template-columns:minmax(220px,1.6fr) repeat(4,minmax(112px,1fr)) minmax(150px,1.15fr) 84px auto;'
    'gap:8px;align-items:end;margin:0 0 12px}.control{display:grid;gap:4px}.control label{color:var(--label);font-size:11px;font-weight:600}'
    '.control input,.control select{height:32px;width:100%;border:1px solid var(--border);border-radius:6px;background:var(--surface);'
    'color:var(--foreground);padding:0 8px;font:inherit;font-size:13px}.toolbar-actions{display:flex;gap:6px;align-items:end;justify-content:flex-end}'
    '.risk-high{border-color:var(--risk-high-border);background:var(--risk-high-bg);color:var(--risk-high-fg)}'
    '.risk-medium{border-color:var(--risk-medium-border);background:var(--risk-medium-bg);color:var(--risk-medium-fg)}'
    '.risk-low{border-color:var(--risk-low-border);background:var(--risk-low-bg);color:var(--risk-low-fg)}'
    '.backlog-layout{display:grid;grid-template-columns:minmax(0,1.12fr) minmax(380px,.88fr);gap:16px;align-items:start}'
    '.backlog-shell.details-closed .backlog-layout{grid-template-columns:1fr}.backlog-shell.details-closed .detail-pane{display:none}'
    '.backlog-list,.detail-pane{min-width:0}.backlog-list{border:1px solid var(--border);border-radius:var(--radius);'
    'background:var(--card);box-shadow:var(--shadow);overflow:auto}.detail-pane{position:sticky;top:78px}'
    '.detail-header{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 0 8px}.detail-header h2{font-size:13px;margin:0}'
    '.detail-pane .empty{border:1px dashed var(--border);border-radius:var(--radius);padding:18px;background:var(--card)}'
    '.item-row{cursor:pointer;outline:none}.item-row[hidden]{display:none!important}.item-row:hover{background:var(--row-hover)}.item-row.active{background:var(--accent)}'
    '.item-row:focus-visible{box-shadow:inset 0 0 0 2px var(--ring)}.item-row.active td:first-child{box-shadow:inset 3px 0 0 var(--ring)}'
    '.item-title-link{color:var(--foreground);text-decoration:none;font-weight:500}.item-title-link:hover{text-decoration:underline;text-underline-offset:3px}'
    '.item-detail{scroll-margin-top:92px}.item-detail h3{border-top:1px solid var(--border);padding-top:12px}'
    '.note-human{border-left:3px solid var(--ring);padding-left:9px;font-size:13px}'
    '.item-actions{display:flex;gap:6px;flex-wrap:wrap}'
    '.js .item-detail{display:none}.js .item-detail.active{display:block}'
    'table{width:100%;table-layout:fixed;border-collapse:separate;border-spacing:0;background:transparent}'
    'th,td{padding:10px 12px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}'
    '.risk-col,.risk-cell{white-space:nowrap;text-align:right;padding-left:4px;padding-right:8px}'
    '.risk-cell .pill{margin:0;padding:1px 6px}'
    'th{position:sticky;top:0;z-index:1;background:var(--background);color:var(--label);font-size:12px;font-weight:600}'
    'tbody tr:last-child td{border-bottom:0}a{color:var(--link)}'
    '.backlog-shell:not(.details-closed) table,.backlog-shell:not(.details-closed) tbody{display:block}'
    '.backlog-shell:not(.details-closed) thead,.backlog-shell:not(.details-closed) colgroup{display:none}'
    '.backlog-shell:not(.details-closed) .item-row{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));'
    'gap:5px 10px;padding:12px;border-bottom:1px solid var(--border)}'
    '.backlog-shell:not(.details-closed) .item-row.active{box-shadow:inset 3px 0 0 var(--ring)}'
    '.backlog-shell:not(.details-closed) .item-row td{display:block;min-width:0;border-bottom:0;padding:0;font-size:13px}'
    '.backlog-shell:not(.details-closed) .item-row td:nth-child(2){grid-column:1/-1;order:1;font-size:14px;line-height:1.35}'
    '.backlog-shell:not(.details-closed) .item-row td:nth-child(1){grid-column:1/-1;order:2;color:var(--muted)}'
    '.backlog-shell:not(.details-closed) .item-row td:first-child{box-shadow:none}.backlog-shell:not(.details-closed) .item-row td:nth-child(1) strong{display:block;font-size:12px}'
    '.backlog-shell:not(.details-closed) .item-row td:nth-child(n+3){order:3;padding-top:4px;color:var(--label);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    '.backlog-shell:not(.details-closed) .risk-cell{text-align:right}.backlog-shell:not(.details-closed) .muted{font-size:12px}'
    '.empty-state{padding:18px;border-top:1px solid var(--border);background:var(--card);color:var(--muted);font-size:13px}'
    '.warn{border-left:4px solid var(--warn-border);padding:10px 12px;background:var(--warn-bg);color:var(--warn-fg);border-radius:0 8px 8px 0}'
    '#stale-banner{margin:0 0 14px}'
    '.dash-queue{margin-top:0}.queue-list{margin:0;padding:0;list-style:none}'
    '.queue-list li{display:flex;gap:10px;align-items:baseline;padding:7px 0;border-bottom:1px solid var(--border)}'
    '.queue-list li:last-child{border-bottom:0}.queue-list .pill{margin-left:auto}'
    'h3{font-size:12px;margin:14px 0 4px;text-transform:uppercase;color:var(--label);font-weight:650}'
    '@media(max-width:900px){header{align-items:flex-start;flex-direction:column}.page-heading{display:block}'
    '.heading-meta{justify-content:flex-start;margin-top:10px}.toolbar{grid-template-columns:1fr 1fr}.toolbar .control:first-child{grid-column:1/-1}'
    '.toolbar-actions{grid-column:1/-1;justify-content:flex-start}.backlog-layout{grid-template-columns:1fr}.detail-pane{position:static}}'
)


STALE_AFTER_DAYS = 45

FEEDBACK_LABEL = "backlog-feedback"


def feedback_issue_url(github_repo: str, item_id: str, action: str, payload_hint: str) -> str:
    """Prefilled GitHub issue link - the hub's only 'write' surface, which is
    none at all: the human authenticates as themselves, and the change is
    applied later as a normal commit in the project repo (see templates/AGENTS.md)."""
    query = urllib.parse.urlencode({
        "labels": FEEDBACK_LABEL,
        "title": f"[{FEEDBACK_LABEL}] {item_id}: {action}",
        "body": f"item: {item_id}\naction: {action}\npayload: {payload_hint}\n",
    })
    return f"https://github.com/{github_repo}/issues/new?{query}"


def item_detail(
    item: dict[str, Any],
    *,
    active: bool = False,
    github_repo: str | None = None,
    today: dt.date | None = None,
    open_prs: list[dict[str, Any]] | None = None,
) -> str:
    body = paragraphs(item["problem"])
    for section in ["value", "scope", "validation", "trigger"]:
        if item.get(section):
            body += f"<h3>{h(section)}</h3>" + paragraphs(item[section])
    risk = item["risk"]
    body += "<h3>risk</h3>"
    body += f"<p><span class=\"pill risk-{h(risk['level'])}\">{h(risk['level'])}</span>" + "".join(
        f"<span class=pill>{h(d)}</span>" for d in risk["dimensions"]
    ) + "</p>"
    if risk.get("rollback"):
        body += f"<p><strong>Rollback:</strong> {h(risk['rollback'])}</p>"
    if item.get("notes"):
        def note_html(note: dict[str, Any]) -> str:
            author = note.get("author", "")
            label = h(note["date"]) + (f" · {h(author)}" if author else "")
            css_class = "note-human" if author.startswith("human") else "muted"
            return f'<p class="{css_class}"><span class=muted>{label}</span> — {h(note["text"])}</p>'

        body += "<h3>notes</h3>" + "".join(note_html(note) for note in item["notes"])
    links = item.get("links") or {}
    if links.get("prs") or links.get("related_ids"):
        def pr_pill(number: Any) -> str:
            if github_repo:
                return f'<a class=pill href="https://github.com/{h(github_repo)}/pull/{h(number)}">PR #{h(number)}</a>'
            return f"<span class=pill>PR #{h(number)}</span>"

        body += "<h3>links</h3><p>" + "".join(
            pr_pill(n) for n in links.get("prs", [])
        ) + "".join(f"<span class=pill>{h(r)}</span>" for r in links.get("related_ids", [])) + "</p>"
    if open_prs:
        body += "<h3>in flight</h3><p>" + "".join(
            f'<a class="pill in-flight" href="{h(pr["url"])}">#{h(pr["number"])} {h(pr["title"])}</a>'
            for pr in open_prs
        ) + "</p>"
    created_label = item["created"]
    stale = False
    if today is not None:
        age = (today - dt.date.fromisoformat(item["created"])).days
        if age >= 0:
            created_label = f"{item['created']} ({age}d)"
        last_activity = max([item["created"]] + [n["date"] for n in item.get("notes", [])])
        stale = (today - dt.date.fromisoformat(last_activity)).days > STALE_AFTER_DAYS
    meta_pills = [f"<span class=pill>{h(item[key])}</span>" for key in ["type", "area", "priority", "status"]]
    meta_pills.append(f"<span class=pill>{h(created_label)}</span>")
    if stale:
        meta_pills.append('<span class="pill stale" title="no note or edit in over 45 days">stale</span>')
    meta = " ".join(meta_pills)
    if github_repo:
        body += "<h3>feedback</h3><p class=item-actions>" + "".join(
            f'<a class=button href="{h(feedback_issue_url(github_repo, item["id"], action, hint))}">{h(label)}</a>'
            for action, label, hint in [
                ("guidance", "Add guidance", "<write your guidance for the agent here>"),
                ("set-priority", "Change priority", "now | next | later (keep exactly one)"),
                ("archive", "Archive", "<optional reason>"),
            ]
        ) + "</p><p class=muted>Opens a prefilled GitHub issue; the change lands as a commit in the project repo.</p>"
    active_class = " active" if active else ""
    return (
        f'<article id="{h(item["id"])}" class="card item-detail{active_class}" data-item-id="{h(item["id"])}">'
        f"<h2>{h(item['id'])} · {h(item['title'])}</h2><div class=\"meta\">{meta}</div>{body}</article>"
    )


def done_search_text(entry: dict[str, Any]) -> str:
    values = [
        entry["id"],
        entry["date"],
        entry["title"],
        *entry["summary"],
        *entry["validation"],
        *entry.get("changed", []),
    ]
    if entry.get("item_id"):
        values.append(entry["item_id"])
    values.extend(entry.get("followup_ids", []))
    if entry.get("source"):
        values.append(entry["source"])
    return " ".join(values).lower()


def done_detail(entry: dict[str, Any]) -> str:
    body = paragraphs(entry["summary"])
    body += "<h3>validation</h3>" + paragraphs(entry["validation"])
    if entry.get("changed"):
        body += "<h3>changed</h3><ul>" + "".join(f"<li>{h(c)}</li>" for c in entry["changed"]) + "</ul>"
    pills = []
    if entry.get("item_id"):
        pills.append(f"<span class=pill>closes {h(entry['item_id'])}</span>")
    pills.extend(f"<span class=pill>follow-up {h(f)}</span>" for f in entry.get("followup_ids", []))
    if pills:
        body += "<h3>links</h3><p>" + "".join(pills) + "</p>"
    meta = f"<span class=pill>{h(entry['date'])}</span>"
    if entry.get("source"):
        meta += f"<span class=pill>{h(entry['source'])}</span>"
    return (
        f'<article class="card done-entry" data-month="{h(entry["date"][:7])}" data-done-search="{h(done_search_text(entry))}">'
        f"<h2>{h(entry['title'])}</h2><div class=\"meta\">{meta}</div>{body}</article>"
    )


def render_site(
    cfg: dict[str, Any],
    items: list[dict[str, Any]],
    done_entries: list[dict[str, Any]],
    commit: str,
    github: dict[str, Any],
    out: Path,
) -> None:
    project = cfg["project"]
    name = project["name"]
    has_prs = bool(project["github_repo"])
    prs = github["prs"]
    pr_error = github["pr_error"]
    issues = github["issues"]
    issue_error = github["issue_error"]
    item_prs, pr_items = match_prs(items, prs)

    (out / "assets").mkdir(parents=True, exist_ok=True)
    (out / "data").mkdir(parents=True, exist_ok=True)
    (out / "assets/styles.css").write_text(CSS, encoding="utf-8")

    generated_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    css_version = "?v=" + generated_at.replace("-", "").replace(":", "").replace("+", "")
    today = dt.date.fromisoformat(generated_at[:10])
    index_data = {
        "backlog": items,
        "commit": commit,
        "done": done_entries,
        "feedback_error": issue_error,
        "feedback_issues": issues,
        "generated_at": generated_at,
        "pr_error": pr_error,
        "prs": prs,
        "ref": project["backlog_ref"],
        "schema_version": 1,
    }
    (out / "data/index.json").write_text(canonical_json(index_data), encoding="utf-8")
    (cfg["paths"]["cache"] / "index.json").write_text(canonical_json(index_data), encoding="utf-8")

    by_status: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    high_risk = 0
    for item in items:
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1
        by_priority[item["priority"]] = by_priority.get(item["priority"], 0) + 1
        high_risk += item["risk"]["level"] == "high"

    def pills(counter: dict[str, int]) -> str:
        return "".join(f"<span class=pill>{h(k)}: {v}</span>" for k, v in sorted(counter.items()))

    active_items = [i for i in items if i["status"] != "archived"]
    archived_count = len(items) - len(active_items)
    latest_done = f"latest: {h(done_entries[0]['date'])}" if done_entries else "no entries yet"
    home = [
        card("Backlog", f"<p>{len(active_items)} active items from <code>{h(project['backlog_dir'])}</code>.</p><p>{pills(by_status)}</p><p>{pills(by_priority)}</p><p><a href=backlog.html>Open backlog →</a></p>"),
        card("Risk", f"<p>{high_risk} high-risk item(s).</p><p><a href=\"backlog.html?risk=high\">View high-risk →</a></p>"),
        card("Done", f"<p>{len(done_entries)} completed-work entries ({latest_done}).</p><p><a href=done.html>View done →</a></p>"),
        card("Baseline", f"<p><code>{h(project['backlog_ref'])}</code> @ <code>{h(commit[:10])}</code></p><p class=muted>generated {h(generated_at)}</p>"),
    ]
    if has_prs:
        linked_pr_count = sum(1 for ids in pr_items.values() if ids)
        home.insert(2, card(
            "Open PRs",
            f"<p>{len(prs)} open pull requests, {linked_pr_count} linked to backlog items.</p>"
            + (f"<p class=warn>{h(pr_error)}</p>" if pr_error else "")
            + "<p><a href=prs.html>View PRs →</a></p>",
        ))
        issue_links = "".join(
            f'<p><a href="{h(i["url"])}">#{h(i["number"])}</a> {h(i["title"])}</p>' for i in issues[:5]
        )
        home.insert(3, card(
            "Feedback",
            f"<p>{len(issues)} open <code>{FEEDBACK_LABEL}</code> issue(s) waiting to be applied.</p>"
            + issue_links
            + (f"<p class=warn>{h(issue_error)}</p>" if issue_error else ""),
        ))
    now_items = [i for i in active_items if i["priority"] == "now"]
    queue_rows = "".join(
        f'<li><a class=item-title-link href="backlog.html#{h(i["id"])}">{h(i["id"])}</a>'
        f'<span>{h(i["title"])}</span>'
        f'<span class="pill risk-{h(i["risk"]["level"])}">{h(i["risk"]["level"])}</span></li>'
        for i in now_items[:10]
    )
    queue_card = card(
        "Now queue",
        f"<ul class=queue-list>{queue_rows}</ul>" if queue_rows else '<p class=muted>No items with priority <code>now</code>.</p>',
    )
    (out / "index.html").write_text(
        page(
            name,
            "Dashboard",
            f"<section class=grid>{''.join(home)}</section><section class=dash-queue>{queue_card}</section>",
            active="home",
            has_prs=has_prs,
            generated_at=generated_at,
            css_version=css_version,
        ),
        encoding="utf-8",
    )

    rows = []
    for idx, item in enumerate(items):
        risk_class = f"risk-{h(item['risk']['level'])}"
        active_class = " active" if idx == 0 else ""
        in_flight = item_prs.get(item["id"], [])
        in_flight_badge = "".join(
            f'<span class="pill in-flight" title="{h(pr["title"])}">PR #{h(pr["number"])}</span>' for pr in in_flight
        )
        search_text = " ".join(
            [
                item["id"],
                item["title"],
                item["type"],
                item["area"],
                item["priority"],
                item["status"],
                item["risk"]["level"],
                item["_path"],
                *(["in-flight"] if in_flight else []),
                *item["problem"],
                *item["value"],
                *item["scope"],
            ]
        )
        rows.append(
            f"<tr class=\"item-row{active_class}\" data-item-id=\"{h(item['id'])}\" data-order=\"{idx}\""
            f" data-title=\"{h(item['title'].lower())}\" data-type=\"{h(item['type'])}\""
            f" data-area=\"{h(item['area'])}\" data-priority=\"{h(item['priority'])}\""
            f" data-status=\"{h(item['status'])}\" data-risk=\"{h(item['risk']['level'])}\""
            f" data-created=\"{h(item['created'])}\" data-search=\"{h(search_text.lower())}\" tabindex=\"0\">"
            f"<td><strong><a class=\"item-title-link\" href=\"#{h(item['id'])}\">{h(item['id'])}</a></strong>"
            f"<div class=muted>{h(item['_path'])}</div></td>"
            f"<td><a class=\"item-title-link\" href=\"#{h(item['id'])}\">{h(item['title'])}</a>{in_flight_badge}</td>"
            f"<td>{h(item['type'])}</td><td>{h(item['area'])}</td>"
            f"<td>{h(item['priority'])}</td><td>{h(item['status'])}</td>"
            f"<td class=risk-cell><span class=\"pill {risk_class}\">{h(item['risk']['level'])}</span></td></tr>"
        )
    detail_cards = "".join(
        item_detail(
            item,
            active=idx == 0,
            github_repo=project["github_repo"],
            today=today,
            open_prs=item_prs.get(item["id"]),
        )
        for idx, item in enumerate(items)
    )
    detail_pane = (
        '<div class=detail-header><h2>Details</h2><button class=button id=close-details type=button>Close</button></div>'
        + (detail_cards or '<div class="empty muted">No open backlog items.</div>')
    )

    def options(values: list[str], label: str = "All") -> str:
        return f'<option value="">{h(label)}</option>' + "".join(
            f'<option value="{h(value)}">{h(value)}</option>' for value in values
        )

    type_options = options(sorted({item["type"] for item in items}))
    area_options = options(sorted({item["area"] for item in items}))
    priority_options = options(["now", "next", "later"])
    status_options = options(["open", "in-progress", "blocked", "archived"], label="Active")
    risk_options = options(["high", "medium", "low"])
    backlog_script = """
<script>
document.documentElement.classList.add("js");
(function () {
  const shell = document.querySelector(".backlog-shell");
  const form = document.getElementById("backlog-controls");
  const tbody = document.querySelector(".backlog-list tbody");
  const rows = Array.from(document.querySelectorAll(".item-row"));
  const details = Array.from(document.querySelectorAll(".item-detail"));
  const emptyState = document.getElementById("empty-filter-state");
  const closeDetails = document.getElementById("close-details");
  const viewToggle = document.getElementById("toggle-details");
  const clearFilters = document.getElementById("clear-filters");
  const controls = {
    query: document.getElementById("filter-query"),
    type: document.getElementById("filter-type"),
    area: document.getElementById("filter-area"),
    priority: document.getElementById("filter-priority"),
    status: document.getElementById("filter-status"),
    risk: document.getElementById("filter-risk"),
    sort: document.getElementById("sort-by"),
    dir: document.getElementById("sort-dir"),
  };
  const priorityRank = { now: 0, next: 1, later: 2 };
  const riskRank = { high: 0, medium: 1, low: 2 };
  const paramMap = { q: "query", type: "type", area: "area", priority: "priority", status: "status", risk: "risk", sort: "sort", dir: "dir" };
  let detailsOpen = true;
  if (!rows.length || !details.length || !shell || !tbody) return;
  form?.addEventListener("submit", (event) => event.preventDefault());

  const initialParams = new URLSearchParams(location.search);
  Object.entries(paramMap).forEach(([param, key]) => {
    const value = initialParams.get(param);
    if (value !== null && controls[key]) controls[key].value = value;
  });

  function syncUrl() {
    const params = new URLSearchParams();
    Object.entries(paramMap).forEach(([param, key]) => {
      const value = (controls[key]?.value || "").trim();
      const isDefault = value === "" || (key === "sort" && value === "order") || (key === "dir" && value === "asc");
      if (!isDefault) params.set(param, value);
    });
    const query = params.toString();
    history.replaceState(null, "", location.pathname + (query ? "?" + query : "") + location.hash);
  }

  function clearActive(updateHash) {
    rows.forEach((row) => {
      row.classList.remove("active");
      row.setAttribute("aria-selected", "false");
    });
    details.forEach((detail) => detail.classList.remove("active"));
    if (updateHash) history.replaceState(null, "", location.pathname + location.search);
  }

  function setDetailsOpen(open) {
    detailsOpen = open;
    shell.classList.toggle("details-closed", !open);
    if (viewToggle) viewToggle.textContent = open ? "List" : "Details";
    if (!open) clearActive(true);
  }

  function showDetail(id, updateHash) {
    setDetailsOpen(true);
    rows.forEach((row) => {
      const selected = row.dataset.itemId === id;
      row.classList.toggle("active", selected);
      row.setAttribute("aria-selected", selected ? "true" : "false");
    });
    details.forEach((detail) => detail.classList.toggle("active", detail.dataset.itemId === id));
    if (updateHash) history.replaceState(null, "", "#" + encodeURIComponent(id));
  }

  function rowValue(row, key) {
    if (key === "priority") return priorityRank[row.dataset.priority] ?? 99;
    if (key === "risk") return riskRank[row.dataset.risk] ?? 99;
    if (key === "order") return Number(row.dataset.order);
    return row.dataset[key] || "";
  }

  function matchesFilters(row) {
    const query = (controls.query?.value || "").trim().toLowerCase();
    const status = controls.status?.value || "";
    return (!query || row.dataset.search.includes(query))
      && (!controls.type?.value || row.dataset.type === controls.type.value)
      && (!controls.area?.value || row.dataset.area === controls.area.value)
      && (!controls.priority?.value || row.dataset.priority === controls.priority.value)
      && (status ? row.dataset.status === status : row.dataset.status !== "archived")
      && (!controls.risk?.value || row.dataset.risk === controls.risk.value);
  }

  function applyListState() {
    const sortKey = controls.sort?.value || "order";
    const sortDir = controls.dir?.value === "desc" ? -1 : 1;
    const sortedRows = rows.slice().sort((a, b) => {
      const aValue = rowValue(a, sortKey);
      const bValue = rowValue(b, sortKey);
      if (typeof aValue === "number" && typeof bValue === "number") return (aValue - bValue) * sortDir;
      return String(aValue).localeCompare(String(bValue), undefined, { numeric: true }) * sortDir;
    });
    sortedRows.forEach((row) => tbody.appendChild(row));
    const visibleRows = [];
    rows.forEach((row) => {
      const visible = matchesFilters(row);
      row.hidden = !visible;
      if (visible) visibleRows.push(row);
    });
    if (emptyState) emptyState.hidden = visibleRows.length > 0;
    const active = rows.find((row) => row.classList.contains("active") && !row.hidden);
    if (detailsOpen && !active) {
      if (visibleRows[0]) showDetail(visibleRows[0].dataset.itemId, false);
      else clearActive(false);
    }
    syncUrl();
  }

  rows.forEach((row) => {
    row.addEventListener("click", () => showDetail(row.dataset.itemId, true));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        showDetail(row.dataset.itemId, true);
      }
    });
  });

  Object.values(controls).forEach((control) => control?.addEventListener("input", applyListState));
  Object.values(controls).forEach((control) => control?.addEventListener("change", applyListState));
  closeDetails?.addEventListener("click", () => setDetailsOpen(false));
  viewToggle?.addEventListener("click", () => {
    if (detailsOpen) {
      setDetailsOpen(false);
      return;
    }
    const requested = decodeURIComponent(location.hash.slice(1));
    const requestedRow = rows.find((row) => !row.hidden && row.dataset.itemId === requested);
    const firstVisible = rows.find((row) => !row.hidden);
    if (requestedRow || firstVisible) showDetail((requestedRow || firstVisible).dataset.itemId, true);
  });
  clearFilters?.addEventListener("click", () => {
    ["query", "type", "area", "priority", "status", "risk"].forEach((key) => {
      if (controls[key]) controls[key].value = "";
    });
    if (controls.sort) controls.sort.value = "order";
    if (controls.dir) controls.dir.value = "asc";
    applyListState();
  });

  function showFromHash() {
    const requested = decodeURIComponent(location.hash.slice(1));
    const requestedRow = rows.find((row) => !row.hidden && row.dataset.itemId === requested);
    const firstVisible = rows.find((row) => !row.hidden);
    if (requestedRow || firstVisible) showDetail((requestedRow || firstVisible).dataset.itemId, false);
  }

  window.addEventListener("hashchange", showFromHash);
  applyListState();
  if (location.hash) showFromHash();
  else showDetail(rows.find((row) => !row.hidden)?.dataset.itemId || details[0].dataset.itemId, false);
})();
</script>
"""
    backlog_body = (
        '<div class=page-heading><div>'
        f"<h1>Backlog</h1><p class=muted>Repo-canonical items read at <code>{h(project['backlog_ref'])}</code>"
        f" @ <code>{h(commit[:10])}</code>. Edits go through the project repo, not the hub.</p></div>"
        f"<div class=heading-meta><span class=pill>{len(active_items)} active</span>"
        + (f"<span class=pill>{archived_count} archived</span>" if archived_count else "")
        + f"<span class=pill>{h(project['backlog_ref'])}</span><span class=pill>{h(commit[:10])}</span></div></div>"
        "<section class=backlog-shell>"
        "<form class=toolbar id=backlog-controls>"
        '<div class=control><label for=filter-query>Search</label><input id=filter-query type=search autocomplete=off></div>'
        f'<div class=control><label for=filter-type>Type</label><select id=filter-type>{type_options}</select></div>'
        f'<div class=control><label for=filter-area>Area</label><select id=filter-area>{area_options}</select></div>'
        f'<div class=control><label for=filter-priority>Priority</label><select id=filter-priority>{priority_options}</select></div>'
        f'<div class=control><label for=filter-status>Status</label><select id=filter-status>{status_options}</select></div>'
        f'<div class=control><label for=filter-risk>Risk</label><select id=filter-risk>{risk_options}</select></div>'
        '<div class=control><label for=sort-by>Sort</label><select id=sort-by>'
        '<option value=order>Default</option><option value=priority>Priority</option><option value=risk>Risk</option>'
        '<option value=status>Status</option><option value=type>Type</option><option value=area>Area</option>'
        '<option value=created>Created</option><option value=title>Title</option></select></div>'
        '<div class=control><label for=sort-dir>Dir</label><select id=sort-dir>'
        '<option value=asc>Asc</option><option value=desc>Desc</option></select></div>'
        '<div class=toolbar-actions><button class=button id=clear-filters type=button>Reset</button>'
        '<button class="button primary" id=toggle-details type=button>List</button></div></form>'
        "<section class=backlog-layout><div class=backlog-list>"
        "<table><colgroup><col style=\"width:22%\"><col style=\"width:28%\"><col style=\"width:11%\">"
        "<col style=\"width:16%\"><col style=\"width:9%\"><col style=\"width:8%\"><col style=\"width:6%\"></colgroup>"
        "<thead><tr><th>ID</th><th>Title</th><th>Type</th><th>Area</th><th>Priority</th>"
        "<th>Status</th><th class=risk-col>Risk</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        '<div class=empty-state id=empty-filter-state hidden>No matching backlog items.</div></div>'
        f"<aside class=detail-pane>{detail_pane}</aside></section></section>"
        + backlog_script
    )
    (out / "backlog.html").write_text(
        page(name, "Backlog", backlog_body, active="backlog", has_prs=has_prs, generated_at=generated_at, css_version=css_version),
        encoding="utf-8",
    )

    month_options = options(sorted({e["date"][:7] for e in done_entries}, reverse=True))
    done_body = (
        "<h1>Done</h1><p class=muted>Completed-work records, newest first. "
        "Grouping is a render concern - storage stays one file per entry.</p>"
        "<form class=toolbar id=done-controls>"
        '<div class=control><label for=done-search>Search</label><input id=done-search type=search autocomplete=off></div>'
        f'<div class=control><label for=done-month>Month</label><select id=done-month>{month_options}</select></div>'
        '<div class=toolbar-actions><button class=button id=clear-done-search type=button>Reset</button></div></form>'
        '<div class=empty-state id=done-empty-state hidden>No matching done entries.</div>'
    )
    current_month = None
    for entry in done_entries:
        month = entry["date"][:7]
        if month != current_month:
            done_body += f'<h2 class=done-month data-month="{h(month)}">{h(month)}</h2>'
            current_month = month
        done_body += done_detail(entry)
    if not done_entries:
        done_body += "<p class=muted>No done entries yet.</p>"
    done_body += """
<script>
document.documentElement.classList.add("js");
(function () {
  const form = document.getElementById("done-controls");
  const search = document.getElementById("done-search");
  const monthSelect = document.getElementById("done-month");
  const reset = document.getElementById("clear-done-search");
  const entries = Array.from(document.querySelectorAll(".done-entry"));
  const months = Array.from(document.querySelectorAll(".done-month"));
  const empty = document.getElementById("done-empty-state");
  if (!search || !entries.length) return;

  function applyDoneSearch() {
    const query = search.value.trim().toLowerCase();
    const month = monthSelect ? monthSelect.value : "";
    let visibleCount = 0;
    entries.forEach((entry) => {
      const visible = (!query || entry.dataset.doneSearch.includes(query))
        && (!month || entry.dataset.month === month);
      entry.hidden = !visible;
      if (visible) visibleCount += 1;
    });
    months.forEach((month) => {
      let sibling = month.nextElementSibling;
      let hasVisibleEntry = false;
      while (sibling && !sibling.classList.contains("done-month")) {
        if (sibling.classList.contains("done-entry") && !sibling.hidden) {
          hasVisibleEntry = true;
          break;
        }
        sibling = sibling.nextElementSibling;
      }
      month.hidden = !hasVisibleEntry;
    });
    if (empty) empty.hidden = visibleCount > 0;
  }

  form?.addEventListener("submit", (event) => event.preventDefault());
  search.addEventListener("input", applyDoneSearch);
  monthSelect?.addEventListener("change", applyDoneSearch);
  reset?.addEventListener("click", () => {
    search.value = "";
    if (monthSelect) monthSelect.value = "";
    applyDoneSearch();
  });
  applyDoneSearch();
})();
</script>
"""
    (out / "done.html").write_text(
        page(name, "Done", done_body, active="done", has_prs=has_prs, generated_at=generated_at, css_version=css_version),
        encoding="utf-8",
    )

    if has_prs:
        matched_count = sum(1 for ids in pr_items.values() if ids)
        pr_rows = []
        for pr in sorted(prs, key=lambda p: (not pr_items.get(p["number"]), p["number"])):
            state = "draft" if pr.get("isDraft") else "ready"
            item_links = "".join(
                f'<a class=pill href="backlog.html#{h(item_id)}">{h(item_id)}</a>'
                for item_id in pr_items.get(pr["number"], [])
            ) or '<span class=muted>—</span>'
            pr_rows.append(
                f"<tr><td><a href=\"{h(pr['url'])}\">#{h(pr['number'])}</a></td><td>{h(pr['title'])}</td>"
                f"<td>{item_links}</td>"
                f"<td><code>{h(pr['headRefName'])}</code> → <code>{h(pr['baseRefName'])}</code></td>"
                f"<td>{h(state)}</td><td>{h(pr.get('updatedAt', ''))}</td></tr>"
            )
        pr_body = (
            "<h1>Open PRs</h1>"
            f"<p class=muted>{matched_count} of {len(prs)} open PRs reference backlog items"
            " (via <code>links.prs</code> or an item id in the branch name / title).</p>"
        )
        if pr_error:
            pr_body += f"<p class=warn>{h(pr_error)}</p>"
        pr_body += (
            "<table><thead><tr><th>PR</th><th>Title</th><th>Backlog items</th><th>Branch</th><th>State</th><th>Updated</th></tr></thead>"
            "<tbody>" + "".join(pr_rows) + "</tbody></table>"
        )
        (out / "prs.html").write_text(
            page(name, "PRs", pr_body, active="prs", has_prs=has_prs, generated_at=generated_at, css_version=css_version),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Self-test

SAMPLE_ITEM = {
    "schema_version": 1,
    "id": "FEAT-20260703-sample-item",
    "title": "Self-test sample item",
    "type": "feature",
    "area": "integrations",
    "status": "open",
    "priority": "next",
    "created": "2026-07-03",
    "source": "hub self-test fixture",
    "risk": {
        "level": "high",
        "dimensions": ["external-integration", "data-exposure"],
        "rollback": "Revert the sample.",
    },
    "problem": ["Sample problem paragraph."],
    "value": ["Sample value paragraph."],
    "scope": ["Sample scope paragraph."],
    "validation": ["Sample validation paragraph."],
    "notes": [{"date": "2026-07-03", "text": "Sample note."}],
}


SAMPLE_DONE = {
    "schema_version": 1,
    "id": "DONE-20260703-sample-entry",
    "date": "2026-07-03",
    "title": "Self-test sample done entry",
    "summary": ["Sample summary paragraph."],
    "validation": ["Sample validation evidence."],
    "changed": ["src/example.py"],
    "item_id": "FEAT-20260703-sample-item",
    "followup_ids": ["FIX-20260704-follow-up"],
}


class _MemorySource:
    def __init__(self, files: dict[str, str]):
        self.files = files
        self.label = "memory"

    def list_files(self) -> list[str]:
        return sorted(self.files)

    def read(self, rel_path: str) -> str:
        return self.files[rel_path]


def cmd_self_test(_args: argparse.Namespace) -> int:
    schema = parse_json(str(BUNDLED_SCHEMA), BUNDLED_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)

    good = canonical_json(SAMPLE_ITEM)
    assert canonical_json(parse_json("x", good)) == good, "canonical form must be idempotent"

    source = _MemorySource({"feature/FEAT-20260703-sample-item.json": good})
    items, errors = validate_items(source, schema, {})
    assert not errors, f"valid item reported errors: {errors}"
    assert items[0]["id"] == "FEAT-20260703-sample-item"

    def expect_error(files: dict[str, str], needle: str) -> None:
        _, errs = validate_items(_MemorySource(files), schema, {})
        assert any(needle in e for e in errs), f"expected error containing {needle!r}, got {errs}"

    bad = dict(SAMPLE_ITEM)
    bad["priority"] = "someday"
    expect_error({"feature/FEAT-20260703-sample-item.json": canonical_json(bad)}, "priority")

    bad = json.loads(good)
    del bad["risk"]["rollback"]
    expect_error({"feature/FEAT-20260703-sample-item.json": canonical_json(bad)}, "rollback")

    expect_error({"security/FEAT-20260703-sample-item.json": good}, "file must be at")

    expect_error({"feature/FEAT-20260703-sample-item.json": good.replace('"open"', '"open" ')}, "canonical form")

    _, errs = validate_items(
        _MemorySource({"feature/FEAT-20260703-sample-item.json": good}), schema, {"areas": ["other-area"]}
    )
    assert any("not in project config areas" in e for e in errs), f"area enum not enforced: {errs}"

    archived = json.loads(good)
    archived["status"] = "archived"
    archived["notes"].append({"author": "human:reviewer", "date": "2026-07-08", "text": "Park this for now."})
    _, errs = validate_items(_MemorySource({"feature/FEAT-20260703-sample-item.json": canonical_json(archived)}), schema, {})
    assert not errs, f"archived item with authored note reported errors: {errs}"

    bad = json.loads(good)
    bad["notes"][0]["author"] = ""
    expect_error({"feature/FEAT-20260703-sample-item.json": canonical_json(bad)}, "author")

    index_errors = validate_index(_MemorySource({"feature/FEAT-20260703-sample-item.json": good}), items)
    assert index_errors and "missing" in index_errors[0], "missing index must be reported"

    done_schema = parse_json(str(BUNDLED_DONE_SCHEMA), BUNDLED_DONE_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(done_schema)
    good_done = canonical_json(SAMPLE_DONE)

    entries, errs = validate_done_entries(_MemorySource({"done/DONE-20260703-sample-entry.json": good_done}), done_schema)
    assert not errs, f"valid done entry reported errors: {errs}"
    assert entries[0]["id"] == "DONE-20260703-sample-entry"

    _, errs = validate_done_entries(_MemorySource({"done/DONE-20260703-wrong-name.json": good_done}), done_schema)
    assert any("file must be at" in e for e in errs), f"done path rule not enforced: {errs}"

    bad_done = json.loads(good_done)
    bad_done["date"] = "2026-07-04"
    _, errs = validate_done_entries(
        _MemorySource({"done/DONE-20260703-sample-entry.json": canonical_json(bad_done)}), done_schema
    )
    assert any("date part" in e for e in errs), f"done id/date rule not enforced: {errs}"

    mixed = _MemorySource({
        "feature/FEAT-20260703-sample-item.json": good,
        "done/DONE-20260703-sample-entry.json": good_done,
    })
    assert item_files(mixed) == ["feature/FEAT-20260703-sample-item.json"], "done files must not be treated as items"
    assert done_files(mixed) == ["done/DONE-20260703-sample-entry.json"]

    fake_prs = [
        {"number": 7, "title": "Fix the sample", "headRefName": "fix/feat-20260703-sample-item", "url": "u7"},
        {"number": 9, "title": "FEAT-20260703-sample-item follow-up", "headRefName": "chore/x", "url": "u9"},
        {"number": 11, "title": "unrelated", "headRefName": "chore/y", "url": "u11"},
        {"number": 12, "title": "linked explicitly", "headRefName": "chore/z", "url": "u12"},
    ]
    fake_items = [{"id": "FEAT-20260703-sample-item", "links": {"prs": [12, 999]}}]
    item_prs, pr_items = match_prs(fake_items, fake_prs)
    matched = [pr["number"] for pr in item_prs["FEAT-20260703-sample-item"]]
    assert matched == [7, 9, 12], f"PR matching wrong: {matched}"
    assert pr_items[7] == ["FEAT-20260703-sample-item"] and pr_items[11] == [], f"reverse PR map wrong: {pr_items}"

    cfg = _resolve_hub_config(Path("test-config.json"), {"schema_version": 1, "project": {"repo_url": "git@example.com:x.git"}})
    assert cfg["project"]["backlog_ref"] == "main", "backlog_ref default must be main"
    assert cfg["project"]["backlog_dir"] == "docs/backlog", "backlog_dir default wrong"
    assert cfg["build"]["releases_keep"] == 20, "releases_keep default must be 20"
    assert cfg["paths"]["mirror"] == cfg["paths"]["root"] / "mirror.git", "mirror default must live under root"
    cfg = _resolve_hub_config(Path("test-config.json"), {"schema_version": 1, "project": {"repo_url": "x"}, "build": {"releases_keep": -5}})
    assert cfg["build"]["releases_keep"] == 0, "negative releases_keep must clamp to 0"
    for bad_cfg in [{"schema_version": 1}, {"schema_version": 2, "project": {"repo_url": "x"}}, []]:
        try:
            _resolve_hub_config(Path("test-config.json"), bad_cfg)
        except ConfigError:
            pass
        else:
            raise AssertionError(f"invalid config accepted: {bad_cfg!r}")

    print("self-test passed")
    return 0


# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    for name, fn in [("fmt", cmd_fmt), ("validate", cmd_validate)]:
        p = sub.add_parser(name)
        p.add_argument("--backlog-dir", default="docs/backlog", help="backlog directory in the project working copy")
        p.set_defaults(fn=fn)

    for name, fn in [("sync", cmd_sync), ("build", cmd_build), ("serve", cmd_serve)]:
        p = sub.add_parser(name)
        p.add_argument("--config", default=None, help="hub config file (default: BACKLOG_HUB_CONFIG or config.json next to the tool)")
        if name == "build":
            p.add_argument("--force", action="store_true", help="render even if nothing changed since the last build")
        p.set_defaults(fn=fn)

    sub.add_parser("self-test").set_defaults(fn=cmd_self_test)

    args = parser.parse_args()
    try:
        return args.fn(args)
    except (ContractError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
