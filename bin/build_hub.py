#!/usr/bin/env python3
"""
Builds the read-only WinPath LAN hub from local YAML records, staging docs/done,
and GitHub PR metadata.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

import yaml

ROOT = Path(os.environ.get("WINPATH_HUB_ROOT", str(Path.home() / "winpath-hub")))
REPO = "pioootrek/win-path-6"
MIRROR = ROOT / "mirror.git"
DATA = ROOT / "data"
CACHE = ROOT / "cache"
RELEASES = ROOT / "public_releases"
PUBLIC = ROOT / "public"

ALLOWED_TYPES = {"feature", "fix", "rework", "security"}
ALLOWED_STATUS = {"open", "in-progress", "blocked"}
ALLOWED_PRIORITY = {"now", "next", "later"}
ALLOWED_RISK = {"low", "medium", "high"}
ALLOWED_AREAS = {
    "pov-workspace",
    "manager-team",
    "org-access",
    "integrations",
    "ai",
    "notifications",
    "ui-system",
    "reporting",
    "billing",
    "release-ops",
    "testing",
    "onboarding",
    "analytics",
}
ALLOWED_RISK_DIMENSIONS = {
    "tenant-isolation",
    "auth-rbac",
    "data-migration",
    "data-exposure",
    "availability",
    "external-integration",
    "ui-only",
    "ci-release",
    "legal-compliance",
    "perf",
    "process",
}
REQUIRED_BACKLOG_FIELDS = [
    "schema_version",
    "id",
    "title",
    "type",
    "area",
    "status",
    "priority",
    "risk",
    "decision",
    "source",
    "summary",
    "problem",
    "value",
    "scope",
    "validation",
    "links",
]
REQUIRED_DONE_FIELDS = [
    "schema_version",
    "date",
    "title",
    "source",
    "summary",
    "changed",
    "validation",
    "followups",
]


class ContractError(Exception):
    pass


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ContractError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError(f"{path}: expected YAML mapping")
    return data


def require_fields(path: Path, data: dict[str, Any], required: list[str]) -> None:
    missing = [field for field in required if field not in data]
    if missing:
        raise ContractError(f"{path}: missing required fields: {', '.join(missing)}")


def expect_str(path: Path, data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{path}: {field} must be a non-empty string")
    return value.strip()


def validate_backlog(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    require_fields(path, data, REQUIRED_BACKLOG_FIELDS)
    if data["schema_version"] != 1:
        raise ContractError(f"{path}: schema_version must be 1")

    item_id = expect_str(path, data, "id")
    if not re.match(r"^(FEAT|FIX|RWK|SEC)-\d{3}$", item_id):
        raise ContractError(f"{path}: id must look like FEAT-001/FIX-001/RWK-001/SEC-001")

    item_type = expect_str(path, data, "type")
    if item_type not in ALLOWED_TYPES:
        raise ContractError(f"{path}: invalid type {item_type}")
    if item_type not in path.parts:
        raise ContractError(f"{path}: type must match parent directory")

    area = expect_str(path, data, "area")
    if area not in ALLOWED_AREAS:
        raise ContractError(f"{path}: invalid area {area}")

    status = expect_str(path, data, "status")
    if status not in ALLOWED_STATUS:
        raise ContractError(f"{path}: invalid status {status}")

    priority = expect_str(path, data, "priority")
    if priority not in ALLOWED_PRIORITY:
        raise ContractError(f"{path}: invalid priority {priority}")

    risk = data.get("risk")
    if not isinstance(risk, dict):
        raise ContractError(f"{path}: risk must be a mapping")
    level = risk.get("level")
    if level not in ALLOWED_RISK:
        raise ContractError(f"{path}: invalid risk.level {level}")
    dimensions = risk.get("dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        raise ContractError(f"{path}: risk.dimensions must be a non-empty list")
    for dimension in dimensions:
        if dimension not in ALLOWED_RISK_DIMENSIONS:
            raise ContractError(f"{path}: invalid risk dimension {dimension}")
    if level in {"medium", "high"} and not str(risk.get("rollback", "")).strip():
        raise ContractError(f"{path}: medium/high risk items need risk.rollback")

    for field in ["title", "summary", "problem", "value", "scope", "validation"]:
        expect_str(path, data, field)
    for field in ["decision", "source", "links"]:
        if not isinstance(data.get(field), dict):
            raise ContractError(f"{path}: {field} must be a mapping")

    links = data["links"]
    if not isinstance(links.get("prs"), list) or not isinstance(links.get("related_ids"), list):
        raise ContractError(f"{path}: links.prs and links.related_ids must be lists")

    try:
        data["_path"] = str(path.relative_to(ROOT))
    except ValueError:
        data["_path"] = str(path)
    return data


def validate_done(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    require_fields(path, data, REQUIRED_DONE_FIELDS)
    if data["schema_version"] != 1:
        raise ContractError(f"{path}: schema_version must be 1")

    date_value = data.get("date")
    if isinstance(date_value, dt.date):
        data["date"] = date_value.isoformat()
    elif isinstance(date_value, str):
        try:
            dt.date.fromisoformat(date_value)
        except ValueError as exc:
            raise ContractError(f"{path}: date must be YYYY-MM-DD") from exc
    else:
        raise ContractError(f"{path}: date must be YYYY-MM-DD")

    for field in ["title", "source", "summary", "validation"]:
        expect_str(path, data, field)
    if not isinstance(data.get("changed"), list) or not isinstance(data.get("followups"), list):
        raise ContractError(f"{path}: changed and followups must be lists")

    try:
        data["_path"] = str(path.relative_to(ROOT))
    except ValueError:
        data["_path"] = str(path)
    return data


def load_backlog() -> list[dict[str, Any]]:
    items = [validate_backlog(path, read_yaml(path)) for path in sorted((DATA / "backlog").glob("*/*.yaml"))]
    ids = [item["id"] for item in items]
    duplicates = sorted({item_id for item_id in ids if ids.count(item_id) > 1})
    if duplicates:
        raise ContractError(f"duplicate backlog ids: {', '.join(duplicates)}")
    priority_order = {"now": 0, "next": 1, "later": 2}
    return sorted(items, key=lambda item: (priority_order[item["priority"]], item["type"], item["id"]))


def load_done() -> list[dict[str, Any]]:
    entries = [validate_done(path, read_yaml(path)) for path in sorted((DATA / "done").glob("*.yaml"))]
    return sorted(entries, key=lambda entry: (entry["date"], entry["title"]), reverse=True)


def git_show_done() -> str:
    if not MIRROR.exists():
        return "Mirror is not initialized yet."
    for ref in ["refs/heads/staging:docs/done.md", "origin/staging:docs/done.md"]:
        result = run(["git", f"--git-dir={MIRROR}", "show", ref], check=False)
        if result.returncode == 0:
            return result.stdout
    return "Could not read docs/done.md from staging."


def load_prs() -> tuple[list[dict[str, Any]], str | None]:
    if shutil.which("gh") is None:
        return [], "gh is not installed"
    result = run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            REPO,
            "--state",
            "open",
            "--json",
            "number,title,headRefName,baseRefName,isDraft,updatedAt,url",
            "--limit",
            "100",
        ],
        check=False,
    )
    if result.returncode != 0:
        return [], result.stderr.strip() or result.stdout.strip() or "gh pr list failed"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as exc:
        return [], f"gh returned invalid JSON: {exc}"


def h(value: Any) -> str:
    return html.escape(str(value), quote=True)


def render_text_block(text: str) -> str:
    paragraphs = [p.strip() for p in str(text).strip().split("\n\n") if p.strip()]
    return "\n".join(f"<p>{h(p)}</p>" for p in paragraphs) or "<p></p>"


def render_markdownish(md: str) -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    in_list = False
    in_code = False
    for line in lines:
        if line.startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                if in_list:
                    out.append("</ul>")
                    in_list = False
                out.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            out.append(h(line))
            continue
        if line.startswith("# "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h1>{h(line[2:].strip())}</h1>")
        elif line.startswith("## "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h2>{h(line[3:].strip())}</h2>")
        elif line.startswith("### "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h3>{h(line[4:].strip())}</h3>")
        elif line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{h(line[2:].strip())}</li>")
        elif line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{h(line.strip())}</p>")
    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def page(title: str, body: str, *, active: str) -> str:
    links = [
        ("home", "index.html", "Dashboard"),
        ("backlog", "backlog.html", "Backlog"),
        ("done", "done.html", "Done"),
        ("prs", "prs.html", "PRs"),
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
<title>{h(title)} · WinPath Hub</title>
<link rel="stylesheet" href="assets/styles.css">
</head>
<body>
<header>
  <div><strong>WinPath LAN Hub</strong><span>read-only · staging baseline · local YAML MVP</span></div>
  <nav>{nav}</nav>
</header>
<main>{body}</main>
</body>
</html>
"""


