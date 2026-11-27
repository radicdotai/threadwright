import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    project: str
    tasks_subscription: str
    updates_topic: str
    repos_dir: Path
    work_dir: Path
    db_path: Path
    repos: tuple[str, ...]
    base_branch: str
    workspace_rules: Path
    workspace_files: Path | None
    claude_bin: str
    claude_model: str
    claude_effort: str
    claude_settings: Path | None
    mcp_config: Path | None
    max_concurrent: int
    task_timeout_s: int
    monthly_budget_usd: float | None
    sessions_bucket: str
    action_envs: tuple[str, ...] = ()
    repo_branches: dict[str, str] = field(default_factory=dict)
    github_app_id: str = ""
    github_installation_id: str = ""
    github_app_key: Path | None = None
    git_name: str = ""
    git_email: str = ""
    memory_dir: Path | None = None
    egress_proxy: str = ""
    triage_model: str = "haiku"


@dataclass(frozen=True)
class ListenerConfig:
    slack_bot_token: str
    slack_app_token: str
    project: str
    zone: str
    worker_instance: str
    tasks_topic: str
    updates_subscription: str
    db_path: Path
    idle_stop_s: int
    idle_action: str
    max_busy_s: int
    allowed_channels: frozenset[str]
    allowed_users: frozenset[str]
    actions_config: Path | None = None
    watch_channels: frozenset[str] = frozenset()
    alert_channels: frozenset[str] = frozenset()
    alert_bots: frozenset[str] = frozenset()
    alert_skip: str = r"(?i)\bresolved\b"
    sweep_channel: str = ""
    sweep_weekday: int = 0
    sweep_time_utc: str = "04:30"
    sweep_prompt: Path | None = None
    github_subscription: str = ""
    ci_attempts: int = 3
    review_attempts: int = 5


def _required(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required setting {name}")
    return value


def _get(env: dict[str, str], name: str, default: str) -> str:
    return env.get(name, "").strip() or default


def _optional_path(env: dict[str, str], name: str) -> Path | None:
    value = env.get(name, "").strip()
    return Path(value) if value else None


def _csv(env: dict[str, str], name: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in env.get(name, "").split(",") if part.strip())


def load(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    entries = [entry.partition(":") for entry in _csv(env, "REPOS")]
    if not entries:
        raise SystemExit("Missing required setting REPOS")
    repos = tuple(name.strip() for name, _, _ in entries)
    budget = env.get("MONTHLY_BUDGET_USD", "").strip()
    return Config(
        project=_required(env, "GCP_PROJECT"),
        tasks_subscription=_get(env, "TASKS_SUBSCRIPTION", "threadwright-tasks-worker"),
        updates_topic=_get(env, "UPDATES_TOPIC", "threadwright-updates"),
        repos_dir=Path(_required(env, "REPOS_DIR")),
        work_dir=Path(_required(env, "WORK_DIR")),
        db_path=Path(_required(env, "DB_PATH")),
        repos=repos,
        base_branch=_get(env, "BASE_BRANCH", "dev"),
        workspace_rules=Path(_required(env, "WORKSPACE_RULES")),
        workspace_files=_optional_path(env, "WORKSPACE_FILES"),
        claude_bin=_get(env, "CLAUDE_BIN", "claude"),
        claude_model=_get(env, "CLAUDE_MODEL", "claude-opus-5-5"),
        claude_effort=_get(env, "CLAUDE_EFFORT", "medium"),
        claude_settings=_optional_path(env, "CLAUDE_SETTINGS"),
        mcp_config=_optional_path(env, "MCP_CONFIG"),
        max_concurrent=int(_get(env, "MAX_CONCURRENT", "1")),
        task_timeout_s=int(_get(env, "TASK_TIMEOUT_S", "2700")),
        monthly_budget_usd=float(budget) if budget else None,
        sessions_bucket=_get(env, "SESSIONS_BUCKET", ""),
        action_envs=_csv(env, "ACTION_ENVS"),
        repo_branches={name.strip(): branch.strip() for name, _, branch in entries if branch.strip()},
        github_app_id=_get(env, "GITHUB_APP_ID", ""),
        github_installation_id=_get(env, "GITHUB_INSTALLATION_ID", ""),
        github_app_key=_optional_path(env, "GITHUB_APP_KEY"),
        git_name=_get(env, "GIT_NAME", ""),
        git_email=_get(env, "GIT_EMAIL", ""),
        memory_dir=_optional_path(env, "MEMORY_DIR"),
        egress_proxy=_get(env, "EGRESS_PROXY", ""),
        triage_model=_get(env, "TRIAGE_MODEL", "haiku"),
    )


def load_listener(env: dict[str, str] | None = None) -> ListenerConfig:
    env = dict(os.environ) if env is None else env
    idle_action = _get(env, "IDLE_ACTION", "stop")
    if idle_action not in ("stop", "suspend"):
        raise SystemExit("IDLE_ACTION must be stop or suspend")
    return ListenerConfig(
        slack_bot_token=_required(env, "SLACK_BOT_TOKEN"),
        slack_app_token=_required(env, "SLACK_APP_TOKEN"),
        project=_required(env, "GCP_PROJECT"),
        zone=_required(env, "WORKER_ZONE"),
        worker_instance=_get(env, "WORKER_INSTANCE", "threadwright-worker"),
        tasks_topic=_get(env, "TASKS_TOPIC", "threadwright-tasks"),
        updates_subscription=_get(env, "UPDATES_SUBSCRIPTION", "threadwright-updates-listener"),
        db_path=Path(_required(env, "DB_PATH")),
        idle_stop_s=int(_get(env, "IDLE_STOP_S", "900")),
        idle_action=idle_action,
        max_busy_s=int(_get(env, "MAX_BUSY_S", "7200")),
        allowed_channels=frozenset(_csv(env, "ALLOWED_CHANNELS")),
        allowed_users=frozenset(_csv(env, "ALLOWED_USERS")),
        actions_config=_optional_path(env, "ACTIONS_CONFIG"),
        watch_channels=frozenset(_csv(env, "WATCH_CHANNELS")),
        alert_channels=frozenset(_csv(env, "ALERT_CHANNELS")),
        alert_bots=frozenset(_csv(env, "ALERT_BOTS")),
        alert_skip=_get(env, "ALERT_SKIP", r"(?i)\bresolved\b"),
        sweep_channel=_get(env, "SWEEP_CHANNEL", ""),
        sweep_weekday=int(_get(env, "SWEEP_WEEKDAY", "0")),
        sweep_time_utc=_get(env, "SWEEP_TIME_UTC", "04:30"),
        sweep_prompt=_optional_path(env, "SWEEP_PROMPT"),
        github_subscription=_get(env, "GITHUB_SUBSCRIPTION", ""),
        ci_attempts=int(_get(env, "CI_ATTEMPTS", "3")),
        review_attempts=int(_get(env, "REVIEW_ATTEMPTS", "5")),
    )
