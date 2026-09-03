# Optyra — GSoC Issue Monitor

A production-ready background worker that watches GitHub organizations for **freshly
created, still-claimable issues** worth attempting as (GSoC-style) contributions, scores
them with a transparent rule engine, enriches the best candidates with an AI **2-line
summary + "worth attempting" verdict**, and delivers ranked **Telegram** alerts —
instant for the hottest, digest for the rest.

Built from the execution report in `01prd.md` and the final decisions in `02prd.md`
(those two documents are the source of truth for every design choice below).

```
ONE BOX · Docker Compose · outbound HTTPS only
┌────────────────────────────────────────────────────────────────┐
│ worker (Python 3.12, asyncio, httpx)                           │
│  ├─ Job A (nightly):  search/repositories per org              │
│  │      → monitored repo whitelist → repos table               │
│  │      → cached org-level GSoC relevance score                │
│  ├─ Job B (tiered):   search/issues per org                    │
│  │      tier 1 orgs every 3 min · tier 2 every 12 min          │
│  │      (token bucket ≤ 20 search/min, per-org watermarks,     │
│  │       60–120 s overlap, DB dedupe)                          │
│  │      → hard filters → whitelist → rule score (0–100)        │
│  ├─ deep-check (candidates only): REST issue + timeline        │
│  │      → linked open PR, assignee confirm, triage timestamp   │
│  ├─ enrich (candidates only): Gemini Flash, strict JSON,       │
│  │      fail-open, one cached call per issue                   │
│  ├─ notify: Telegram instant ≥ 85 · digest 70–84 every 20 min  │
│  ├─ Job C (hourly): state refresh (assigned/closed transitions)│
│  └─ maintenance: prune > 90 days · Healthchecks.io ping        │
├─ postgres:16 (local volume, ~30–60 MB/month)                   │
└─ /healthz (loopback port 8080)                                 │
```

