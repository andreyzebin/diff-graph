from __future__ import annotations

import json
import re
import shutil
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
import openai

from bitbucket.base import AgentPRView, CommentThread, ReviewStatus
from runner.scenario_loader import Scenario


# ── Output types ───────────────────────────────────────────────────

@dataclass
class CommentJudgement:
    expected_id: str
    found: bool
    matched_comment_id: int | None = None
    location_accurate: bool = False
    match_confidence: float = 0.0
    reasoning: str = ""


@dataclass
class FalsePositive:
    comment_id: int
    reasoning: str


@dataclass
class ScenarioWarning:
    kind: str       # leaky-description | unfulfillable-expectation | contradiction | trigger-mismatch | other
    detail: str


@dataclass
class AgentWarning:
    """Judge's call-out on the agent's reasoning quality, independent of
    whether a required finding was matched. E.g. wrong file/line reference,
    bogus root cause that contradicts the codebase, surface-level approval
    of a fix the scenario expected to be challenged.
    """
    kind: str       # wrong-location | wrong-reasoning | surface-acceptance | contradicts-codebase | methodology-gap | other
    detail: str
    comment_id: int | None = None   # which agent comment the concern is about, if applicable


@dataclass
class JudgeOutput:
    overall_score: float
    required_comments: list[CommentJudgement]
    false_positives: list[FalsePositive]
    status_change_verdict: str   # ok | unexpected | missing | wrong
    verdict: str                 # pass | fail
    summary: str
    raw_response: str = ""
    # Agent outputs captured during evaluation — available for scoring and reporting
    # without needing to re-fetch from Bitbucket.
    comments: list[CommentThread] = field(default_factory=list)
    review_status: ReviewStatus | None = None
    # Judge's critique of the scenario itself, independent of the agent's verdict.
    scenario_warnings: list[ScenarioWarning] = field(default_factory=list)
    # Judge's call-outs on the agent's reasoning quality. Doesn't affect the
    # numeric score (those come from required_comments / forbidden / etc.) —
    # surfaces concerns a binary scorecard would miss: wrong location, bogus
    # root cause, surface-level acceptance of an issue the scenario expected
    # to be challenged.
    agent_warnings: list[AgentWarning] = field(default_factory=list)


# ── LLM client abstraction ─────────────────────────────────────────

def _stream_print(accumulated: str) -> None:
    """Overwrite current terminal line with the tail of *accumulated* (newlines stripped)."""
    width = shutil.get_terminal_size(fallback=(120, 24)).columns
    max_len = max(width - 6, 20)
    flat = accumulated.replace("\n", " ").replace("\r", "")
    tail = flat[-max_len:]
    sys.stdout.write(f"\r  ↻  {tail:<{max_len}}")
    sys.stdout.flush()


class LLMClient(ABC):
    @abstractmethod
    def complete_json(self, prompt: str) -> dict: ...


