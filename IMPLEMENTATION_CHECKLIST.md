# Optyra — Implementation Checklist

Source of truth: `01prd.md` (execution & feasibility report) + `02prd.md` (doubt resolution / final decisions).
This file is the concrete engineering translation of those reports. Every item links back to the report section it implements.

**Stack (locked by report):** Python 3.12 · httpx (async) · SQLAlchemy 2.0 + asyncpg · PostgreSQL 16 (Docker) ·
plain asyncio scheduler (no Celery/queues) · Telegram Bot API · Gemini Flash (AI Studio key) · Docker Compose ·
`/deploy/` owned by DevOps friend, app code owned by you/agent. No webhooks, no GraphQL, no Redis, no inbound ports.

---

## M1 — Scaffolding & configuration
- [x] Git repo, `main` branch, deployable from first commit (02prd §6).
- [x] `pyproject.toml` (deps: httpx, SQLAlchemy, asyncpg, PyYAML; dev: pytest, pytest-asyncio, aiosqlite, ruff), src layout.
- [x] `config/config.yaml` — thresholds, scoring weights, intervals, label maps, rate limits (01prd §11, §12.1).
- [x] `config/orgs.yaml` — org list with `tier` (1 = priority, 2 = long tail) + `gsoc_years` seed data, curated by user (02prd §3, §7).
- [x] `config/ai_criteria.yaml` — versioned LLM no-go criteria / preferences / prompt contract (02prd §4).
- [x] Config loader with validation + env overrides for secrets (`GH_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DATABASE_URL`, `AI_API_KEY`, `AI_MODEL`, …) — the `.env` contract (02prd §6).
- [x] Structured logging with **secret scrubbing** of token values in every log line (01prd §15).
- [x] `.gitignore`, MIT `LICENSE`.

## M2 — Database (01prd §6)
- [x] PostgreSQL 16 schema: `orgs`, `repos`, `issues` (PK `repo_full_name+number` = dedupe key), `notifications` (PK `issue_key+channel`), `poll_state` (reliability backbone), `meta`.
- [x] `jsonb` for labels/assignees/raw (JSON variant for sqlite in tests).
- [x] Idempotent schema bootstrap on startup (`create_all` + schema version marker) — deploy stays `docker compose up -d` simple.
- [x] Async data-access layer: upserts, insert-if-new, notification insert-then-send, pending-digest query, prune, org stats.
- [x] Postgres advisory lock so two workers can't run concurrently.
- [x] Extensions beyond report schema (needed by features, not redesign): `issues.linked_open_pr`, `first_comment_at`, `filtered_reason`, `ai_*` columns, `orgs.gsoc_score/gsoc_components`.

## M3 — GitHub client (01prd §3, §16)
- [x] httpx async client, fine-grained PAT (`GH_TOKEN`), `X-GitHub-Api-Version`, redirects followed (301 rename handling).
- [x] **Search API is the core mechanism**; `per_page=100`, `Link` header pagination, early stop by watermark.
- [x] Retry: exponential + jitter on 5xx; honor `Retry-After` (403/429) and `X-RateLimit-Reset`; `304`-friendly ETag not needed (search); 404/410 → typed errors.
- [x] Global **token bucket ≤ 20 search req/min** (report ceiling 30/min), REST concurrency semaphore.
- [x] Typed errors: `NotFound`, `RateLimited`, `GitHubError`.

## M4 — Filtering & scoring (01prd §10, §11)
- [x] Hard filters (score 0, never notified): closed, assigned, negative labels (`question/support/invalid/duplicate/wontfix/security`), repo not whitelisted, age > `max_age_hours`, bot authors, body < 50 chars.
- [x] Contribution Opportunity Score 0–100 with report weights: recency 25 / unassigned 20 / no linked open PR 15 / labels 15 / repo pushed 10 / stars 5 / body quality 5; cap 100; all weights in config.
- [x] GSoC relevance score (0–100, org-level, computed nightly, cached per issue): GSoC years 40 / mega-repo 20 / newcomer-friendliness 20 / triage proxy 20.

## M5 — Job A: nightly repo sync (01prd §1, §19)
- [x] `search/repositories?q=org:{o} stars:>={min} archived:false` per org → upsert `repos` by `github_id`, demote repos that dropped out, rename/404/410 handling.
- [x] Compute org GSoC stats from own data (label ratio, triage samples) + cache `orgs.gsoc_score`.

