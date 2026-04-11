"""
Tests for webhook router — config, routing, A/B split.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest

from webhook.config import load_config
from webhook.bitbucket import parse_event, extract_commands, PRMeta, WebhookEvent, CommandRequest
from webhook.router import route_commands, _eval_when, _resolve_agent

log = logging.getLogger(__name__)

EXAMPLE_CONFIG = Path(__file__).parent.parent / "config.example.toml"


# ── Config loading ───────────────────────────────────────────────────────────


class TestLoadConfig:

    def test_loads_example(self):
        cfg = load_config(EXAMPLE_CONFIG)
        assert "dg2" in cfg.agents
        assert "dg1" in cfg.agents
        assert "pra" in cfg.agents
        assert len(cfg.routes) == 3

    def test_agents_have_commands(self):
        cfg = load_config(EXAMPLE_CONFIG)
        assert "cli.py" in cfg.agents["dg2"].command
        assert cfg.agents["dg2"].trigger == "cli"
        assert cfg.agents["dg2"].timeout == 600

    def test_events_parsed(self):
        cfg = load_config(EXAMPLE_CONFIG)
        assert cfg.events["pr:opened"] == ["review"]
        assert cfg.events["pr:comment:added"] == "parse"
        assert cfg.events["repo:refs_changed"] == []

    def test_routes_order(self):
        cfg = load_config(EXAMPLE_CONFIG)
        assert cfg.routes[0].name == "orderflow-canary"
        assert cfg.routes[1].name == "sbloom-ab"
        assert cfg.routes[2].name == "default"

    def test_route_ab_split(self):
        cfg = load_config(EXAMPLE_CONFIG)
        sbloom = cfg.routes[1]
        assert isinstance(sbloom.agent, dict)
        assert sbloom.agent["dg2"] == 30
        assert sbloom.agent["dg1"] == 70

    def test_route_per_command_override(self):
        cfg = load_config(EXAMPLE_CONFIG)
        sbloom = cfg.routes[1]
        assert sbloom.commands["improve"] == "pra"


# ── Bitbucket event parsing ─────────────────────────────────────────────────


def _make_bb_event(event_key="pr:opened", project="SBLOOM", repo="my-repo",
                   pr_id=42, comment_text=""):
    data = {
        "eventKey": event_key,
        "pullRequest": {
            "id": pr_id,
            "title": "Test PR",
            "fromRef": {
                "displayId": "feature/test",
                "repository": {
                    "slug": repo,
                    "project": {"key": project},
                },
            },
            "toRef": {
                "displayId": "master",
                "repository": {
                    "slug": repo,
                    "project": {"key": project},
                },
            },
            "author": {"user": {"name": "testuser"}},
        },
    }
    if comment_text:
        data["comment"] = {"text": comment_text}
    return data


class TestParseEvent:

    def test_pr_opened(self):
        ev = parse_event(_make_bb_event("pr:opened"), "https://bb.example.com")
        assert ev.event_key == "pr:opened"
        assert ev.pr.project == "SBLOOM"
        assert ev.pr.repo == "my-repo"
        assert ev.pr.pr_id == 42
        assert "pull-requests/42" in ev.pr.pr_url

    def test_comment_event(self):
        ev = parse_event(
            _make_bb_event("pr:comment:added", comment_text="/review"),
            "https://bb.example.com",
        )
        assert ev.event_key == "pr:comment:added"
        assert ev.comment_text == "/review"

    def test_no_pr(self):
        ev = parse_event({"eventKey": "something"}, "")
        assert ev is None

    def test_invalid_pr_id(self):
        data = _make_bb_event()
        data["pullRequest"]["id"] = -1
        ev = parse_event(data, "")
        assert ev is None


class TestExtractCommands:

    def test_pr_opened_auto(self):
        cfg = load_config(EXAMPLE_CONFIG)
        ev = WebhookEvent(
            event_key="pr:opened",
            pr=PRMeta("SBLOOM", "test", 1, "", "", "", "", ""),
        )
        cmds = extract_commands(ev, cfg.events)
        assert len(cmds) == 1
        assert cmds[0].name == "review"

    def test_comment_parse_review(self):
        cfg = load_config(EXAMPLE_CONFIG)
        ev = WebhookEvent(
            event_key="pr:comment:added",
            pr=PRMeta("SBLOOM", "test", 1, "", "", "", "", ""),
            comment_text="/review",
        )
        cmds = extract_commands(ev, cfg.events)
        assert len(cmds) == 1
        assert cmds[0].name == "review"
        assert cmds[0].args == ""

    def test_comment_parse_improve(self):
        cfg = load_config(EXAMPLE_CONFIG)
        ev = WebhookEvent(
            event_key="pr:comment:added",
            pr=PRMeta("SBLOOM", "test", 1, "", "", "", "", ""),
            comment_text="/improve",
        )
        cmds = extract_commands(ev, cfg.events)
        assert cmds[0].name == "improve"

    def test_comment_with_mention(self):
        cfg = load_config(EXAMPLE_CONFIG)
        ev = WebhookEvent(
            event_key="pr:comment:added",
            pr=PRMeta("SBLOOM", "test", 1, "", "", "", "", ""),
            comment_text="@diffgraph /review",
        )
        cmds = extract_commands(ev, cfg.events)
        assert len(cmds) == 1
        assert cmds[0].name == "review"

    def test_comment_ask_with_question(self):
        cfg = load_config(EXAMPLE_CONFIG)
        ev = WebhookEvent(
            event_key="pr:comment:added",
            pr=PRMeta("SBLOOM", "test", 1, "", "", "", "", ""),
            comment_text="@diffgraph /ask What about null safety in this method?",
        )
        cmds = extract_commands(ev, cfg.events)
        assert len(cmds) == 1
        assert cmds[0].name == "ask"
        assert cmds[0].args == "What about null safety in this method?"
        log.info("ask command: name=%s args=%s", cmds[0].name, cmds[0].args)

    def test_comment_improve_in_thread(self):
        """Threaded /improve gets parent comment ID."""
        cfg = load_config(EXAMPLE_CONFIG)
        ev = WebhookEvent(
            event_key="pr:comment:added",
            pr=PRMeta("SBLOOM", "test", 1, "", "", "", "", ""),
            comment_text="/improve",
            comment_id=200,
            parent_comment_id=150,
        )
        cmds = extract_commands(ev, cfg.events)
        assert len(cmds) == 1
        assert cmds[0].name == "improve"
        assert cmds[0].comment_id == 150
        log.info("threaded improve: comment_id=%s", cmds[0].comment_id)

    def test_comment_no_command(self):
        cfg = load_config(EXAMPLE_CONFIG)
        ev = WebhookEvent(
            event_key="pr:comment:added",
            pr=PRMeta("SBLOOM", "test", 1, "", "", "", "", ""),
            comment_text="just a regular comment",
        )
        cmds = extract_commands(ev, cfg.events)
        assert cmds == []

    def test_push_no_commands(self):
        cfg = load_config(EXAMPLE_CONFIG)
        ev = WebhookEvent(
            event_key="repo:refs_changed",
            pr=PRMeta("SBLOOM", "test", 1, "", "", "", "", ""),
        )
        cmds = extract_commands(ev, cfg.events)
        assert cmds == []

    def test_unknown_event(self):
        cfg = load_config(EXAMPLE_CONFIG)
        ev = WebhookEvent(
            event_key="unknown:event",
            pr=PRMeta("SBLOOM", "test", 1, "", "", "", "", ""),
        )
        cmds = extract_commands(ev, cfg.events)
        assert cmds == []


# ── Routing ──────────────────────────────────────────────────────────────────


class TestEvalWhen:

    def test_true(self):
        assert _eval_when("true", {})
        assert _eval_when("True", {})
        assert _eval_when("*", {})

    def test_simple_eq(self):
        assert _eval_when("project == 'SBLOOM'", {"project": "SBLOOM"})
        assert not _eval_when("project == 'SBLOOM'", {"project": "OTHER"})

    def test_and(self):
        ctx = {"project": "SBLOOM", "repo": "my-repo"}
        assert _eval_when("project == 'SBLOOM' and repo == 'my-repo'", ctx)
        assert not _eval_when("project == 'SBLOOM' and repo == 'other'", ctx)

    def test_startswith(self):
        ctx = {"repo": "code-review-example-orderflow"}
        assert _eval_when("repo.startswith('code-review')", ctx)

    def test_invalid_expr(self):
        assert not _eval_when("import os", {})


class TestResolveAgent:

    def test_exact(self):
        assert _resolve_agent("dg2", "http://pr/1") == "dg2"

    def test_ab_deterministic(self):
        """Same pr_url always gives same agent."""
        url = "http://bb.example.com/projects/X/repos/Y/pull-requests/42"
        results = {_resolve_agent({"dg2": 30, "dg1": 70}, url) for _ in range(100)}
        assert len(results) == 1  # always same

    def test_ab_distribution(self):
        """Over many URLs, distribution roughly matches weights."""
        spec = {"dg2": 30, "dg1": 70}
        counts = {"dg2": 0, "dg1": 0}
        for i in range(1000):
            agent = _resolve_agent(spec, f"http://pr/{i}")
            counts[agent] += 1
        log.info("A/B distribution: %s", counts)
        # Allow ±10% tolerance
        assert 200 < counts["dg2"] < 400
        assert 600 < counts["dg1"] < 800


class TestRouteCommands:

    def _cmd(self, name, args="", comment_id=None):
        return CommandRequest(name=name, args=args, comment_id=comment_id)

    def test_specific_repo_matches_first(self):
        cfg = load_config(EXAMPLE_CONFIG)
        pr = PRMeta("SBLOOM", "code-review-example-orderflow", 1,
                     "http://pr/1", "user", "feature", "master", "Test")
        decisions = route_commands([self._cmd("review")], pr, cfg)
        assert len(decisions) == 1
        assert decisions[0].agent_name == "dg2"
        assert decisions[0].route_name == "orderflow-canary"
        log.info("specific repo: %s → %s", decisions[0].route_name, decisions[0].agent_name)

    def test_sbloom_ab(self):
        cfg = load_config(EXAMPLE_CONFIG)
        pr = PRMeta("SBLOOM", "other-repo", 1,
                     "http://pr/1", "user", "feature", "master", "Test")
        decisions = route_commands([self._cmd("review")], pr, cfg)
        assert len(decisions) == 1
        assert decisions[0].agent_name in ("dg2", "dg1")
        assert decisions[0].route_name == "sbloom-ab"
        log.info("sbloom A/B: %s → %s", decisions[0].route_name, decisions[0].agent_name)

    def test_sbloom_improve_override(self):
        cfg = load_config(EXAMPLE_CONFIG)
        pr = PRMeta("SBLOOM", "other-repo", 1,
                     "http://pr/1", "user", "feature", "master", "Test")
        decisions = route_commands([self._cmd("improve")], pr, cfg)
        assert len(decisions) == 1
        assert decisions[0].agent_name == "pra"
        log.info("sbloom improve: %s → %s", decisions[0].route_name, decisions[0].agent_name)

    def test_default_fallback(self):
        cfg = load_config(EXAMPLE_CONFIG)
        pr = PRMeta("OTHER", "some-repo", 1,
                     "http://pr/1", "user", "feature", "master", "Test")
        decisions = route_commands([self._cmd("review")], pr, cfg)
        assert len(decisions) == 1
        assert decisions[0].agent_name == "dg1"
        assert decisions[0].route_name == "default"

    def test_multiple_commands(self):
        cfg = load_config(EXAMPLE_CONFIG)
        pr = PRMeta("SBLOOM", "code-review-example-orderflow", 1,
                     "http://pr/1", "user", "feature", "master", "Test")
        decisions = route_commands([self._cmd("review"), self._cmd("improve")], pr, cfg)
        log.info("multi-command: %s", [(d.command.name, d.agent_name) for d in decisions])
        assert len(decisions) == 2
        assert all(d.agent_name == "dg2" for d in decisions)

    def test_command_args_preserved(self):
        """Command args (question text) survive routing."""
        cfg = load_config(EXAMPLE_CONFIG)
        pr = PRMeta("OTHER", "repo", 1,
                     "http://pr/1", "user", "feature", "master", "Test")
        decisions = route_commands([self._cmd("ask", args="Is this null-safe?")], pr, cfg)
        assert len(decisions) == 1
        assert decisions[0].command.args == "Is this null-safe?"
        log.info("args preserved: %s", decisions[0].command.args)

    def test_comment_id_preserved(self):
        """Parent comment ID survives routing."""
        cfg = load_config(EXAMPLE_CONFIG)
        pr = PRMeta("OTHER", "repo", 1,
                     "http://pr/1", "user", "feature", "master", "Test")
        decisions = route_commands([self._cmd("improve", comment_id=150)], pr, cfg)
        assert len(decisions) == 1
        assert decisions[0].command.comment_id == 150
        log.info("comment_id preserved: %s", decisions[0].command.comment_id)
