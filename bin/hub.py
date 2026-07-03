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
import html
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
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


def load_prs(github_repo: str | None) -> tuple[list[dict[str, Any]], str | None]:
    if not github_repo:
        return [], None
    if shutil.which("gh") is None:
        return [], "gh is not installed"
    result = run(
        ["gh", "pr", "list", "--repo", github_repo, "--state", "open",
         "--json", "number,title,headRefName,baseRefName,isDraft,updatedAt,url", "--limit", "100"]
    )
    if result.returncode != 0:
        return [], result.stderr.strip() or "gh pr list failed"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as exc:
        return [], f"gh returned invalid JSON: {exc}"


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

    prs, pr_error = load_prs(project["github_repo"])

    paths["cache"].mkdir(parents=True, exist_ok=True)
    paths["releases"].mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    release = paths["releases"] / stamp
    if release.exists():
        release = paths["releases"] / f"{stamp}-{os.getpid()}"

    render_site(cfg, items, done_entries, commit, prs, pr_error, release)

    tmp_link = paths["root"] / "public.next"
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(release, target_is_directory=True)
    os.replace(tmp_link, paths["public"])
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


def page(project_name: str, title: str, body: str, *, active: str, has_prs: bool) -> str:
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
<link rel="stylesheet" href="assets/styles.css">
</head>
<body>
<header>
  <div><strong>{h(project_name)} Backlog Hub</strong><span>read-only · git is the source of truth</span></div>
  <nav>{nav}</nav>
