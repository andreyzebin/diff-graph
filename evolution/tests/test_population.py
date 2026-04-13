"""
Tests for evolution population manager.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from evolution.models import Branch, Status, EvolutionConfig, Measurement
from evolution.connectors import TracingConnector
from evolution.population import Population


@pytest.fixture
def tmp_state():
    f = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    f.close()
    yield Path(f.name)
    Path(f.name).unlink(missing_ok=True)


@pytest.fixture
def pop(tmp_state):
    return Population(state_file=tmp_state)


class TestPopulation:

    def test_init_main(self, pop):
        b = pop.init_main("diffgraph/prompts", "abc123")
        assert b.id == "main"
        assert b.status == Status.ACTIVE
        assert b.sample_pct == 100.0

    def test_tree(self, pop):
        pop.init_main("diffgraph/prompts", "abc123")
        tree = pop.tree()
        assert "main" in tree
        assert "100%" in tree

    def test_add_branch(self, pop):
        pop.init_main("prompts", "abc")
        child = Branch(
            id="mut-001-budget",
            prompt_ref="prompts/v2",
            prompt_hash="def456",
            parent_id="main",
            axis="budget",
            hypothesis="increase reviewer budget",
            status=Status.BORN,
            generation=1,
        )
        pop.add_branch(child)
        assert pop.get_branch("mut-001-budget") is not None
        assert pop.get_branch("mut-001-budget").parent_id == "main"

    def test_active_branches(self, pop):
        pop.init_main("prompts", "abc")
        assert len(pop.active_branches()) == 1

        child = Branch(id="mut-001", prompt_ref="v2", prompt_hash="def",
                       status=Status.BORN, generation=1)
        pop.add_branch(child)
        assert len(pop.active_branches()) == 1  # BORN not active

        child.status = Status.ACTIVE
        assert len(pop.active_branches()) == 2

    def test_persistence(self, tmp_state):
        pop1 = Population(state_file=tmp_state)
        pop1.init_main("prompts", "abc123")
        pop1.add_branch(Branch(id="mut-001", prompt_ref="v2", prompt_hash="def",
                                status=Status.ACTIVE, generation=1))

        # Reload from file
        pop2 = Population(state_file=tmp_state)
        assert len(pop2.branches) == 2
        assert pop2.get_branch("main").prompt_hash == "abc123"
        assert pop2.get_branch("mut-001").status == Status.ACTIVE

    def test_status_dict(self, pop):
        pop.init_main("prompts", "abc")
        s = pop.status()
        assert "branches" in s
        assert "config" in s
        assert len(s["branches"]) == 1

    def test_tree_with_children(self, pop):
        pop.init_main("prompts", "abc")
        pop.add_branch(Branch(id="mut-001", prompt_ref="v2", prompt_hash="def",
                               parent_id="main", status=Status.ACTIVE, generation=1))
        pop.add_branch(Branch(id="mut-002", prompt_ref="v3", prompt_hash="ghi",
                               parent_id="main", status=Status.EXTINCT, generation=1))
        tree = pop.tree()
        assert "main" in tree
        assert "mut-001" in tree
        assert "mut-002" in tree


class TestFitness:

    def test_compute_fitness(self, pop):
        m = Measurement(branch_id="test", timestamp="now")
        m.benchmark_score = 0.8
        m.acceptance_rate = 0.7
        m.tokens_per_finding = 5000
        m.feedback_rate = 0.4
        f = pop._compute_fitness(m)
        assert f > 0
        # 0.35*0.8 + 0.35*0.7 + 0.2*(1-5000/10000) + 0.1*0.4
        # = 0.28 + 0.245 + 0.1 + 0.04 = 0.665
        assert abs(f - 0.665) < 0.01

    def test_fitness_no_data(self, pop):
        m = Measurement(branch_id="test", timestamp="now")
        f = pop._compute_fitness(m)
        assert f == 0.0