## M6 — Job B: tiered issue poller (01prd §7–8, 02prd §3)
- [x] Tiered schedule: tier1 orgs every 3 min, tier2 every 12 min (config; uniform 5-min supported), per-org watermarks, staggered starts.
- [x] Query: `org:{o} is:issue is:open created:>=watermark-60s` (overlap configurable, default 120 s for search-index lag), sort created desc.
- [x] DB dedupe via composite PK → at-least-once detection, exactly-once notification.
- [x] Whitelist post-filter against monitored repos (search can't express stars threshold).
- [x] **Deep-check only for candidates ≥ threshold**: fresh issue fetch (assignee confirm) + timeline (linked open PR, first-comment timestamp).
- [x] Final score after deep-check; re-filter if claimed in the meantime.
- [x] Catch-up mode: watermark older than `max_catchup` (72 h) → time-sliced windows, then resume; circuit breaker per org after N failures; `poll_state` updates (`last_ok`, `consecutive_failures`, breaker cooldown).

## M7 — AI layer (01prd §17, 02prd §4)
- [x] Gemini Flash via AI Studio key (`AI_API_KEY`, model via `AI_MODEL` env, swappable).
- [x] Only for threshold-passing candidates, inline before notification, one call per issue cached in DB.
- [x] Strict JSON via `responseMimeType` + `responseSchema`; 20 s timeout, 2 retries, 1 JSON-repair retry; **fail open** (notify without summary).
- [x] Output contract: `{summary ≤2 lines, worth_attempting bool, reason_codes[], difficulty}`; `worth_attempting:false` = annotation, never a drop.
- [x] Criteria (no-go: huge builds, GPU/cluster, proprietary SDKs/hardware, Windows-only; language prefs) in `ai_criteria.yaml`.

## M8 — Telegram delivery (01prd §9, 02prd §7)
- [x] Instant ≥ 85, digest 70–84 (flush every 20 min, ranked, chunked ≤ 4096 chars), HTML + inline "Open Issue" button.
- [x] chat_id allowlist (we only ever push to configured chats; incoming messages never read).
- [x] Exactly-once via `notifications` PK; insert-then-send; crash gap documented (01prd §16).
- [x] Unsent instant rows self-heal into the next digest.

## M9 — Job C + maintenance (01prd §1, §8)
- [x] Hourly state refresh of recent high-score/notified issues (assignment/taken transitions) — DB only, no re-notification.
- [x] Daily prune (>90 days) of issues + notifications.
- [x] Digest flush job on its own interval.

## M10 — Reliability & ops (01prd §15–16)
- [x] Watermark crash recovery (resume from watermark − overlap), catch-up, breaker — self-healing, zero manual intervention.
- [x] `/healthz` HTTP endpoint (stdlib server, port 8080) with live counters.
- [x] Healthchecks.io ping after each productive poll sweep (dead-man switch).
- [x] Graceful shutdown, per-job error isolation (one job can't kill the worker).
- [x] Secrets only via env; never logged (scrubbing filter); DB bound to Docker network.

## M11 — Deployment package (02prd §2, §6, §7)
- [x] Root `Dockerfile` (python:3.12-slim, non-root, arm64-friendly, HEALTHCHECK).
- [x] `deploy/docker-compose.yml`: postgres:16 + worker, healthchecks, no public DB port.
- [x] `deploy/.env.example` — **the contract between the two roles** (02prd §6).
- [x] `deploy/runbook.md` — first deploy, updates, rollback, token rotation (90-day PAT), watchdog, troubleshooting.
- [x] `deploy/backup.sh` — nightly `pg_dump | gzip` + retention (B2/rclone hooks documented).
- [x] CI on every PR/push: `ruff` + `pytest` (Postgres service for DB tests) + Docker build (02prd §6: "the gate keeps main green").
- [x] Release workflow: tag `v*` → multi-arch (amd64+arm64) image to GHCR.
- [x] Optional self-hosted-runner deploy workflow on tags (friend's side, documented in runbook).

## M12 — Tests (CI gate, 02prd §6) — VERIFIED: 76/76 green on SQLite AND real Postgres 16;
Docker image built and full container smoke test passed (mock GitHub/Telegram/Gemini → instant delivery observed)
- [x] Unit: filters, scoring (incl. cap + weight overrides), GSoC mapping, normalize, config validation, token bucket (fake clock).
- [x] GitHub client: Link pagination, early-stop by watermark, 5xx retry, Retry-After, rate-limit reset, 404.
- [x] AI enricher: strict parse, JSON-repair retry, fail-open on timeout/garbage, reason-code sanitizing.
- [x] Telegram: HTML escaping, 429 retry, digest chunking, insert-then-send semantics.
- [x] DB: dedupe PKs, watermark upsert, pending digest, prune, notification conflicts (sqlite; Postgres run in CI service).
- [x] **End-to-end pipeline integration test**: fake GitHub + fake AI + fake Telegram → sync → poll → filter → score → deep-check → enrich → instant + digest delivery → dedupe on second sweep → state refresh.
- [x] Spike script (`scripts/spike.py`) — the report's Day-1 validation (discovery + 24 h search + score, print top 20).
- [x] `scripts/mock_stack.py` + Docker smoke run: schema bootstrap on PG → watermark backfill → whitelist gate → sync → poll → deep-check → Gemini enrich → Telegram instant w/ button, noise filtered.
- [x] `GITHUB_API_BASE` / `AI_API_BASE` env overrides (also enables GitHub Enterprise / proxies).

## M13 — Docs
- [x] `README.md`: architecture, quickstart, config guide, env contract, testing, deploy handoff.
- [x] This checklist updated as items land.

## Explicit non-goals for v0.1 (per report §12/§20)
Dashboard (phase 2), comment-contention mining, Discord/email channels, multi-user, GitHub App, GraphQL, webhooks, queues.