class AnthropicLLMClient(LLMClient):
    def __init__(self, model: str = "claude-opus-4-6", temperature: float = 0,
                 stream_output: bool = False):
        self._model = model
        self._temperature = temperature
        self._stream_output = stream_output
        self._client = anthropic.Anthropic()

    def complete_json(self, prompt: str) -> dict:
        if self._stream_output:
            accumulated = ""
            with self._client.messages.stream(
                model=self._model,
                max_tokens=4096,
                temperature=self._temperature,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for text in stream.text_stream:
                    accumulated += text
                    _stream_print(accumulated)
            print(flush=True)
            return _parse_raw(accumulated)

        message = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            temperature=self._temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        return _parse_raw(message.content[0].text)


class OpenAILLMClient(LLMClient):
    """Any OpenAI-compatible endpoint (OpenAI, DeepSeek, Ollama, vLLM, etc.)."""

    def __init__(self, model: str, api_url: str, api_key: str = "", temperature: float = 0,
                 stream_output: bool = False, extra_body: dict | None = None,
                 timeout: int | None = None):
        self._model = model
        self._temperature = temperature
        self._stream_output = stream_output
        # Forwarded to every request — vendor extensions like
        # {"chat_template_kwargs": {"enable_thinking": False}} for Qwen3
        # on vLLM (without it the qwen3 tool parser leaks <think>…</think>
        # / `</parameter>` XML fragments into the JSON output).
        self._extra_body = extra_body or None
        kwargs: dict = {"base_url": api_url, "api_key": api_key or "none"}
        if timeout is not None:
            kwargs["timeout"] = timeout
        self._client = openai.OpenAI(**kwargs)

    def _create_kwargs(self, prompt: str) -> dict:
        kw: dict = {
            "model": self._model,
            "temperature": self._temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self._extra_body:
            kw["extra_body"] = self._extra_body
        return kw

    def complete_json(self, prompt: str) -> dict:
        if self._stream_output:
            accumulated = ""
            response = self._client.chat.completions.create(
                **self._create_kwargs(prompt),
                stream=True,
            )
            for chunk in response:
                text = chunk.choices[0].delta.content or ""
                accumulated += text
                _stream_print(accumulated)
            print(flush=True)
            return _parse_raw(accumulated)

        response = self._client.chat.completions.create(**self._create_kwargs(prompt))
        return _parse_raw(response.choices[0].message.content)


# ── Judge abstraction ──────────────────────────────────────────────

class Judge(ABC):
    @abstractmethod
    async def evaluate(self, scenario: Scenario) -> JudgeOutput: ...


# ── Concrete judge ─────────────────────────────────────────────────

PROMPT_TEMPLATE_PATH = Path(__file__).parent.parent / "prompts" / "judge.txt"


class LLMJudge(Judge):
    """
    Evaluates agent output against a scenario using an LLM.

    The view is injected at construction time so that evaluate() has a
    stable signature regardless of what new capabilities AgentPRView gains.
    Adding e.g. get_diff() to AgentPRView only requires changes here, not
    at every call site.
    """

    def __init__(self, llm_client: LLMClient, view: AgentPRView,
                 judge_dir: str | Path | None = None,
                 agent_dir: str | Path | None = None,
                 model: str = "",
                 verdict_source: str = "api",
                 scenario_id: str = "",
                 scenario_tags: list[str] | None = None,
                 linked_run_id: str = ""):
        self._llm_client = llm_client
        self._view = view
        self._template = PROMPT_TEMPLATE_PATH.read_text()
        # When set, dump request/response of every judge LLM call under
        # this directory using the unified trace layout (TODO §5e.10a):
        # judge_dir/run.json, events.jsonl, agents/judge-0/step-NN-{request,response}.json
        # The benchmark passes <attempt_dir>/runs/judge/.
        self._judge_dir_path = Path(judge_dir).expanduser() if judge_dir else None
        self._model = model
        # One trace writer per scenario evaluation. Lazily created on
        # first _trace_request to avoid spawning empty trace dirs when
        # evaluate() is never called (smoke / dry-run paths).
        self._trace_writer = None
        self._trace_step = 0
        # Search-dimension metadata that the bench knows at construction
        # time (TODO §5e.11) — passed through to the writer so the runs
        # row is filterable by scenario / tags / linked agent run.
        self._scenario_id = scenario_id or ""
        self._scenario_tags = list(scenario_tags or [])
        self._linked_run_id = linked_run_id or ""
        # The agent subprocess writes runs/agent/run.json with its
        # own SQLite run_id. Read it lazily on first trace request —
        # by then the subprocess has finished, so run.json is on disk.
        # Used to populate `linked_run_id` on the judge row + back-fill
        # the agent row's linked_run_id after judge writer starts.
        self._agent_dir_path = Path(agent_dir).expanduser() if agent_dir else None
        # Channel through which the agent surfaces its APPROVED/NEEDS_WORK
        # verdict — the agent's output interface contract. "api" reads
        # Bitbucket's participants endpoint (production); "comment" scans
        # the agent's general comments for a [verdict:STATUS] marker (bench
        # convenience when the bot is also the PR author and self-approve
        # via API is blocked); "both" prefers the API and falls back to
        # the marker.
        self._verdict_source = (verdict_source or "api").strip().lower() or "api"

    async def evaluate(self, scenario: Scenario,
                       exclude_comment_ids: set[int] | None = None) -> JudgeOutput:
        comments = await self._view.get_comments()
        review_status = await self._view.get_review_status(self._verdict_source)

        # Pull intended outputs from invocations.json — see
        # _load_intended_findings / _load_intended_concerns. Used by
        # agent-isolation unit tests where the agent doesn't publish
        # via PR tools (investigator standalone, reviewer
        # concerns-only, …).
        intended_findings = self._load_intended_findings()
        intended_concerns = self._load_intended_concerns()
        intended_text = self._load_intended_text()

        # When the bench posts seed_comments + the trigger comment via the
        # same Bitbucket account as the agent (single-token setup), those
        # ids land in `comments` too. Filter them out so the judge only
        # sees the agent's actual replies.
        exclude_comment_ids = exclude_comment_ids or set()
        if exclude_comment_ids:
            comments = [c for c in comments if c.id not in exclude_comment_ids]

        # Pull extra grounding so the judge can verify wrong-location and
        # contradicts-codebase claims against actual code instead of relying
        # on world knowledge alone. Best-effort — providers that don't
        # support these methods just feed the judge an empty string.
        pr_diff = ""
        agents_md = ""
        try:
            pr_diff = await self._view.get_diff()
        except NotImplementedError:
            pass
        except Exception as exc:
            log_module = __import__("logging")
            log_module.getLogger(__name__).warning("get_diff failed: %s", exc)
        try:
            from_branch = (scenario.input.get("bitbucket", {})
                           .get("pull_request", {})
                           .get("from_branch", ""))
            agents_md = await self._view.get_raw_file("AGENTS.md", from_branch)
        except NotImplementedError:
            pass
        except Exception as exc:
            log_module = __import__("logging")
            log_module.getLogger(__name__).warning("get_raw_file AGENTS.md failed: %s", exc)

        # Interaction scenarios (/ask /help): score the agent's reply text
        # against expected_output.reply, plus check side_effects.
        if scenario.expected_output.reply is not None:
            try:
                full_thread = await self._view.get_all_comments()
            except NotImplementedError:
                full_thread = comments
            prompt = _build_reply_prompt(scenario, full_thread, comments,
                                         exclude_comment_ids,
                                         pr_diff=pr_diff, agents_md=agents_md)
            self._trace_request(scenario.id, prompt)
            try:
                data = self._llm_client.complete_json(prompt)
            except Exception as exc:
                self._trace_error(scenario.id, exc)
                raise
            self._trace_response(scenario.id, data)
            output = _interpret_reply(data, scenario, comments, review_status)
            output.comments = comments
            output.review_status = review_status
            return output

        # Default: review scoring against required_comments / forbidden / status
        prompt = _build_prompt(self._template, scenario, comments, review_status,
                               pr_diff=pr_diff, agents_md=agents_md,
                               intended_findings=intended_findings,
                               intended_concerns=intended_concerns,
                               intended_text=intended_text)
        self._trace_request(scenario.id, prompt)
        try:
            data = self._llm_client.complete_json(prompt)
        except Exception as exc:
            self._trace_error(scenario.id, exc)
            raise
        self._trace_response(scenario.id, data)
        output = _interpret(data, scenario.expected_output.required_comments)
        output.comments = comments
        output.review_status = review_status
        return output

    # ── trace helpers ────────────────────────────────────────────────
    def _load_invocations(self) -> list[dict]:
        """Read invocations.json — written by the agent's CLI when
        --invocations-out points at a path. Layout (post §5e.10a):
            attempt_dir/
                runs/
                    agent/    ← judge_dir.parent.parent / 'agent' / invocations.json
                    judge/    ← judge_dir
        Falls back to legacy `<attempt>/invocations.json` for older
        sessions that pre-date the runs/<kind>/ layout.
        """
        if self._judge_dir_path is None:
            return []
        # New layout: attempt_dir/runs/agent/invocations.json
        candidates = [
            self._judge_dir_path.parent / "agent" / "invocations.json",
            # Legacy: attempt_dir/invocations.json (kept as fallback)
            self._judge_dir_path.parent / "invocations.json",
            self._judge_dir_path.parent.parent / "invocations.json",
        ]
        inv_path = next((p for p in candidates if p.exists()), None)
        if inv_path is None:
            return []
        try:
            data = json.loads(inv_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        return data.get("invocations") or []

    def _load_intended_findings(self) -> list[dict]:
        """Findings the agent passed to its final `done()` call. Used
        when an agent without post_comment (e.g. investigator) is
        scored — the judge sees done() args as a virtual comment list
        parallel to the real PR comments.
        """
        findings: list[dict] = []
        for inv in self._load_invocations():
            if (inv.get("tool") or "") != "done":
                continue
            args = inv.get("args") or {}
            raw = args.get("findings") or []
            # Same JSON-string-vs-array quirk as reflect.concerns.
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    raw = []
            if not isinstance(raw, list):
                raw = []
            for f in raw:
                if isinstance(f, dict):
                    findings.append(f)
        return findings

    def _load_intended_concerns(self) -> list[dict]:
        """Concerns the reviewer surfaced during the LOOK phase.

        Two sources, merged:
          1. `reflect(concerns=[{title, description}, ...])` calls —
             the canonical place reviewers list concerns.
          2. `spawn_agent(focus=...)` args — when the reviewer
             actually spawns investigators, the focus string is the
             concern in another shape.

        Returns a list of {title, description} dicts so the judge can
        match `concern_focuses` keyword groups against either field.
        """
        out: list[dict] = []
        for inv in self._load_invocations():
            tool = inv.get("tool") or ""
            args = inv.get("args") or {}
            if tool == "reflect":
                # The reflect schema's `questions_remaining` is the
                # canonical place for "things the agent wants to
                # investigate" — i.e. concerns. Each item is
                # {id, text}; text carries the concern as a question.
                raw = args.get("questions_remaining") or []
                if not isinstance(raw, list):
                    raw = []
                for q in raw:
                    if isinstance(q, dict):
                        text = str(q.get("text", "")).strip()
                        if not text:
                            continue
                        out.append({
                            "source": "reflect.questions_remaining",
                            "title": text[:80],
                            "description": text,
                        })
                    elif isinstance(q, str) and q.strip():
                        out.append({
                            "source": "reflect.questions_remaining",
                            "title": q[:80],
                            "description": q,
                        })
            elif tool == "spawn_agent":
                focus = str(args.get("focus", ""))
                if focus:
                    out.append({
                        "source": "spawn_agent",
                        "title": focus[:80],
                        "description": focus,
                    })
        return out

    def _load_intended_text(self) -> str:
        """Agent's final text deliverable for tasks whose channel is
        plain prose. Two emission shapes the judge knows about:

          1. `text_answer(text=...)` — capture-style tool registered
             via prompt frontmatter's `extra_tools`. Works on
             `tool_choice=required` providers (DeepSeek etc.) that
             can't return a tool-less turn.
          2. A text-only LLM turn (no tool_calls, content present)
             — emitted by orchestra as a `kind: text` invocation
             with `args.text` / `args.content` carrying the body.

        Returned: the LAST such payload (run's final deliverable).
        Empty string when nothing fits — judge sees an empty
        intended_text section and grades the agent as having
        produced no usable output.
        """
        invs = self._load_invocations()
        for inv in reversed(invs):
            args = inv.get("args") or {}
            tool = inv.get("tool") or ""
            kind = args.get("kind") if isinstance(args, dict) else None
            if tool == "text_answer" or tool == "text" or kind == "text":
                return str(args.get("text") or args.get("content") or "")
        return ""

    def _ensure_writer(self):
        """Lazy-init the unified writer on the first request.

        Reads the agent subprocess's run_id from runs/agent/run.json
        right before first use — by then the subprocess has finished
        and that file is on disk. Pairs the judge row to the agent
        run via linked_run_id (agent row → judge_run_id back-fill is
        done in _finish_trace).
        """
        if self._trace_writer is not None:
            return self._trace_writer
        # Late-bind linked_run_id from the agent's run.json if available
        # and not already set explicitly by the caller.
        if not self._linked_run_id and self._agent_dir_path is not None:
            agent_run_json = self._agent_dir_path / "run.json"
            if agent_run_json.exists():
                try:
                    data = json.loads(agent_run_json.read_text(encoding="utf-8"))
                    self._linked_run_id = (data.get("run_id") or "").strip()
                except Exception:
                    pass
        from .trace_writer import JudgeTraceWriter
        self._trace_writer = JudgeTraceWriter(
            run_dir=self._judge_dir_path,
            model=self._model,
            sub_agent_name="judge",
            kind="judge",
            scenario_id=self._scenario_id,
            scenario_tags=self._scenario_tags,
            linked_run_id=self._linked_run_id,
        )
        return self._trace_writer

    def _trace_request(self, scenario_id: str, prompt: str) -> None:
        if self._judge_dir_path is None:
            return
        w = self._ensure_writer()
        # Stash the step #; response comes back paired in _trace_response.
        self._pending_step = self._trace_step
        self._pending_prompt = prompt

    def _trace_response(self, scenario_id: str, data: dict) -> None:
        if self._judge_dir_path is None:
            return
        w = self._ensure_writer()
        step = getattr(self, "_pending_step", self._trace_step)
        prompt = getattr(self, "_pending_prompt", "")
        w.write_step(
            step=step,
            request={"prompt": prompt, "model": self._model},
            response={"data": data},
        )
        self._trace_step = step + 1

    def _trace_error(self, scenario_id: str, exc: Exception) -> None:
        if self._judge_dir_path is None:
            return
        w = self._ensure_writer()
        step = getattr(self, "_pending_step", self._trace_step)
        prompt = getattr(self, "_pending_prompt", "")
        w.write_step(
            step=step,
            request={"prompt": prompt, "model": self._model},
            error=f"{type(exc).__name__}: {exc}",
        )
        self._trace_step = step + 1
        # Mark the run as errored on finish.
        self._trace_status = "error"

    def _finish_trace(self):
        """Called by run_scenario after evaluate() returns (or raises).

        Side effect: back-fills the agent run row's linked_run_id with
        the judge's run_id, completing the bidirectional link.
        """
        if self._trace_writer is None:
            return
        status = getattr(self, "_trace_status", "completed")
        self._trace_writer.finish(status=status)
        # Back-fill agent row → judge run_id, so /api/search/runs/{agent_id}
        # surfaces the judge counterpart and vice versa.
        if self._linked_run_id and self._trace_writer.run_id:
            self._backfill_agent_linked(
                agent_run_id=self._linked_run_id,
                judge_run_id=self._trace_writer.run_id,
            )

    @staticmethod
    def _backfill_agent_linked(agent_run_id: str, judge_run_id: str) -> None:
        """Update the agent row's linked_run_id column.

        Best-effort. Trace must never crash the bench, so we swallow
        any DB error.
        """
        try:
            import sqlite3
            from pathlib import Path
            db_path = Path.home() / ".diffgraph" / "traces.db"
            if not db_path.exists():
                return
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "UPDATE runs SET linked_run_id=? "
                    "WHERE id=? AND (linked_run_id IS NULL OR linked_run_id='')",
                    (judge_run_id, agent_run_id),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass


# ── Pure helpers ───────────────────────────────────────────────────

def _build_prompt(
    template: str,
    scenario: Scenario,
    comments: list[CommentThread],
    review_status: ReviewStatus | None,
    pr_diff: str = "",
    agents_md: str = "",
    intended_findings: list[dict] | None = None,
    intended_concerns: list[dict] | None = None,
    intended_text: str = "",
) -> str:
    eo = scenario.expected_output
    required_str = json.dumps([
        {
            "id": rc.id,
            "type": rc.type,
            "severity": rc.severity,
            "location": rc.location,
            "keywords": rc.description_keywords,
            "rationale": rc.rationale,
        }
        for rc in eo.required_comments
    ], ensure_ascii=False, indent=2)

    forbidden_str = json.dumps([
        {"description": fc.description}
        for fc in eo.forbidden_comments
    ], ensure_ascii=False, indent=2)

    concern_focuses_str = json.dumps([
        {
            "id": cf.id,
            "keywords": cf.description_keywords,
            "rationale": cf.rationale,
        }
        for cf in eo.concern_focuses
    ], ensure_ascii=False, indent=2) if eo.concern_focuses else "[]"

    assert_via = list(eo.assert_via) if eo.assert_via else ["pr_comments"]
    assert_via_str = ", ".join(assert_via)

    trigger_type = getattr(scenario.trigger, "type", "") or ""
    ack_required = bool(eo.acknowledgement_required) and trigger_type == "comment"
    return template.format(
        agent_comments=_format_comments(comments),
        intended_findings=_format_intended_findings(intended_findings or []),
        intended_concerns=_format_intended_concerns(intended_concerns or []),
        intended_text=_format_intended_text(intended_text),
        required_comments=required_str,
        forbidden_comments=forbidden_str,
        concern_focuses=concern_focuses_str,
        assert_via=assert_via_str,
        expected_status_change=eo.expected_status_change or "none",
        actual_status_change=review_status.status if review_status else "none",
        pr_diff=_truncate(pr_diff, 30_000) or "(diff unavailable)",
        agents_md=_truncate(agents_md, 10_000) or "(AGENTS.md unavailable)",
        acknowledgement_required="yes" if ack_required else "no",
    )


def _format_intended_concerns(concerns: list[dict]) -> str:
    """Serialise the agent's surfaced concerns (from reflect() args
    and/or spawn_agent.focus) for the judge. For agents whose only
    job in this run is concern identification (e.g. reviewer
    concerns-only mode) this is the test signal."""
    if not concerns:
        return "(none — agent did not call reflect(concerns=...) or spawn_agent(focus=...))"
    out: list[str] = []
    for i, c in enumerate(concerns, 1):
        title = (c.get("title") or "").strip()
        desc = (c.get("description") or "").strip()
        src = c.get("source", "?")
        out.append(f"#{i} [{src}] {title}\n  {desc[:600]}")
    return "\n\n".join(out)


def _format_intended_text(text: str) -> str:
    """The agent's text deliverable — captured from the last
    text_answer/text capture tool call. For text-output tasks
    (e.g. concerns-text) this is the run's only output channel,
    so the judge grades against it directly."""
    if not text or not text.strip():
        return "(none — agent did not call text_answer / text capture tool)"
    return text.strip()[:6000]


def _format_intended_findings(findings: list[dict]) -> str:
    """Serialise the agent's done(findings=...) args for the judge.
    These are findings the agent INTENDED to publish — for agents that
    don't have post_comment in their tool list (e.g. investigator) this
    is the only signal."""
    if not findings:
        return "(none — agent did not pass a non-empty findings list to done())"
    out: list[str] = []
    for i, f in enumerate(findings, 1):
        sev = f.get("severity", "")
        title = f.get("title", "")
        file = f.get("file", "")
        line = f.get("line", "")
        explanation = (f.get("explanation") or "").strip()
        evidence = (f.get("evidence") or "").strip()
        out.append(
            f"#{i} [{sev}] {file}:{line} — {title}\n"
            f"  {explanation[:400]}\n"
            f"  evidence: {evidence[:200]}"
        )
    return "\n\n".join(out)


def _truncate(text: str, max_chars: int) -> str:
    """Cap a long blob with a clear marker. Keeps the prompt size bounded
    when a PR diff or AGENTS.md happens to be enormous.
    """
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    head = text[: max_chars - 200]
    return head + f"\n\n... [truncated; original was {len(text)} chars]"


def _interpret(data: dict, required_comments) -> JudgeOutput:
    return JudgeOutput(
        overall_score=data.get("overall_score", 0.0),
        required_comments=[
            CommentJudgement(
                expected_id=rc.get("expected_id", ""),
                found=rc.get("found", False),
                matched_comment_id=rc.get("matched_comment_id"),
                location_accurate=rc.get("location_accurate", False),
                match_confidence=rc.get("match_confidence", 0.0),
                reasoning=rc.get("reasoning", ""),
            )
            for rc in data.get("required_comments", [])
        ],
        false_positives=[
            FalsePositive(
                comment_id=fp.get("comment_id", 0),
                reasoning=fp.get("reasoning", ""),
            )
            for fp in data.get("false_positives", [])
        ],
        status_change_verdict=data.get("status_change_verdict", "unknown"),
        verdict=data.get("verdict", "fail"),
        summary=data.get("summary", ""),
        scenario_warnings=_parse_scenario_warnings(data),
        agent_warnings=_parse_agent_warnings(data),
    )


def _parse_raw(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise ValueError(f"Could not parse LLM response as JSON: {raw[:200]}")


def _format_comments(comments: list[CommentThread]) -> str:
    parts = []
    for c in comments:
        if c.anchor:
            parts.append(f"[inline] {c.anchor.path}:{c.anchor.line} — {c.text}")
        else:
            parts.append(f"[general] {c.text}")
    return "\n".join(parts) if parts else "(no comments)"


# ── Interaction scenario scoring (/ask /help) ────────────────────────

REPLY_PROMPT = """You are an LLM-as-judge evaluating a CONVERSATIONAL agent on a Bitbucket PR.

You have THREE jobs:

(A) Score the agent's reply against the expectations.
(B) Critique the SCENARIO itself — flag issues that make the test unfair or
    leaky regardless of how the agent behaved.
(C) Call out the AGENT's reasoning quality — wrong references, bogus root
    causes, surface-level acceptance — even when the binary scorecard
    happens to land in its favour.

CONVERSATION (full thread, oldest → newest, each line tagged USER or AGENT):
{thread}

USER REQUEST that triggered the agent (also visible in the thread above):
{trigger_text}

EXPECTED REPLY:
{expectations}

AGENT'S REPLY TEXT (this is what you score — collected by author from the thread):
{agent_comments}

INLINE COMMENTS POSTED BY AGENT (count): {inline_count}

PR DIFF (the changed code the conversation is about — use to verify
agent claims about file/line locations):
{pr_diff}

AGENTS.md (project conventions — use to verify methodology-gap and
contradicts-codebase claims grounded in the actual ruleset):
{agents_md}

Return STRICT JSON, no prose:
{{
  "overall_score": 0.0..1.0,
  "must_mention": [
    {{"row": 0, "matched": true|false, "matched_words": [...], "reasoning": "..."}}
  ],
  "must_address_satisfied": true|false,
  "forbidden_present": [{{"row": 0, "reasoning": "..."}}],
  "side_effects_ok": true|false,
  "side_effects_reasoning": "...",
  "verdict": "pass" | "fail" | "error",
  "summary": "1-2 sentence verdict",
  "scenario_warnings": [
    {{"kind": "leaky-description"|"unfulfillable-expectation"|"contradiction"|"trigger-mismatch"|"other",
      "detail": "1-sentence concern about the scenario itself"}}
  ],
  "agent_warnings": [
    {{"kind": "wrong-location"|"wrong-reasoning"|"surface-acceptance"|"contradicts-codebase"|"methodology-gap"|"interface-violation"|"other",
      "detail": "1-sentence concern about HOW the agent reasoned or formatted output",
      "comment_id": null|<id of the offending agent comment if applicable>}}
  ]
}}

Scoring rubric (job A):
- Score the AGENT's text only. Lines tagged USER are the conversation context, NOT
  the agent's output — never count them as "the agent merely echoed X".
- Each must_mention row matches if any of its alternatives appears semantically (synonyms ok)
  in the agent's reply.
- must_address checks the agent gave an explicit answer to the user's question
  (yes/no, fixed/not, etc) in its reply.
- forbidden_topics counts a hit if the agent introduced a topic not asked about.
- side_effects: scenario specifies whether inline_comments / status changes are allowed.
- overall_score: weighted average. Subtract heavily for missing must_address (that's the whole point).

Scenario critique (job B) — emit a warning when you see:
- "leaky-description": the PR description or a seed comment leaks the test
  intent (e.g. "we are checking that the agent does X"), or — equally bad —
  leaks the BENCH-FRAMEWORK SCAFFOLDING that this is a test at all. A
  natural PR description never says what's being tested AND never mentions
  the test machinery. Trip on phrases like:
    "isolation unit test", "unit test of", "this is a test",
    "mocked investigator(s)", "mocked reviewer", "mocked subagent",
    "see fixtures/", "fixture file", "tool_mocks", "spawn_mocks",
    "BENCHMARK scenario", "agent-isolation", "reviewer-isolation".
  ANY of these in `pr_title` / `pr_description` / a seed comment is a
  leaky-description warning, regardless of whether the agent acted on it.
- "unfulfillable-expectation": a must_mention row asks for information the agent
  could not have plausibly known from the thread + PR.
- "contradiction": expectations contradict each other or contradict the trigger
  (e.g. must_mention X but forbidden_topics also bans X).
- "trigger-mismatch": the user's question can't be answered with the seeded context
  (the conversation lacks the data needed to satisfy must_address).
- "other": anything else that smells off about the scenario design.
Empty list when the scenario looks clean.

Agent reasoning critique (job C) — emit a warning when you see:
- "wrong-location": the agent's finding references a file/line that doesn't
  match the issue described (e.g. talks about OrderService but pins the
  comment on Order.java).
- "wrong-reasoning": the agent's stated explanation contradicts the codebase
  or general knowledge of the framework in use (e.g. JPA / Hibernate /
  language semantics) — even if the surface conclusion happens to be right.
- "surface-acceptance": the agent treats a symptom-level patch as adequate
  when AGENTS.md / scenario context calls for a root-cause challenge.
- "contradicts-codebase": the explanation conflicts with patterns visible
  in adjacent files (e.g. claims a rule that other files demonstrably
  violate, or vice versa).
- "methodology-gap": the agent skipped an investigation step a reasonable
  reviewer would do (e.g. didn't open AGENTS.md when the scenario hinges
  on a project convention).
- "interface-violation": the comment format doesn't conform to the agreed
  agent interface — e.g. no `[bot_user]` prefix at the start
  (`[tuz_spasibo__qodo] ...` / `[qodo] ...`), or no dg trace footer at the
  end (`qodo:diffgraph:abc123:run-001` in inline code). Without these,
  analytics can't tie comments back to a prompt generation and humans
  can't tell the agent from a human author. State which piece is missing
  in `detail`.
- "other": anything else worth flagging about the agent's reasoning quality.
Empty list when the agent's reasoning looks sound — even when the score is
low for unrelated reasons (e.g. coverage gaps).
"""


def _build_reply_prompt(scenario: Scenario, full_thread: list[CommentThread],
                        agent_comments: list[CommentThread],
                        exclude_comment_ids: set[int] | None = None,
                        pr_diff: str = "",
                        agents_md: str = "") -> str:
    eo = scenario.expected_output
    reply = eo.reply
    expectations = json.dumps({
        "must_mention": reply.must_mention if reply else [],
        "must_address": reply.must_address if reply else [],
        "forbidden_topics": reply.forbidden_topics if reply else [],
        "forbidden_keywords": reply.forbidden_keywords if reply else [],
        "rationale": reply.rationale if reply else "",
        "side_effects": {
            "inline_comments_max": eo.side_effects.inline_comments
            if eo.side_effects and eo.side_effects.inline_comments is not None else "any",
            "review_status_change_allowed": eo.side_effects.review_status_change
            if eo.side_effects else "any",
        },
    }, ensure_ascii=False, indent=2)

    # Tag each thread comment so the judge can tell who said what even
    # though everything came from the same Bitbucket account. Comments
    # whose id is in exclude_comment_ids were posted by the bench itself
    # (seed messages + the trigger /command); everything else is the
    # agent's reply.
    exclude = exclude_comment_ids or set()
    thread_lines = []
    for i, c in enumerate(full_thread, start=1):
        role = "USER" if c.id in exclude else "AGENT"
        thread_lines.append(f"[{i}] ({role}) {c.text}")
    thread = "\n".join(thread_lines) or "(empty thread)"

    inline_count = sum(1 for c in agent_comments if c.anchor)

    return REPLY_PROMPT.format(
        thread=thread,
        trigger_text=scenario.trigger.text or "(auto-trigger / no comment)",
        expectations=expectations,
        agent_comments=_format_comments(agent_comments),
        inline_count=inline_count,
        pr_diff=_truncate(pr_diff, 30_000) or "(diff unavailable)",
        agents_md=_truncate(agents_md, 10_000) or "(AGENTS.md unavailable)",
    )


def _interpret_reply(data: dict, scenario: Scenario,
                     agent_comments: list[CommentThread],
                     review_status: ReviewStatus | None) -> JudgeOutput:
    """Adapt reply-judge output into the existing JudgeOutput shape."""
    score = float(data.get("overall_score", 0.0))
    summary = data.get("summary", "")

    # Verdict default: pass if score >= threshold
    verdict = data.get("verdict") or (
        "pass" if score >= scenario.expected_output.thresholds.min_score else "fail"
    )

    # Status verdict for interaction scenarios:
    se = scenario.expected_output.side_effects
    if se and se.review_status_change is False:
        # Must NOT change status
        if review_status is None:
            status_verdict = "correct"
        else:
            status_verdict = "incorrect"
    else:
        status_verdict = "n/a"

    return JudgeOutput(
        overall_score=score,
        required_comments=[],     # not applicable
        false_positives=[],       # could populate from forbidden_present, but verdict text suffices
        status_change_verdict=status_verdict,
        verdict=verdict,
        summary=summary,
        scenario_warnings=_parse_scenario_warnings(data),
        agent_warnings=_parse_agent_warnings(data),
    )


def _parse_scenario_warnings(data: dict) -> list[ScenarioWarning]:
    raw = data.get("scenario_warnings") or []
    out: list[ScenarioWarning] = []
    for w in raw:
        if not isinstance(w, dict):
            continue
        kind = str(w.get("kind") or w.get("type") or "other").strip() or "other"
        detail = str(w.get("detail", "")).strip()
        if not detail:
            continue
        out.append(ScenarioWarning(kind=kind, detail=detail))
    return out


def _parse_agent_warnings(data: dict) -> list[AgentWarning]:
    raw = data.get("agent_warnings") or []
    out: list[AgentWarning] = []
    for w in raw:
        if not isinstance(w, dict):
            continue
        # Some judges (e.g. deepseek-chat) emit the taxonomy under "type"
        # rather than the schema's "kind" — accept both so the user-facing
        # category survives instead of falling back to "other".
        kind = str(w.get("kind") or w.get("type") or "other").strip() or "other"
        detail = str(w.get("detail", "")).strip()
        if not detail:
            continue
        cid = w.get("comment_id")
        if cid is not None:
            try:
                cid = int(cid)
            except (ValueError, TypeError):
                cid = None
        out.append(AgentWarning(kind=kind, detail=detail, comment_id=cid))
    return out
