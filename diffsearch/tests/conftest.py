"""
Test fixtures: build real git repos from fixture directories.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

FIXTURES = Path(__file__).parent / "fixtures"


def repo_from_fixture(name: str) -> tuple[str, str, str]:
    """
    Build a real git repo from fixture dirs.

    Returns (repo_path, base_sha, source_sha).
    """
    fixture = FIXTURES / name
    manifest = yaml.safe_load((fixture / "manifest.yaml").read_text())

    repo = tempfile.mkdtemp(prefix=f"test-{name}-")
    run = lambda *args: subprocess.run(
        ["git"] + list(args), cwd=repo, capture_output=True, text=True, check=True,
    )

    run("init")
    run("config", "user.email", "test@test.com")
    run("config", "user.name", "test")

    shas = {}
    for commit in manifest["commits"]:
        ref = commit["ref"]
        src = fixture / ref

        # Clear working tree (except .git)
        for p in Path(repo).iterdir():
            if p.name != ".git":
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()

        # Copy fixture files
        if src.exists():
            shutil.copytree(src, repo, dirs_exist_ok=True)

        run("add", "-A")
        run("commit", "-m", commit["message"], "--allow-empty")
        shas[ref] = run("rev-parse", "HEAD").stdout.strip()

    return repo, shas["base"], shas["source"]


@pytest.fixture
def rename_field_repo():
    repo, base, source = repo_from_fixture("rename_field")
    yield repo, base, source
    shutil.rmtree(repo, ignore_errors=True)


@pytest.fixture
def split_method_repo():
    repo, base, source = repo_from_fixture("split_method")
    yield repo, base, source
    shutil.rmtree(repo, ignore_errors=True)


@pytest.fixture
def new_file_repo():
    repo, base, source = repo_from_fixture("new_file")
    yield repo, base, source
    shutil.rmtree(repo, ignore_errors=True)


@pytest.fixture
def deleted_file_repo():
    repo, base, source = repo_from_fixture("deleted_file")
    yield repo, base, source
    shutil.rmtree(repo, ignore_errors=True)


@pytest.fixture
def renamed_file_repo():
    repo, base, source = repo_from_fixture("renamed_file")
    yield repo, base, source
    shutil.rmtree(repo, ignore_errors=True)


@pytest.fixture(params=["rename_field", "split_method", "new_file", "deleted_file", "renamed_file"])
def any_repo(request):
    repo, base, source = repo_from_fixture(request.param)
    yield request.param, repo, base, source
    shutil.rmtree(repo, ignore_errors=True)
