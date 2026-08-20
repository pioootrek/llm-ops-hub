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
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

import jsonschema

TOOL_ROOT = Path(__file__).resolve().parent.parent
BUNDLED_SCHEMA = TOOL_ROOT / "schema" / "backlog-item.schema.json"
BUNDLED_DONE_SCHEMA = TOOL_ROOT / "schema" / "done-entry.schema.json"
BUNDLED_NOTE_SCHEMA = TOOL_ROOT / "schema" / "note.schema.json"
BUNDLED_DOCS_SCHEMA = TOOL_ROOT / "schema" / "docs-header.schema.json"
MONITORED_PROJECT_CI_TEMPLATE = TOOL_ROOT / "templates" / "github-actions" / "backlog.yml"

TYPE_PREFIX = {"feature": "FEAT", "fix": "FIX", "rework": "RWK", "security": "SEC"}
PRIORITY_ORDER = {"now": 0, "next": 1, "later": 2}
INDEX_FILE = "index.json"
PROJECT_SCHEMA_FILE = "schema.json"
PROJECT_DONE_SCHEMA_FILE = "done-schema.json"
PROJECT_NOTE_SCHEMA_FILE = "note-schema.json"
PROJECT_DOCS_SCHEMA_FILE = "docs-header-schema.json"
PROJECT_CONFIG_FILE = "config.json"
DOCS_EXCLUDE = {"AGENTS.md", "CLAUDE.md", "README.md"}
DOCS_STALE_DAYS_DEFAULT = 60
HEALTH_STALE_DAYS_DEFAULT = 45
NOTES_STALE_DAYS_DEFAULT = 90
DONE_SUBDIR = "done"
NOTES_SUBDIR = "notes"
NOTE_MANIFEST = "note.json"
NOTE_TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv", ".log"}
NOTE_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
ALLOWED_NOTE_FILE_SUFFIXES = NOTE_TEXT_SUFFIXES | NOTE_IMAGE_SUFFIXES
MAX_NOTE_FILE_BYTES = 5 * 1024 * 1024


class ContractError(Exception):
    pass


class ConfigError(Exception):
    pass


class Diagnostic(str):
    """A validation error with a legacy-compatible text representation."""

    def __new__(cls, file: str, rule: str, message: str):
        text = f"{file}: {message}" if file else message
        value = super().__new__(cls, text)
        value.file = file
        value.rule = rule
        value.message = message
        return value

    def as_dict(self) -> dict[str, str]:
        return {"file": self.file, "rule": self.rule, "message": self.message}


def contract_diagnostic(exc: ContractError, file: str, rule: str) -> Diagnostic:
    message = str(exc)
    prefix = f"{file}: "
    if message.startswith(prefix):
        message = message[len(prefix):]
    return Diagnostic(file, rule, message)


def diagnostics_json(errors: list[Diagnostic]) -> str:
    return json.dumps([error.as_dict() for error in errors], ensure_ascii=False)


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
# Canonical frontmatter (docs headers)
#
# Docs pages carry their metadata as a YAML frontmatter block, but the
# contract stays JSON: the block is only surface syntax for a flat
# all-string dictionary validated against docs-header.schema.json. The
# subset parsed here is deliberately tiny - flat `key: value` lines, values
# either double-quoted or bare scalars - so no YAML library (and none of
# YAML's coercion traps: `no` -> false, bare dates -> objects) enters the
# tool. canonical_frontmatter() is the one canonical form, mirroring
# canonical_json(): sorted keys, every value double-quoted.

FRONTMATTER_KEY_RE = re.compile(r"^([a-z0-9]+(?:_[a-z0-9]+)*):(.*)$")


