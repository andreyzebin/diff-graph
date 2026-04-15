"""
Tests for webhook router — config, routing, forward/command modes, sample.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from webhook.config import load_config
from webhook.bitbucket import parse_event, extract_commands, PRMeta, WebhookEvent, CommandRequest
from webhook.router import route_event, ForwardDecision, CommandDecision, _eval_when, _sample_match

log = logging.getLogger(__name__)

EXAMPLE_CONFIG = Path(__file__).parent.parent / "config.example.toml"


# ── Config loading ───────────────────────────────────────────────────────────


class TestLoadConfig:

    def test_loads_example(self):
        cfg = load_config(EXAMPLE_CONFIG)
        assert "dg2" in cfg.agents
        assert "dg1" in cfg.agents
        assert "pra" in cfg.agents

    def test_agents_have_commands(self):
        cfg = load_config(EXAMPLE_CONFIG)
        assert "cli.py" in cfg.agents["dg2"].command
        assert cfg.agents["dg2"].trigger == "cli"
        assert cfg.agents["pra"].trigger == "webhook"

    def test_events_parsed(self):
        cfg = load_config(EXAMPLE_CONFIG)
        assert cfg.events["pr:opened"] == ["review"]
        assert cfg.events["pr:comment:added"] == "parse"

    def test_routes_have_forward_and_agent(self):
        cfg = load_config(EXAMPLE_CONFIG)
        forwards = [r for r in cfg.routes if r.forward]
        agents = [r for r in cfg.routes if r.agent]
        assert len(forwards) >= 2  # legacy-forward, platform-forward
        assert len(agents) >= 3    # platform-commands, canary, sbloom, default

    def test_route_sample(self):
        cfg = load_config(EXAMPLE_CONFIG)
        platform_fwd = next(r for r in cfg.routes if r.name == "platform-forward")
        assert platform_fwd.sample == 50
        assert platform_fwd.forward == "pra"

    def test_route_per_command_override(self):
        cfg = load_config(EXAMPLE_CONFIG)
        sbloom = next(r for r in cfg.routes if r.name == "sbloom")
        assert sbloom.commands["improve"] == "pra"


# ── Bitbucket event parsing ─────────────────────────────────────────────────


def _make_bb_event(event_key="pr:opened", project="SBLOOM", repo="my-repo",
                   pr_id=42, comment_text="", comment_id=None, parent_id=None):
    data = {
        "eventKey": event_key,
        "pullRequest": {
            "id": pr_id,
            "title": "Test PR",
            "fromRef": {
                "displayId": "feature/test",
                "repository": {"slug": repo, "project": {"key": project}},
            },
            "toRef": {
                "displayId": "master",
                "repository": {"slug": repo, "project": {"key": project}},
            },
            "author": {"user": {"name": "testuser"}},
        },
    }
    if comment_text:
        comment = {"text": comment_text}
        if comment_id:
            comment["id"] = comment_id
        if parent_id:
            comment["parent"] = {"id": parent_id}
        data["comment"] = comment
    return data


class TestParseEvent:

    def test_pr_opened(self):
        ev = parse_event(_make_bb_event("pr:opened"), "https://bb.example.com")
        assert ev.event_key == "pr:opened"
        assert ev.pr.project == "SBLOOM"
        assert "pull-requests/42" in ev.pr.pr_url

    def test_comment_with_mention_and_args(self):
        ev = parse_event(
            _make_bb_event("pr:comment:added",
                          comment_text="@diffgraph /ask Is this null-safe?",
                          comment_id=200, parent_id=150),
            "",
        )
        assert ev.comment_text == "@diffgraph /ask Is this null-safe?"
        assert ev.comment_id == 200
        assert ev.parent_comment_id == 150

    def test_no_pr(self):
        assert parse_event({"eventKey": "something"}, "") is None


class TestExtractCommands:

    def test_pr_opened_auto(self):
        cfg = load_config(EXAMPLE_CONFIG)
        ev = WebhookEvent(event_key="pr:opened", pr=PRMeta("X", "x", 1, "", "", "", "", ""))
        cmds = extract_commands(ev, cfg.events)
        assert len(cmds) == 1
        assert cmds[0].name == "review"

    def test_comment_parse_with_args(self):
        cfg = load_config(EXAMPLE_CONFIG)
        ev = WebhookEvent(
            event_key="pr:comment:added",
            pr=PRMeta("X", "x", 1, "", "", "", "", ""),
            comment_text="@bot /ask What about null safety?",
            comment_id=200,
        )
        cmds = extract_commands(ev, cfg.events)
        assert cmds[0].name == "ask"
        assert cmds[0].args == "What about null safety?"
        assert cmds[0].comment_id == 200

    def test_comment_no_command(self):
        cfg = load_config(EXAMPLE_CONFIG)
        ev = WebhookEvent(
            event_key="pr:comment:added",
            pr=PRMeta("X", "x", 1, "", "", "", "", ""),
            comment_text="just a comment",
        )
        cmds = extract_commands(ev, cfg.events)
        assert len(cmds) == 1
        assert cmds[0].name == "default"
        assert cmds[0].args == "just a comment"

    def test_unknown_event(self):
        cfg = load_config(EXAMPLE_CONFIG)
        ev = WebhookEvent(event_key="unknown", pr=PRMeta("X", "x", 1, "", "", "", "", ""))
        assert extract_commands(ev, cfg.events) == []


# ── Eval & sample ────────────────────────────────────────────────────────────


class TestEvalWhen:

    def test_true(self):
        assert _eval_when("true", {})
        assert _eval_when("*", {})

    def test_eq(self):
        assert _eval_when("project == 'X'", {"project": "X"})
        assert not _eval_when("project == 'X'", {"project": "Y"})

    def test_and(self):
        assert _eval_when("project == 'X' and repo == 'y'", {"project": "X", "repo": "y"})

    def test_startswith(self):
        assert _eval_when("repo.startswith('code')", {"repo": "code-review"})

    def test_invalid(self):
        assert not _eval_when("import os", {})


class TestSampleMatch:

    def test_100_always(self):
        assert _sample_match(100, "any-url")

    def test_0_never(self):
        assert not _sample_match(0, "any-url")

    def test_deterministic(self):
        url = "http://pr/42"
        results = {_sample_match(50, url) for _ in range(100)}
        assert len(results) == 1  # always same

    def test_distribution(self):
        count = sum(1 for i in range(1000) if _sample_match(50, f"http://pr/{i}"))
        log.info("sample=50 distribution: %d/1000", count)
        assert 400 < count < 600


# ── Routing ──────────────────────────────────────────────────────────────────


class TestForwardRouting:

    def _cmd(self, name="review"):
        return CommandRequest(name=name)

    def test_legacy_forwards(self):
        cfg = load_config(EXAMPLE_CONFIG)
        pr = PRMeta("LEGACY", "repo", 1, "http://pr/1", "", "", "", "")
        result = route_event([self._cmd()], pr, cfg)
        assert isinstance(result, ForwardDecision)
        assert result.agent_name == "pra"
        assert result.route_name == "legacy-forward"
        log.info("legacy → forward:%s", result.agent_name)

    def test_platform_sample_forward(self):
        """Some PLATFORM PRs forward, others go to command routing."""
        cfg = load_config(EXAMPLE_CONFIG)
        forwards = 0
        commands = 0
        for i in range(200):
            pr = PRMeta("PLATFORM", "repo", i, f"http://pr/{i}", "", "", "", "")
            result = route_event([self._cmd()], pr, cfg)
            if isinstance(result, ForwardDecision):
                forwards += 1
            elif isinstance(result, list):
                commands += 1
        log.info("PLATFORM A/B: %d forward, %d commands", forwards, commands)
        assert forwards > 50   # ~100 expected
        assert commands > 50   # ~100 expected


class TestCommandRouting:

    def _cmd(self, name="review", args="", comment_id=None):
        return CommandRequest(name=name, args=args, comment_id=comment_id)

    def test_canary_all_commands(self):
        cfg = load_config(EXAMPLE_CONFIG)
        pr = PRMeta("SBLOOM", "code-review-example-orderflow", 1,
                     "http://pr/1", "", "", "", "")
        result = route_event([self._cmd("review")], pr, cfg)
        assert isinstance(result, list)
        assert result[0].agent_name == "dg2"
        assert result[0].route_name == "orderflow-canary"

    def test_sbloom_per_command_override(self):
        cfg = load_config(EXAMPLE_CONFIG)
        pr = PRMeta("SBLOOM", "other-repo", 1, "http://pr/1", "", "", "", "")
        result = route_event([self._cmd("improve")], pr, cfg)
        assert isinstance(result, list)
        assert result[0].agent_name == "pra"
        log.info("sbloom /improve → %s", result[0].agent_name)

    def test_default_fallback(self):
        cfg = load_config(EXAMPLE_CONFIG)
        pr = PRMeta("OTHER", "repo", 1, "http://pr/1", "", "", "", "")
        result = route_event([self._cmd()], pr, cfg)
        assert isinstance(result, list)
        assert result[0].agent_name == "dg1"
        assert result[0].route_name == "default"

    def test_multiple_commands(self):
        cfg = load_config(EXAMPLE_CONFIG)
        pr = PRMeta("SBLOOM", "code-review-example-orderflow", 1,
                     "http://pr/1", "", "", "", "")
        result = route_event([self._cmd("review"), self._cmd("improve")], pr, cfg)
        assert isinstance(result, list)
        assert len(result) == 2
        log.info("multi: %s", [(d.command.name, d.agent_name) for d in result])

    def test_args_preserved(self):
        cfg = load_config(EXAMPLE_CONFIG)
        pr = PRMeta("OTHER", "repo", 1, "http://pr/1", "", "", "", "")
        result = route_event([self._cmd("ask", args="Is this safe?")], pr, cfg)
        assert isinstance(result, list)
        assert result[0].command.args == "Is this safe?"

    def test_comment_id_preserved(self):
        cfg = load_config(EXAMPLE_CONFIG)
        pr = PRMeta("OTHER", "repo", 1, "http://pr/1", "", "", "", "")
        result = route_event([self._cmd("improve", comment_id=150)], pr, cfg)
        assert isinstance(result, list)
        assert result[0].command.comment_id == 150

    def test_no_commands_no_match(self):
        cfg = load_config(EXAMPLE_CONFIG)
        pr = PRMeta("OTHER", "repo", 1, "http://pr/1", "", "", "", "")
        result = route_event([], pr, cfg)
        # No commands → default route has agent but no commands to route
        assert result == []
