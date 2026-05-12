from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
import jsonschema


# ── Path resolution: relative-to-yaml or diffgraph:<path> ─────────────────
#
# Mirrors `runner/run_unit.py:_resolve_prompt_path`. Two URI shapes:
#   - plain relative path → relative to the scenario yaml's directory
#   - `diffgraph:<path>`  → relative to diff-graph repo root (env
#                            DIFFGRAPH_REPO, default /home/andrey/...)
#
# The diffgraph: shape lets task prompts live in diff-graph next to
# production agent prompts (diffgraph/test_prompts/<agent>/<file>.md)
# so unit + integration scenarios + production share one source of
# truth, avoiding drift.


_DIFFGRAPH_REPO_DEFAULT = "/home/andrey/repos/diff-graph"


def _resolve_prompt_path(spec: str, fixture_dir: Path) -> Path:
    if spec.startswith("diffgraph:"):
        repo = os.environ.get("DIFFGRAPH_REPO", _DIFFGRAPH_REPO_DEFAULT)
        return Path(repo).expanduser() / spec[len("diffgraph:"):]
    return (fixture_dir / spec).resolve()

SCENARIO_SCHEMA = {
    "type": "object",
    "required": ["id", "name", "input", "expected_output"],
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "input": {
            "type": "object",
            "required": ["bitbucket"],
            "properties": {
                "bitbucket": {"type": "object", "required": ["provider"]},
            },
        },
        "expected_output": {
            "type": "object",
            "properties": {
                "required_comments": {"type": "array"},
                "forbidden_comments": {"type": "array"},
                "expected_status_change": {},
                "thresholds": {"type": "object"},
            },
        },
        "metadata": {"type": "object"},
    },
}


@dataclass
class ExpectedComment:
    id: str
    type: str             # inline | general
    severity: str         # critical | major | minor
    location: dict | None
    description_keywords: list[list[str]]
    rationale: str


@dataclass
class ForbiddenComment:
    description: str


@dataclass
class ExpectedConcernFocus:
    """Concern the reviewer should have surfaced via reflect() or
    spawn_agent.focus. Match by keyword groups against the union of
    titles + descriptions extracted from invocations.json — same
    AND-of-OR semantics as ExpectedComment.description_keywords.
    """
    id: str
    description_keywords: list[list[str]]
    rationale: str = ""


@dataclass
class Thresholds:
    min_score: float = 0.70
    min_required_found: int = 1
    max_false_positives: int = 5


@dataclass
class ExpectedReply:
    """Score the agent's reply text on the PR thread (vs inline comments)."""
    must_mention: list[list[str]] = field(default_factory=list)   # AND-of-OR semantic match
    must_address: list[str] = field(default_factory=list)         # at least one must appear
    forbidden_topics: list[list[str]] = field(default_factory=list)
    forbidden_keywords: list[list[str]] = field(default_factory=list)
    rationale: str = ""


@dataclass
class SideEffectExpectations:
    """What MUST NOT change for /ask /help (these are read-only commands)."""
    inline_comments: int | None = None    # exact count expected (0 for /ask /help)
    review_status_change: bool | None = None  # False = must stay UNAPPROVED


@dataclass
class TriggerSpec:
    """How the scenario invokes the agent."""
    type: str = "auto"                  # auto (pr:opened) | comment | review
    text: str = ""                      # comment body when type=comment
    # Where to plant the trigger comment:
    #   None        — post as a new root comment (existing behaviour)
    #   list[int]   — path of indices walking the seed_comments tree to a
    #                 specific node; trigger becomes a reply to that node.
    #                 e.g. [1, 0, 0] = 2nd root → 1st reply → 1st reply.
    in_reply_to: list[int] | None = None
    # Agent-isolation knobs. Override which agent the CLI invokes
    # (default behaviour: dispatcher when text is set, reviewer
    # otherwise). `data` adds `-d key=value` flags so e.g. an
    # investigator unit test can pass its `focus` from the scenario.
    agent: str = ""
    data: dict = field(default_factory=dict)
    # Override the agent's default user-message template. The system
    # prompt (methodology) stays intact; only the user-side framing
    # of the task changes. Used to test the same agent on different
    # task framings — e.g. reviewer's consolidation phase by
    # pre-feeding investigation results.
    user_message: str = ""              # inline text from yaml
    user_message_from: str = ""         # relative path; loader resolves
    user_message_path: Path | None = None  # resolved absolute path


@dataclass
class SeedComment:
    """One seeded thread node — text plus optional nested replies."""
    text: str
    replies: list["SeedComment"] = field(default_factory=list)


