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
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

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
    # Optional ToolMocks fixture path — same shape integration tier
    # uses via setup.mocks. Plumbed as --mocks to cli.py so e.g.
    # dispatcher-tests can short-circuit spawn_agent(reviewer) with a
    # canned response instead of running the heavy chain.
    mocks: Optional[str] = None
    # Optional scenario-shape blocks — when present the runner can
    # invoke an LLM judge against the agent's invocations.json after
    # the subprocess finishes (TODO §5d.3 / §5e.14 Stage 4). Stored
    # as raw dicts; we convert to scenario_loader dataclasses at
    # judge-build time so we don't pay the import cost for fixtures
    # that don't opt in.
    expected_output: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
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
    # Populated when a judge ran (fixture had expected_output + caller
    # supplied attempt_dir + judge_cfg). None on the no-judge path.
    judge_score: Optional[float] = None
    judge_verdict: Optional[str] = None
    judge_summary: Optional[str] = None
    judge_run_id: Optional[str] = None
    attempt_dir: Optional[Path] = None


# ── Path resolution: relative-to-yaml or diffgraph:<path> ─────────────────


_DIFFGRAPH_REPO_DEFAULT = "/home/andrey/repos/diff-graph"


def _resolve_prompt_path(spec: str, fixture_dir: Path) -> Path:
    """Resolve a prompt/mocks file path from a fixture yaml field.

    Two URI shapes supported:

    - Plain relative (default): `../../path.md` → resolved against
      the fixture yaml's directory. Stays inside the bench repo.
    - `diffgraph:<path>`: resolved against the diff-graph repo root
      (env `DIFFGRAPH_REPO`, default `/home/andrey/repos/diff-graph`).
      Used when a prompt lives next to production agent prompts in
      diff-graph/diffgraph/test_prompts/ — sharing a single source
      of truth across unit + integration scenarios + production
      avoids drift.
    """
    import os as _os
    if spec.startswith("diffgraph:"):
        repo = _os.environ.get("DIFFGRAPH_REPO", _DIFFGRAPH_REPO_DEFAULT)
        return Path(repo).expanduser() / spec[len("diffgraph:"):]
    return (fixture_dir / spec).resolve()


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
    # user_message_from is resolved against one of two roots:
    #   - relative path (default): relative to the fixture yaml's
    #     directory. Used for paths inside bench (e.g. ../mocks/x.yaml).
    #   - `diffgraph:<path>`: relative to the diff-graph repo root,
    #     resolved via DIFFGRAPH_REPO env (default
    #     /home/andrey/repos/diff-graph). Used for task prompts that
    #     live in diff-graph/diffgraph/test_prompts/<agent>/<file>.md
    #     so they sit next to production prompts instead of being
    #     scattered in bench.
    umf_raw = raw.get("user_message_from")
    umf: Optional[str] = None
    if umf_raw:
        umf_path = _resolve_prompt_path(str(umf_raw), p.parent)
        if not umf_path.exists():
            raise FileNotFoundError(
                f"{p}: user_message_from -> {umf_path} does not exist"
            )
        umf = str(umf_path)
    # mocks path — same resolution rules as user_message_from
    # (relative-to-yaml OR `diffgraph:<path>`).
    mocks_raw = raw.get("mocks")
    mocks_resolved: Optional[str] = None
    if mocks_raw:
        mp = _resolve_prompt_path(str(mocks_raw), p.parent)
        if not mp.exists():
            raise FileNotFoundError(
                f"{p}: mocks -> {mp} does not exist"
            )
        mocks_resolved = str(mp)
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
        mocks=mocks_resolved,
        expected_output=dict(raw.get("expected_output") or {}),
        tags=list(raw.get("tags") or []),
        raw=raw,
    )


