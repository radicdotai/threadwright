import re
import shutil
import subprocess
import threading
from pathlib import Path

from threadwright.config import Config

_git_lock = threading.Lock()
_GIT_TIMEOUT_S = 600


def thread_slug(channel: str, thread_ts: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", f"{channel}-{thread_ts}")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=_GIT_TIMEOUT_S)


def prepare(cfg: Config, slug: str) -> Path:
    root = cfg.work_dir / slug
    if (root / "CLAUDE.md").exists():
        return root
    with _git_lock:
        try:
            root.mkdir(parents=True, exist_ok=True)
            for repo in cfg.repos:
                mirror = cfg.repos_dir / repo
                _git(mirror, "fetch", "--quiet", "origin", cfg.base_branch)
                _git(mirror, "worktree", "add", "--quiet", "-B", f"threadwright/{slug}", str(root / repo),
                     f"origin/{cfg.base_branch}")
            if cfg.workspace_files:
                shutil.copytree(cfg.workspace_files, root, dirs_exist_ok=True)
            shutil.copyfile(cfg.workspace_rules, root / "CLAUDE.md")
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            for repo in cfg.repos:
                subprocess.run(["git", "worktree", "prune"], cwd=cfg.repos_dir / repo, capture_output=True)
            raise
    return root
