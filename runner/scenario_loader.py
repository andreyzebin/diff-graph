from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
import jsonschema

SCENARIO_SCHEMA = {
    "type": "object",
    "required": ["id", "name", "input", "expected_output"],
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "input": {
            "type": "object",
            "required": ["bitbucket", "jira"],
            "properties": {
                "bitbucket": {"type": "object", "required": ["base_provider"]},
                "jira": {"type": "object", "required": ["base_provider"]},
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
class Thresholds:
    min_score: float = 0.70
    min_required_found: int = 1
    max_false_positives: int = 3


@dataclass
class ExpectedOutput:
    required_comments: list[ExpectedComment]
    forbidden_comments: list[ForbiddenComment]
    expected_status_change: str | None
    thresholds: Thresholds


@dataclass
class ScenarioMetadata:
    difficulty: str = "medium"
    language: str = "unknown"
    pr_size: str = "small"
    scenario_type: str = "bug"
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

    thr_data = eo.get("thresholds", {})
    thresholds = Thresholds(
        min_score=thr_data.get("min_score", 0.70),
        min_required_found=thr_data.get("min_required_found", 1),
        max_false_positives=thr_data.get("max_false_positives", 3),
    )

    meta_data = data.get("metadata", {})
    metadata = ScenarioMetadata(
        difficulty=meta_data.get("difficulty", "medium"),
        language=meta_data.get("language", "unknown"),
        pr_size=meta_data.get("pr_size", "small"),
        scenario_type=meta_data.get("scenario_type", "bug"),
        author=meta_data.get("author", "team"),
        created=meta_data.get("created", ""),
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
        ),
        metadata=metadata,
        source_path=path,
    )


def load_scenarios(
    scenarios_dir: Path,
    tags: list[str] | None = None,
    scenario_id: str | None = None,
) -> list[Scenario]:
    scenarios = []
    for yaml_path in sorted(scenarios_dir.rglob("*.yaml")):
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
