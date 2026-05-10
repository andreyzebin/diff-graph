"""Fake Bitbucket module — yaml-fixture driven.

Activated by setting `DIFFGRAPH_FAKE_PR_FILE` to a path containing
a self-contained JSON payload prepared by bench's unit-tier runner
(see `code-review-benchmarks/benchmark/runner/run_unit.py`). Shape:

    {
      "pr_url":      "fake://orderflow/PROJECT/repos/foo/pull-requests/1",
      "repo_path":   "/tmp/unit-…/repo",        # local clone of fixture repo
      "base_sha":    "<40-hex>",
      "source_sha":  "<40-hex>",
      "metadata":    {title, description, …}    # PRMeta shape
      "comments":    [{id, parent_id, author, text, anchor}, …]
      "self_user":   "diffgraph-bot"            # whoever the agent posts as
    }

Write-side calls (post_pr_comment, react_to_pr_comment, …) append
JSONL records to `DIFFGRAPH_FAKE_PR_SINK` so the runner can read
back what the agent did. Read-side calls reply from the in-memory
payload — `get_comment_thread` walks `parent_id` chains to render
the thread tree on demand.

This module matches the interface in `diffgraph.bitbucket_api`.
`diffgraph.bitbucket`'s tail rebinds its exports here when the
env switch is on.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Optional


# ── Lazy load + cache the fixture payload ─────────────────────────────────

_LOCK = threading.Lock()
_PAYLOAD: dict[str, Any] | None = None
_AUTO_ID = [10_000]   # ids assigned to comments written during the run


def _load() -> dict[str, Any]:
    global _PAYLOAD
    if _PAYLOAD is not None:
        return _PAYLOAD
    with _LOCK:
        if _PAYLOAD is not None:
            return _PAYLOAD
        path = os.environ.get("DIFFGRAPH_FAKE_PR_FILE", "").strip()
        if not path:
            # Defensive: the module shouldn't even be loaded without
            # the env, but if it is, return an empty shell.
            _PAYLOAD = {"comments": [], "metadata": {}, "pr_url": "",
                        "repo_path": "", "base_sha": "", "source_sha": "",
                        "self_user": ""}
            return _PAYLOAD
        p = Path(path).expanduser()
        _PAYLOAD = json.loads(p.read_text(encoding="utf-8"))
        # Seed _AUTO_ID above the highest existing id so writes don't collide.
        max_id = max((int(c.get("id", 0)) for c in _PAYLOAD.get("comments", [])),
                     default=0)
        _AUTO_ID[0] = max(_AUTO_ID[0], max_id + 1)
        return _PAYLOAD


def _sink_append(rec: dict) -> None:
    """Append one JSON line to the sink (write-side actions go here)."""
    path = os.environ.get("DIFFGRAPH_FAKE_PR_SINK", "").strip()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _next_id() -> int:
    with _LOCK:
        i = _AUTO_ID[0]
        _AUTO_ID[0] += 1
        return i


# ── parse_pr_url stays real-shape (pure parser) ───────────────────────────

def parse_pr_url(pr_url: str) -> tuple[str, str, str, int]:
    """Accept either real-shape (https://…/projects/X/repos/Y/pull-requests/N)
    or fake-shape (fake://orderflow/X/repos/Y/pull-requests/N). Returns the
    same tuple shape so callers don't care."""
    from urllib.parse import urlparse
    p = urlparse(pr_url)
    parts = p.path.strip("/").split("/")
    try:
        i = parts.index("projects")
        project = parts[i + 1]
        repo = parts[i + 3]
        pr_id = int(parts[i + 5])
        server = f"{p.scheme}://{p.netloc}"
        return server, project, repo, pr_id
    except (ValueError, IndexError):
        return ("fake://orderflow", "FAKE", "fake-repo", 0)


# ── Read side ────────────────────────────────────────────────────────────

def fetch_pr(
    pr_url: str,
    token: Optional[str] = None,
    ca_bundle: Optional[str] = None,
    client_cert: Optional[str] = None,
    on_status: Optional[Callable[[str], None]] = None,
) -> tuple[str, str, Callable[[], None], dict]:
    """Use the pre-cloned repo at `repo_path` and compute the diff
    locally via `git diff base..source`. No network calls. Cleanup
    is a no-op — the runner owns the temp clone."""
    pl = _load()
    repo_path = pl.get("repo_path", "")
    base_sha = pl.get("base_sha", "")
    source_sha = pl.get("source_sha", "")
    if not (repo_path and base_sha and source_sha):
        raise RuntimeError(
            "bitbucket_fake.fetch_pr: payload missing repo_path/base_sha/source_sha"
        )
    if on_status:
        try: on_status(f"fake fetch_pr: {base_sha[:8]}..{source_sha[:8]} @ {repo_path}")
        except Exception: pass
    diff_text = subprocess.run(
        ["git", "diff", f"{base_sha}..{source_sha}"],
        cwd=repo_path, capture_output=True, text=True, check=True,
    ).stdout
    pr_meta = dict(pl.get("metadata") or {})
    pr_meta.setdefault("base_ref", base_sha)
    pr_meta.setdefault("source_ref", source_sha)
    return diff_text, repo_path, (lambda: None), pr_meta


def get_pr_info(
    pr_url: str,
    token: Optional[str] = None,
    ca_bundle: Optional[str] = None,
    client_cert: Optional[str] = None,
) -> dict:
    pl = _load()
    md = dict(pl.get("metadata") or {})
    md.setdefault("base_ref", pl.get("base_sha", ""))
    md.setdefault("source_ref", pl.get("source_sha", ""))
    return md


def get_pr_comments(
    pr_url: str,
    token: Optional[str] = None,
    ca_bundle: Optional[str] = None,
    client_cert: Optional[str] = None,
) -> list[dict]:
    return list(_load().get("comments") or [])


def _comment_index(comments: list[dict]) -> dict[int, dict]:
    return {int(c.get("id", 0)): c for c in comments if c.get("id") is not None}


def _walk_chain_up(idx: dict[int, dict], cid: int) -> int:
    """Walk parent_id pointers up to the root of the chain
    containing `cid`. Returns the root id (0 if not found)."""
    seen: set[int] = set()
    cur = cid
    while cur and cur not in seen:
        seen.add(cur)
        c = idx.get(cur)
        if not c:
            return 0
        parent = int(c.get("parent_id", 0) or 0)
        if not parent:
            return cur
        cur = parent
    return cur


def _render_thread(comments: list[dict], root_id: int, *,
                   bot_user: str = "", subject_pattern: str = "") -> str:
    """Render the subtree under `root_id` as plain text — same
    shape the real `get_comment_thread` returns. Children are
    indented by depth; self-comments tagged [SELF]."""
    idx = _comment_index(comments)
    children_of: dict[int, list[int]] = {}
    for c in comments:
        children_of.setdefault(int(c.get("parent_id", 0) or 0), []).append(
            int(c["id"]))

    lines: list[str] = []

    def _author(c: dict) -> str:
        a = c.get("author") or {}
        name = a.get("name") or a.get("slug") or "unknown"
        return name

    def _render(node_id: int, depth: int) -> None:
        c = idx.get(node_id)
        if not c:
            return
        prefix = "  " * depth
        author = _author(c)
        tag = "[SELF]" if bot_user and (
            (c.get("author") or {}).get("slug") == bot_user
        ) else ""
        anchor = c.get("anchor") or {}
        anchor_s = (f" ({anchor.get('path','?')}:{anchor.get('line','?')})"
                    if anchor.get("path") else "")
        lines.append(
            f"{prefix}#{node_id} {author}{tag}{anchor_s}: {c.get('text','')}"
        )
        for child_id in children_of.get(node_id, []):
            _render(child_id, depth + 1)

    _render(root_id, 0)
    return "\n".join(lines)


def get_comment_thread(
    pr_url: str,
    comment_id: int,
    token: Optional[str] = None,
    ca_bundle: Optional[str] = None,
    client_cert: Optional[str] = None,
    bot_user: str = "",
    subject_pattern: str = "",
) -> str:
    pl = _load()
    comments = list(pl.get("comments") or [])
    if not comments:
        return ""
    idx = _comment_index(comments)
    root_id = _walk_chain_up(idx, int(comment_id))
    if not root_id:
        return ""
    return _render_thread(comments, root_id,
                          bot_user=bot_user or pl.get("self_user", ""),
                          subject_pattern=subject_pattern)


# ── Write side — record to sink, return plausible ids ────────────────────


def reply_to_pr_comment(
    pr_url: str,
    comment_id: int,
    text: str,
    token: Optional[str] = None,
    ca_bundle: Optional[str] = None,
    client_cert: Optional[str] = None,
) -> None:
    new_id = _next_id()
    _sink_append({"kind": "reply", "parent_id": int(comment_id),
                  "new_id": new_id, "text": text})


def resolve_pr_comment(
    pr_url: str,
    comment_id: int,
    token: Optional[str] = None,
    ca_bundle: Optional[str] = None,
    client_cert: Optional[str] = None,
) -> None:
    _sink_append({"kind": "resolve", "comment_id": int(comment_id)})


def post_pr_comment(
    pr_url: str,
    *,
    text: str,
    file: str = "",
    line: int = 0,
    severity: str = "",
    parent_id: int = 0,
    line_type: str = "ADDED",
    token: Optional[str] = None,
    ca_bundle: Optional[str] = None,
    client_cert: Optional[str] = None,
) -> int:
    new_id = _next_id()
    _sink_append({"kind": "post_comment", "new_id": new_id,
                  "text": text, "file": file, "line": line,
                  "severity": severity, "parent_id": int(parent_id),
                  "line_type": line_type})
    return new_id


def post_general_pr_comment(
    pr_url: str,
    text: str,
    token: Optional[str] = None,
    ca_bundle: Optional[str] = None,
    client_cert: Optional[str] = None,
) -> int:
    new_id = _next_id()
    _sink_append({"kind": "post_general", "new_id": new_id, "text": text})
    return new_id


def post_review_comments(
    pr_url: str,
    comments: list,
    token: Optional[str] = None,
    ca_bundle: Optional[str] = None,
    client_cert: Optional[str] = None,
    on_status: Optional[Callable[[str], None]] = None,
    changed_lines: Optional[dict] = None,
    decorate: Optional[Callable[[str], str]] = None,
) -> int:
    posted = 0
    for c in comments or []:
        try:
            text = getattr(c, "body", None) or getattr(c, "text", None) or ""
            if decorate:
                try: text = decorate(text)
                except Exception: pass
            new_id = _next_id()
            _sink_append({"kind": "review_comment", "new_id": new_id,
                          "text": text,
                          "file": getattr(c, "file", "") or "",
                          "line": getattr(c, "line", 0) or 0,
                          "severity": getattr(c, "severity", "") or ""})
            posted += 1
        except Exception:
            pass
    return posted


def react_to_pr_comment(
    pr_url: str,
    comment_id: int,
    emoticon: str,
    token: Optional[str] = None,
    ca_bundle: Optional[str] = None,
    client_cert: Optional[str] = None,
) -> None:
    _sink_append({"kind": "react", "comment_id": int(comment_id),
                  "emoticon": emoticon})


def set_review_status(
    pr_url: str,
    user_slug: str,
    status: str,
    token: Optional[str] = None,
    ca_bundle: Optional[str] = None,
    client_cert: Optional[str] = None,
) -> None:
    _sink_append({"kind": "set_status", "user_slug": user_slug,
                  "status": status})