def _git(cwd: Path | str, *args: str, check: bool = True) -> str:
    """Run `git ...` with stderr captured. On failure log BOTH the
    command and git's own stderr message to the bench system log,
    then re-raise — Rich's default traceback rendering shows only
    `str(CalledProcessError)` which doesn't include git's reason
    text, leaving operators staring at a bare "exit 128" with no
    hint why. See plan 212 task #3636 for the wild-type case."""
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(cwd), capture_output=True, text=True, check=check,
        )
    except subprocess.CalledProcessError as exc:
        log.error(
            "git failed: cmd=%s cwd=%s rc=%s stderr=%r stdout=%r",
            ["git", *args], str(cwd), exc.returncode,
            (exc.stderr or "")[:2000],
            (exc.stdout or "")[:500],
        )
        raise
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
    attempt_dir: str | Path | None = None,
    judge_cfg: Optional[dict] = None,
) -> UnitRunResult:
    """End-to-end: load fixture → clone → checkout → invoke cli.py.

    When the fixture has a `pr_state` block, build a fake-PR payload
    + sink and point cli.py at them via env (bitbucket_fake reads the
    payload, records write-side actions to the sink).

    Stage 4 (TODO §5d.3) — when `attempt_dir` is given AND the fixture
    has an `expected_output` block AND a `judge_cfg` is supplied, the
    runner ALSO invokes an LLM judge against the agent's
    invocations.json after the subprocess finishes. The judge writes
    its trace under `attempt_dir/runs/judge/` (mirroring integration
    tier layout) and a `runs` row to ~/.diffgraph/traces.db so the
    QA dashboard surfaces unit scenarios alongside integration ones.
    """
    fixture = load_fixture(fixture_path)
    tmp_repo = _clone_local(fixture)
    base_sha, source_sha = _checkout_refs(tmp_repo, fixture)

    # Per-attempt trace layout (TODO §5e.10a). When attempt_dir is set,
    # diff-graph's cli.py writes its OTel/SQLite traces under
    # runs/agent, the judge writes under runs/judge, and both are
    # linked via linked_run_id. Without attempt_dir we fall back to
    # the legacy no-trace path — the bench just runs the agent and
    # prints output (good for ad-hoc local debugging).
    agent_dir: Optional[Path] = None
    judge_dir: Optional[Path] = None
    invocations_path: Optional[Path] = None
    if attempt_dir is not None:
        attempt_dir = Path(attempt_dir).expanduser().resolve()
        attempt_dir.mkdir(parents=True, exist_ok=True)
        agent_dir = attempt_dir / "runs" / "agent"
        judge_dir = attempt_dir / "runs" / "judge"
        agent_dir.mkdir(parents=True, exist_ok=True)
        judge_dir.mkdir(parents=True, exist_ok=True)
        invocations_path = agent_dir / "invocations.json"

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
        if fixture.mocks:
            # cli.py --mocks=<path> ⇒ orchestra.ToolMocks intercepts the
            # named tool calls (e.g. spawn_agent → canned reviewer
            # response). Same as integration tier's setup.mocks plumbing.
            cmd.extend(["--mocks", fixture.mocks])
        for k, v in fixture.agent_data.items():
            cmd.extend(["-d", f"{k}={v}"])
        # Tell the agent to dump every tool invocation to a file so the
        # judge can score reflect/done/spawn args after the run.
        if invocations_path is not None:
            cmd.append(f"--invocations-out={invocations_path}")

        # Tier tags: built-ins + whatever the fixture yaml declares.
        # The DB row uses these for /qa/scoring filters (TODO §5e.11).
        all_tags = ["tier:unit"]
        for t in fixture.tags:
            if t and t not in all_tags:
                all_tags.append(t)
        env = {**os.environ,
               "DIFFGRAPH_SCENARIO_ID": fixture.fixture_id,
               "DIFFGRAPH_SCENARIO_TAGS": ",".join(all_tags)}

        # Fake-PR plumbing — runs for EVERY fixture, including those
        # without a `pr_state` block. Why: under cli.py's
        # `_run_with_dispatcher`, the first domain tool call triggers
        # `_lazy_init` ⇒ `fetch_pr(pr_url)` ⇒ `parse_pr_url(pr_url)`.
        # With an empty pr_url that raises ValueError, the agent
        # runner wraps it as `"error: …"`, and every `diff_*` tool
        # call returns that error. Net effect: investigators on
        # fixtures without pr_state run blind. Always wiring up a
        # fake PR (empty comments + minimal metadata) lets the
        # bitbucket_fake provider answer `get_pr_info` / `fetch_pr`
        # /etc. with the local repo, so tools work the same way they
        # do for reviewer fixtures. `_build_fake_pr_payload` is
        # already defensive about missing pr_state — `pr.get(...) or
        # default` everywhere.
        payload = _build_fake_pr_payload(fixture, tmp_repo, base_sha, source_sha)
        fpfd, fake_pr_path = tempfile.mkstemp(suffix=".json", prefix="unit-fake-pr-")
        os.close(fpfd)
        Path(fake_pr_path).write_text(json.dumps(payload), encoding="utf-8")
        snk_fd, sink_path = tempfile.mkstemp(suffix=".jsonl", prefix="unit-sink-")
        os.close(snk_fd)
        env["DIFFGRAPH_FAKE_PR_FILE"] = fake_pr_path
        env["DIFFGRAPH_FAKE_PR_SINK"] = sink_path
        if agent_dir is not None:
            # cli.py reads DIFFGRAPH_TRACE_PATH and routes both OTel
            # filesystem and SQLite trace inserts under it. We hand
            # cli.py the agent's runs/ dir so the layout mirrors the
            # integration tier's attempt-NN/runs/agent/.
            env["DIFFGRAPH_TRACE_PATH"] = str(agent_dir)
        # Pass --pr-url so cli.py routes via _run_with_dispatcher
        # (the path that calls get_pr_info / get_pr_comments /
        # get_comment_thread). bitbucket_fake intercepts them.
        cmd.extend(["--pr-url", payload["pr_url"]])
        # Trigger plumbs into --message / --comment-id — only relevant
        # when the fixture explicitly carries one (reviewer/dispatcher).
        if fixture.pr_state:
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
        # Bench-side orphan-catch — cli.py's try/finally fires on
        # SIGTERM but NOT on SIGKILL / OOM / segfault. When the child
        # died abruptly, its `runs` row stays at status='running',
        # finished_at=NULL, and the global orphan-sweeper takes up to
        # an hour to notice. We're the closest parent that SAW the
        # subprocess die — close the row here, immediately, so /qa/
        # sessions reflects the failure within seconds. Best-effort:
        # cli.py may have already closed the row on its own normal
        # path, in which case the WHERE clause makes this a no-op.
        if proc.returncode != 0 and agent_dir is not None:
            _close_agent_run_if_orphaned(agent_dir, exit_code=proc.returncode)

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
            attempt_dir=attempt_dir,
        )

        # ── Stage 4: LLM judge invocation ────────────────────────────────
        # Runs only when (a) the fixture declared expected_output, (b)
        # the caller supplied a judge_cfg, and (c) the agent subprocess
        # didn't crash (exit_code 0). Crash recovery later — for now we
        # skip the judge on errors because invocations.json may be
        # missing or truncated.
        if (
            fixture.expected_output
            and judge_cfg
            and proc.returncode == 0
            and judge_dir is not None
        ):
            try:
                _judge_summary = _run_judge_for_unit_fixture(
                    fixture=fixture,
                    payload=payload,
                    sink_records=posted,
                    tmp_repo=tmp_repo,
                    base_sha=base_sha,
                    source_sha=source_sha,
                    judge_dir=judge_dir,
                    agent_dir=agent_dir,
                    judge_cfg=judge_cfg,
                )
                result.judge_score   = _judge_summary.get("score")
                result.judge_verdict = _judge_summary.get("verdict")
                result.judge_summary = _judge_summary.get("summary")
                result.judge_run_id  = _judge_summary.get("run_id")
            except Exception as exc:
                # Judge failure must not poison the agent's result —
                # the user can still inspect what the agent did. We
                # surface the error tersely in stdout_tail so it's
                # visible without forcing a re-run.
                result.stderr_tail = (
                    (result.stderr_tail or "")
                    + f"\n[judge error: {type(exc).__name__}: {exc}]"
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


# ── Stage 4 helpers: UnitFixture → Scenario adapter + judge driver ───────


def _close_agent_run_if_orphaned(agent_dir: Path, *, exit_code: int) -> None:
    """Force-close the agent's `runs` row when the subprocess died
    abnormally (SIGKILL / OOM / segfault). cli.py's own try/finally
    handles SIGTERM and clean exits — but anything that bypasses
    Python's signal handlers leaks orphans.

    We discover the run_id via agent_dir/run.json (cli.py writes it
    on startup, before any work). The UPDATE is guarded by
    status='running' so a clean shutdown that beat us to the punch
    doesn't get clobbered.

    We also insert one `agent_orphaned` event so the trace UI shows
    an explicit terminal point (a stray tool_call/llm_request with
    no closing arrow is indistinguishable from "still loading"
    otherwise).
    """
    run_json = Path(agent_dir) / "run.json"
    if not run_json.exists():
        return
    try:
        meta = json.loads(run_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    run_id = str(meta.get("run_id") or "").strip()
    if not run_id:
        return

    # Same DB path cli.py and the trace server use.
    import sqlite3
    from datetime import datetime, timezone
    db_path = Path.home() / ".diffgraph" / "traces.db"
    if not db_path.exists():
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA busy_timeout=5000")
        # Race-free close — only if the row IS still running. cli.py's
        # exception handler may have beaten us here on a clean error.
        cur = conn.execute(
            "UPDATE runs SET status='failed', "
            "finished_at=COALESCE(finished_at, ?) "
            "WHERE id=? AND status='running'",
            (now, run_id),
        )
        if cur.rowcount > 0:
            # Anchor the synthetic event to the last real one so the
            # sequence diagram puts the ⚠ on the right actor + step.
            last = conn.execute(
                "SELECT agent_id, agent_name, step FROM events "
                "WHERE run_id=? ORDER BY id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if last:
                agent_id, agent_name, step = last
                conn.execute(
                    "INSERT INTO events (run_id, agent_id, agent_name, "
                    " timestamp, event_type, step, data_json) "
                    "VALUES (?, ?, ?, ?, 'agent_orphaned', ?, ?)",
                    (
                        run_id, agent_id, agent_name, now, int(step or 0),
                        json.dumps({
                            "reason": "bench-side orphan catch",
                            "exit_code": int(exit_code),
                            "detail": (
                                "subprocess returned non-zero and the "
                                "agent's runs row was still 'running' — "
                                "cli.py likely killed by SIGKILL / OOM."
                            ),
                        }),
                    ),
                )
        conn.commit()
        conn.close()
    except sqlite3.OperationalError:
        # DB busy / schema mismatch — defer to global orphan sweeper.
        pass


def _build_scenario_from_unit_fixture(
    fixture: UnitFixture, payload: dict
) -> Any:
    """Synthesize a `Scenario` dataclass from a unit yaml so LLMJudge —
    designed against the integration tier's `Scenario` shape — can
    consume our unit fixtures unchanged.

    Unit yamls use the `repo` / `pr_state` / `bench_cmd` top-level
    shape (loaded by `load_fixture` above); integration yamls use
    `input.bitbucket` / `name` / etc. (loaded by `scenario_loader`).
    The two shapes carry the same `expected_output` block; we just
    repackage the rest to satisfy the dataclass.
    """
    # Local imports — heavy module + only needed when a judge actually
    # runs. Keeps `bench run-unit` startup snappy for the no-judge path.
    from .scenario_loader import (
        ExpectedComment, ExpectedConcernFocus, ExpectedOutput,
        ExpectedReply, ForbiddenComment, Scenario, ScenarioMetadata,
        ScenarioSetup, SideEffectExpectations, Thresholds, TriggerSpec,
    )

    eo = fixture.expected_output or {}

    required = [
        ExpectedComment(
            id=rc.get("id", ""),
            type=rc.get("type", "inline"),
            severity=rc.get("severity", "major"),
            location=rc.get("location"),
            description_keywords=rc.get("description_keywords", []),
            rationale=rc.get("rationale", ""),
        )
        for rc in (eo.get("required_comments") or [])
    ]
    forbidden = [
        ForbiddenComment(description=fc.get("description", ""))
        for fc in (eo.get("forbidden_comments") or [])
    ]
    concern_focuses = [
        ExpectedConcernFocus(
            id=cf.get("id", ""),
            description_keywords=cf.get("description_keywords", []),
            rationale=cf.get("rationale", ""),
        )
        for cf in (eo.get("concern_focuses") or [])
    ]
    thr = eo.get("thresholds") or {}
    thresholds = Thresholds(
        min_score=thr.get("min_score", 0.70),
        min_required_found=thr.get("min_required_found", 1),
        max_false_positives=thr.get("max_false_positives", 5),
    )
    reply = None
    if isinstance(eo.get("reply"), dict):
        r = eo["reply"]
        reply = ExpectedReply(
            must_mention=r.get("must_mention", []) or [],
            must_address=r.get("must_address", []) or [],
            forbidden_topics=r.get("forbidden_topics", []) or [],
            forbidden_keywords=r.get("forbidden_keywords", []) or [],
            rationale=r.get("rationale", ""),
        )
    side_effects = None
    if isinstance(eo.get("side_effects"), dict):
        se = eo["side_effects"]
        side_effects = SideEffectExpectations(
            inline_comments=se.get("inline_comments"),
            review_status_change=se.get("review_status_change"),
        )
    raw_assert_via = eo.get("assert_via") or []
    if isinstance(raw_assert_via, str):
        raw_assert_via = [raw_assert_via]
    assert_via = [str(ch).strip() for ch in raw_assert_via if str(ch).strip()]

    meta_data = (fixture.raw.get("metadata") or {})
    metadata = ScenarioMetadata(
        difficulty=meta_data.get("difficulty", "medium"),
        language=meta_data.get("language", "unknown"),
        pr_size=meta_data.get("pr_size", "small"),
        scenario_type=meta_data.get("scenario_type", "agent_unit"),
        capabilities=meta_data.get("capabilities", []),
    )

    # Trigger — propagate from yaml (reviewer/dispatcher fixtures have
    # `trigger:` at top level with type/text/comment_id).
    trig = fixture.trigger or {}
    trigger = TriggerSpec(
        type=str(trig.get("type") or "auto"),
        text=str(trig.get("text") or ""),
        agent=fixture.agent,
        data={str(k): str(v) for k, v in (fixture.agent_data or {}).items()},
        user_message_from=str(fixture.user_message_from or ""),
        user_message_path=(Path(fixture.user_message_from)
                           if fixture.user_message_from else None),
    )

    # `input` is a free-form dict the judge sometimes peeks into
    # (it reads input.bitbucket.pull_request.from_branch for AGENTS.md
    # lookups). Synthesize that key from the unit yaml's repo block.
    from_branch = fixture.source_branch
    input_block = {
        "bitbucket": {
            "provider": "fake",
            "pull_request": {
                "from_branch": from_branch,
                "to_branch":   fixture.base_branch,
                "title":       (payload.get("metadata") or {}).get("title", ""),
                "description": (payload.get("metadata") or {}).get("description", ""),
            },
        },
    }

    return Scenario(
        id=fixture.fixture_id,
        name=str(fixture.raw.get("name") or fixture.fixture_id),
        tags=list(fixture.tags or []),
        input=input_block,
        expected_output=ExpectedOutput(
            required_comments=required,
            forbidden_comments=forbidden,
            expected_status_change=eo.get("expected_status_change"),
            thresholds=thresholds,
            reply=reply,
            side_effects=side_effects,
            acknowledgement_required=bool(eo.get("acknowledgement_required", False)),
            concern_focuses=concern_focuses,
            assert_via=assert_via,
        ),
        metadata=metadata,
        setup=ScenarioSetup(),
        trigger=trigger,
        source_path=fixture.fixture_path,
    )


def _run_judge_for_unit_fixture(
    *,
    fixture: UnitFixture,
    payload: dict,
    sink_records: list[dict],
    tmp_repo: Path,
    base_sha: str,
    source_sha: str,
    judge_dir: Path,
    agent_dir: Optional[Path],
    judge_cfg: dict,
) -> dict:
    """Drive LLMJudge against the agent's invocations + fake-PR view.

    Returns a flat dict with score / verdict / summary / run_id so the
    caller can stash them on UnitRunResult without depending on the
    bench's heavier JudgeOutput / ScenarioResult types here.
    """
    import asyncio
    # Heavy modules — only imported on the judge path.
    from .judge import LLMJudge
    from .fake_view import FakeBenchPRView

    # Pick the LLM client the same way `bench run` does. cli.py's
    # _make_llm_client is the single source of truth; we import it
    # lazily so this module stays importable even when bench config
    # isn't present (unit tests for run_unit itself don't need it).
    from cli import _make_llm_client  # type: ignore
    llm_client = _make_llm_client(judge_cfg)

    view = FakeBenchPRView(
        payload=payload,
        sink_records=sink_records,
        repo_path=tmp_repo,
        base_sha=base_sha,
        source_sha=source_sha,
        source_branch=fixture.source_branch,
    )

    scenario = _build_scenario_from_unit_fixture(fixture, payload)

    judge = LLMJudge(
        llm_client, view,
        judge_dir=judge_dir,
        agent_dir=agent_dir,
        model=judge_cfg.get("model", ""),
        verdict_source=judge_cfg.get("verdict_source", "api"),
        scenario_id=scenario.id,
        scenario_tags=list(scenario.tags or []),
    )

    # Mirror runner/run.py:218-228 — wrap the judge call so its trace
    # writer ALWAYS finalises (sqlite runs row → status='completed',
    # FS run.json updated). Without this the judge writes its
    # step-00-request/response files but the runs row stays at
    # status='running' forever and /qa/scoring sees a dangling row.
    try:
        output = asyncio.run(judge.evaluate(scenario))
    finally:
        finish = getattr(judge, "_finish_trace", None)
        if callable(finish):
            try:
                finish()
            except Exception:
                pass

    writer = getattr(judge, "_trace_writer", None)
    return {
        "score":   float(getattr(output, "overall_score", 0.0) or 0.0),
        "verdict": str(getattr(output, "verdict", "") or ""),
        "summary": str(getattr(output, "summary", "") or ""),
        "run_id":  str(writer.run_id) if writer is not None else "",
    }