def card(title: str, content: str, meta: str = "") -> str:
    meta_html = f'<div class="meta">{meta}</div>' if meta else ""
    return f'<article class="card"><h2>{h(title)}</h2>{meta_html}{content}</article>'


def write_css(out: Path) -> None:
    css = """body{margin:0;background:#f7f8f5;color:#20231f;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.45}header{position:sticky;top:0;background:#fffffb;border-bottom:1px solid #d8ddcf;padding:14px 24px;display:flex;justify-content:space-between;gap:16px;align-items:center}header span{display:block;color:#687064;font-size:13px}nav{display:flex;gap:8px;flex-wrap:wrap}nav a{color:#29332b;text-decoration:none;border:1px solid #cbd4c5;padding:6px 10px;border-radius:6px;background:#fff}nav a.active{background:#18392b;color:#fff;border-color:#18392b}main{max-width:1180px;margin:0 auto;padding:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}.card{background:#fff;border:1px solid #dce2d5;border-radius:8px;padding:16px;margin:0 0 14px}.card h2{font-size:18px;margin:0 0 8px}.meta,.muted{color:#667064;font-size:13px}.pill{display:inline-block;border:1px solid #c7d0c0;background:#f3f6ef;border-radius:999px;padding:2px 8px;margin:2px;font-size:12px}.risk-high{border-color:#b04444;background:#fff0f0}.risk-medium{border-color:#b88728;background:#fff8e8}.risk-low{border-color:#4d8565;background:#edf8f1}table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dce2d5}th,td{padding:9px 10px;border-bottom:1px solid #e6eadf;text-align:left;vertical-align:top}th{background:#eef2e8}pre{background:#1f2520;color:#f8fff8;padding:14px;border-radius:8px;overflow:auto}.legacy{background:#fff;border:1px solid #dce2d5;border-radius:8px;padding:18px}a{color:#145c42}.warn{border-left:4px solid #b88728;padding-left:12px;background:#fff8e8}"""
    (out / "assets/styles.css").write_text(css, encoding="utf-8")