</header>
<main>{body}</main>
</body>
</html>
"""


def card(title: str, content: str, meta: str = "") -> str:
    meta_html = f'<div class="meta">{meta}</div>' if meta else ""
    return f'<article class="card"><h2>{h(title)}</h2>{meta_html}{content}</article>'


CSS = (
    'body{margin:0;background:#f7f8f5;color:#20231f;font-family:-apple-system,BlinkMacSystemFont,'
    '"Segoe UI",sans-serif;line-height:1.45}header{position:sticky;top:0;background:#fffffb;'
    'border-bottom:1px solid #d8ddcf;padding:14px 24px;display:flex;justify-content:space-between;'
    'gap:16px;align-items:center}header span{display:block;color:#687064;font-size:13px}'
    'nav{display:flex;gap:8px;flex-wrap:wrap}nav a{color:#29332b;text-decoration:none;'
    'border:1px solid #cbd4c5;padding:6px 10px;border-radius:6px;background:#fff}'
    'nav a.active{background:#18392b;color:#fff;border-color:#18392b}'
    'main{max-width:1180px;margin:0 auto;padding:24px}'
    '.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}'
    '.card{background:#fff;border:1px solid #dce2d5;border-radius:8px;padding:16px;margin:0 0 14px}'
    '.card h2{font-size:18px;margin:0 0 8px}.meta,.muted{color:#667064;font-size:13px}'
    '.pill{display:inline-block;border:1px solid #c7d0c0;background:#f3f6ef;border-radius:999px;'
    'padding:2px 8px;margin:2px;font-size:12px}'
    '.risk-high{border-color:#b04444;background:#fff0f0}'
    '.risk-medium{border-color:#b88728;background:#fff8e8}'
    '.risk-low{border-color:#4d8565;background:#edf8f1}'
    'table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dce2d5}'
    'th,td{padding:9px 10px;border-bottom:1px solid #e6eadf;text-align:left;vertical-align:top}'
    'th{background:#eef2e8}a{color:#145c42}'
    '.warn{border-left:4px solid #b88728;padding-left:12px;background:#fff8e8}'
    'h3{font-size:14px;margin:14px 0 4px;text-transform:uppercase;letter-spacing:.04em;color:#4c584e}'
)


def item_detail(item: dict[str, Any]) -> str:
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
        body += "<h3>notes</h3>" + "".join(
            f"<p class=muted>{h(note['date'])} — {h(note['text'])}</p>" for note in item["notes"]
        )
    links = item.get("links") or {}
    if links.get("prs") or links.get("related_ids"):
        body += "<h3>links</h3><p>" + "".join(
            f"<span class=pill>PR #{h(n)}</span>" for n in links.get("prs", [])
        ) + "".join(f"<span class=pill>{h(r)}</span>" for r in links.get("related_ids", [])) + "</p>"
    meta = " ".join(
        f"<span class=pill>{h(item[key])}</span>" for key in ["type", "area", "priority", "status", "created"]
    )
    return card(f"{item['id']} · {item['title']}", body, meta)


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
    return card(entry["title"], body, meta)


def render_site(
    cfg: dict[str, Any],
    items: list[dict[str, Any]],
    done_entries: list[dict[str, Any]],
    commit: str,
    prs: list[dict[str, Any]],
    pr_error: str | None,
    out: Path,
) -> None:
    project = cfg["project"]
    name = project["name"]
    has_prs = bool(project["github_repo"])

    (out / "assets").mkdir(parents=True, exist_ok=True)
    (out / "data").mkdir(parents=True, exist_ok=True)
    (out / "assets/styles.css").write_text(CSS, encoding="utf-8")

    generated_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    index_data = {
        "backlog": items,
        "commit": commit,
        "done": done_entries,
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

    latest_done = f"latest: {h(done_entries[0]['date'])}" if done_entries else "no entries yet"
    home = [
        card("Backlog", f"<p>{len(items)} open items from <code>{h(project['backlog_dir'])}</code>.</p><p>{pills(by_status)}</p><p>{pills(by_priority)}</p>"),
        card("Risk", f"<p>{high_risk} high-risk item(s).</p>"),
        card("Done", f"<p>{len(done_entries)} completed-work entries ({latest_done}).</p>"),
        card("Baseline", f"<p><code>{h(project['backlog_ref'])}</code> @ <code>{h(commit[:10])}</code></p><p class=muted>generated {h(generated_at)}</p>"),
    ]
    if has_prs:
        home.insert(2, card("Open PRs", f"<p>{len(prs)} open pull requests.</p>" + (f"<p class=warn>{h(pr_error)}</p>" if pr_error else "")))
    (out / "index.html").write_text(
        page(name, "Dashboard", f"<section class=grid>{''.join(home)}</section>", active="home", has_prs=has_prs),
        encoding="utf-8",
    )

    rows = []
    for item in items:
        risk_class = f"risk-{h(item['risk']['level'])}"
        rows.append(
            f"<tr><td><strong>{h(item['id'])}</strong><div class=muted>{h(item['_path'])}</div></td>"
            f"<td>{h(item['title'])}</td><td>{h(item['type'])}</td><td>{h(item['area'])}</td>"
            f"<td>{h(item['priority'])}</td><td>{h(item['status'])}</td>"
            f"<td><span class=\"pill {risk_class}\">{h(item['risk']['level'])}</span></td></tr>"
        )
    backlog_body = (
        f"<h1>Backlog</h1><p class=muted>Repo-canonical items read at <code>{h(project['backlog_ref'])}</code>"
        f" @ <code>{h(commit[:10])}</code>. Edits go through the project repo, not the hub.</p>"
        "<table><thead><tr><th>ID</th><th>Title</th><th>Type</th><th>Area</th><th>Priority</th>"
        "<th>Status</th><th>Risk</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        + "".join(item_detail(item) for item in items)
    )
    (out / "backlog.html").write_text(page(name, "Backlog", backlog_body, active="backlog", has_prs=has_prs), encoding="utf-8")

    done_body = (
        "<h1>Done</h1><p class=muted>Completed-work records, newest first. "
        "Grouping is a render concern - storage stays one file per entry.</p>"
    )
    current_month = None
    for entry in done_entries:
        month = entry["date"][:7]
        if month != current_month:
            done_body += f"<h2>{h(month)}</h2>"
            current_month = month
        done_body += done_detail(entry)
    if not done_entries:
        done_body += "<p class=muted>No done entries yet.</p>"
    (out / "done.html").write_text(page(name, "Done", done_body, active="done", has_prs=has_prs), encoding="utf-8")

    if has_prs:
        pr_rows = []
        for pr in prs:
            state = "draft" if pr.get("isDraft") else "ready"
            pr_rows.append(
                f"<tr><td><a href=\"{h(pr['url'])}\">#{h(pr['number'])}</a></td><td>{h(pr['title'])}</td>"
                f"<td><code>{h(pr['headRefName'])}</code> → <code>{h(pr['baseRefName'])}</code></td>"
                f"<td>{h(state)}</td><td>{h(pr.get('updatedAt', ''))}</td></tr>"
            )
        pr_body = "<h1>Open PRs</h1>"
        if pr_error:
            pr_body += f"<p class=warn>{h(pr_error)}</p>"
        pr_body += (
            "<table><thead><tr><th>PR</th><th>Title</th><th>Branch</th><th>State</th><th>Updated</th></tr></thead>"
            "<tbody>" + "".join(pr_rows) + "</tbody></table>"
        )
        (out / "prs.html").write_text(page(name, "PRs", pr_body, active="prs", has_prs=has_prs), encoding="utf-8")


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
