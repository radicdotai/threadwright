from pathlib import Path

import pytest

from threadwright.config import Config


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    rules = tmp_path / "rules.md"
    rules.write_text("# rules\n")
    return Config(
        project="test-project",
        tasks_subscription="threadwright-tasks-worker",
        updates_topic="threadwright-updates",
        repos_dir=tmp_path / "repos",
        work_dir=tmp_path / "work",
        db_path=tmp_path / "state" / "threadwright.db",
        repos=("svc-a", "svc-b"),
        base_branch="dev",
        workspace_rules=rules,
        workspace_files=None,
        claude_bin="claude",
        claude_model="",
        claude_effort="",
        claude_settings=None,
        mcp_config=None,
        max_concurrent=1,
        task_timeout_s=30,
        monthly_budget_usd=None,
        sessions_bucket="",
    )