def render_site(
    backlog: list[dict[str, Any]],
    done: list[dict[str, Any]],
    prs: list[dict[str, Any]],
    pr_error: str | None,
    legacy_done: str,
    out: Path,
) -> None:
    (out / "assets").mkdir(parents=True, exist_ok=True)
    (out / "data").mkdir(parents=True, exist_ok=True)
    write_css(out)

    generated_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    index_data = {
        "generated_at": generated_at,
        "baseline": "origin/staging",
        "backlog": backlog,
        "done": done,
        "prs": prs,
        "pr_error": pr_error,
    }
    (out / "data/index.json").write_text(json.dumps(index_data, indent=2, sort_keys=True), encoding="utf-8")
    (CACHE / "index.json").write_text(json.dumps(index_data, indent=2, sort_keys=True), encoding="utf-8")

    by_status: dict[str, int] = {}
    for item in backlog:
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1
    home_cards = [
        card("Backlog YAML", f"<p>{len(backlog)} local test records.</p><p>{''.join(f'<span class=pill>{h(k)}: {v}</span>' for k, v in sorted(by_status.items()))}</p>"),
        card("Open PRs", f"<p>{len(prs)} open pull requests from GitHub.</p>" + (f"<p class=warn>{h(pr_error)}</p>" if pr_error else "")),
        card("Done", f"<p>{len(done)} local YAML done entries plus repo <code>docs/done.md</code> from staging.</p>"),
        card("Baseline", "<p><code>origin/staging</code> is canonical. PR overlays are metadata-only in this MVP.</p>"),
    ]
    (out / "index.html").write_text(page("Dashboard", f"<section class=grid>{''.join(home_cards)}</section>", active="home"), encoding="utf-8")

    rows = []
    detail_cards = []
    for item in backlog:
        risk_class = f"risk-{h(item['risk']['level'])}"
        rows.append(
            f"<tr><td><strong>{h(item['id'])}</strong><div class=muted>{h(item['_path'])}</div></td>"
            f"<td>{h(item['title'])}<div class=muted>{h(item['summary'])}</div></td>"
            f"<td>{h(item['type'])}</td><td>{h(item['area'])}</td><td>{h(item['priority'])}</td>"
            f"<td>{h(item['status'])}</td><td><span class=\"pill {risk_class}\">{h(item['risk']['level'])}</span></td></tr>"
        )
        body = (
            render_text_block(item["problem"])
            + "<h3>Value</h3>"
            + render_text_block(item["value"])
            + "<h3>Scope</h3>"
            + render_text_block(item["scope"])
            + "<h3>Validation</h3>"
            + render_text_block(item["validation"])
        )
        meta = " ".join(
            [
                f"<span class=pill>{h(item['type'])}</span>",
                f"<span class=pill>{h(item['area'])}</span>",
                f"<span class=pill>{h(item['priority'])}</span>",
                f"<span class=pill>{h(item['status'])}</span>",
            ]
        )
        detail_cards.append(card(f"{item['id']} · {item['title']}", body, meta))
    backlog_body = (
        "<h1>Backlog</h1><p class=muted>Local test YAML records. These are not repo-canonical yet.</p>"
        "<table><thead><tr><th>ID</th><th>Title</th><th>Type</th><th>Area</th><th>Priority</th><th>Status</th><th>Risk</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        + "".join(detail_cards)
    )
    (out / "backlog.html").write_text(page("Backlog", backlog_body, active="backlog"), encoding="utf-8")

    done_body = "<h1>Done</h1><p class=muted>Local YAML done entries followed by staging docs/done.md.</p>"
    for entry in done:
        content = render_text_block(entry["summary"]) + f"<p><strong>Validation:</strong> {h(entry['validation'])}</p>"
        if entry["followups"]:
            content += "<p><strong>Follow-ups:</strong> " + ", ".join(h(value) for value in entry["followups"]) + "</p>"
        done_body += card(f"{entry['date']} · {entry['title']}", content, f"<span class=pill>{h(entry['source'])}</span>")
    done_body += "<h1>Legacy Done Log from staging</h1><section class=legacy>" + render_markdownish(legacy_done) + "</section>"
    (out / "done.html").write_text(page("Done", done_body, active="done"), encoding="utf-8")

    pr_body = "<h1>Open PRs</h1><p class=muted>Metadata only in MVP. YAML overlays will use these branches later.</p>"
    if pr_error:
        pr_body += f"<p class=warn>{h(pr_error)}</p>"
    pr_rows = []
    for pr in prs:
        state = "draft" if pr.get("isDraft") else "ready"
        pr_rows.append(
            f"<tr><td><a href=\"{h(pr['url'])}\">#{h(pr['number'])}</a></td><td>{h(pr['title'])}</td>"
            f"<td><code>{h(pr['headRefName'])}</code> → <code>{h(pr['baseRefName'])}</code></td>"
            f"<td>{h(state)}</td><td>{h(pr.get('updatedAt', ''))}</td></tr>"
        )
    pr_body += "<table><thead><tr><th>PR</th><th>Title</th><th>Branch</th><th>State</th><th>Updated</th></tr></thead><tbody>" + "".join(pr_rows) + "</tbody></table>"
    (out / "prs.html").write_text(page("PRs", pr_body, active="prs"), encoding="utf-8")


