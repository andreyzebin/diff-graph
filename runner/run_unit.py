"""Unit-tier scenario runner — see TODO §5e.14.

Stage 1: minimum viable plumbing.
- Loads a unit-fixture yaml that references branches in a LOCAL repo
  (e.g. /home/andrey/repos/code-review-examples/orderflow).
- `git clone --local --no-hardlinks` the source repo into a tempdir.
  --no-hardlinks isolates writes; --local makes it instant (loose
  object copy, no pack negotiation). No GitHub / Bitbucket calls,
  so unit runs don't burn API quota or risk account bans.
- Checks out the source branch, resolves both base and source SHAs,
  then invokes diff-graph cli.py with --repo / --base / --source.

No LLM judge yet (Stage 4). No spawn-policy enforcement yet (Stage 2).
No FakePR layer yet (Stage 3 — needed for reviewer/dispatcher
fixtures that depend on PR threads / comments).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class UnitFixture:
    fixture_path: Path
    fixture_id: str
    agent: str
    repo_source: Path
    base_branch: str
    source_branch: str
    agent_data: dict[str, str] = field(default_factory=dict)
    pr_state: dict[str, Any] = field(default_factory=dict)
    trigger: dict[str, Any] = field(default_factory=dict)
    user_message_from: Optional[str] = None
    raw: dict = field(default_factory=dict)


@dataclass
class UnitRunResult:
    fixture_id: str
    agent: str
    exit_code: int
    cli_output: Any            # parsed JSON from --output, or None
    stdout_tail: str           # last ~2KB of stdout for debugging
    stderr_tail: str
    base_sha: str
    source_sha: str
    tmp_repo: Path             # left on disk for inspection on failure
    cleaned_up: bool = False
    posted: list[dict] = field(default_factory=list)  # parsed sink JSONL


def load_fixture(fixture_path: str | Path) -> UnitFixture:
    """Parse a fixture yaml. The yaml may live anywhere — referenced
    repo path is resolved as-is (absolute) from the repo.source field."""
    p = Path(fixture_path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"fixture yaml not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    repo_cfg = raw.get("repo") or {}
    repo_source = repo_cfg.get("source")
    if not repo_source:
        raise ValueError(f"{p}: missing repo.source")
    repo_source_path = Path(str(repo_source)).expanduser().resolve()
    if not (repo_source_path / ".git").exists():
        raise FileNotFoundError(
            f"{p}: repo.source {repo_source_path} is not a git checkout"
        )
    # user_message_from is resolved relative to the fixture yaml.
    umf_raw = raw.get("user_message_from")
    umf: Optional[str] = None
    if umf_raw:
        umf_path = (p.parent / str(umf_raw)).resolve()
        if not umf_path.exists():
            raise FileNotFoundError(
                f"{p}: user_message_from -> {umf_path} does not exist"
            )
        umf = str(umf_path)
    return UnitFixture(
        fixture_path=p,
        fixture_id=str(raw.get("id") or p.stem),
        agent=str(raw.get("agent") or "investigator"),
        repo_source=repo_source_path,
        base_branch=str(repo_cfg.get("base_branch") or "master"),
        source_branch=str(repo_cfg.get("source_branch") or ""),
        agent_data=dict(raw.get("agent_data") or {}),
        pr_state=dict(raw.get("pr_state") or {}),
        trigger=dict(raw.get("trigger") or {}),
        user_message_from=umf,
        raw=raw,
    )


def _git(cwd: Path | str, *args: str, check: bool = True) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=str(cwd), capture_output=True, text=True, check=check,
    )
    return r.stdout.strip()


def _clone_local(fixture: UnitFixture) -> Path:
    """Clone fixture.repo_source into a tempdir using --local --no-hardlinks.

    --local: bypass git's normal transport, just copy loose objects.
    --no-hardlinks: don't share inodes — the temp clone is fully
    independent, can be deleted without affecting the source.
    """
    tmp = Path(tempfile.mkdtemp(prefix=f"unit-{fixture.fixture_id}-"))
    # `git clone` requires the dest dir not to exist; mkdtemp already
    # created it, so clone into a subdir and then flatten by moving up.
    target = tmp / "repo"
    _git(".", "clone", "--local", "--no-hardlinks", "-q",
         str(fixture.repo_source), str(target))
    return target


def _checkout_refs(repo: Path, fixture: UnitFixture) -> tuple[str, str]:
    """Resolve base + source SHAs in the cloned repo. Source is checked
    out so that materialize_vfs sees it as HEAD (matches how a real PR
    would look)."""
    if not fixture.source_branch:
        raise ValueError(f"{fixture.fixture_path}: repo.source_branch is required")
    # Make source branch local if it only exists as origin/<name>.
    # Check for both `<name>` and `origin/<name>`.
    has_local = _git(repo, "rev-parse", "--verify", "--quiet",
                     fixture.source_branch, check=False)
    if not has_local:
        _git(repo, "fetch", "-q", "origin",
             f"{fixture.source_branch}:{fixture.source_branch}",
             check=False)
    _git(repo, "checkout", "-q", fixture.source_branch)
    source_sha = _git(repo, "rev-parse", "HEAD")
    # Resolve base ref: prefer local branch, fall back to origin/<name>.
    base_sha = _git(repo, "rev-parse", "--verify", "--quiet",
                    fixture.base_branch, check=False)
    if not base_sha:
        base_sha = _git(repo, "rev-parse", "--verify",
                        f"origin/{fixture.base_branch}")
    return base_sha, source_sha


def _build_fake_pr_payload(
    fixture: UnitFixture, tmp_repo: Path, base_sha: str, source_sha: str
) -> dict:
    """Combine yaml pr_state with runtime data into the self-contained
    payload that diff-graph's bitbucket_fake reads."""
    pr = fixture.pr_state or {}
    metadata = dict(pr.get("metadata") or {})
    # Default PR URL — synthetic fake:// so diff-graph's parse_pr_url
    # still produces a (server, project, repo, pr_id) tuple.
    pr_url = (metadata.get("pr_url") or pr.get("pr_url") or
              f"fake://orderflow/UNIT/repos/{fixture.fixture_id}/pull-requests/1")
    return {
        "pr_url": pr_url,
        "repo_path": str(tmp_repo),
        "base_sha": base_sha,
        "source_sha": source_sha,
        "metadata": metadata,
        "comments": list(pr.get("comments") or []),
        "self_user": metadata.get("bot_user") or pr.get("self_user") or "diffgraph-bot",
    }