@dataclass
class ScenarioSetup:
    """State to seed in the PR before the trigger fires.

    Supports two YAML shapes for `seed_comments`:
      • flat list of strings → each becomes a root comment
      • tree of {text, replies: [...]} dicts → arbitrary depth
    Mixing is allowed — a string is treated as a leaf SeedComment.

    `mocks` is a file path relative to the scenario YAML pointing at
    a tool-mock fixture (orchestra's --mocks format). When set, the
    bench passes it through to the agent CLI as --mocks <abspath>,
    short-circuiting spawn_agent / read_file / etc. with canned
    responses for fast isolated unit tests of one agent at a time.
    Resolved to an absolute path at load time (see `mocks_path`).
    """
    seed_comments: list[SeedComment] = field(default_factory=list)
    mocks: str = ""                    # raw value from yaml (relative path)
    mocks_path: Path | None = None     # resolved absolute path; set on load


def _parse_seed_tree(items: list) -> list[SeedComment]:
    """Recursive YAML → SeedComment tree."""
    out: list[SeedComment] = []
    for item in items or []:
        if isinstance(item, str):
            out.append(SeedComment(text=item))
        elif isinstance(item, dict):
            out.append(SeedComment(
                text=str(item.get("text", "")),
                replies=_parse_seed_tree(item.get("replies", []) or []),
            ))
    return out


@dataclass
class ExpectedOutput:
    required_comments: list[ExpectedComment]
    forbidden_comments: list[ForbiddenComment]
    expected_status_change: str | None
    thresholds: Thresholds
    reply: ExpectedReply | None = None
    side_effects: SideEffectExpectations | None = None
    # When True, the judge checks that the dispatcher posted a quick
    # "Starting review of <PR>..." reply (or similar acknowledgement)
    # to the trigger comment BEFORE any findings appear. Only applies
    # when scenario.trigger.type == "comment" — direct reviewer
    # invocations don't have a comment to ack.
    acknowledgement_required: bool = False
    # Concerns the reviewer should have surfaced. Used by tests that
    # short-circuit the pipeline before INVESTIGATE (e.g. REV-001
    # concerns-only): judge extracts the concerns the reviewer wrote
    # to reflect() and/or the focuses it passed to spawn_agent from
    # invocations.json, then matches each concern_focuses keyword
    # group against the union.
    concern_focuses: list[ExpectedConcernFocus] = field(default_factory=list)
    # Channels the judge should match `required_comments` against:
    #   "pr_comments"        — real comments posted via post_comment
    #                          (default when assert_via is empty)
    #   "intended_findings"  — done(findings=[...]) args from
    #                          invocations.json
    #   "intended_concerns"  — reflect(concerns=[...]) +
    #                          spawn_agent(focus=...) from invocations.json
    # The judge takes the UNION of enabled channels. Lets a scenario
    # explicitly say "investigator standalone — match against done(),
    # not the PR" or "reviewer concerns-only — match against reflect()".
    assert_via: list[str] = field(default_factory=list)


@dataclass
class ScenarioMetadata:
    difficulty: str = "medium"
    language: str = "unknown"
    pr_size: str = "small"
    scenario_type: str = "bug"
    capabilities: list[str] = field(default_factory=list)
    author: str = "team"
    created: str = ""


@dataclass
class Scenario:
    id: str
    name: str
    tags: list[str]
    input: dict
    expected_output: ExpectedOutput
    metadata: ScenarioMetadata
    setup: ScenarioSetup = field(default_factory=ScenarioSetup)
    trigger: TriggerSpec = field(default_factory=TriggerSpec)
    source_path: Path | None = None


