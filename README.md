# Threadwright

A Slack bot that runs Claude Code on your own GCP VM. Tag the bot (`@threadwright` by default; the handle is your Slack app's name) in Slack with a bug or a question. A headless Claude Code session picks it up, gathers context from your logs, databases and tools (read-only), works in its own copy of your repos, and replies in the thread with an answer or pull requests. A human merges.

## How it works

```
Slack ──Socket Mode──▶ listener (e2-micro, always on, holds Slack tokens)
                         │  ▲
          threadwright-tasks  │  │  threadwright-updates (progress, result, heartbeat)
                         ▼  │
                       worker (e2-standard-8, runs only while there is work; Docker, Claude, repos)
```

1. The listener checks who asked, reacts 👀, posts "Queued…", downloads the thread's text and files (up to 6 MB), and publishes a task to Pub/Sub.
2. If the worker VM is stopped or suspended, the listener starts it ("starting the worker, 1–3 min").
3. The worker pulls the task and creates a folder per Slack thread with one git worktree per repo on `threadwright/<thread>`, plus your workspace instructions as `CLAUDE.md` and any dev files from `WORKSPACE_FILES`.
4. It runs `claude -p` headless there and publishes progress; the listener edits the status message.
5. The result is posted in the thread. Asking again in the same thread resumes the same Claude session.
6. After `IDLE_STOP_S` (15 min) with nothing running, the listener stops the worker. Queued tasks wait in Pub/Sub, so nothing is lost while it sleeps or restarts.

| Trigger | Starts or continues |
| --- | --- |
| `@threadwright` in a channel or thread | Starts a task, or continues the thread's session |
| Plain reply in a channel thread | Ignored, so people can talk; tag `@threadwright` again to continue |
| DM to the app (text or file) | Starts a task; replies in that DM thread continue it |
| `@threadwright hold 30m` (or `2h`, max 4 h) | Wakes the worker and keeps it awake until then, for hands-on admin work. No Claude run. `@threadwright hold off` releases it |

Only full members of your Slack workspace can use it: guests, Slack Connect users and bots are refused. `ALLOWED_USERS` and `ALLOWED_CHANNELS` narrow it further.

## Configuration files

Everything specific to your setup lives in files you create next to the examples in `deploy/`. They are git-ignored, so you can keep them in a private repo and copy them over this one before deploying.

| File | Used by | What it holds |
| --- | --- | --- |
| `deploy/site.env` (from `site.env.example`) | `gcp-setup.sh`, `bootstrap.sh` | GCP project, repos to clone, git identity, database proxies, extra packages |
| `deploy/listener.env` (from `listener.env.example`) | listener service | Slack tokens, worker VM, limits |
| `deploy/worker.env` (from `worker.env.example`) | worker service, and every Claude run | Claude login, repos, connector credentials |
| `deploy/workspace-CLAUDE.md` (from `workspace-CLAUDE.example.md`) | Claude | How to work in your repos and how to reach each connector |
| `deploy/mcp.json` (from `mcp.example.json`) | Claude | MCP servers |
| `deploy/actions.json` (from `actions.example.json`) | listener | Approved-actions targets, see below |

`bootstrap.sh` installs `deploy/<role>.env` (or the example) to `/etc/threadwright-<role>.env` the first time only; after that, edit the file on the VM. Secrets belong only in `/etc`, never in git.

## Setup

1. **GCP.** Fill `deploy/site.env`, then `./deploy/gcp-setup.sh` creates the two service accounts, topics, subscriptions, IAM bindings and both VMs. Run it with `DRY_RUN=1` first to see every command.
2. **Slack app.** api.slack.com/apps → Create from manifest → `deploy/slack-manifest.yaml`. Install it (`SLACK_BOT_TOKEN`), and create an app-level token with `connections:write` (`SLACK_APP_TOKEN`). Invite `@threadwright` to its channels.
3. **Listener VM.** Copy this repo over, `sudo deploy/bootstrap.sh listener`, fill `/etc/threadwright-listener.env`, `sudo systemctl enable --now threadwright@listener`.
4. **Worker VM.** Copy this repo over, `sudo deploy/bootstrap.sh worker`, then:
    - `sudo -iu threadwright gh auth login` as a GitHub bot account with write (not admin) access, and re-run the bootstrap to clone `REPOS` from `ORG`.
    - `sudo -iu threadwright claude setup-token` and put the token in `CLAUDE_CODE_OAUTH_TOKEN` (or set `ANTHROPIC_API_KEY`).
    - Put dev-only files your stack needs under `/srv/threadwright/dev-files/<repo>/` (for example `api/.env`); they are copied into every workspace.
    - Fill `/etc/threadwright-worker.env`, then `sudo systemctl enable --now threadwright@worker`.

One way to deploy from a laptop:

```bash
mkdir -p /tmp/of && git archive HEAD | tar -x -C /tmp/of && cp -r ../my-threadwright-config/deploy/. /tmp/of/deploy/
gcloud compute scp --recurse /tmp/of threadwright-worker:/tmp/of && gcloud compute ssh threadwright-worker -- sudo bash /tmp/of/deploy/bootstrap.sh worker
```

## Connectors

Claude reaches your systems the same way a developer would: with CLI tools, HTTP APIs and MCP servers. A connector is three things:

1. **A credential** in `/etc/threadwright-worker.env`. Every variable in that file except `SLACK_*` reaches Claude. Use read-only tokens.
2. **A line in your workspace `CLAUDE.md`** telling Claude the variable name and an example call. The example file has Cloud Logging, Postgres, Grafana, Sentry and Jira entries to start from.
3. **Optionally, an MCP server** in `deploy/mcp.json` with `MCP_CONFIG=/opt/threadwright/deploy/mcp.json`.

Built-in helpers for GCP:

- **Logs and Cloud Run.** `READ_PROJECTS` in `site.env` lists the projects where the worker's service account gets `logging.viewer`, `run.viewer` and `monitoring.viewer`.
- **Cloud SQL.** `DB_INSTANCES` (in `PROJECT`) gives the worker's service account IAM sign-in on those instances and creates its database user. Grant read access on each database with `psql -v dbuser=threadwright-worker@<project>.iam -v schemas=public,app -f deploy/db-readonly-grants.sql`. `DB_PROXIES="dev=5433=<project>:<region>:<instance>"` makes `bootstrap.sh worker` run a Cloud SQL proxy per entry on `127.0.0.1:<port>`; point `DEV_DB_URL` at it.

## Session pages

Set `SESSIONS_BUCKET` and every task's Slack status links to **View session**: a page showing the whole run, including the request, Claude's messages and thinking, each command with its full output, file diffs, to-do lists and the final answer with cost. It updates every ~10 s while the task runs.

- Pages are written to `gs://$SESSIONS_BUCKET/sessions/<channel>/<thread>/<message>.html` and opened through `storage.cloud.google.com`, so viewers need a Google login with read access to the bucket.
- Create the bucket yourself: enforce public access prevention, grant `roles/storage.objectViewer` only to your Google Workspace domain (`domain:example.com`), give the worker's service account `roles/storage.objectCreator`, and add a lifecycle rule to delete old pages.
- Secrets are redacted before upload: tokens, keys, passwords, `*_SECRET=`-style lines, Bearer headers and private keys.
- Leave `SESSIONS_BUCKET` empty to turn pages off.

## Approved actions

Threadwright never holds write access. When a fix needs an API call (accept a booking, retry a job), Claude writes the calls to `.threadwright/actions.json`, and the listener posts them in the thread with Approve and Reject buttons.

- Only the Slack users in `approvers` can decide; each proposal is decided once and expires after 24 hours.
- On Approve, the listener signs a 60-second, single-use ES256 JWT with a Cloud KMS key that only the listener's service account may use, and runs the calls in order, stopping at the first failure.
- The worker's proposal is re-checked by the listener (env, method, path, count); every decision and result is kept in the listener's `proposals` table.

Setup: set `ACTION_KEYRING` and `ACTION_BACKEND_SA` in `site.env` and re-run `gcp-setup.sh` to create the `threadwright-assertion` key. Fill `deploy/actions.json`, set `ACTIONS_CONFIG=/opt/threadwright/deploy/actions.json` on the listener and `ACTION_ENVS` on the worker. The Slack app needs interactivity and `users:read.email`.

`actions.json`:

| Key | Meaning |
| --- | --- |
| `approvers` | Slack user ids allowed to approve |
| `signer` | The listener's service account email; sent as `iss` |
| `audience` | Sent as `aud`; your API checks it |
| `envs.<name>.api` | Base URL of that environment's API |
| `envs.<name>.key_version` | Full KMS key version name; sent as `kid` |
| `envs.<name>.users` | Approver email → `[user id, role]` in that environment |

Each request carries `Authorization: Bearer <jwt>` with claims `iss`, `aud`, `iat`, `exp`, `jti`, `sub` and `requestedBy` (the approver's user id), `userRoles` (`[role]`) and `approverEmail`. Your API must verify the signature with the KMS public key, check `iss`, `aud` and `exp`, reject a reused `jti`, and confirm the approver's email and role against its own records, so a call can do nothing the approver could not do themselves.

## Safety model

- **The worker is one trust zone.** Claude has Docker there, which is root, so it can reach everything on that machine: other threads' workspaces, the Claude login, the GitHub bot, read-only database URLs, dev files and the worker's service account (read-only cloud roles, and pull/publish on the two queues). Keep production data and credentials off it.
- **What stays outside it:** the Slack tokens (only on the listener, root-owned env file, non-dumpable process), the approved-actions signing key, and the power to start or stop VMs (only the listener, only on the worker instance).
- **What a misbehaving session could still do with the queues:** post text into threads that are waiting on a reply, or drop queued tasks. It cannot post into any other channel: the listener only edits status messages it created.
- **Cost cap:** the listener stops the worker after `IDLE_STOP_S` idle, and after `MAX_BUSY_S` (2 h) even if it keeps reporting busy.
- **Merges:** branch protection, not prompts, is what stops a merge. `deploy/claude-settings.json` denies merging, force-pushing, pushing to `dev`/`main` and Terraform as a second layer; adjust it to your branch names.
- **Timeouts:** a timeout, or an error while Claude runs, kills Claude and every process it started. Docker containers it started keep running until the next task stops them or the worker stops.

## Cost

| Piece | Approx. per month |
| --- | --- |
| Listener e2-micro + 30 GB standard disk in us-central1 | $0 (always-free tier) |
| Worker e2-standard-8, ~70–90 h of use | ~$18–24 |
| Worker 100 GB balanced disk (billed while stopped) | ~$10 |
| Pub/Sub | ~$0 |

An always-on worker would be ~$196/month. Set `IDLE_ACTION=suspend` to keep Docker warm between tasks (faster starts, small memory-storage charge); test it first, suspend support for E2 is not documented.

## Operating it

- Logs: `journalctl -u threadwright@listener -f` on the listener, `journalctl -u threadwright@worker -f` on the worker.
- Kill switch: `sudo systemctl stop threadwright@listener` (no new tasks), and stop the worker VM.
- Spend: `sqlite3 /srv/threadwright/state/worker.db "select asker, round(sum(cost_usd),2) from runs group by asker"` on the worker.
- Limits: `ALLOWED_USERS`, `ALLOWED_CHANNELS`, `IDLE_STOP_S`, `MAX_BUSY_S` (listener); `MONTHLY_BUDGET_USD`, `TASK_TIMEOUT_S`, `MAX_CONCURRENT` (worker). Keep `MAX_CONCURRENT=1` if every task runs its own copy of a full stack.

## Known limits

- Using one person's Claude subscription for a shared bot may be outside Anthropic's terms for subscription seats. Confirm with Anthropic or use `ANTHROPIC_API_KEY`.
- With a subscription login, `cost_usd` is the API-equivalent figure, not money billed.
- Old thread folders under `/srv/threadwright/work` are not cleaned up yet.
- GCP only: the worker VM, queues, session pages and signing key all use Google Cloud.

## Development

```bash
uv sync
uv run ruff check . && uv run mypy src tests && uv run pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under [MIT](LICENSE).
