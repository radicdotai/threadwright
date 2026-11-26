import dataclasses
import subprocess
from pathlib import Path

import pytest

from threadwright import workspace
from threadwright.config import Config


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_mirror(cfg: Config, name: str, tmp_path: Path) -> None:
    origin = tmp_path / "origins" / name
    origin.mkdir(parents=True)
    git(origin, "init", "-q", "-b", "dev")
    git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    cfg.repos_dir.mkdir(parents=True, exist_ok=True)
    git(cfg.repos_dir, "clone", "-q", str(origin), name)


def test_thread_slug_is_path_safe() -> None:
    assert workspace.thread_slug("C123", "1700000000.123456") == "C123-1700000000-123456"


def test_prepare_creates_one_worktree_per_repo(cfg: Config, tmp_path: Path) -> None:
    for name in cfg.repos:
        make_mirror(cfg, name, tmp_path)
    root = workspace.prepare(cfg, "C1-1")
    assert (root / "CLAUDE.md").read_text() == "# rules\n"
    for name in cfg.repos:
        assert git(root / name, "branch", "--show-current") == "threadwright/C1-1"
    assert workspace.prepare(cfg, "C1-1") == root


def test_prepare_cleans_up_when_a_repo_is_missing(cfg: Config, tmp_path: Path) -> None:
    make_mirror(cfg, "svc-a", tmp_path)
    broken = dataclasses.replace(cfg, repos=("svc-a", "svc-missing"))
    with pytest.raises((FileNotFoundError, subprocess.CalledProcessError)):
        workspace.prepare(broken, "C1-2")
    assert not (cfg.work_dir / "C1-2").exists()
    assert "C1-2" not in git(cfg.repos_dir / "svc-a", "worktree", "list")


def test_prepare_copies_dev_files_into_each_repo(cfg: Config, tmp_path: Path) -> None:
    for name in cfg.repos:
        make_mirror(cfg, name, tmp_path)
    extra = tmp_path / "extra" / "svc-a"
    extra.mkdir(parents=True)
    (extra / ".env").write_text("S2S_PRIVATE_KEY=dev\n")
    root = workspace.prepare(dataclasses.replace(cfg, workspace_files=tmp_path / "extra"), "C1-3")
    assert (root / "svc-a" / ".env").read_text() == "S2S_PRIVATE_KEY=dev\n"
    assert git(root / "svc-a", "status", "--porcelain") == "?? .env"