def load_scenario(path: Path) -> Scenario:
    with open(path) as f:
        data = yaml.safe_load(f)

    try:
        jsonschema.validate(data, SCENARIO_SCHEMA)
    except jsonschema.ValidationError as e:
        raise ValueError(f"Invalid scenario {path}: {e.message}") from e

    eo = data.get("expected_output", {})
    required = []
    for rc in eo.get("required_comments", []):
        required.append(ExpectedComment(
            id=rc.get("id", ""),
            type=rc.get("type", "inline"),
            severity=rc.get("severity", "major"),
            location=rc.get("location"),
            description_keywords=rc.get("description_keywords", []),
            rationale=rc.get("rationale", ""),
        ))

    forbidden = [
        ForbiddenComment(description=fc.get("description", ""))
        for fc in eo.get("forbidden_comments", [])
    ]

    concern_focuses = [
        ExpectedConcernFocus(
            id=cf.get("id", ""),
            description_keywords=cf.get("description_keywords", []),
            rationale=cf.get("rationale", ""),
        )
        for cf in eo.get("concern_focuses", [])
    ]

    raw_assert_via = eo.get("assert_via") or []
    if isinstance(raw_assert_via, str):
        raw_assert_via = [raw_assert_via]
    valid_channels = {"pr_comments", "intended_findings", "intended_concerns"}
    assert_via: list[str] = []
    for ch in raw_assert_via:
        ch = str(ch).strip()
        if ch not in valid_channels:
            raise ValueError(
                f"scenario {path}: expected_output.assert_via has unknown "
                f"channel {ch!r}; allowed: {sorted(valid_channels)}"
            )
        assert_via.append(ch)

    thr_data = eo.get("thresholds", {})
    thresholds = Thresholds(
        min_score=thr_data.get("min_score", 0.70),
        min_required_found=thr_data.get("min_required_found", 1),
        max_false_positives=thr_data.get("max_false_positives", 5),
    )

    meta_data = data.get("metadata", {})
    metadata = ScenarioMetadata(
        difficulty=meta_data.get("difficulty", "medium"),
        language=meta_data.get("language", "unknown"),
        pr_size=meta_data.get("pr_size", "small"),
        scenario_type=meta_data.get("scenario_type", "bug"),
        capabilities=meta_data.get("capabilities", []),
        author=meta_data.get("author", "team"),
        created=meta_data.get("created", ""),
    )

    # Optional reply / side-effect blocks (interaction scenarios)
    reply = None
    if "reply" in eo and isinstance(eo["reply"], dict):
        r = eo["reply"]
        reply = ExpectedReply(
            must_mention=r.get("must_mention", []) or [],
            must_address=r.get("must_address", []) or [],
            forbidden_topics=r.get("forbidden_topics", []) or [],
            forbidden_keywords=r.get("forbidden_keywords", []) or [],
            rationale=r.get("rationale", ""),
        )
    side_effects = None
    if "side_effects" in eo and isinstance(eo["side_effects"], dict):
        se = eo["side_effects"]
        side_effects = SideEffectExpectations(
            inline_comments=se.get("inline_comments"),
            review_status_change=se.get("review_status_change"),
        )

    setup_data = data.get("input", {}).get("setup", {}) or {}
    mocks_rel = str(setup_data.get("mocks", "") or "")
    mocks_path: Path | None = None
    if mocks_rel:
        # Resolve relative to scenario yaml (default) OR via
        # diffgraph: URI prefix → diff-graph repo root. See
        # _resolve_prompt_path docstring for the rationale.
        mp = _resolve_prompt_path(mocks_rel, path.parent)
        if not mp.exists():
            raise FileNotFoundError(
                f"scenario {path}: setup.mocks → {mp} does not exist"
            )
        mocks_path = mp
    setup = ScenarioSetup(
        seed_comments=_parse_seed_tree(setup_data.get("seed_comments", []) or []),
        mocks=mocks_rel,
        mocks_path=mocks_path,
    )

    trig_data = data.get("input", {}).get("trigger", {}) or {}
    trigger_data_field = trig_data.get("data") or {}
    if not isinstance(trigger_data_field, dict):
        raise ValueError(
            f"scenario {path}: trigger.data must be a mapping (got "
            f"{type(trigger_data_field).__name__})"
        )
    user_message_inline = str(trig_data.get("user_message", "") or "")
    user_message_from_rel = str(trig_data.get("user_message_from", "") or "")
    user_message_path: Path | None = None
    if user_message_from_rel:
        candidate = _resolve_prompt_path(user_message_from_rel, path.parent)
        if not candidate.exists():
            raise FileNotFoundError(
                f"scenario {path}: trigger.user_message_from → {candidate} does not exist"
            )
        user_message_path = candidate
    trigger = TriggerSpec(
        type=trig_data.get("type", "auto"),
        text=trig_data.get("text", ""),
        in_reply_to=trig_data.get("in_reply_to") or None,
        agent=str(trig_data.get("agent", "") or ""),
        data={str(k): str(v) for k, v in trigger_data_field.items()},
        user_message=user_message_inline,
        user_message_from=user_message_from_rel,
        user_message_path=user_message_path,
    )

    return Scenario(
        id=data["id"],
        name=data["name"],
        tags=data.get("tags", []),
        input=data["input"],
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
        setup=setup,
        trigger=trigger,
        source_path=path,
    )


def load_scenarios(
    scenarios_dir: Path,
    tags: list[str] | None = None,
    scenario_id: str | None = None,
) -> list[Scenario]:
    scenarios = []
    for yaml_path in sorted(scenarios_dir.rglob("*.yaml")):
        # Anything under a `drafts/` directory is design-only — keeps WIP
        # scenario specs (e.g. SCEN-302 refs-updated which needs bench-side
        # BranchUpdater) visible in the tree without tripping the runner.
        if "drafts" in yaml_path.parts:
            continue
        try:
            scenario = load_scenario(yaml_path)
        except Exception as e:
            print(f"Warning: skipping {yaml_path}: {e}")
            continue

        if scenario_id and scenario.id != scenario_id:
            continue

        if tags:
            scenario_tags = set(scenario.tags)
            if not any(t in scenario_tags for t in tags):
                continue

        scenarios.append(scenario)

    return scenarios