def _unquote_frontmatter_value(label: str, raw: str) -> str:
    value = raw.strip()
    if not value.startswith('"'):
        return value  # bare scalar: always read as a string, never coerced
    if len(value) < 2 or not value.endswith('"'):
        raise ContractError(f"{label}: unterminated quoted value: {raw.strip()!r}")
    body = value[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == '"':
            raise ContractError(f"{label}: unescaped quote inside value: {raw.strip()!r}")
        if ch == "\\":
            if i + 1 >= len(body) or body[i + 1] not in '"\\':
                raise ContractError(f"{label}: unsupported escape in value: {raw.strip()!r}")
            out.append(body[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_frontmatter(label: str, text: str) -> tuple[dict[str, str], str]:
    """Returns (header, body). The file must open with a frontmatter block."""
    lines = text.split("\n")
    if lines[0] != "---":
        raise ContractError(f"{label}: missing frontmatter (file must start with ---)")
    header: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            return header, "\n".join(lines[index + 1:])
        if line == "" and index == len(lines) - 1:
            break  # trailing newline at EOF: the block was never closed
        match = FRONTMATTER_KEY_RE.match(line)
        if not match:
            raise ContractError(f"{label}: frontmatter line is not `key: value`: {line!r}")
        key = match.group(1)
        if key in header:
            raise ContractError(f"{label}: duplicate frontmatter key {key!r}")
        header[key] = _unquote_frontmatter_value(label, match.group(2))
    raise ContractError(f"{label}: frontmatter block is never closed (missing ---)")


def canonical_frontmatter(header: dict[str, str]) -> str:
    quoted = {
        key: value.replace("\\", "\\\\").replace('"', '\\"')
        for key, value in header.items()
    }
    return "---\n" + "".join(f'{key}: "{quoted[key]}"\n' for key in sorted(quoted)) + "---\n"


# ---------------------------------------------------------------------------
# Hub config

def load_hub_config(explicit: str | None) -> dict[str, Any]:
    candidates = [
        Path(explicit).expanduser() if explicit else None,
        Path(os.environ["LLM_OPS_HUB_CONFIG"]).expanduser() if os.environ.get("LLM_OPS_HUB_CONFIG") else None,
        TOOL_ROOT / "config.json",
        Path.home() / ".local" / "share" / "llm-ops-hub" / "config.json",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            raw = parse_json(str(candidate), candidate.read_text(encoding="utf-8"))
            return _resolve_hub_config(candidate, raw)
    raise ConfigError("no config file found (use --config or LLM_OPS_HUB_CONFIG)")


def _resolve_hub_config(path: Path, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ConfigError(f"{path}: expected a mapping with schema_version 1")
    project = raw.get("project")
    if not isinstance(project, dict) or not str(project.get("repo_url", "")).strip():
        raise ConfigError(f"{path}: project.repo_url is required")

    paths_raw = raw.get("paths") if isinstance(raw.get("paths"), dict) else {}
    root = Path(str(paths_raw.get("root", "~/.local/share/llm-ops-hub"))).expanduser()

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
            for p in self.backlog_dir.rglob("*")
            if p.is_file()
        )

    def read(self, rel_path: str) -> str:
        return (self.backlog_dir / rel_path).read_text(encoding="utf-8")

    def read_bytes(self, rel_path: str) -> bytes:
        return (self.backlog_dir / rel_path).read_bytes()

    def size(self, rel_path: str) -> int:
        return (self.backlog_dir / rel_path).stat().st_size


class MirrorSource:
    """Reads a backlog directory from a bare mirror at a ref (build). The
    listing (with blob sizes) is cached for the source's lifetime: a bare
    mirror at a fixed ref is immutable within one build, and callers hit
    list_files()/size() many times per run."""

    def __init__(self, mirror: Path, ref: str, backlog_dir: str):
        self.mirror = mirror
        self.ref = ref
        self.backlog_dir = backlog_dir
        self.label = f"{ref}:{backlog_dir}"
        self._entries: dict[str, int] | None = None

    def resolve_commit(self) -> str | None:
        result = run(["git", f"--git-dir={self.mirror}", "rev-parse", "--verify", "--quiet", self.ref])
        return result.stdout.strip() or None

    def _load_entries(self) -> dict[str, int]:
        if self._entries is not None:
            return self._entries
        # -z: NUL-separated, no C-quoting of unusual filenames; -l: blob sizes.
        result = run(
            ["git", f"--git-dir={self.mirror}", "ls-tree", "-r", "-l", "-z", self.ref, "--", self.backlog_dir]
        )
        if result.returncode != 0:
            raise ContractError(f"could not list {self.backlog_dir} at {self.ref}: {result.stderr.strip()}")
        prefix = f"{self.backlog_dir}/"
        entries: dict[str, int] = {}
        for line in result.stdout.split("\0"):
            meta, _, path = line.partition("\t")
            if not path.startswith(prefix):
                continue
            size_field = meta.split()[3]
            entries[path[len(prefix):]] = int(size_field) if size_field.isdigit() else 0
        self._entries = entries
        return entries

    def list_files(self) -> list[str]:
        return sorted(self._load_entries())

    def read(self, rel_path: str) -> str:
        result = run(["git", f"--git-dir={self.mirror}", "show", f"{self.ref}:{self.backlog_dir}/{rel_path}"])
        if result.returncode != 0:
            raise ContractError(f"could not read {rel_path} at {self.ref}")
        return result.stdout

    def read_bytes(self, rel_path: str) -> bytes:
        result = subprocess.run(
            ["git", f"--git-dir={self.mirror}", "show", f"{self.ref}:{self.backlog_dir}/{rel_path}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode != 0:
            raise ContractError(f"could not read {rel_path} at {self.ref}")
        return result.stdout

    def size(self, rel_path: str) -> int:
        entries = self._load_entries()
        if rel_path not in entries:
            raise ContractError(f"could not stat {rel_path} at {self.ref}")
        return entries[rel_path]


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


def load_note_schema(source) -> dict[str, Any]:
    """Project-owned note-schema.json wins over the bundled default."""
    if PROJECT_NOTE_SCHEMA_FILE in source.list_files():
        return parse_json(f"{source.label}/{PROJECT_NOTE_SCHEMA_FILE}", source.read(PROJECT_NOTE_SCHEMA_FILE))
    return parse_json(str(BUNDLED_NOTE_SCHEMA), BUNDLED_NOTE_SCHEMA.read_text(encoding="utf-8"))


def load_docs_schema(source) -> dict[str, Any]:
    """Project-owned docs-header-schema.json (in the backlog dir) wins over
    the bundled default."""
    if PROJECT_DOCS_SCHEMA_FILE in source.list_files():
        return parse_json(f"{source.label}/{PROJECT_DOCS_SCHEMA_FILE}", source.read(PROJECT_DOCS_SCHEMA_FILE))
    return parse_json(str(BUNDLED_DOCS_SCHEMA), BUNDLED_DOCS_SCHEMA.read_text(encoding="utf-8"))


def load_project_config(source) -> dict[str, Any]:
    if PROJECT_CONFIG_FILE in source.list_files():
        data = parse_json(f"{source.label}/{PROJECT_CONFIG_FILE}", source.read(PROJECT_CONFIG_FILE))
        if isinstance(data, dict):
            return data
    return {}


def docs_settings(project_cfg: dict[str, Any]) -> dict[str, Any] | None:
    """The docs module is enabled by the project config (in the backlog dir)
    naming a docs directory - repo-root-relative, like backlog_dir. The
    project owns the switch, so agent-side fmt/validate and the hub build
    cannot disagree about whether docs are part of the contract."""
    docs_dir = str(project_cfg.get("docs_dir", "")).strip().strip("/")
    if not docs_dir:
        return None
    try:
        stale_days = max(0, int(project_cfg.get("docs_stale_days", DOCS_STALE_DAYS_DEFAULT)))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"project config: docs_stale_days must be an integer: {exc}") from exc
    return {
        "dir": docs_dir,
        "index_file": str(project_cfg.get("docs_index_file", "")).strip(),
        "stale_days": stale_days,
    }


def health_settings(project_cfg: dict[str, Any]) -> dict[str, int]:
    values = {}
    for key, default in [
        ("health_stale_days", HEALTH_STALE_DAYS_DEFAULT),
        ("notes_stale_days", NOTES_STALE_DAYS_DEFAULT),
    ]:
        try:
            values[key] = max(0, int(project_cfg.get(key, default)))
        except (TypeError, ValueError) as exc:
            raise ContractError(f"project config: {key} must be an integer: {exc}") from exc
    return values


def is_hidden(rel_path: str) -> bool:
    """Dotfiles (.gitkeep, .DS_Store, hidden dirs) are never records and are
    ignored uniformly here, so the worktree and the mirror cannot disagree
    about them (a divergence would pass local validate yet fail the hub build)."""
    return any(part.startswith(".") for part in rel_path.split("/"))


def item_files(source) -> list[str]:
    reserved = {INDEX_FILE, PROJECT_SCHEMA_FILE, PROJECT_DONE_SCHEMA_FILE, PROJECT_NOTE_SCHEMA_FILE, PROJECT_CONFIG_FILE}
    return [
        p for p in source.list_files()
        if p.endswith(".json")
        and not is_hidden(p)
        and p not in reserved
        and not p.startswith(DONE_SUBDIR + "/")
        and not p.startswith(NOTES_SUBDIR + "/")
    ]


def done_files(source) -> list[str]:
    return [
        p for p in source.list_files()
        if p.startswith(DONE_SUBDIR + "/") and p.endswith(".json") and not is_hidden(p)
    ]


def note_files(source) -> list[str]:
    """Everything under notes/ - manifests and free-form payload files alike."""
    return [p for p in source.list_files() if p.startswith(NOTES_SUBDIR + "/") and not is_hidden(p)]


def note_manifest_files(source) -> list[str]:
    return [p for p in note_files(source) if p.count("/") == 2 and p.endswith("/" + NOTE_MANIFEST)]


def validate_items(source, schema: dict[str, Any], project_cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], list[Diagnostic]]:
    """Returns (parsed items sorted for display, error messages)."""
    validator = jsonschema.Draft202012Validator(schema)
    areas = project_cfg.get("areas")
    errors: list[Diagnostic] = []
    items: list[dict[str, Any]] = []
    seen_ids: dict[str, str] = {}

    for rel_path in item_files(source):
        label = f"{source.label}/{rel_path}"
        try:
            raw = source.read(rel_path)
            data = parse_json(label, raw)
        except ContractError as exc:
            errors.append(contract_diagnostic(exc, label, "json.invalid"))
            continue

        schema_errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
        if schema_errors:
            for err in schema_errors:
                where = "/".join(str(p) for p in err.path) or "(root)"
                errors.append(Diagnostic(label, "schema", f"{where}: {err.message}"))
            continue

        item_id = data["id"]
        item_type = data["type"]
        expected_rel = f"{item_type}/{item_id}.json"
        if rel_path != expected_rel:
            errors.append(Diagnostic(label, "path.mismatch", f"file must be at {expected_rel} (id and type define the path)"))
        if not item_id.startswith(TYPE_PREFIX[item_type] + "-"):
            errors.append(Diagnostic(label, "item.id_prefix", f"id prefix must be {TYPE_PREFIX[item_type]} for type {item_type}"))
        if isinstance(areas, list) and areas and data["area"] not in areas:
            errors.append(Diagnostic(label, "item.area", f"area {data['area']!r} not in project config areas"))
        if item_id in seen_ids:
            errors.append(Diagnostic(label, "id.duplicate", f"duplicate id {item_id} (also in {seen_ids[item_id]})"))
        else:
            seen_ids[item_id] = rel_path

        if raw != canonical_json(data):
            errors.append(Diagnostic(label, "canonical.form", "not in canonical form (run: hub.py fmt)"))

        data["_path"] = rel_path
        items.append(data)

    items.sort(key=lambda i: (PRIORITY_ORDER[i["priority"]], i["type"], i["id"]))
    return items, errors


def validate_done_entries(source, schema: dict[str, Any]) -> tuple[list[dict[str, Any]], list[Diagnostic]]:
    """Returns (done entries newest first, error messages)."""
    validator = jsonschema.Draft202012Validator(schema)
    errors: list[Diagnostic] = []
    entries: list[dict[str, Any]] = []
    seen_ids: dict[str, str] = {}

    for rel_path in done_files(source):
        label = f"{source.label}/{rel_path}"
        try:
            raw = source.read(rel_path)
            data = parse_json(label, raw)
        except ContractError as exc:
            errors.append(contract_diagnostic(exc, label, "json.invalid"))
            continue

        schema_errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
        if schema_errors:
            for err in schema_errors:
                where = "/".join(str(p) for p in err.path) or "(root)"
                errors.append(Diagnostic(label, "schema", f"{where}: {err.message}"))
            continue

        entry_id = data["id"]
        expected_rel = f"{DONE_SUBDIR}/{entry_id}.json"
        if rel_path != expected_rel:
            errors.append(Diagnostic(label, "path.mismatch", f"file must be at {expected_rel} (id defines the path)"))
        if entry_id[5:13] != data["date"].replace("-", ""):
            errors.append(Diagnostic(label, "done.id_date", f"id date part must match date {data['date']}"))
        if entry_id in seen_ids:
            errors.append(Diagnostic(label, "id.duplicate", f"duplicate id {entry_id} (also in {seen_ids[entry_id]})"))
        else:
            seen_ids[entry_id] = rel_path

        if raw != canonical_json(data):
            errors.append(Diagnostic(label, "canonical.form", "not in canonical form (run: hub.py fmt)"))

        data["_path"] = rel_path
        entries.append(data)

    entries.sort(key=lambda e: (e["date"], e["id"]), reverse=True)
    return entries, errors


def validate_notes(source, schema: dict[str, Any]) -> tuple[list[dict[str, Any]], list[Diagnostic]]:
    """Returns (agent notes newest first, error messages). A note is a
    directory notes/<id>/ holding a validated note.json manifest plus
    free-form payload files - only the manifest and the payload envelope
    (allowed types, size) are checked; content belongs to the agents."""
    validator = jsonschema.Draft202012Validator(schema)
    errors: list[Diagnostic] = []
    notes: list[dict[str, Any]] = []
    by_dir: dict[str, list[str]] = {}

    for rel_path in note_files(source):
        parts = rel_path.split("/")
        if len(parts) < 3:
            errors.append(Diagnostic(
                f"{source.label}/{rel_path}", "note.layout",
                f"notes are directories - expected {NOTES_SUBDIR}/<note-id>/{NOTE_MANIFEST} plus free-form files",
            ))
            continue
        by_dir.setdefault(parts[1], []).append("/".join(parts[2:]))

    for note_dir, files in sorted(by_dir.items()):
        dir_label = f"{source.label}/{NOTES_SUBDIR}/{note_dir}"
        payload = sorted(f for f in files if f != NOTE_MANIFEST)

        for rel_file in payload:
            file_rel = f"{NOTES_SUBDIR}/{note_dir}/{rel_file}"
            suffix = Path(rel_file).suffix.lower()
            if suffix not in ALLOWED_NOTE_FILE_SUFFIXES:
                allowed = " ".join(sorted(ALLOWED_NOTE_FILE_SUFFIXES))
                errors.append(Diagnostic(f"{source.label}/{file_rel}", "note.file_type", f"file type {suffix or '(none)'} not allowed in notes (allowed: {allowed})"))
            elif source.size(file_rel) > MAX_NOTE_FILE_BYTES:
                errors.append(Diagnostic(f"{source.label}/{file_rel}", "note.file_size", f"exceeds the {MAX_NOTE_FILE_BYTES // (1024 * 1024)} MB note file limit"))

        if NOTE_MANIFEST not in files:
            errors.append(Diagnostic(dir_label, "note.manifest_missing", f"missing {NOTE_MANIFEST} manifest"))
            continue
        manifest_rel = f"{NOTES_SUBDIR}/{note_dir}/{NOTE_MANIFEST}"
        label = f"{source.label}/{manifest_rel}"
        try:
            raw = source.read(manifest_rel)
            data = parse_json(label, raw)
        except ContractError as exc:
            errors.append(contract_diagnostic(exc, label, "json.invalid"))
            continue

        schema_errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
        if schema_errors:
            for err in schema_errors:
                where = "/".join(str(p) for p in err.path) or "(root)"
                errors.append(Diagnostic(label, "schema", f"{where}: {err.message}"))
            continue

        note_id = data["id"]
        if note_id != note_dir:
            errors.append(Diagnostic(label, "note.id_directory", f"id {note_id} must match the directory name {note_dir}"))
        if note_id[5:13] != data["created"].replace("-", ""):
            errors.append(Diagnostic(label, "note.id_date", f"id date part must match created {data['created']}"))
        if "last_reviewed" in data:
            try:
                dt.date.fromisoformat(data["last_reviewed"])
            except (TypeError, ValueError):
                errors.append(Diagnostic(label, "note.last_reviewed", f"last_reviewed is not a real calendar date: {data['last_reviewed']!r}"))
        if raw != canonical_json(data):
            errors.append(Diagnostic(label, "canonical.form", "not in canonical form (run: hub.py fmt)"))

        data["_path"] = f"{NOTES_SUBDIR}/{note_dir}"
        data["_files"] = payload
        notes.append(data)

    notes.sort(key=lambda n: (n["created"], n["id"]), reverse=True)
    return notes, errors


def docs_files(source) -> list[str]:
    """Docs records are the top-level *.md pages of the docs dir. Instruction
    files (AGENTS.md/CLAUDE.md/README.md) and subdirectories are out of the
    contract's first pass."""
    return [
        p for p in source.list_files()
        if p.endswith(".md") and "/" not in p and not is_hidden(p) and p not in DOCS_EXCLUDE
    ]


def validate_docs(source, schema: dict[str, Any]) -> tuple[list[dict[str, Any]], list[Diagnostic]]:
    """Returns (docs pages sorted by file name, error messages). Only the
    frontmatter header is contract-checked; the markdown body is free-form
    and kept for the docs-health link scan."""
    validator = jsonschema.Draft202012Validator(schema)
    errors: list[Diagnostic] = []
    docs: list[dict[str, Any]] = []

    files = docs_files(source)
    if not files:
        errors.append(Diagnostic(source.label, "docs.empty", "docs module is enabled but has no top-level *.md pages (check docs_dir)"))

    for rel_path in files:
        label = f"{source.label}/{rel_path}"
        try:
            raw = source.read(rel_path)
            header, body = parse_frontmatter(label, raw)
        except ContractError as exc:
            errors.append(contract_diagnostic(exc, label, "docs.frontmatter.invalid"))
            continue

        schema_errors = sorted(validator.iter_errors(header), key=lambda e: list(e.path))
        if schema_errors:
            for err in schema_errors:
                where = "/".join(str(p) for p in err.path) or "(root)"
                errors.append(Diagnostic(label, "schema", f"{where}: {err.message}"))
            continue

        if not raw.startswith(canonical_frontmatter(header)):
            errors.append(Diagnostic(label, "docs.frontmatter.canonical", "frontmatter not in canonical form (run: hub.py fmt)"))

        # Calendar validity is beyond the schema's regex (2026-02-30 matches
        # the pattern), and a bad date must not survive into docs_health():
        # this check holds even if a project's override schema drops the
        # pattern entirely.
        if "last_reviewed" in header:
            try:
                dt.date.fromisoformat(header["last_reviewed"])
            except ValueError:
                errors.append(Diagnostic(label, "docs.last_reviewed", f"last_reviewed is not a real calendar date: {header['last_reviewed']!r}"))
                continue

        docs.append({"file": rel_path, "header": header, "_body": body})

    docs.sort(key=lambda d: d["file"])
    return docs, errors


MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def doc_relative_links(body: str) -> list[str]:
    """Relative link targets in a markdown body - external URLs, anchors, and
    absolute paths are somebody else's problem."""
    targets = []
    for target in MD_LINK_RE.findall(body):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith(("/", "#")):
            continue
        cleaned = target.split("#", 1)[0]
        if cleaned:
            targets.append(cleaned)
    return targets


def docs_health(
    docs: list[dict[str, Any]],
    source,
    settings: dict[str, Any],
    today: dt.date,
) -> dict[str, Any]:
    """Per-page findings for the Docs health page. These are report-only:
    unlike a broken header they never fail the build, so one dead link
    cannot take the whole hub release hostage."""
    entries = set(source.list_files())
    dirs = set()
    for rel in entries:
        parts = rel.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:depth]))
    index_file = settings["index_file"]
    index_text = source.read(index_file) if index_file and index_file in entries else None

    by_status: dict[str, int] = {}
    for doc in docs:
        header = doc["header"]
        by_status[header["status"]] = by_status.get(header["status"], 0) + 1

        reviewed_days = None
        if re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", header.get("last_reviewed", "")):
            reviewed_days = (today - dt.date.fromisoformat(header["last_reviewed"])).days
        doc["reviewed_days"] = reviewed_days
        doc["overdue"] = (
            header["status"] in ("active", "reference")
            and settings["stale_days"] > 0
            and (reviewed_days is None or reviewed_days > settings["stale_days"])
        )

        doc["missing_from_index"] = (
            index_text is not None
            and doc["file"] != index_file
            and f"({doc['file']})" not in index_text
        )

        dead = []
        for target in doc_relative_links(doc["_body"]):
            normalized = os.path.normpath(target).replace(os.sep, "/")
            if normalized.startswith(".."):
                continue  # points outside the docs tree; not checkable here
            if normalized not in entries and normalized.rstrip("/") not in dirs:
                dead.append(target)
        doc["dead_links"] = sorted(set(dead))

    return {
        "by_status": by_status,
        "dead_link_count": sum(len(d["dead_links"]) for d in docs),
        "missing_from_index": sorted(d["file"] for d in docs if d["missing_from_index"]),
        "overdue": sorted(d["file"] for d in docs if d["overdue"]),
        "stale_days": settings["stale_days"],
    }


def project_health(
    items: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    settings: dict[str, int],
    today: dt.date,
) -> dict[str, Any]:
    item_findings = []
    for item in items:
        if item["status"] == "archived":
            continue
        item_notes = item.get("notes", [])
        last_activity = max([item["created"]] + [note["date"] for note in item_notes])
        age_days = (today - dt.date.fromisoformat(last_activity)).days
        rules = []
        if not item_notes:
            rules.append("zero_notes")
        if item["status"] == "blocked" and not item_notes:
            rules.append("blocked_without_notes")
        threshold = settings["health_stale_days"]
        if threshold > 0 and age_days > threshold:
            if item["status"] == "in-progress":
                rules.append("stale_in_progress")
            if item["status"] == "blocked":
                rules.append("stale_blocked")
            if item["priority"] == "now":
                rules.append("stale_now")
        if rules:
            item_findings.append({
                "age_days": age_days,
                "id": item["id"],
                "last_activity": last_activity,
                "rules": rules,
                "title": item["title"],
            })

    note_findings = []
    note_threshold = settings["notes_stale_days"]
    if note_threshold > 0:
        for note in notes:
            if note["status"] != "active":
                continue
            freshness = max(note["created"], note.get("last_reviewed", note["created"]))
            age_days = (today - dt.date.fromisoformat(freshness)).days
            if age_days > note_threshold:
                note_findings.append({
                    "age_days": age_days,
                    "id": note["id"],
                    "freshness": freshness,
                    "title": note["title"],
                })
    return {
        "item_findings": item_findings,
        "notes_stale_days": note_threshold,
        "note_findings": note_findings,
        "stale_days": settings["health_stale_days"],
    }


def build_index(items: list[dict[str, Any]], notes: list[dict[str, Any]]) -> dict[str, Any]:
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
        "notes": [
            {
                "author": n["author"],
                "created": n["created"],
                "files": n["_files"],
                "id": n["id"],
                "path": n["_path"],
                "status": n["status"],
                "tags": n.get("tags", []),
                "title": n["title"],
            }
            for n in notes
        ],
        "schema_version": 1,
    }


