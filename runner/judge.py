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
                 model: str = ""):
        self._llm_client = llm_client
        self._view = view
        self._template = PROMPT_TEMPLATE_PATH.read_text()
        # When set, dump request/response of every judge LLM call into this
        # exact directory. The benchmark passes <attempt_dir>/judge/.
        self._judge_dir_path = Path(judge_dir).expanduser() if judge_dir else None
        self._model = model

    async def evaluate(self, scenario: Scenario) -> JudgeOutput:
        comments = await self._view.get_comments()
        review_status = await self._view.get_review_status()

        prompt = _build_prompt(self._template, scenario, comments, review_status)
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
    )


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