def build() -> None:
    backlog = load_backlog()
    done = load_done()
    legacy_done = git_show_done()
    prs, pr_error = load_prs()

    CACHE.mkdir(parents=True, exist_ok=True)
    RELEASES.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    release = RELEASES / stamp
    if release.exists():
        release = RELEASES / f"{stamp}-{os.getpid()}"
    render_site(backlog, done, prs, pr_error, legacy_done, release)

    tmp_link = ROOT / "public.next"
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(release, target_is_directory=True)
    os.replace(tmp_link, PUBLIC)
    print(f"Built {PUBLIC} -> {release}")


def self_test() -> None:
    sample = DATA / "backlog/feature/FEAT-001-salesforce-read-only-sync.yaml"
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        valid = base / "feature/FEAT-999-valid.yaml"
        valid.parent.mkdir(parents=True)
        valid.write_text(sample.read_text(encoding="utf-8"), encoding="utf-8")
        assert validate_backlog(valid, read_yaml(valid))["id"] == "FEAT-001"

        missing = base / "feature/FEAT-998-missing.yaml"
        missing.write_text("schema_version: 1\nid: FEAT-998\n", encoding="utf-8")
        try:
            validate_backlog(missing, read_yaml(missing))
        except ContractError:
            pass
        else:
            raise AssertionError("missing required field did not fail")

        invalid = base / "feature/FEAT-997-invalid.yaml"
        invalid.write_text(valid.read_text(encoding="utf-8").replace("priority: next", "priority: someday"), encoding="utf-8")
        try:
            validate_backlog(invalid, read_yaml(invalid))
        except ContractError:
            pass
        else:
            raise AssertionError("invalid enum did not fail")
    print("self-test passed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    try:
        if args.self_test:
            self_test()
        else:
            build()
    except ContractError as exc:
        print(f"Contract error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
