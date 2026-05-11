"""Fake Bitbucket — yaml-fixture driven, two-mode (env + in-process).

Two ways to drive this module:

1. **Subprocess mode (legacy).** Set `DIFFGRAPH_FAKE_PR_FILE` to a JSON
   payload path; diff-graph's `bitbucket.py` tail-rebinds its exports
   here at import time. Used by bench's unit-tier runner
   (`code-review-benchmarks/benchmark/runner/run_unit.py`) which
   shells out to `cli.py`.

2. **In-process mode (new — for §5e.14 isolated agent unit tests).**
   Build a `FakeBitbucket` instance directly and call `install()`. Each
   test gets its own state — no shared module-level cache, safe under
   pytest-xdist, no env-var leakage. Use `reset()` in a fixture teardown.

The payload shape is identical in both modes::

    {
      "pr_url":      "fake://orderflow/PROJECT/repos/foo/pull-requests/1",
      "repo_path":   "/tmp/unit-…/repo",        # local clone of fixture repo
      "base_sha":    "<40-hex>",
      "source_sha":  "<40-hex>",
      "metadata":    {title, description, …}    # PRMeta shape
      "comments":    [{id, parent_id, author, text, anchor}, …]
      "self_user":   "diffgraph-bot"            # whoever the agent posts as
    }

Write-side calls (`post_pr_comment`, `react_to_pr_comment`, …) append
records to the configured sink — either a JSONL file path (subprocess
mode, the runner reads it back) or a Python list (in-process mode, the
test reads `instance.sink_records`).

This module matches the interface in `diffgraph.bitbucket_api`.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Optional


# ── parse_pr_url (pure parser; identical in real and fake) ────────────────

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


# ── FakeBitbucket — per-instance state, no module globals ────────────────


_EMPTY_PAYLOAD: dict[str, Any] = {
    "pr_url": "", "repo_path": "", "base_sha": "", "source_sha": "",
    "metadata": {}, "comments": [], "self_user": "",
}


class FakeBitbucket:
    """One PR's worth of fake-Bitbucket state.

    Each instance is independent — two tests can have two fakes side-
    by-side without races. State that USED to be module-level
    (`_PAYLOAD`, `_AUTO_ID`) is now per-instance. Locks are kept
    because a single instance is still allowed to serve multiple
    threads (e.g. a parallel-agent scenario)."""

    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        sink_path: str | None = None,
        sink_records: list[dict] | None = None,
    ) -> None:
        # Either sink_path (JSONL file) or sink_records (in-memory list)
        # or neither (writes are dropped). Setting both is legal — the
        # write goes to both.
        self.payload: dict[str, Any] = dict(payload or _EMPTY_PAYLOAD)
        self.sink_path = sink_path
        self.sink_records: list[dict] = sink_records if sink_records is not None else []
        self._lock = threading.Lock()
        # Seed auto-id above the highest existing comment id so writes
        # don't collide with fixture-provided ids.
        max_id = max(
            (int(c.get("id", 0) or 0) for c in self.payload.get("comments", [])),
            default=0,
        )
        self._auto_id = max(10_000, max_id + 1)

    # ── read-side ────────────────────────────────────────────────────────

    def fetch_pr(
        self,
        pr_url: str,
        token: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> tuple[str, str, Callable[[], None], dict]:
        """Compute the diff locally via `git diff base..source` against
        the pre-cloned repo. No network, no cleanup (runner owns the
        temp clone)."""
        repo_path = self.payload.get("repo_path", "")
        base_sha = self.payload.get("base_sha", "")
        source_sha = self.payload.get("source_sha", "")
        if not (repo_path and base_sha and source_sha):
            raise RuntimeError(
                "FakeBitbucket.fetch_pr: payload missing repo_path/base_sha/source_sha"
            )
        if on_status:
            try: on_status(f"fake fetch_pr: {base_sha[:8]}..{source_sha[:8]} @ {repo_path}")
            except Exception: pass
        diff_text = subprocess.run(
            ["git", "diff", f"{base_sha}..{source_sha}"],
            cwd=repo_path, capture_output=True, text=True, check=True,
        ).stdout
        pr_meta = dict(self.payload.get("metadata") or {})
        pr_meta.setdefault("base_ref", base_sha)
        pr_meta.setdefault("source_ref", source_sha)
        return diff_text, repo_path, (lambda: None), pr_meta

    def get_pr_info(
        self,
        pr_url: str,
        token: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
    ) -> dict:
        md = dict(self.payload.get("metadata") or {})
        md.setdefault("base_ref", self.payload.get("base_sha", ""))
        md.setdefault("source_ref", self.payload.get("source_sha", ""))
        return md

    def get_pr_comments(
        self,
        pr_url: str,
        token: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
    ) -> list[dict]:
        return [_normalise_comment(c) for c in (self.payload.get("comments") or [])]

    def get_comment_thread(
        self,
        pr_url: str,
        comment_id: int,
        token: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
        bot_user: str = "",
        subject_pattern: str = "",
    ) -> str:
        comments = self._normalised_comments()
        if not comments:
            return ""
        idx = _comment_index(comments)
        root_id = _walk_chain_up(idx, int(comment_id))
        if not root_id:
            return ""
        return _render_thread(comments, root_id,
                              bot_user=bot_user or self.payload.get("self_user", ""),
                              subject_pattern=subject_pattern)

    def _normalised_comments(self) -> list[dict]:
        return [_normalise_comment(c) for c in (self.payload.get("comments") or [])]

    # ── write-side ───────────────────────────────────────────────────────

    def _record(self, rec: dict) -> None:
        """Append one event to the configured sink(s). Errors swallowed —
        a flaky sink shouldn't crash the agent under test."""
        self.sink_records.append(rec)
        if self.sink_path:
            try:
                with open(self.sink_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            except Exception:
                pass

    def _next_id(self) -> int:
        with self._lock:
            i = self._auto_id
            self._auto_id += 1
            return i

    def reply_to_pr_comment(
        self, pr_url: str, comment_id: int, text: str,
        token: Optional[str] = None, ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
    ) -> None:
        new_id = self._next_id()
        self._record({"kind": "reply", "parent_id": int(comment_id),
                      "new_id": new_id, "text": text})

    def resolve_pr_comment(
        self, pr_url: str, comment_id: int,
        token: Optional[str] = None, ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
    ) -> None:
        self._record({"kind": "resolve", "comment_id": int(comment_id)})

    def post_pr_comment(
        self, pr_url: str, *,
        text: str, file: str = "", line: int = 0, severity: str = "",
        parent_id: int = 0, line_type: str = "ADDED",
        token: Optional[str] = None, ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
    ) -> int:
        new_id = self._next_id()
        self._record({"kind": "post_comment", "new_id": new_id,
                      "text": text, "file": file, "line": line,
                      "severity": severity, "parent_id": int(parent_id),
                      "line_type": line_type})
        return new_id

    def post_general_pr_comment(
        self, pr_url: str, text: str,
        token: Optional[str] = None, ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
    ) -> int:
        new_id = self._next_id()
        self._record({"kind": "post_general", "new_id": new_id, "text": text})
        return new_id

    def post_review_comments(
        self, pr_url: str, comments: list,
        token: Optional[str] = None, ca_bundle: Optional[str] = None,
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
                new_id = self._next_id()
                self._record({"kind": "review_comment", "new_id": new_id,
                              "text": text,
                              "file": getattr(c, "file", "") or "",
                              "line": getattr(c, "line", 0) or 0,
                              "severity": getattr(c, "severity", "") or ""})
                posted += 1
            except Exception:
                pass
        return posted

    def react_to_pr_comment(
        self, pr_url: str, comment_id: int, emoticon: str,
        token: Optional[str] = None, ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
    ) -> None:
        self._record({"kind": "react", "comment_id": int(comment_id),
                      "emoticon": emoticon})

    def set_review_status(
        self, pr_url: str, user_slug: str, status: str,
        token: Optional[str] = None, ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
    ) -> None:
        self._record({"kind": "set_status", "user_slug": user_slug,
                      "status": status})


# ── comment-thread helpers (pure functions; shared with both modes) ──────


def _normalise_comment(c: dict) -> dict:
    """Convert yaml-friendly shape into the legacy flat shape that
    diff-graph's call sites expect:
      {id, parent_id, depth, file, line, text, author, author_slug,
       resolved, anchored}.
    Accepts either flat (author/author_slug strings) or nested
    (author={name, slug}, anchor={path, line}) inputs."""
    a = c.get("author")
    if isinstance(a, dict):
        author = a.get("name") or a.get("displayName") or a.get("slug") or ""
        author_slug = a.get("slug") or a.get("name") or ""
    else:
        author = str(a or "")
        author_slug = str(c.get("author_slug") or author or "")
    anc = c.get("anchor") or {}
    file_path = anc.get("path") or c.get("file") or ""
    line = anc.get("line") or c.get("line") or 0
    return {
        "id":          int(c.get("id", 0) or 0),
        "parent_id":   int(c.get("parent_id", 0) or 0),
        "depth":       int(c.get("depth", 0) or 0),
        "file":        str(file_path),
        "line":        int(line or 0),
        "text":        str(c.get("text") or ""),
        "author":      str(author),
        "author_slug": str(author_slug),
        "resolved":    bool(c.get("resolved") or False),
        "anchored":    bool(file_path),
    }


def _comment_index(comments: list[dict]) -> dict[int, dict]:
    return {int(c.get("id", 0)): c for c in comments if c.get("id") is not None}


def _walk_chain_up(idx: dict[int, dict], cid: int) -> int:
    """Walk parent_id pointers up to the root of the chain containing
    `cid`. Returns the root id (0 if not found)."""
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
    """Render the subtree under `root_id` as plain text. Self-comments
    are tagged [SELF]; children are indented."""
    idx = _comment_index(comments)
    children_of: dict[int, list[int]] = {}
    for c in comments:
        children_of.setdefault(int(c.get("parent_id", 0) or 0), []).append(
            int(c["id"]))

    lines: list[str] = []
    visited: set[int] = set()  # cycle guard — malformed fixtures can loop

    def _render(node_id: int, depth: int) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        c = idx.get(node_id)
        if not c:
            return
        prefix = "  " * depth
        author = c.get("author") or "unknown"
        tag = "[SELF]" if bot_user and c.get("author_slug") == bot_user else ""
        anchor_s = ""
        if c.get("file"):
            anchor_s = f" ({c.get('file')}:{c.get('line') or '?'})"
        lines.append(
            f"{prefix}#{node_id} {author}{tag}{anchor_s}: {c.get('text','')}"
        )
        for child_id in children_of.get(node_id, []):
            _render(child_id, depth + 1)

    _render(root_id, 0)
    return "\n".join(lines)


# ── Singleton routing for module-level functions ─────────────────────────
#
# `diffgraph/bitbucket.py` tail-rebinds these names at import time when
# `DIFFGRAPH_FAKE_PR_FILE` is set. We keep them as thin wrappers around
# a process-wide singleton so that callsite code (and external code
# that did `from diffgraph.bitbucket_fake import post_pr_comment`)
# keeps working without changes.
#
# In-process tests should call `install(instance)` in a fixture to
# replace the singleton, then `reset()` in teardown. Don't rely on
# the env-var initialization in that mode — it reads at first call
# and caches, which would leak fixture state into the next test.

_INSTANCE: FakeBitbucket | None = None
_INIT_LOCK = threading.Lock()


def _instance() -> FakeBitbucket:
    """Return the active FakeBitbucket. Lazily initialised from
    `DIFFGRAPH_FAKE_PR_FILE` on first call if no instance has been
    installed yet."""
    global _INSTANCE
    if _INSTANCE is not None:
        return _INSTANCE
    with _INIT_LOCK:
        if _INSTANCE is not None:
            return _INSTANCE
        path = os.environ.get("DIFFGRAPH_FAKE_PR_FILE", "").strip()
        if path:
            payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        else:
            payload = None  # empty shell
        sink = os.environ.get("DIFFGRAPH_FAKE_PR_SINK", "").strip() or None
        _INSTANCE = FakeBitbucket(payload, sink_path=sink)
        return _INSTANCE


def install(instance: FakeBitbucket) -> None:
    """Replace the process-wide FakeBitbucket. Intended for in-process
    tests: build a FakeBitbucket, install it, run the agent, inspect
    `instance.sink_records`, then `reset()` in teardown."""
    global _INSTANCE
    with _INIT_LOCK:
        _INSTANCE = instance


def reset() -> None:
    """Forget the current instance. Next module-level call will re-
    initialise from env (or to an empty shell)."""
    global _INSTANCE
    with _INIT_LOCK:
        _INSTANCE = None


# Module-level functions — thin wrappers, identical signatures to the
# real `diffgraph.bitbucket` exports. `diffgraph/bitbucket.py` rebinds
# its own names to these when the env switch is on.


def fetch_pr(pr_url, token=None, ca_bundle=None, client_cert=None, on_status=None):
    return _instance().fetch_pr(pr_url, token=token, ca_bundle=ca_bundle,
                                client_cert=client_cert, on_status=on_status)


def get_pr_info(pr_url, token=None, ca_bundle=None, client_cert=None):
    return _instance().get_pr_info(pr_url, token=token, ca_bundle=ca_bundle,
                                   client_cert=client_cert)


def get_pr_comments(pr_url, token=None, ca_bundle=None, client_cert=None):
    return _instance().get_pr_comments(pr_url, token=token, ca_bundle=ca_bundle,
                                       client_cert=client_cert)


def get_comment_thread(pr_url, comment_id, token=None, ca_bundle=None,
                       client_cert=None, bot_user="", subject_pattern=""):
    return _instance().get_comment_thread(pr_url, comment_id, token=token,
                                          ca_bundle=ca_bundle, client_cert=client_cert,
                                          bot_user=bot_user,
                                          subject_pattern=subject_pattern)


def reply_to_pr_comment(pr_url, comment_id, text,
                        token=None, ca_bundle=None, client_cert=None):
    return _instance().reply_to_pr_comment(pr_url, comment_id, text,
                                           token=token, ca_bundle=ca_bundle,
                                           client_cert=client_cert)


def resolve_pr_comment(pr_url, comment_id,
                       token=None, ca_bundle=None, client_cert=None):
    return _instance().resolve_pr_comment(pr_url, comment_id,
                                          token=token, ca_bundle=ca_bundle,
                                          client_cert=client_cert)


def post_pr_comment(pr_url, *, text, file="", line=0, severity="",
                    parent_id=0, line_type="ADDED",
                    token=None, ca_bundle=None, client_cert=None):
    return _instance().post_pr_comment(pr_url, text=text, file=file, line=line,
                                       severity=severity, parent_id=parent_id,
                                       line_type=line_type, token=token,
                                       ca_bundle=ca_bundle, client_cert=client_cert)


def post_general_pr_comment(pr_url, text,
                            token=None, ca_bundle=None, client_cert=None):
    return _instance().post_general_pr_comment(pr_url, text, token=token,
                                               ca_bundle=ca_bundle,
                                               client_cert=client_cert)


def post_review_comments(pr_url, comments,
                         token=None, ca_bundle=None, client_cert=None,
                         on_status=None, changed_lines=None, decorate=None):
    return _instance().post_review_comments(pr_url, comments, token=token,
                                            ca_bundle=ca_bundle, client_cert=client_cert,
                                            on_status=on_status,
                                            changed_lines=changed_lines,
                                            decorate=decorate)


def react_to_pr_comment(pr_url, comment_id, emoticon,
                        token=None, ca_bundle=None, client_cert=None):
    return _instance().react_to_pr_comment(pr_url, comment_id, emoticon,
                                           token=token, ca_bundle=ca_bundle,
                                           client_cert=client_cert)


def set_review_status(pr_url, user_slug, status,
                      token=None, ca_bundle=None, client_cert=None):
    return _instance().set_review_status(pr_url, user_slug, status,
                                         token=token, ca_bundle=ca_bundle,
                                         client_cert=client_cert)