**No webhooks** (impossible on repos you don't admin), **no GraphQL**, no queues, no
Redis, no inbound ports. One process, one database, five asyncio jobs.

## Features

- **Tiered search polling** — one `search/issues` query covers *every* repo in an org
  (10,000 repos cost the same as 50). Tier 1 orgs poll every 3 min (~2–5 min detection
  latency), the long tail every 12 min (~10–12 min). Sustained search usage stays ≤ 20
  req/min against the 30/min authenticated ceiling.
- **Watermark + overlap + dedupe** — per-org watermarks with a configurable overlap make
  detection at-least-once while the `issues(repo_full_name, number)` primary key and the
  `notifications(issue_key, channel)` primary key make notifications exactly-once.
  Restarts resume from the watermark; if one is older than 72 h the poller catch-up
  scans in time-sliced windows and then resets — self-healing, zero manual steps.
- **Hard filters** (never notified): closed, assigned, `question/support/invalid/
  duplicate/wontfix/security` labels, repo outside the whitelist, age > 72 h, bot
  authors, bodies < 50 chars.
- **Rule score 0–100** (all weights in `config/config.yaml`): recency 25 · unassigned 20
  · no linked open PR 15 · labels 15 · repo pushed ≤ 30 days 10 · stars 5 ·
  reproducible body 5. Deterministic, debuggable, tunable — no ML for the gating.
- **GSoC relevance score** (org-level, recomputed nightly, cached on issues): GSoC
  participations in the last 6 years 40 · >10 k-star actively-maintained repo 20 ·
  newcomer-label ratio 20 · median triage within 48 h proxy 20.
- **Deep-check before you're bothered**: only threshold-passing candidates cost REST
  calls — a fresh issue fetch (did someone get assigned in the last seconds?) plus the
  issue timeline (linked open PR, first-maintainer-comment timestamp).
- **AI layer that cannot hurt you**: Gemini Flash, called only for candidates, strict
  JSON with schema, 20 s timeout, retries, one cached call per issue, and **fail-open**
  — if the model is down you still get the alert, just without the summary. Your
  personal no-go criteria (huge builds, GPU/cluster, proprietary SDKs/hardware,
  Windows-only, language prefs) live in `config/ai_criteria.yaml`, versioned, no code.
- **Telegram with taste**: instant messages for score ≥ 85, everything else queued into
  a ranked digest every 20 min. chat_id allowlist (we never read incoming messages),
  inline "Open Issue" button, 4096-char chunking, 429-aware.
- **Ops**: `/healthz` with live counters, Healthchecks.io dead-man ping, secret-scrubbing
  structured (or JSON) logs, per-job error isolation, Postgres advisory lock so two
  workers can't double-run, 90-day pruning.

## Quickstart (local dev)

```bash
# 1. Postgres
docker run -d --name optyra-pg -e POSTGRES_USER=optyra -e POSTGRES_PASSWORD=optyra \
  -e POSTGRES_DB=optyra -p 5432:5432 postgres:16-alpine

# 2. Environment (the contract — see .env.example)
export GH_TOKEN=github_pat_...            # fine-grained PAT, Public repos read-only
export TELEGRAM_BOT_TOKEN=123:ABC         # from @BotFather
export TELEGRAM_CHAT_ID=123456789         # your numeric chat id
export AI_API_KEY=...                     # Google AI Studio (optional)
export DATABASE_URL=postgresql+asyncpg://optyra:optyra@localhost:5432/optyra

# 3. Run
pip install -e ".[dev]"
python -m optyra                 # worker: schema bootstrap + 5 jobs + /healthz :8080
python scripts/spike.py          # report §20 Day-1 spike: discovery + 24 h search + score, no DB
```

Config lives in three YAML files under `config/`:

| File | Owns |
|---|---|
| `config/config.yaml` | intervals, thresholds, scoring weights, label maps, AI/ops knobs |
| `config/orgs.yaml` | the org list: `login`, `tier` (1 = priority), `gsoc_years` seed |
| `config/ai_criteria.yaml` | LLM task, allowed reason codes, your no-go criteria |

## Tests

```bash
pytest                       # SQLite-backed: 76 tests, no services needed
TEST_DATABASE_URL=postgresql+asyncpg://optyra:optyra@localhost:5432/optyra_test pytest
                             # same suite against real Postgres (CI runs both)
ruff check src tests && ruff format --check src tests
```

The suite covers filters, scoring, GSoC mapping, the token bucket, GitHub client
behavior (Link pagination, early stop by watermark, 5xx/Retry-After handling), the AI
enricher (strict parse, repair retry, fail-open), Telegram formatting/chunking/429, the
DAL (dedupe PKs, poll_state, prune), and an **end-to-end pipeline test** with fake
GitHub/AI/Telegram: sync → poll → filter → score → deep-check → enrich → instant +
digest delivery → dedupe on the second sweep → state refresh.

## Deployment (DevOps friend's side)

Everything lives in [`deploy/`](deploy/) — `docker-compose.yml` (postgres + worker),
`.env.example` (**the contract between the two roles**), `runbook.md` (first deploy,
updates, rollback, token rotation, backups, troubleshooting), and `backup.sh` (nightly
`pg_dump | gzip` + optional rclone → Backblaze B2).

CI/CD: every PR runs `ruff` + `pytest` (Postgres service) + `docker build`; pushing a
tag `v*` builds a multi-arch (amd64 + arm64) image to GHCR; an optional self-hosted
runner workflow deploys on tags with a healthz gate.

## Deployment on Render (free tier)

[`render.yaml`](render.yaml) is a Blueprint for a one-click deploy: New →
Blueprint → connect your fork. Fill the secrets (`GH_TOKEN`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DATABASE_URL` — a managed Postgres
such as Neon/Aiven free tier; paste the URL as-is, incompatible query params
like `sslmode` are stripped automatically), then deploy. Afterwards set the
service's **Health Check Path to `/healthz`** (Settings → Health Checks) — the
prober doubles as the free-tier keep-alive.

Three behaviors make overlapping Render deploys safe (they used to kill every
deploy): the worker **waits up to `ops.worker_lock_timeout_seconds`** (default
180 s) for the previous instance's advisory lock instead of exiting, the old
instance **releases the lock promptly on SIGTERM**, and the health endpoint
follows **`$PORT`** when Render sets it. No Docker Command override needed —
leave that field empty.

## Environment contract

| Variable | Required | Meaning |
|---|---|---|
| `GH_TOKEN` | ✅ | fine-grained PAT, **Public repositories (read-only)**, no extra permissions, rotate every 90 days |
| `TELEGRAM_BOT_TOKEN` | for alerts | from @BotFather |
| `TELEGRAM_CHAT_ID` | for alerts | your chat id (comma-separated = allowlist) |
| `DATABASE_URL` | ✅ | `postgresql+asyncpg://user:pass@host:5432/db` |
| `AI_API_KEY` | optional | Google AI Studio key; without it the worker runs without AI summaries (fail-open) |
| `AI_MODEL` | optional | model override (default from config.yaml) |
| `CONFIG_PATH` | optional | directory containing the three YAML files (default `config`) |
| `LOG_LEVEL` / `LOG_JSON` | optional | `INFO` default; `LOG_JSON=1` for JSON lines |
| `HEALTHCHECK_URL` | optional | Healthchecks.io ping URL (dead-man switch) |

## Design notes & known limitations (deliberate, per the report)

- Notification PK insert-then-send means a crash between Telegram's 200 and the
  `sent_at` update can produce one rare duplicate — documented in report §16 and
  accepted.
- Search-index lag (seconds to ~2 min) sits on top of the poll interval; the 120 s
  overlap absorbs most of it.
- The triage component of the GSoC score learns from deep-checked candidates only, so
  it starts at 0 and warms up over days.
- Issues in org repos that don't pass the nightly `stars:>=2000 archived:false`
  discovery filter are not whitelisted (by design — that's the report's quality gate).
- Out of scope for v0.1 (report §12/§20): dashboard, comment-contention mining, Discord/
  email channels, multi-user, GitHub App, GraphQL, queues.
