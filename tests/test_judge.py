from __future__ import annotations

from bitbucket.base import AgentPRView, CommentAnchor, CommentThread, ReviewStatus
from runner.judge import (
    Judge, JudgeOutput, LLMClient, LLMJudge,
    CommentJudgement, FalsePositive,
)
from runner.scorer import score_scenario
from runner.scenario_loader import (
    Scenario, ExpectedOutput, ExpectedComment, ForbiddenComment,
    Thresholds, ScenarioMetadata,
)


# ── Test doubles ───────────────────────────────────────────────────

class CapturingLLMClient(LLMClient):
    """Records the prompt it receives, returns a fixed dict."""

    def __init__(self, response: dict):
        self._response = response
        self.last_prompt: str | None = None

    def complete_json(self, prompt: str) -> dict:
        self.last_prompt = prompt
        return self._response


class MockProxy(AgentPRView):
    def __init__(
        self,
        pr_id: int,
        comments: list[CommentThread],
        review_status: ReviewStatus | None = None,
    ):
        self._pr_id = pr_id
        self._comments = comments
        self._review_status = review_status

    @property
    def pr_id(self) -> int:
        return self._pr_id

    async def close(self) -> None:
        pass

    async def get_comments(self) -> list[CommentThread]:
        return self._comments

    async def get_review_status(self) -> ReviewStatus | None:
        return self._review_status


# ── Helpers ────────────────────────────────────────────────────────

def _make_scenario(
    required: list[ExpectedComment],
    forbidden: list[ForbiddenComment] | None = None,
    expected_status: str | None = "NEEDS_WORK",
) -> Scenario:
    return Scenario(
        id="TEST-001",
        name="Test scenario",
        tags=[],
        input={
            "bitbucket": {"base_provider": "fixture", "data": {}},
            "jira": {"base_provider": "fixture", "data": {}},
        },
        expected_output=ExpectedOutput(
            required_comments=required,
            forbidden_comments=forbidden or [],
            expected_status_change=expected_status,
            thresholds=Thresholds(min_score=0.7, min_required_found=1, max_false_positives=2),
        ),
        metadata=ScenarioMetadata(),
    )


# ── Tests ──────────────────────────────────────────────────────────

async def test_judge_found_comment_passes():
    scenario = _make_scenario([
        ExpectedComment(
            id="EXP-1",
            type="inline",
            severity="critical",
            location={"file": "src/main/java/com/example/UserService.java", "line": 42},
            description_keywords=[["null", "pointer"], ["findById", "null"]],
            rationale="Agent should catch that findById may return null and user.getName() will NPE",
        )
    ])

    comments = [
        CommentThread(
            id=1,
            text="This can cause a NullPointerException: user may be null after findById()",
            anchor=CommentAnchor(
                path="src/main/java/com/example/UserService.java",
                line=42,
                line_type="ADDED",
            ),
        )
    ]
    review_status = ReviewStatus(status="NEEDS_WORK")

    llm = CapturingLLMClient(response={
        "overall_score": 0.9,
        "required_comments": [
            {
                "expected_id": "EXP-1",
                "found": True,
                "matched_comment_id": 1,
                "location_accurate": True,
                "match_confidence": 0.95,
                "reasoning": "Agent correctly identified NPE risk at line 42",
            }
        ],
        "false_positives": [],
        "status_change_verdict": "ok",
        "verdict": "pass",
        "summary": "Agent found the critical issue",
    })

    judge = LLMJudge(llm, MockProxy(1, comments, review_status))
    output = await judge.evaluate(scenario)

    print(f"\n{'─' * 60}\nPROMPT SENT TO LLM:\n{'─' * 60}\n{llm.last_prompt}\n{'─' * 60}")

    result = score_scenario(scenario, output.comments, output.review_status, output, duration_seconds=1.0)

    assert output.verdict == "pass"
    assert output.overall_score == 0.9
    assert output.required_comments[0].found is True
    assert output.required_comments[0].location_accurate is True
    assert output.status_change_verdict == "ok"
    assert result.verdict == "pass"
    assert result.required_found == 1
    assert result.false_positives == 0
    assert llm.last_prompt is not None
    assert "EXP-1" in llm.last_prompt
    assert "findById" in llm.last_prompt


async def test_judge_missed_comment_fails():
    scenario = _make_scenario([
        ExpectedComment(
            id="EXP-1",
            type="inline",
            severity="critical",
            location={"file": "src/main/java/com/example/UserService.java", "line": 42},
            description_keywords=[["null", "pointer"], ["findById", "null"]],
            rationale="Agent should catch that findById may return null and user.getName() will NPE",
        )
    ])

    comments = [
        CommentThread(id=1, text="Looks good overall", anchor=None)
    ]

    llm = CapturingLLMClient(response={
        "overall_score": 0.1,
        "required_comments": [
            {
                "expected_id": "EXP-1",
                "found": False,
                "matched_comment_id": None,
                "location_accurate": False,
                "match_confidence": 0.0,
                "reasoning": "Agent did not mention NPE risk",
            }
        ],
        "false_positives": [],
        "status_change_verdict": "missing",
        "verdict": "fail",
        "summary": "Agent missed the critical issue",
    })

    judge = LLMJudge(llm, MockProxy(1, comments))
    output = await judge.evaluate(scenario)

    print(f"\n{'─' * 60}\nPROMPT SENT TO LLM:\n{'─' * 60}\n{llm.last_prompt}\n{'─' * 60}")

    result = score_scenario(scenario, output.comments, output.review_status, output, duration_seconds=1.0)

    assert output.verdict == "fail"
    assert output.required_comments[0].found is False
    assert result.verdict == "fail"
    assert result.required_found == 0
