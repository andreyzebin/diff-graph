from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import anthropic

from bitbucket.base import CommentThread, FileDiff, FileContent, ReviewStatus
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


# ── LLM client abstraction ─────────────────────────────────────────

class LLMClient(ABC):
    @abstractmethod
    def complete_json(self, prompt: str) -> dict: ...


class AnthropicLLMClient(LLMClient):
    def __init__(self, model: str = "claude-opus-4-6", temperature: float = 0):
        self._model = model
        self._temperature = temperature
        self._client = anthropic.Anthropic()

    def complete_json(self, prompt: str) -> dict:
        message = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            temperature=self._temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text
        return _parse_raw(raw)


# ── Judge abstraction ──────────────────────────────────────────────

class Judge(ABC):
    @abstractmethod
    async def evaluate(
        self,
        scenario: Scenario,
        comments: list[CommentThread],
        review_status: ReviewStatus | None,
        diff: list[FileDiff] | None = None,
        codebase_context: list[FileContent] | None = None,
        jira_summary: str = "",
        jira_description: str = "",
    ) -> JudgeOutput: ...


# ── Concrete judge ─────────────────────────────────────────────────

PROMPT_TEMPLATE_PATH = Path(__file__).parent.parent / "prompts" / "judge.txt"


class LLMJudge(Judge):
    def __init__(self, llm_client: LLMClient):
        self._llm_client = llm_client
        self._template = (
            PROMPT_TEMPLATE_PATH.read_text()
            if PROMPT_TEMPLATE_PATH.exists()
            else _DEFAULT_PROMPT
        )

    async def evaluate(
        self,
        scenario: Scenario,
        comments: list[CommentThread],
        review_status: ReviewStatus | None,
        diff: list[FileDiff] | None = None,
        codebase_context: list[FileContent] | None = None,
        jira_summary: str = "",
        jira_description: str = "",
    ) -> JudgeOutput:
        prompt = _build_prompt(
            self._template, scenario, comments, review_status,
            diff or [], codebase_context or [], jira_summary, jira_description,
        )
        data = self._llm_client.complete_json(prompt)
        return _interpret(data, scenario.expected_output.required_comments)


# ── Pure helpers ───────────────────────────────────────────────────

def _build_prompt(
    template: str,
    scenario: Scenario,
    comments: list[CommentThread],
    review_status: ReviewStatus | None,
    diff: list[FileDiff],
    codebase_context: list[FileContent],
    jira_summary: str,
    jira_description: str,
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
        jira_key=scenario.id,
        jira_summary=jira_summary,
        jira_description=jira_description,
        diff=_format_diff(diff),
        codebase_context=_format_context(codebase_context),
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


def _format_diff(diffs: list[FileDiff]) -> str:
    parts = []
    for fd in diffs:
        parts.append(f"File: {fd.path} ({fd.change_type})")
        for h in fd.hunks:
            parts.append(f"  @@ -{h.old_start} +{h.new_start} @@")
            for line in h.lines:
                parts.append(f"  {line}")
    return "\n".join(parts)


def _format_context(files: list[FileContent]) -> str:
    return "\n\n".join(f"=== {f.path} ===\n{f.content}" for f in files)


def _format_comments(comments: list[CommentThread]) -> str:
    parts = []
    for c in comments:
        if c.anchor:
            parts.append(f"[inline] {c.anchor.path}:{c.anchor.line} — {c.text}")
        else:
            parts.append(f"[general] {c.text}")
    return "\n".join(parts) if parts else "(no comments)"


_DEFAULT_PROMPT = """
Ты — эксперт по код-ревью. Оцени качество ревью выполненного AI-агентом.

## Контекст задачи
Jira: {jira_key} — {jira_summary}
{jira_description}

## Изменения в PR (diff)
{diff}

## Контекст кодовой базы (файлы запрошенные агентом)
{codebase_context}

## Что агент написал в PR
{agent_comments}

## Задание

1. Для каждого ОБЯЗАТЕЛЬНОГО замечания определи:
   - Нашёл ли агент его (семантически, не текстуально)?
   - Указал ли на правильный файл и строку (±2 строки допустимо)?
   - Уверенность совпадения (0.0–1.0)

2. Найди ЛИШНИЕ замечания — не связанные с задачей или diff.

3. Оцени корректность смены статуса PR.

4. Поставь общий балл 0.0–1.0.

Обязательные замечания:
{required_comments}

Запрещённые темы:
{forbidden_comments}

Ожидаемый статус PR: {expected_status_change}
Фактический статус PR: {actual_status_change}

Отвечай строго в JSON по следующей схеме:
{{
  "overall_score": 0.85,
  "required_comments": [
    {{
      "expected_id": "EXP-1",
      "found": true,
      "matched_comment_id": 2,
      "location_accurate": true,
      "match_confidence": 0.92,
      "reasoning": "..."
    }}
  ],
  "false_positives": [
    {{
      "comment_id": 5,
      "reasoning": "..."
    }}
  ],
  "status_change_verdict": "ok",
  "verdict": "pass",
  "summary": "..."
}}

Без текста вне JSON.
"""