def run_unit_fixture(
    fixture_path: str | Path,
    *,
    diffgraph_repo: str | Path = "/home/andrey/repos/diff-graph",
    provider: Optional[str] = None,
    timeout: int = 300,
    keep_tmp_on_success: bool = False,
) -> UnitRunResult:
    """End-to-end: load fixture → clone → checkout → invoke cli.py.

    When the fixture has a `pr_state` block, build a fake-PR payload
    + sink and point cli.py at them via env (bitbucket_fake reads the
    payload, records write-side actions to the sink)."""
    fixture = load_fixture(fixture_path)
    tmp_repo = _clone_local(fixture)
    base_sha, source_sha = _checkout_refs(tmp_repo, fixture)

    # Temp files: --output + fake-PR payload + sink (latter two only
    # used when fixture has pr_state).
    out_fd, out_path = tempfile.mkstemp(suffix=".json", prefix="unit-")
    os.close(out_fd)
    fake_pr_path: Optional[str] = None
    sink_path: Optional[str] = None

    try:
        diff_repo = Path(diffgraph_repo).expanduser().resolve()
        cmd = [
            str(diff_repo / ".venv" / "bin" / "python"),
            str(diff_repo / "cli.py"), "run",
            f"--agent={fixture.agent}",
            "--repo", str(tmp_repo),
            "--base", base_sha,
            "--source", source_sha,
            "--output", out_path,
        ]
        if provider:
            cmd.extend(["--provider", provider])
        if fixture.user_message_from:
            cmd.extend(["--user-message-from", fixture.user_message_from])
        for k, v in fixture.agent_data.items():
            cmd.extend(["-d", f"{k}={v}"])

        env = {**os.environ,
               "DIFFGRAPH_SCENARIO_ID": fixture.fixture_id,
               "DIFFGRAPH_SCENARIO_TAGS": "tier:unit",
               # Hard backstop — even if the user-message instructs
               # the agent not to spawn / post / set_status, the LLM
               # may not comply. Force these to error at dispatch
               # time so the agent's response history reflects
               # "tool unavailable" and it has to adjust.
               "DIFFGRAPH_FORBIDDEN_TOOLS":
                   "spawn_agent,post_comment,react_to_comment,set_review_status"}

        # PR-state plumbing (reviewer / dispatcher fixtures).
        if fixture.pr_state:
            payload = _build_fake_pr_payload(fixture, tmp_repo, base_sha, source_sha)
            fpfd, fake_pr_path = tempfile.mkstemp(suffix=".json", prefix="unit-fake-pr-")
            os.close(fpfd)
            Path(fake_pr_path).write_text(json.dumps(payload), encoding="utf-8")
            snk_fd, sink_path = tempfile.mkstemp(suffix=".jsonl", prefix="unit-sink-")
            os.close(snk_fd)
            env["DIFFGRAPH_FAKE_PR_FILE"] = fake_pr_path
            env["DIFFGRAPH_FAKE_PR_SINK"] = sink_path
            # Pass --pr-url so cli.py routes via _run_with_dispatcher
            # (the path that calls get_pr_info / get_pr_comments /
            # get_comment_thread). bitbucket_fake intercepts them.
            cmd.extend(["--pr-url", payload["pr_url"]])
            # Trigger plumbs into --message / --comment-id.
            trig = fixture.trigger or {}
            text = trig.get("text")
            if text:
                cmd.extend(["--message", str(text)])
            cid = trig.get("comment_id")
            if cid is not None:
                cmd.extend(["--comment-id", str(cid)])

        proc = subprocess.run(
            cmd, capture_output=True, text=True, env=env,
            timeout=timeout, cwd=str(diff_repo),
        )
        cli_output: Any = None
        try:
            cli_output = json.loads(Path(out_path).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            cli_output = None

        # Read sink — what the agent posted/reacted/set_status'd.
        posted: list[dict] = []
        if sink_path and Path(sink_path).exists():
            for line in Path(sink_path).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    posted.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        result = UnitRunResult(
            fixture_id=fixture.fixture_id,
            agent=fixture.agent,
            exit_code=proc.returncode,
            cli_output=cli_output,
            stdout_tail=proc.stdout[-2000:],
            stderr_tail=proc.stderr[-1000:],
            base_sha=base_sha,
            source_sha=source_sha,
            tmp_repo=tmp_repo,
            posted=posted,
        )
        if proc.returncode == 0 and not keep_tmp_on_success:
            shutil.rmtree(tmp_repo.parent, ignore_errors=True)
            result.cleaned_up = True
        return result
    finally:
        for p in (out_path, fake_pr_path, sink_path):
            if p:
                try: Path(p).unlink()
                except OSError: pass