def validate_index(source, items: list[dict[str, Any]], notes: list[dict[str, Any]]) -> list[Diagnostic]:
    expected = canonical_json(build_index(items, notes))
    if INDEX_FILE not in source.list_files():
        return [Diagnostic(f"{source.label}/{INDEX_FILE}", "index.missing", "missing (run: hub.py fmt)")]
    if source.read(INDEX_FILE) != expected:
        return [Diagnostic(f"{source.label}/{INDEX_FILE}", "index.stale", "stale (run: hub.py fmt)")]
    return []


# ---------------------------------------------------------------------------
# Commands: fmt / validate (working copy)

def _worktree(backlog_dir: str) -> WorktreeSource:
    path = Path(backlog_dir).expanduser()
    if not path.is_dir():
        raise ContractError(f"backlog directory not found: {path}")
    return WorktreeSource(path)


def _docs_worktree(source: WorktreeSource, settings: dict[str, Any]) -> WorktreeSource:
    """docs_dir is repo-root-relative (like backlog_dir), so the repo root is
    resolved through git - the backlog contract already requires a checkout."""
    result = run(["git", "-C", str(source.backlog_dir), "rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        raise ContractError(
            f"docs_dir {settings['dir']!r} is configured but the repo root could not be "
            f"resolved ({result.stderr.strip() or 'not a git checkout'})"
        )
    path = Path(result.stdout.strip()) / settings["dir"]
    if not path.is_dir():
        raise ContractError(f"docs directory not found: {path}")
    return WorktreeSource(path)


def cmd_fmt(args: argparse.Namespace) -> int:
    source = _worktree(args.backlog_dir)
    changed = 0
    # Only manifests are canonicalized under notes/ - payload files (even
    # .json ones) are the agents' own format and are never rewritten.
    for rel_path in item_files(source) + done_files(source) + note_manifest_files(source):
        label = f"{source.label}/{rel_path}"
        raw = source.read(rel_path)
        formatted = canonical_json(parse_json(label, raw))
        if raw != formatted:
            (source.backlog_dir / rel_path).write_text(formatted, encoding="utf-8")
            changed += 1
            print(f"formatted {rel_path}")

    schema = load_schema(source)
    project_cfg = load_project_config(source)
    health_settings(project_cfg)
    settings = docs_settings(project_cfg)
    docs_source = _docs_worktree(source, settings) if settings else None
    if docs_source is not None:
        # Canonicalize only the frontmatter block; the markdown body is the
        # authors' own text and is never rewritten.
        for rel_path in docs_files(docs_source):
            label = f"{docs_source.label}/{rel_path}"
            raw = docs_source.read(rel_path)
            try:
                header, body = parse_frontmatter(label, raw)
            except ContractError:
                continue  # reported by the validation pass below
            formatted = canonical_frontmatter(header) + body
            if raw != formatted:
                (docs_source.backlog_dir / rel_path).write_text(formatted, encoding="utf-8")
                changed += 1
                print(f"formatted {settings['dir']}/{rel_path}")

    items, errors = validate_items(source, schema, project_cfg)
    _, done_errors = validate_done_entries(source, load_done_schema(source))
    errors += done_errors
    notes, note_errors = validate_notes(source, load_note_schema(source))
    errors += note_errors
    if docs_source is not None:
        _, docs_errors = validate_docs(docs_source, load_docs_schema(source))
        errors += docs_errors
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print("fmt: canonicalized files, but contract errors remain (see above)", file=sys.stderr)
        return 2

    index_text = canonical_json(build_index(items, notes))
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
    health_settings(project_cfg)
    items, errors = validate_items(source, schema, project_cfg)
    done_entries, done_errors = validate_done_entries(source, load_done_schema(source))
    errors += done_errors
    notes, note_errors = validate_notes(source, load_note_schema(source))
    errors += note_errors
    errors += validate_index(source, items, notes)
    settings = docs_settings(project_cfg)
    docs = []
    if settings:
        docs, docs_errors = validate_docs(_docs_worktree(source, settings), load_docs_schema(source))
        errors += docs_errors
    if errors:
        if args.json:
            print(diagnostics_json(errors))
            return 2
        for error in errors:
            print(error, file=sys.stderr)
        print(f"validate: {len(errors)} error(s)", file=sys.stderr)
        return 2
    if args.json:
        print("[]")
        return 0
    docs_note = f", {len(docs)} docs pages" if settings else ""
    print(f"validate: OK ({len(items)} items, {len(done_entries)} done entries, {len(notes)} notes{docs_note})")
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


def load_feedback_issues(github_repo: str | None) -> tuple[list[dict[str, Any]], str | None]:
    if not github_repo:
        return [], None
    return gh_json(
        ["issue", "list", "--repo", github_repo, "--state", "open",
         "--label", FEEDBACK_LABEL, "--json", "number,title,url,updatedAt", "--limit", "100"]
    )


def build_state_key(project: dict[str, Any], commit: str, github: dict[str, Any]) -> str:
    """Everything a release depends on: repo state, GitHub data, tool, project
    config. If this key is unchanged since the last successful build, rendering
    again would produce an identical release."""
    return canonical_json({
        "commit": commit,
        "github": github,
        "project": project,
        "schemas": {
            str(path.relative_to(TOOL_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [BUNDLED_SCHEMA, BUNDLED_DONE_SCHEMA, BUNDLED_NOTE_SCHEMA, BUNDLED_DOCS_SCHEMA]
        },
        "tool": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    })


def prune_releases(releases_dir: Path, current: Path, keep: int) -> None:
    if keep <= 0:
        return
    releases = sorted(p for p in releases_dir.iterdir() if p.is_dir())
    for old in releases[:-keep]:
        if old != current:
            shutil.rmtree(old, ignore_errors=True)


def write_heartbeat(public: Path, commit: str, result: str) -> None:
    """The one mutable file inside the otherwise immutable live release.
    Touched on every SUCCESSFUL build attempt - including no-op skips - it
    proves the sync/build pipeline is alive; the staleness banner alarms on
    heartbeat age, never on content age (a quiet backlog is not a failure)."""
    heartbeat = {
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "commit": commit,
        "result": result,
        "schema_version": 1,
    }
    (public / "heartbeat.json").write_text(canonical_json(heartbeat), encoding="utf-8")


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

    issues, issue_error = load_feedback_issues(project["github_repo"])
    github = {"issue_error": issue_error, "issues": issues}

    state_key = build_state_key(project, commit, github)
    state_path = cfg["paths"]["cache"] / "last_build.json"
    if (
        not args.force
        and state_path.is_file()
        and state_path.read_text(encoding="utf-8") == state_key
        and paths["public"].exists()
    ):
        write_heartbeat(paths["public"], commit, "skipped")
        print(f"build: no changes at {project['backlog_ref']} ({commit[:10]}); keeping current release")
        return 0

    schema = load_schema(source)
    project_cfg = load_project_config(source)
    health_settings(project_cfg)
    items, errors = validate_items(source, schema, project_cfg)
    done_entries, done_errors = validate_done_entries(source, load_done_schema(source))
    errors += done_errors
    notes, note_errors = validate_notes(source, load_note_schema(source))
    errors += note_errors
    errors += validate_index(source, items, notes)
    settings = docs_settings(project_cfg)
    docs = None
    docs_source = None
    if settings:
        docs_source = MirrorSource(paths["mirror"], project["backlog_ref"], settings["dir"])
        docs, docs_errors = validate_docs(docs_source, load_docs_schema(source))
        errors += docs_errors
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

    render_site(
        cfg, source, items, done_entries, notes, commit, github, release,
        docs=docs, docs_source=docs_source, docs_cfg=settings,
    )

    tmp_link = paths["root"] / "public.next"
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(release, target_is_directory=True)
    os.replace(tmp_link, paths["public"])
    write_heartbeat(paths["public"], commit, "rendered")
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


def prose_values(item: dict[str, Any], key: str) -> list[str]:
    """Prose fields the bundled schema requires but an override schema may
    relax - tolerate them missing or malformed instead of crashing the build."""
    values = item.get(key, [])
    if not isinstance(values, list):
        return []
    return [str(value) for value in values]


STALE_AFTER_MINUTES = 15

# The banner alarms on heartbeat age (pipeline health), never on snapshot age:
# a backlog with no commits for hours is healthy, a heartbeat that old is not.
# heartbeat.json is rewritten by every successful build attempt, skips included.
STALE_SCRIPT = """
<script>
(function () {
  const banner = document.getElementById("stale-banner");
  if (!banner) return;
  const limit = Number(banner.dataset.staleAfter);
  async function check() {
    let checkedAt = NaN;
    try {
      const response = await fetch("heartbeat.json", { cache: "no-store" });
      if (response.ok) checkedAt = Date.parse((await response.json()).checked_at);
    } catch (error) {
      /* no HTTP context or heartbeat missing - stay silent rather than false-alarm */
    }
    if (!isFinite(checkedAt)) {
      banner.hidden = true;
      return;
    }
    const minutes = Math.round((Date.now() - checkedAt) / 60000);
    if (minutes < limit) {
      banner.hidden = true;
      return;
    }
    banner.textContent = "Last successful sync/build check was " + minutes
      + " minutes ago — the pipeline may be down or failing.";
    banner.hidden = false;
  }
  check();
  setInterval(check, 60000);
})();
</script>
"""


def page(project_name: str, title: str, body: str, *, active: str, generated_at: str = "", css_version: str = "", with_docs: bool = False) -> str:
    links = [
        ("home", "index.html", "Dashboard"),
        ("backlog", "backlog.html", "Backlog"),
        ("notes", "notes.html", "Notes"),
        ("done", "done.html", "Done"),
        ("health", "health.html", "Health"),
        *([("docs", "docs.html", "Docs")] if with_docs else []),
        ("data", "data/index.json", "JSON"),
    ]
    nav = "".join(
        f'<a class="{"active" if key == active else ""}" href="{href}">{label}</a>'
        for key, href, label in links
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h(title)} · {h(project_name)} · LLM Ops Hub</title>
<link rel="stylesheet" href="assets/styles.css{h(css_version)}">
</head>
<body>
<header>
  <div><strong>{h(project_name)} · LLM Ops Hub</strong><span>read-only · git is the source of truth</span></div>
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
    'padding:1px 5px;font-size:.92em}'
    'pre{white-space:pre-wrap;word-break:break-word;border:1px solid var(--border);border-radius:6px;'
    'background:var(--muted-bg);padding:10px 12px;margin:8px 0;font-size:12.5px;line-height:1.5;overflow-x:auto}'
    '.pill{display:inline-flex;align-items:center;border:1px solid var(--border);background:var(--surface);'
    'border-radius:6px;padding:2px 7px;margin:2px;font-size:12px;font-weight:500;line-height:1.45}'
    'a.pill{color:inherit;text-decoration:none}a.pill:hover{border-color:var(--ring)}'
    '.pill.stale{border-color:var(--warn-border);background:var(--warn-bg);color:var(--warn-fg)}'
    '.button{display:inline-flex;align-items:center;justify-content:center;height:32px;border:1px solid var(--border);'
    'border-radius:6px;background:var(--surface);color:var(--foreground);padding:0 10px;font:inherit;font-size:13px;font-weight:500;cursor:pointer;text-decoration:none}'
    '.button:hover{background:var(--surface-hover)}.button.primary{background:var(--foreground);color:var(--on-strong);border-color:var(--foreground)}.button:focus-visible,'
    '.control input:focus,.control select:focus{outline:2px solid var(--ring);outline-offset:2px}'
    '.toolbar{display:grid;grid-template-columns:minmax(220px,1.6fr) repeat(5,minmax(112px,1fr)) minmax(150px,1.15fr) 84px auto;'
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
    '.note-image{max-width:100%;border:1px solid var(--border);border-radius:6px}'
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


RISK_ACCEPTANCE_EXPIRING_DAYS = 14

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


def risk_acceptance_state(item: dict[str, Any], today: dt.date) -> str:
    acceptance = item.get("risk_acceptance")
    if not isinstance(acceptance, dict):
        return "none"
    try:
        expires_on = dt.date.fromisoformat(str(acceptance["expires_on"]))
    except (KeyError, TypeError, ValueError):
        return "invalid"
    days_remaining = (expires_on - today).days
    if days_remaining < 0:
        return "expired"
    if days_remaining <= RISK_ACCEPTANCE_EXPIRING_DAYS:
        return "expiring"
    return "active"


def risk_acceptance_pill(state: str) -> str:
    tone = {
        "active": "risk-low",
        "expiring": "risk-medium",
        "expired": "risk-high",
        "invalid": "risk-high",
    }.get(state, "")
    return f'<span class="pill {tone}">risk acceptance: {h(state)}</span>'


def item_detail(
    item: dict[str, Any],
    *,
    active: bool = False,
    github_repo: str | None = None,
    today: dt.date | None = None,
    stale_days: int = HEALTH_STALE_DAYS_DEFAULT,
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
    acceptance = item.get("risk_acceptance")
    if isinstance(acceptance, dict) and today is not None:
        acceptance_state = risk_acceptance_state(item, today)
        body += "<h3>risk acceptance</h3>"
        body += (
            f"<p>{risk_acceptance_pill(acceptance_state)}"
            f"<span class=pill>expires {h(acceptance.get('expires_on', ''))}</span></p>"
            f"<p><strong>Approved by:</strong> {h(acceptance.get('approved_by', ''))}"
            f" on {h(acceptance.get('approved_on', ''))}</p>"
            f"<p><strong>Rationale:</strong> {h(acceptance.get('rationale', ''))}</p>"
            "<h3>accepted scope</h3>"
            + paragraphs(acceptance.get("scope", []))
        )
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
    created_label = item["created"]
    stale = False
    if today is not None:
        age = (today - dt.date.fromisoformat(item["created"])).days
        if age >= 0:
            created_label = f"{item['created']} ({age}d)"
        last_activity = max([item["created"]] + [n["date"] for n in item.get("notes", [])])
        stale = stale_days > 0 and (today - dt.date.fromisoformat(last_activity)).days > stale_days
    meta_pills = [f"<span class=pill>{h(item[key])}</span>" for key in ["type", "area", "priority", "status"]]
    meta_pills.append(f"<span class=pill>{h(created_label)}</span>")
    if stale:
        meta_pills.append(f'<span class="pill stale" title="no activity in over {stale_days} days">stale</span>')
    if today is not None:
        acceptance_state = risk_acceptance_state(item, today)
        if acceptance_state != "none":
            meta_pills.append(risk_acceptance_pill(acceptance_state))
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


def backlog_row(item: dict[str, Any], idx: int, today: dt.date) -> str:
    risk_class = f"risk-{h(item['risk']['level'])}"
    acceptance_state = risk_acceptance_state(item, today)
    acceptance = item.get("risk_acceptance") if isinstance(item.get("risk_acceptance"), dict) else {}
    active_class = " active" if idx == 0 else ""
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
            *prose_values(item, "problem"),
            *prose_values(item, "value"),
            *prose_values(item, "scope"),
            str(acceptance.get("approved_by", "")),
            str(acceptance.get("rationale", "")),
            *(acceptance.get("scope", []) if isinstance(acceptance.get("scope"), list) else []),
        ]
    )
    return (
        f"<tr class=\"item-row{active_class}\" data-item-id=\"{h(item['id'])}\" data-order=\"{idx}\""
        f" data-title=\"{h(item['title'].lower())}\" data-type=\"{h(item['type'])}\""
        f" data-area=\"{h(item['area'])}\" data-priority=\"{h(item['priority'])}\""
        f" data-status=\"{h(item['status'])}\" data-risk=\"{h(item['risk']['level'])}\""
        f" data-acceptance=\"{h(acceptance_state)}\""
        f" data-created=\"{h(item['created'])}\" data-search=\"{h(search_text.lower())}\" tabindex=\"0\">"
        f"<td><strong><a class=\"item-title-link\" href=\"#{h(item['id'])}\">{h(item['id'])}</a></strong>"
        f"<div class=muted>{h(item['_path'])}</div></td>"
        f"<td><a class=\"item-title-link\" href=\"#{h(item['id'])}\">{h(item['title'])}</a></td>"
        f"<td>{h(item['type'])}</td><td>{h(item['area'])}</td>"
        f"<td>{h(item['priority'])}</td><td>{h(item['status'])}"
        + (risk_acceptance_pill(acceptance_state) if acceptance_state != "none" else "")
        + "</td>"
        f"<td class=risk-cell><span class=\"pill {risk_class}\">{h(item['risk']['level'])}</span></td></tr>"
    )


NOTE_ENVELOPE_KEYS = {
    "schema_version", "id", "title", "created", "last_reviewed", "author",
    "status", "tags", "body", "_path", "_files",
}
NOTE_RENDER_TEXT_LIMIT = 20000


def note_detail(note: dict[str, Any], *, payloads: dict[str, bytes], github_repo: str | None = None) -> str:
    content = ""
    search_parts = [canonical_json({k: v for k, v in note.items() if not k.startswith("_")})]
    if "body" in note:
        body = note["body"]
        content += f"<pre>{h(body if isinstance(body, str) else canonical_json(body))}</pre>"
    extras = {k: v for k, v in note.items() if k not in NOTE_ENVELOPE_KEYS}
    if extras:
        content += f"<h3>extra fields</h3><pre>{h(canonical_json(extras))}</pre>"
    for rel_file in note["_files"]:
        file_rel = f"{note['_path']}/{rel_file}"
        search_parts.append(rel_file)
        if Path(rel_file).suffix.lower() in NOTE_IMAGE_SUFFIXES:
            content += (
                f"<h3>{h(rel_file)}</h3>"
                f'<p><img class=note-image src="{h(file_rel)}" alt="{h(rel_file)}" loading=lazy></p>'
            )
            continue
        text = payloads[file_rel].decode("utf-8", errors="replace")
        search_parts.append(text[:NOTE_RENDER_TEXT_LIMIT])
        content += f"<h3>{h(rel_file)}</h3><pre>{h(text[:NOTE_RENDER_TEXT_LIMIT])}</pre>"
        if len(text) > NOTE_RENDER_TEXT_LIMIT:
            content += f'<p class=muted>truncated — <a href="{h(file_rel)}">full file</a></p>'
    if github_repo:
        content += "<h3>manage</h3><p class=item-actions>" + "".join(
            f'<a class=button href="{h(feedback_issue_url(github_repo, note["id"], action, hint))}">{h(label)}</a>'
            for action, label, hint in [
                ("archive-note", "Archive", "<optional reason>"),
                ("delete-note", "Delete", "<optional reason>"),
            ]
        ) + "</p>"
    meta = "".join(
        f"<span class=pill>{h(value)}</span>"
        for value in [
            note["created"],
            *([f"reviewed {note['last_reviewed']}"] if note.get("last_reviewed") else []),
            note["author"],
            note["status"],
            *note.get("tags", []),
        ]
    )
    search = " ".join(search_parts).lower()
    return (
        f'<article id="{h(note["id"])}" class="card note-entry" data-status="{h(note["status"])}" data-note-search="{h(search)}">'
        f"<h2>{h(note['id'])} · {h(note['title'])}</h2><div class=\"meta\">{meta}</div>{content}</article>"
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


def docs_status_pill(status: str) -> str:
    tone = {"active": "risk-low", "reference": "", "superseded": "risk-medium", "archived": ""}.get(status, "")
    return f'<span class="pill {tone}">{h(status)}</span>'


def project_health_body(
    health: dict[str, Any],
    project: dict[str, Any],
    commit: str,
    docs_summary: dict[str, Any] | None = None,
) -> str:
    rule_labels = {
        "zero_notes": "no notes",
        "blocked_without_notes": "blocked without notes",
        "stale_in_progress": "stale in progress",
        "stale_blocked": "stale blocked",
        "stale_now": "stale now",
    }
    def finding_pills(finding: dict[str, Any]) -> str:
        return "".join(f'<span class="pill stale">{h(rule_labels[rule])}</span>' for rule in finding["rules"])

    item_rows = "".join(
        f'<tr><td><a href="backlog.html#{h(finding["id"])}"><strong>{h(finding["id"])}</strong></a>'
        f'<div class=muted>{h(finding["title"])}</div></td>'
        f'<td>{h(finding["last_activity"])} ({finding["age_days"]}d)</td>'
        f'<td>{finding_pills(finding)}</td></tr>'
        for finding in health["item_findings"]
    ) or '<tr><td colspan=3 class=muted>No backlog findings.</td></tr>'
    note_rows = "".join(
        f'<tr><td><a href="notes.html#{h(finding["id"])}"><strong>{h(finding["id"])}</strong></a>'
        f'<div class=muted>{h(finding["title"])}</div></td>'
        f'<td>{h(finding["freshness"])} ({finding["age_days"]}d)</td></tr>'
        for finding in health["note_findings"]
    ) or '<tr><td colspan=2 class=muted>No stale active notes.</td></tr>'
    docs_card = ""
    if docs_summary is not None:
        count = len(docs_summary["overdue"]) + len(docs_summary["missing_from_index"]) + docs_summary["dead_link_count"]
        docs_card = card("Docs", f"<p>{count} finding(s).</p><p><a href=docs.html>Open docs details →</a></p>")
    return (
        '<div class=page-heading><div><h1>Health</h1>'
        f'<p class=muted>Report-only findings at <code>{h(project["backlog_ref"])}</code> @ <code>{h(commit[:10])}</code>.</p></div></div>'
        '<section class=grid>'
        + card("Backlog", f'<p>{len(health["item_findings"])} item(s) with findings.</p><p>Age threshold: {health["stale_days"]} days.</p>')
        + card("Notes", f'<p>{len(health["note_findings"])} active note(s) need review.</p><p>Review threshold: {health["notes_stale_days"]} days.</p>')
        + docs_card
        + '</section><h2>Backlog findings</h2><div class=backlog-list><table><thead><tr><th>Item</th><th>Last activity</th><th>Findings</th></tr></thead><tbody>'
        + item_rows
        + '</tbody></table></div><h2>Notes to review</h2><div class=backlog-list><table><thead><tr><th>Note</th><th>Freshness date</th></tr></thead><tbody>'
        + note_rows + '</tbody></table></div>'
    )


def docs_health_body(
    docs: list[dict[str, Any]],
    health: dict[str, Any],
    docs_cfg: dict[str, Any],
    project: dict[str, Any],
    commit: str,
) -> str:
    """The docs-health page: one row per page plus the standing findings the
    hub replaces one-off documentation audits with (stale reviews, index
    gaps, dead relative links)."""
    status_pills = "".join(
        f"<span class=pill>{h(status)}: {count}</span>" for status, count in sorted(health["by_status"].items())
    )
    summary_cards = [
        card("Statuses", f"<p>{len(docs)} pages in <code>{h(docs_cfg['dir'])}</code>.</p><p>{status_pills}</p>"),
        card(
            "Review freshness",
            f"<p>{len(health['overdue'])} active/reference page(s) not reviewed in the last "
            f"{health['stale_days']} days.</p>"
            + "".join(f'<p class=muted><code>{h(f)}</code></p>' for f in health["overdue"][:8]),
        ),
        card(
            "Index coverage",
            (
                f"<p>{len(health['missing_from_index'])} page(s) missing from <code>{h(docs_cfg['index_file'])}</code>.</p>"
                + "".join(f'<p class=muted><code>{h(f)}</code></p>' for f in health["missing_from_index"][:8])
                if docs_cfg["index_file"]
                else "<p class=muted>No index file configured (<code>docs_index_file</code>).</p>"
            ),
        ),
        card(
            "Links",
            f"<p>{health['dead_link_count']} dead relative link(s) between docs.</p>"
            "<p class=muted>Only targets inside the docs tree are checked.</p>",
        ),
    ]

    rows = []
    for doc in docs:
        header = doc["header"]
        reviewed = header.get("last_reviewed", "")
        if reviewed and doc["reviewed_days"] is not None:
            reviewed = f"{reviewed} ({doc['reviewed_days']}d)"
        findings = []
        if doc["overdue"]:
            findings.append('<span class="pill stale">review overdue</span>')
        if doc["missing_from_index"]:
            findings.append('<span class="pill stale">not in index</span>')
        findings.extend(
            f'<span class="pill stale" title="dead link">→ {h(target)}</span>' for target in doc["dead_links"]
        )
        detail = h(header.get("source_of_truth", "")) or h(header.get("audience", ""))
        if header.get("superseded_by"):
            detail += f' <span class=pill>superseded by <code>{h(header["superseded_by"])}</code></span>'
        rows.append(
            f"<tr><td><strong>{h(doc['file'])}</strong><div class=muted>{detail}</div></td>"
            f"<td>{docs_status_pill(header['status'])}</td>"
            f"<td>{h(reviewed) if header.get('last_reviewed') else '<span class=muted>—</span>'}</td>"
            f"<td>{' '.join(findings) if findings else '<span class=muted>ok</span>'}</td></tr>"
        )
    table = (
        "<div class=backlog-list><table>"
        '<colgroup><col style="width:44%"><col style="width:12%"><col style="width:16%"><col style="width:28%"></colgroup>'
        "<thead><tr><th>Page</th><th>Status</th><th>Last reviewed</th><th>Findings</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table></div>"
    )
    return (
        '<div class=page-heading><div><h1>Docs health</h1>'
        f"<p class=muted>Frontmatter contract read from <code>{h(docs_cfg['dir'])}</code> at "
        f"<code>{h(project['backlog_ref'])}</code> @ <code>{h(commit[:10])}</code>. "
        "Headers are enforced by validate/build; the findings below are report-only.</p></div>"
        f"<div class=heading-meta><span class=pill>{len(docs)} pages</span>"
        f"<span class=pill>threshold {health['stale_days']}d</span></div></div>"
        f"<section class=grid>{''.join(summary_cards)}</section>"
        f"<section style=\"margin-top:14px\">{table}</section>"
    )


def render_site(
    cfg: dict[str, Any],
    source,
    items: list[dict[str, Any]],
    done_entries: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    commit: str,
    github: dict[str, Any],
    out: Path,
    *,
    docs: list[dict[str, Any]] | None = None,
    docs_source=None,
    docs_cfg: dict[str, Any] | None = None,
) -> None:
    project = cfg["project"]
    name = project["name"]
    has_github = bool(project["github_repo"])
    issues = github["issues"]
    issue_error = github["issue_error"]
    with_docs = docs is not None and docs_source is not None and docs_cfg is not None

    (out / "assets").mkdir(parents=True, exist_ok=True)
    (out / "data").mkdir(parents=True, exist_ok=True)
    (out / "assets/styles.css").write_text(CSS, encoding="utf-8")

    generated_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    css_version = "?v=" + generated_at.replace("-", "").replace(":", "").replace("+", "")
    today = dt.date.fromisoformat(generated_at[:10])
    docs_report = docs_health(docs, docs_source, docs_cfg, today) if with_docs else None
    health_cfg = health_settings(load_project_config(source))
    health = project_health(items, notes, health_cfg, today)
    index_data = {
        "backlog": items,
        "commit": commit,
        "done": done_entries,
        "feedback_error": issue_error,
        "feedback_issues": issues,
        "generated_at": generated_at,
        "health": health,
        "notes": notes,
        "ref": project["backlog_ref"],
        "schema_version": 1,
    }
    if with_docs:
        index_data["docs"] = {
            "dir": docs_cfg["dir"],
            "health": docs_report,
            "pages": [
                {
                    "dead_links": doc["dead_links"],
                    "file": doc["file"],
                    "header": doc["header"],
                    "missing_from_index": doc["missing_from_index"],
                    "overdue": doc["overdue"],
                    "reviewed_days": doc["reviewed_days"],
                }
                for doc in docs
            ],
        }
    (out / "data/index.json").write_text(canonical_json(index_data), encoding="utf-8")
    (cfg["paths"]["cache"] / "index.json").write_text(canonical_json(index_data), encoding="utf-8")

    active_items = [i for i in items if i["status"] != "archived"]
    archived_count = len(items) - len(active_items)

    # Dashboard counts cover active items only, matching what the linked
    # backlog views show by default; archived is reported as its own number.
    by_status: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    high_risk = 0
    acceptance_counts: dict[str, int] = {}
    for item in active_items:
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1
        by_priority[item["priority"]] = by_priority.get(item["priority"], 0) + 1
        high_risk += item["risk"]["level"] == "high"
        acceptance_state = risk_acceptance_state(item, today)
        if acceptance_state != "none":
            acceptance_counts[acceptance_state] = acceptance_counts.get(acceptance_state, 0) + 1

    def pills(counter: dict[str, int]) -> str:
        return "".join(f"<span class=pill>{h(k)}: {v}</span>" for k, v in sorted(counter.items()))

    latest_done = f"latest: {h(done_entries[0]['date'])}" if done_entries else "no entries yet"
    archived_note = (
        f'<p class=muted>+ {archived_count} archived (hidden from default views).</p>' if archived_count else ""
    )
    home = [
        card("Backlog", f"<p>{len(active_items)} active items from <code>{h(project['backlog_dir'])}</code>.</p><p>{pills(by_status)}</p><p>{pills(by_priority)}</p>{archived_note}<p><a href=backlog.html>Open backlog →</a></p>"),
        card(
            "Risk",
            f"<p>{high_risk} high-risk item(s).</p>"
            f"<p>{pills(acceptance_counts) or '<span class=muted>No risk acceptances.</span>'}</p>"
            '<p><a href="backlog.html?risk=high">View high-risk →</a></p>',
        ),
        card("Done", f"<p>{len(done_entries)} completed-work entries ({latest_done}).</p><p><a href=done.html>View done →</a></p>"),
        card("Notes", (
            f"<p>{sum(1 for n in notes if n['status'] == 'active')} active agent note(s)"
            + (f" (latest: {h(notes[0]['created'])})" if notes else "")
            + ".</p><p><a href=notes.html>Browse notes →</a></p>"
        )),
        card("Health", (
            f'<p>{len(health["item_findings"])} backlog finding(s), '
            f'{len(health["note_findings"])} note(s) to review.</p>'
            '<p><a href=health.html>Open health report →</a></p>'
        )),
        card("Baseline", f"<p><code>{h(project['backlog_ref'])}</code> @ <code>{h(commit[:10])}</code></p><p class=muted>generated {h(generated_at)}</p>"),
    ]
    if with_docs:
        finding_pills = "".join(
            f'<span class="pill stale">{count} {h(label)}</span>'
            for count, label in [
                (len(docs_report["overdue"]), "overdue review(s)"),
                (len(docs_report["missing_from_index"]), "not in index"),
                (docs_report["dead_link_count"], "dead link(s)"),
            ]
            if count
        )
        docs_summary = (
            f"<p>{len(docs)} pages in <code>{h(docs_cfg['dir'])}</code>.</p><p>{pills(docs_report['by_status'])}</p>"
            + (f"<p>{finding_pills}</p>" if finding_pills else "<p class=muted>No findings.</p>")
            + "<p><a href=docs.html>Open docs health →</a></p>"
        )
        home.insert(4, card("Docs health", docs_summary))
    if has_github:
        issue_links = "".join(
            f'<p><a href="{h(i["url"])}">#{h(i["number"])}</a> {h(i["title"])}</p>' for i in issues[:5]
        )
        home.insert(2, card(
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
            generated_at=generated_at,
            css_version=css_version,
            with_docs=with_docs,
        ),
        encoding="utf-8",
    )

    rows = [backlog_row(item, idx, today) for idx, item in enumerate(items)]
    detail_cards = "".join(
        item_detail(item, active=idx == 0, github_repo=project["github_repo"], today=today, stale_days=health_cfg["health_stale_days"])
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
    acceptance_options = options(["none", "active", "expiring", "expired", "invalid"])
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
    acceptance: document.getElementById("filter-acceptance"),
    sort: document.getElementById("sort-by"),
    dir: document.getElementById("sort-dir"),
  };
  const priorityRank = { now: 0, next: 1, later: 2 };
  const riskRank = { high: 0, medium: 1, low: 2 };
  const paramMap = { q: "query", type: "type", area: "area", priority: "priority", status: "status", risk: "risk", acceptance: "acceptance", sort: "sort", dir: "dir" };
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
      && (!controls.risk?.value || row.dataset.risk === controls.risk.value)
      && (!controls.acceptance?.value || row.dataset.acceptance === controls.acceptance.value);
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
    ["query", "type", "area", "priority", "status", "risk", "acceptance"].forEach((key) => {
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
        f'<div class=control><label for=filter-acceptance>Risk acceptance</label><select id=filter-acceptance>{acceptance_options}</select></div>'
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
        page(name, "Backlog", backlog_body, active="backlog", generated_at=generated_at, css_version=css_version, with_docs=with_docs),
        encoding="utf-8",
    )

    note_payloads: dict[str, bytes] = {}
    for note in notes:
        for rel_file in note["_files"]:
            file_rel = f"{note['_path']}/{rel_file}"
            data = source.read_bytes(file_rel)
            note_payloads[file_rel] = data
            dest = out / file_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

    note_status_options = (
        '<option value="">Active</option><option value=archived>Archived</option><option value=all>All</option>'
    )
    notes_body = (
        "<h1>Notes</h1><p class=muted>Agent memory: durable findings written by LLM agents for LLM agents "
        "(audit results, discovered bugs, gotchas). Content and structure belong to the agents; "
        "humans browse, search, archive, or delete.</p>"
        "<form class=toolbar id=notes-controls>"
        '<div class=control><label for=note-search>Search</label><input id=note-search type=search autocomplete=off></div>'
        f'<div class=control><label for=note-status>Status</label><select id=note-status>{note_status_options}</select></div>'
        '<div class=toolbar-actions><button class=button id=clear-note-search type=button>Reset</button></div></form>'
        '<div class=empty-state id=note-empty-state hidden>No matching notes.</div>'
        + "".join(note_detail(note, payloads=note_payloads, github_repo=project["github_repo"]) for note in notes)
        + ("<p class=muted>No notes yet.</p>" if not notes else "")
        + """
<script>
document.documentElement.classList.add("js");
(function () {
  const form = document.getElementById("notes-controls");
  const search = document.getElementById("note-search");
  const statusSelect = document.getElementById("note-status");
  const reset = document.getElementById("clear-note-search");
  const entries = Array.from(document.querySelectorAll(".note-entry"));
  const empty = document.getElementById("note-empty-state");
  if (!search || !entries.length) return;

  function applyNoteFilters() {
    const query = search.value.trim().toLowerCase();
    const status = statusSelect ? statusSelect.value : "";
    let visibleCount = 0;
    entries.forEach((entry) => {
      const statusOk = status === "all"
        || (status ? entry.dataset.status === status : entry.dataset.status !== "archived");
      const visible = statusOk && (!query || entry.dataset.noteSearch.includes(query));
      entry.hidden = !visible;
      if (visible) visibleCount += 1;
    });
    if (empty) empty.hidden = visibleCount > 0;
  }

  form?.addEventListener("submit", (event) => event.preventDefault());
  search.addEventListener("input", applyNoteFilters);
  statusSelect?.addEventListener("change", applyNoteFilters);
  reset?.addEventListener("click", () => {
    search.value = "";
    if (statusSelect) statusSelect.value = "";
    applyNoteFilters();
  });
  applyNoteFilters();
})();
</script>
"""
    )
    (out / "notes.html").write_text(
        page(name, "Notes", notes_body, active="notes", generated_at=generated_at, css_version=css_version, with_docs=with_docs),
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
        page(name, "Done", done_body, active="done", generated_at=generated_at, css_version=css_version, with_docs=with_docs),
        encoding="utf-8",
    )

    (out / "health.html").write_text(
        page(
            name,
            "Health",
            project_health_body(health, project, commit, docs_report),
            active="health",
            generated_at=generated_at,
            css_version=css_version,
            with_docs=with_docs,
        ),
        encoding="utf-8",
    )

    if with_docs:
        (out / "docs.html").write_text(
            page(
                name,
                "Docs health",
                docs_health_body(docs, docs_report, docs_cfg, project, commit),
                active="docs",
                generated_at=generated_at,
                css_version=css_version,
                with_docs=True,
            ),
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


SAMPLE_NOTE = {
    "schema_version": 1,
    "id": "NOTE-20260703-sample-finding",
    "title": "Self-test sample note",
    "created": "2026-07-03",
    "author": "agent:self-test",
    "status": "active",
    "tags": ["audit"],
    "body": {"finding": "Sample structured finding.", "confidence": "high"},
    "custom_field": ["agents may add whatever structure they need"],
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
    def __init__(self, files: dict[str, Any]):
        self.files = files
        self.label = "memory"

    def list_files(self) -> list[str]:
        return sorted(self.files)

    def read(self, rel_path: str) -> str:
        return self.files[rel_path]

    def read_bytes(self, rel_path: str) -> bytes:
        value = self.files[rel_path]
        return value if isinstance(value, bytes) else value.encode("utf-8")

    def size(self, rel_path: str) -> int:
        return len(self.read_bytes(rel_path))


def cmd_self_test(_args: argparse.Namespace) -> int:
    schema = parse_json(str(BUNDLED_SCHEMA), BUNDLED_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)

    ci_template = MONITORED_PROJECT_CI_TEMPLATE.read_text(encoding="utf-8")
    for required in [
        'paths:\n      - "docs/backlog/**"',
        'permissions:\n  contents: read',
        'repository: pioootrek/llm-ops-hub',
        'ref: "<hub-ref>"',
        'path: .llm-ops-hub',
        'persist-credentials: false',
        'hub.py fmt --backlog-dir docs/backlog',
        'git diff --exit-code',
        'hub.py validate --backlog-dir docs/backlog',
    ]:
        assert required in ci_template, f"monitored-project CI template missing {required!r}"
    assert ci_template.count("uses: actions/checkout@v7") == 2, "CI template needs separate project and hub checkouts"
    assert "ref: main" not in ci_template, "monitored-project CI must pin the hub version"
    fmt_pos = ci_template.index("hub.py fmt")
    diff_pos = ci_template.index("git diff --exit-code")
    validate_pos = ci_template.index("hub.py validate")
    assert fmt_pos < diff_pos < validate_pos, "CI template must check fmt changes before validate"

    good = canonical_json(SAMPLE_ITEM)
    assert canonical_json(parse_json("x", good)) == good, "canonical form must be idempotent"

    source = _MemorySource({"feature/FEAT-20260703-sample-item.json": good})
    items, errors = validate_items(source, schema, {})
    assert not errors, f"valid item reported errors: {errors}"
    assert items[0]["id"] == "FEAT-20260703-sample-item"

    relaxed = json.loads(good)
    del relaxed["value"]
    relaxed["scope"] = "not-a-list"
    assert prose_values(relaxed, "value") == [], "missing prose field must yield empty search text"
    assert prose_values(relaxed, "scope") == [], "malformed prose field must yield empty search text"
    assert prose_values(relaxed, "problem") == ["Sample problem paragraph."]

    def expect_error(files: dict[str, str], needle: str) -> None:
        _, errs = validate_items(_MemorySource(files), schema, {})
        assert any(needle in e for e in errs), f"expected error containing {needle!r}, got {errs}"

    bad = dict(SAMPLE_ITEM)
    bad["priority"] = "someday"
    expect_error({"feature/FEAT-20260703-sample-item.json": canonical_json(bad)}, "priority")
    _, structured_errors = validate_items(
        _MemorySource({"feature/FEAT-20260703-sample-item.json": canonical_json(bad)}), schema, {}
    )
    assert structured_errors[0].file.endswith("feature/FEAT-20260703-sample-item.json")
    assert structured_errors[0].rule == "schema"
    assert "priority" in structured_errors[0].message
    assert structured_errors[0].as_dict() == {
        "file": structured_errors[0].file,
        "rule": "schema",
        "message": structured_errors[0].message,
    }
    assert json.loads(diagnostics_json(structured_errors)) == [structured_errors[0].as_dict()]
    assert diagnostics_json([]) == "[]"

    bad = json.loads(good)
    del bad["risk"]["rollback"]
    expect_error({"feature/FEAT-20260703-sample-item.json": canonical_json(bad)}, "rollback")

    accepted = json.loads(good)
    accepted["risk_acceptance"] = {
        "approved_by": "human:security-owner",
        "approved_on": "2026-07-03",
        "expires_on": "2026-07-31",
        "rationale": "Time-bounded self-test decision.",
        "scope": ["Only the self-test fixture."],
    }
    _, errs = validate_items(
        _MemorySource({"feature/FEAT-20260703-sample-item.json": canonical_json(accepted)}),
        schema,
        {},
    )
    assert not errs, f"valid risk acceptance reported errors: {errs}"
    assert risk_acceptance_state(accepted, dt.date(2026, 7, 1)) == "active"
    assert risk_acceptance_state(accepted, dt.date(2026, 7, 20)) == "expiring"
    assert risk_acceptance_state(accepted, dt.date(2026, 8, 1)) == "expired"
    accepted["_path"] = "feature/FEAT-20260703-sample-item.json"
    accepted["risk_acceptance"]["rationale"] = "Contains <untrusted> text."
    accepted_html = item_detail(accepted, today=dt.date(2026, 7, 20))
    assert "risk acceptance: expiring" in accepted_html
    assert "&lt;untrusted&gt;" in accepted_html and "<untrusted>" not in accepted_html
    bad_acceptance = json.loads(canonical_json(accepted))
    del bad_acceptance["_path"]
    del bad_acceptance["risk_acceptance"]["approved_by"]
    expect_error(
        {"feature/FEAT-20260703-sample-item.json": canonical_json(bad_acceptance)},
        "approved_by",
    )

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

    unsafe_item = json.loads(good)
    unsafe_item["title"] = '<script>alert("item")</script> x" onfocus="alert(1)'
    unsafe_item["_path"] = "feature/FEAT-20260703-sample-item.json"
    unsafe_item_html = item_detail(unsafe_item, today=dt.date(2026, 7, 10))
    assert '&lt;script&gt;alert(&quot;item&quot;)&lt;/script&gt; x&quot; onfocus=&quot;alert(1)' in unsafe_item_html
    assert '<script>alert("item")</script>' not in unsafe_item_html, "item title must be HTML-escaped"
    unsafe_item_row = backlog_row(unsafe_item, 0, dt.date(2026, 7, 10))
    assert 'data-title="&lt;script&gt;alert(&quot;item&quot;)&lt;/script&gt; x&quot; onfocus=&quot;alert(1)"' in unsafe_item_row
    assert 'x" onfocus="alert(1)' not in unsafe_item_row, "backlog row attributes must escape quotes"

    bad = json.loads(good)
    bad["notes"][0]["author"] = ""
    expect_error({"feature/FEAT-20260703-sample-item.json": canonical_json(bad)}, "author")

    index_errors = validate_index(_MemorySource({"feature/FEAT-20260703-sample-item.json": good}), items, [])
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

    unsafe_done = json.loads(good_done)
    unsafe_done["title"] = 'x" onfocus="alert(1)'
    unsafe_done_html = done_detail(unsafe_done)
    assert 'data-done-search="' in unsafe_done_html and 'x&quot; onfocus=&quot;alert(1)' in unsafe_done_html
    assert 'x" onfocus="alert(1)' not in unsafe_done_html, "done entry attributes must escape quotes"

    note_schema = parse_json(str(BUNDLED_NOTE_SCHEMA), BUNDLED_NOTE_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(note_schema)
    good_note = canonical_json(SAMPLE_NOTE)
    note_dir = "notes/NOTE-20260703-sample-finding"

    parsed_notes, errs = validate_notes(_MemorySource({
        f"{note_dir}/note.json": good_note,
        f"{note_dir}/findings.md": "# Findings\n\nFree-form markdown payload.",
        f"{note_dir}/shot.png": b"\x89PNG fake image bytes",
    }), note_schema)
    assert not errs, f"valid note dir reported errors: {errs}"
    assert parsed_notes[0]["id"] == "NOTE-20260703-sample-finding"
    assert parsed_notes[0]["_files"] == ["findings.md", "shot.png"], f"payload listing wrong: {parsed_notes[0]['_files']}"

    reviewed_note = json.loads(good_note)
    reviewed_note["last_reviewed"] = "2026-07-10"
    reviewed_notes, errs = validate_notes(
        _MemorySource({f"{note_dir}/note.json": canonical_json(reviewed_note)}), note_schema
    )
    assert not errs, f"reviewed note reported errors: {errs}"
    reviewed_note_html = note_detail(reviewed_notes[0], payloads={})
    assert f'id="{reviewed_note["id"]}"' in reviewed_note_html
    assert "reviewed 2026-07-10" in reviewed_note_html
    bad_reviewed_note = dict(reviewed_note, last_reviewed="2026-02-30")
    _, errs = validate_notes(
        _MemorySource({f"{note_dir}/note.json": canonical_json(bad_reviewed_note)}), note_schema
    )
    assert any(error.rule == "note.last_reviewed" for error in errs), "invalid note review date not reported"

    health_items = [json.loads(good)]
    health_items[0]["_path"] = "feature/FEAT-20260703-sample-item.json"
    health_items[0]["status"] = "in-progress"
    health_items[0]["priority"] = "now"
    health_items[0]["notes"] = []
    project_report = project_health(
        health_items,
        reviewed_notes,
        {"health_stale_days": 5, "notes_stale_days": 5},
        dt.date(2026, 7, 20),
    )
    assert project_report["item_findings"][0]["rules"] == ["zero_notes", "stale_in_progress", "stale_now"]
    assert project_report["note_findings"] == [
        {
            "age_days": 10,
            "id": reviewed_note["id"],
            "freshness": "2026-07-10",
            "title": reviewed_note["title"],
        }
    ]
    disabled_report = project_health(
        health_items,
        reviewed_notes,
        {"health_stale_days": 0, "notes_stale_days": 0},
        dt.date(2026, 7, 20),
    )
    assert disabled_report["item_findings"][0]["rules"] == ["zero_notes"]
    assert disabled_report["note_findings"] == []
    assert health_settings({}) == {"health_stale_days": 45, "notes_stale_days": 90}
    try:
        health_settings({"health_stale_days": "later"})
    except ContractError:
        pass
    else:
        raise AssertionError("invalid health threshold accepted")
    project_report["item_findings"][0]["title"] = '<script>alert("health")</script>'
    health_html = project_health_body(project_report, {"backlog_ref": "main"}, "abcdef123456")
    assert '&lt;script&gt;alert(&quot;health&quot;)&lt;/script&gt;' in health_html
    assert '<script>' not in health_html, "health findings must be HTML-escaped"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cache_path = tmp_path / "cache"
        cache_path.mkdir()
        render_site(
            {
                "project": {
                    "backlog_dir": "docs/backlog",
                    "backlog_ref": "main",
                    "github_repo": "",
                    "name": "Self-test",
                },
                "paths": {"cache": cache_path},
            },
            _MemorySource({}),
            health_items,
            [],
            reviewed_notes,
            "abcdef1234567890",
            {"issue_error": None, "issues": []},
            tmp_path / "site",
        )
        rendered_index = parse_json("rendered index", (tmp_path / "site/data/index.json").read_text(encoding="utf-8"))
        assert rendered_index["health"]["item_findings"], "rendered index missing backlog health"
        rendered_health = (tmp_path / "site/health.html").read_text(encoding="utf-8")
        assert "Backlog findings" in rendered_health and "Notes to review" in rendered_health

    unsafe_note = dict(parsed_notes[0])
    unsafe_note["body"] = '<script>alert("note")</script> x" onfocus="alert(1)'
    unsafe_image = 'shot" onerror="alert(1).png'
    unsafe_note["_files"] = [unsafe_image]
    unsafe_note_html = note_detail(unsafe_note, payloads={
        f"{note_dir}/{unsafe_image}": b"\x89PNG fake image bytes",
    })
    assert '&lt;script&gt;alert(&quot;note&quot;)&lt;/script&gt;' in unsafe_note_html
    assert '<script>alert("note")</script>' not in unsafe_note_html, "note body must be HTML-escaped"
    assert 'data-note-search="' in unsafe_note_html and '&quot; onfocus=\\&quot;alert(1)' in unsafe_note_html
    assert 'src="notes/NOTE-20260703-sample-finding/shot&quot; onerror=&quot;alert(1).png"' in unsafe_note_html
    assert 'shot" onerror="alert(1).png' not in unsafe_note_html, "note attributes must escape quotes"

    _, errs = validate_notes(_MemorySource({"notes/NOTE-20260703-wrong-dir/note.json": good_note}), note_schema)
    assert any("must match the directory name" in e for e in errs), f"note id/dir rule not enforced: {errs}"

    _, errs = validate_notes(_MemorySource({f"{note_dir}/findings.md": "orphan payload"}), note_schema)
    assert any("missing note.json" in e for e in errs), f"missing manifest not reported: {errs}"

    _, errs = validate_notes(_MemorySource({"notes/NOTE-20260703-loose.json": good_note}), note_schema)
    assert any("notes are directories" in e for e in errs), f"loose file under notes/ not reported: {errs}"

    _, errs = validate_notes(_MemorySource({
        f"{note_dir}/note.json": good_note,
        f"{note_dir}/payload.exe": b"nope",
    }), note_schema)
    assert any("not allowed in notes" in e for e in errs), f"extension allowlist not enforced: {errs}"

    _, errs = validate_notes(_MemorySource({
        f"{note_dir}/note.json": good_note,
        f"{note_dir}/huge.log": b"x" * (MAX_NOTE_FILE_BYTES + 1),
    }), note_schema)
    assert any("MB note file limit" in e for e in errs), f"size limit not enforced: {errs}"

    bad_note = json.loads(good_note)
    bad_note["created"] = "2026-07-04"
    _, errs = validate_notes(_MemorySource({f"{note_dir}/note.json": canonical_json(bad_note)}), note_schema)
    assert any("date part" in e for e in errs), f"note id/created rule not enforced: {errs}"

    bad_note = json.loads(good_note)
    del bad_note["author"]
    _, errs = validate_notes(_MemorySource({f"{note_dir}/note.json": canonical_json(bad_note)}), note_schema)
    assert any("author" in e for e in errs), f"note author requirement not enforced: {errs}"

    index = build_index(items, parsed_notes)
    assert index["notes"][0]["files"] == ["findings.md", "shot.png"], "index must list note payload files"
    assert index["notes"][0]["path"] == note_dir, "index must carry the note directory path"

    docs_schema = parse_json(str(BUNDLED_DOCS_SCHEMA), BUNDLED_DOCS_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(docs_schema)

    doc_header = {"audience": "self-test readers", "last_reviewed": "2026-07-01", "status": "active"}
    doc_body = "\n# Sample Playbook\n\nSee [other](other-page.md) and [index](playbooks.md).\n"
    good_doc = canonical_frontmatter(doc_header) + doc_body

    parsed_header, parsed_body = parse_frontmatter("x", good_doc)
    assert parsed_header == doc_header and parsed_body == doc_body, "frontmatter parse must round-trip"
    assert canonical_frontmatter(parsed_header) + parsed_body == good_doc, "canonical frontmatter must be idempotent"
    tricky = {"note": 'quote " and backslash \\ survive'}
    reparsed, _ = parse_frontmatter("x", canonical_frontmatter(tricky) + "body")
    assert reparsed == tricky, "escaping must round-trip"
    bare_header, _ = parse_frontmatter("x", '---\nstatus: no\n---\nbody')
    assert bare_header["status"] == "no", "bare scalars must stay strings, never YAML-coerced"

    def expect_doc_error(files: dict[str, Any], needle: str) -> None:
        _, errs = validate_docs(_MemorySource(files), docs_schema)
        assert any(needle in e for e in errs), f"expected docs error containing {needle!r}, got {errs}"

    index_doc = canonical_frontmatter(
        {"audience": "self-test readers", "last_reviewed": "2026-07-01", "status": "active"}
    ) + "\n# Index\n\n[sample](sample-playbook.md)\n"
    docs_tree = {
        "sample-playbook.md": good_doc,
        "playbooks.md": index_doc,
        "other-page.md": canonical_frontmatter(
            {"audience": "x", "status": "superseded", "superseded_by": "sample-playbook.md"}
        ) + "\n[dead](gone.md) [dir](backlog/) [out](https://example.com) [anchor](#x)\n",
        "AGENTS.md": "instruction files are not docs records",
        "backlog/feature/x.json": "{}",
    }
    parsed_docs, errs = validate_docs(_MemorySource(docs_tree), docs_schema)
    assert not errs, f"valid docs tree reported errors: {errs}"
    assert [d["file"] for d in parsed_docs] == ["other-page.md", "playbooks.md", "sample-playbook.md"], (
        "docs records must be the non-instruction top-level *.md files only"
    )

    expect_doc_error({"sample.md": "# No frontmatter\n"}, "missing frontmatter")
    expect_doc_error({"sample.md": '---\nstatus: "active"\n'}, "never closed")
    expect_doc_error({"sample.md": '---\nStatus: "active"\n---\n'}, "not `key: value`")
    expect_doc_error({"sample.md": '---\na: "1"\na: "2"\n---\n'}, "duplicate frontmatter key")
    expect_doc_error({"sample.md": '---\na: "unterminated\n---\n'}, "unterminated quoted value")
    bad_doc = dict(doc_header, status="draft")
    expect_doc_error({"sample.md": canonical_frontmatter(bad_doc) + doc_body}, "status")
    bad_doc = {"audience": "x", "status": "active"}
    expect_doc_error({"sample.md": canonical_frontmatter(bad_doc) + doc_body}, "last_reviewed")
    bad_doc = {"audience": "x", "status": "superseded"}
    expect_doc_error({"sample.md": canonical_frontmatter(bad_doc) + doc_body}, "superseded_by")
    bad_doc = dict(doc_header, last_reviewed="2026-02-30")
    expect_doc_error(
        {"sample.md": canonical_frontmatter(bad_doc) + doc_body}, "not a real calendar date"
    )  # matches the schema regex but must not reach docs_health(), which would crash on it
    unsorted = '---\nstatus: "archived"\naudience: "x"\n---\nbody'
    expect_doc_error({"sample.md": unsorted}, "canonical form")
    unquoted = '---\naudience: x\nstatus: "archived"\n---\nbody'
    expect_doc_error({"sample.md": unquoted}, "canonical form")
    expect_doc_error({"AGENTS.md": "only instruction files here"}, "no top-level *.md pages")

    assert doc_relative_links(docs_tree["other-page.md"]) == ["gone.md", "backlog/"], (
        "relative-link scan must skip external URLs and anchors"
    )

    health = docs_health(
        parsed_docs,
        _MemorySource(docs_tree),
        {"dir": "docs", "index_file": "playbooks.md", "stale_days": 60},
        dt.date(2026, 7, 10),
    )
    assert health["by_status"] == {"active": 2, "superseded": 1}, f"status counts wrong: {health['by_status']}"
    assert health["overdue"] == [], f"9-day-old review must not be overdue at 60d: {health['overdue']}"
    assert health["missing_from_index"] == ["other-page.md"], f"index gap not detected: {health['missing_from_index']}"
    by_file = {d["file"]: d for d in parsed_docs}
    assert by_file["other-page.md"]["dead_links"] == ["gone.md"], (
        f"dead link detection wrong (backlog/ exists as a dir): {by_file['other-page.md']['dead_links']}"
    )
    assert by_file["sample-playbook.md"]["dead_links"] == [], (
        f"links to existing pages must not be dead: {by_file['sample-playbook.md']['dead_links']}"
    )
    health = docs_health(
        parsed_docs,
        _MemorySource(docs_tree),
        {"dir": "docs", "index_file": "", "stale_days": 5},
        dt.date(2026, 7, 10),
    )
    assert health["overdue"] == ["playbooks.md", "sample-playbook.md"], (
        f"active/reference pages older than the threshold must be overdue: {health['overdue']}"
    )
    assert health["missing_from_index"] == [], "no index file configured means no index findings"

    unsafe_docs = [{
        "dead_links": [],
        "file": '<script>alert("file")</script>.md',
        "header": {
            "audience": "self-test readers",
            "last_reviewed": "2026-07-01",
            "source_of_truth": '<script>alert("docs")</script>',
            "status": "active",
        },
        "missing_from_index": False,
        "overdue": False,
        "reviewed_days": 9,
    }]
    unsafe_docs_html = docs_health_body(
        unsafe_docs,
        {
            "by_status": {"active": 1},
            "dead_link_count": 0,
            "missing_from_index": [],
            "overdue": [],
            "stale_days": 60,
        },
        {"dir": "docs", "index_file": "", "stale_days": 60},
        {"backlog_ref": "main"},
        "abc123",
    )
    assert '&lt;script&gt;alert(&quot;file&quot;)&lt;/script&gt;.md' in unsafe_docs_html
    assert '&lt;script&gt;alert(&quot;docs&quot;)&lt;/script&gt;' in unsafe_docs_html
    assert "<script>" not in unsafe_docs_html, "docs metadata must be HTML-escaped"

    assert docs_settings({}) is None, "docs module must default to disabled"
    settings = docs_settings({"docs_dir": "docs/", "docs_index_file": "playbooks.md"})
    assert settings == {"dir": "docs", "index_file": "playbooks.md", "stale_days": DOCS_STALE_DAYS_DEFAULT}, settings

    mixed = _MemorySource({
        "feature/FEAT-20260703-sample-item.json": good,
        "AGENTS.md": "top-level docs are ignored",
        "done/DONE-20260703-sample-entry.json": good_done,
        f"{note_dir}/note.json": good_note,
        f"{note_dir}/findings.md": "payload",
    })
    assert item_files(mixed) == ["feature/FEAT-20260703-sample-item.json"], "done/note/doc files must not be treated as items"
    assert done_files(mixed) == ["done/DONE-20260703-sample-entry.json"]
    assert note_files(mixed) == [f"{note_dir}/findings.md", f"{note_dir}/note.json"]
    assert note_manifest_files(mixed) == [f"{note_dir}/note.json"]

    hidden = _MemorySource({
        "feature/FEAT-20260703-sample-item.json": good,
        ".hidden.json": "junk",
        "done/.keep": "",
        "notes/.gitkeep": "",
        f"{note_dir}/note.json": good_note,
        f"{note_dir}/.DS_Store": b"junk",
    })
    assert item_files(hidden) == ["feature/FEAT-20260703-sample-item.json"], "hidden top-level files must be ignored"
    assert done_files(hidden) == [], "hidden files under done/ must be ignored"
    _, errs = validate_notes(hidden, note_schema)
    assert not errs, f"hidden files under notes/ must be ignored: {errs}"

    url = feedback_issue_url("owner/repo", "FEAT-20260703-sample-item", "set-priority", "now")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert url.startswith("https://github.com/owner/repo/issues/new?"), f"unexpected issue URL: {url}"
    assert query["labels"] == [FEEDBACK_LABEL]
    assert query["title"] == [f"[{FEEDBACK_LABEL}] FEAT-20260703-sample-item: set-priority"]
    assert query["body"] == ["item: FEAT-20260703-sample-item\naction: set-priority\npayload: now\n"], (
        "the issue body is a producer/consumer contract - agents parse it per "
        "templates/AGENTS.md, so change both together"
    )

    state_key = parse_json("state-key", build_state_key({"name": "x"}, "abc123", {"issues": [], "issue_error": None}))
    assert set(state_key["schemas"]) == {
        "schema/backlog-item.schema.json",
        "schema/done-entry.schema.json",
        "schema/note.schema.json",
        "schema/docs-header.schema.json",
    }, "build state key must include bundled schema hashes"

    cfg = _resolve_hub_config(Path("test-config.json"), {"schema_version": 1, "project": {"repo_url": "git@example.com:x.git"}})
    assert cfg["project"]["backlog_ref"] == "main", "backlog_ref default must be main"
    assert cfg["project"]["backlog_dir"] == "docs/backlog", "backlog_dir default wrong"
    assert cfg["build"]["releases_keep"] == 20, "releases_keep default must be 20"
    assert cfg["paths"]["root"] == Path.home() / ".local" / "share" / "llm-ops-hub", (
        "runtime root default must use the generic LLM Ops Hub data directory"
    )
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
        if name == "validate":
            p.add_argument("--json", action="store_true", help="emit diagnostics as a JSON array on stdout")
        p.set_defaults(fn=fn)

    for name, fn in [("sync", cmd_sync), ("build", cmd_build), ("serve", cmd_serve)]:
        p = sub.add_parser(name)
        p.add_argument("--config", default=None, help="hub config file (default: LLM_OPS_HUB_CONFIG or config.json next to the tool)")
        if name == "build":
            p.add_argument("--force", action="store_true", help="render even if nothing changed since the last build")
        p.set_defaults(fn=fn)

    sub.add_parser("self-test").set_defaults(fn=cmd_self_test)

    args = parser.parse_args()
    try:
        return args.fn(args)
    except (ContractError, ConfigError) as exc:
        if getattr(args, "json", False):
            error = Diagnostic("", "configuration", str(exc))
            print(json.dumps([error.as_dict()], ensure_ascii=False))
            return 2
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
