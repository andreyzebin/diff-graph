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
                 model: str = "",
                 verdict_source: str = "api"):
        self._llm_client = llm_client
        self._view = view
        self._template = PROMPT_TEMPLATE_PATH.read_text()
        # When set, dump request/response of every judge LLM call into this
        # exact directory. The benchmark passes <attempt_dir>/judge/.
        self._judge_dir_path = Path(judge_dir).expanduser() if judge_dir else None
        self._model = model
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
                               pr_diff=pr_diff, agents_md=agents_md)
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
    def _judge_dir(self, scenario_id: str) -> Path | None:
        if self._judge_dir_path is None:
            return None
        self._judge_dir_path.mkdir(parents=True, exist_ok=True)
        return self._judge_dir_path

    def _atomic_write(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        tmp.replace(path)

    def _trace_request(self, scenario_id: str, prompt: str) -> None:
        d = self._judge_dir(scenario_id)
        if not d:
            return
        from datetime import datetime
        self._atomic_write(d / "request.json", {
            "ts": datetime.now().isoformat(),
            "model": self._model,
            "prompt": prompt,
        })

    def _trace_response(self, scenario_id: str, data: dict) -> None:
        d = self._judge_dir(scenario_id)
        if not d:
            return
        from datetime import datetime
        self._atomic_write(d / "response.json", {
            "ts": datetime.now().isoformat(),
            "data": data,
        })

    def _trace_error(self, scenario_id: str, exc: Exception) -> None:
        d = self._judge_dir(scenario_id)
        if not d:
            return
        from datetime import datetime
        self._atomic_write(d / "error.json", {
            "ts": datetime.now().isoformat(),
            "error": str(exc),
            "type": type(exc).__name__,
        })


# ── Pure helpers ───────────────────────────────────────────────────

def _build_prompt(
    template: str,
    scenario: Scenario,
    comments: list[CommentThread],
    review_status: ReviewStatus | None,
    pr_diff: str = "",
    agents_md: str = "",
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

    return template.format(
        agent_comments=_format_comments(comments),
        required_comments=required_str,
        forbidden_comments=forbidden_str,
        expected_status_change=eo.expected_status_change or "none",
        actual_status_change=review_status.status if review_status else "none",
        pr_diff=_truncate(pr_diff, 30_000) or "(diff unavailable)",
        agents_md=_truncate(agents_md, 10_000) or "(AGENTS.md unavailable)",
    )


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
- "leaky-description": the PR description or a seed comment leaks the test intent
  (e.g. "we are checking that the agent does X"), making the agent's output
  trivially aligned. A natural PR description never says what's being tested.
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
