# Optyra — GitHub contribution-opportunity monitor

A production-ready background worker that watches a **curated mission list of GitHub
orgs** (16 orgs + 3 pinned repos, all flagship-tier projects) for **freshly created,
still-claimable issues**, scores them with a transparent rule engine, enriches the best
candidates with an AI **2-line summary + "worth attempting" + setup-weight verdict**, and
delivers ranked **Telegram** alerts — instant for the hottest, budgeted digests for the
rest. Built from `01prd.md` (architecture) + `02prd.md` (decisions), redesigned in v2 to
the mission spec: *only these orgs, only ≥10k-star repos, software-only fixes with no
heavy/proprietary setup.*

```
ONE PROCESS · Postgres · outbound HTTPS only (Docker Compose or Render web service)
┌────────────────────────────────────────────────────────────────┐
│ worker (Python 3.12, asyncio, httpx)                           │
│  ├─ Job A (6h):   repo discovery per org (stars/metadata)      │
│  │                + direct GET for pinned repos (personal acct) │
│  ├─ Job B (180s): search/issues — 16 org scopes + 1 combined   │
│  │                pinned-repo scope (~5.7 search req/min,      │
│  │                token-bucket ≤ 20/min, per-scope watermarks, │
│  │                120 s overlap, DB dedupe on (repo, number))  │
│  │                → on-demand star gate (client-side!) →       │
│  │                  hard filters → score 0-100                 │
│  ├─ deep-check (candidates only): fresh issue + timeline       │
│  │                → linked open PR, assignee confirm           │
│  ├─ enrich (candidates only): Gemini Flash strict JSON         │
│  │                → summary + worth + setup_weight             │
│  │                (fail-open, per-repo priors, daily cap)      │
│  ├─ setup policy: heavy → DROP (per-org hard/soft override)    │
│  ├─ lane gate: digest lane needs newcomer label OR worth=yes   │
│  ├─ notify: instant ≥ 85 · digest 70–84 ranked flush every    │
│  │           20 min WITH per-owner daily budget + size cap     │
│  ├─ Job C (hourly): state refresh (assigned/closed) — DB only  │
│  └─ maintenance: prune > 90 d · daily funnel self-report ·     │
│                  Healthchecks.io ping · /healthz + config hash │
├─ postgres:16 (or any managed Postgres; asyncpg)                │
└─ config baked in image (orgs.yaml is the ONLY thing to curate) │
```

**No webhooks, no GraphQL, no queues, no tiers, no `stars:` qualifier in issue queries**
(GitHub's issue search cannot combine `org:` with `stars:` — verified live; the gate is
enforced client-side against repo metadata, evaluated on-demand, so a repo crossing
10k★ is watched from the very next poll — no 24h race).

## The watch set (`config/orgs.yaml`)

| | v2 |
|---|---|
| Orgs (16) | tensorflow, electron, godotengine, google-gemini, neovim, opencv, django, python, swiftlang, webpack, FFmpeg, git, llvm, jenkinsci, NixOS, dart-lang |
| Pinned repos (3) | `laurent22/joplin` (personal account — `org:` can never see it), `NixOS/nixpkgs`, `dart-lang/sdk` (star-flicker immunity) |
| Blocked owners | pytorch, rust-lang (post-search code filter, never query negation) |
| Gate | `sync.min_stars: 10000`, client-side, live |
| Multi-repo orgs | tensorflow → tensorflow(199k)/models(78k)/tfjs(19k); python → cpython(76k)/mypy(21k) — auto |

Org identifiers were verified against the live API on 2026-09-03 (`swiftlang/swift`,
`godotengine/godot`, `google-gemini/gemini-cli`, `jenkinsci/jenkins`, `django/django`).
Every star count in the file comments is from that sweep. "Intel Video Audio for Linux"
was resolved as: no watch added.

## Scoring v2 (config-driven, deterministic)

`recency 25 · unassigned 20 · no linked open PR 15 · labels 20 (gfi/fto) · repo active 3
· stars tier 5/4/3 (50k/20k/10k) · body 5 · setup-minimal bonus +5 · parked penalty −8`
→ cap 100, floor 0. The v1 stars (10-pt) and repo-activity factors went degenerate above
a 10k floor and were recalibrated; the GSoC org score is retired (a 40-pt org-history
factor doesn't discriminate within a curated mission list).

Hard rejects (never notified): closed / assigned / `question, support, invalid,
duplicate, wontfix, security` + v2 `stale, lifecycle/stale, upstream` / non-monitorable
repo / age > 72h / bot author (`user.type == "Bot"` primary, suffix fallback) / body <
50 chars. **Soft** "maintainer-parked" labels (`needs triage`, `needs more info`,
`waiting on author`, `discussion`, `rfc`, …) only cost −8 + a ⚠️ tag — repo label culture
differs, dropping on them would silently lose work.

## AI setup filter (the v2 brain addition)

Gemini Flash classifies every candidate: `{summary ≤2 lines, worth_attempting,
reason_codes[], difficulty, setup_weight: minimal|moderate|heavy}`.

* `minimal` → +5 rank bonus. `moderate` (one clone+build of the project itself) → normal.
* `heavy` (Xcode/NDK/GPU/proprietary/multi-GB) → **dropped** by default; `ai.org_setup_filter`
  flips any org to `soft` (digest-only demotion). Default policy `hard` — the user's
  explicit instruction. llvm/electron/swiftlang are annotated `hard` in config so the
  trade-off is visible and one-line flippable.
* **Per-repo prior**: last N verdicts cached on `repos.setup_prior`; on Gemini-down days
  a repo whose majority history says `heavy` still gets dropped (default `moderate` =
  allow → fail-open preserved). Optional keyword screen (config) is the third layer.
* Daily Gemini call cap (300 default). AI never blocks delivery, one cached call/issue.

## Notification math (v2)

Instant ≥ 85 (exempt from caps) · digest 70–84 every 20 min, **flush-time budgeted**:
per-owner 5/day (`notify.per_owner_daily_cap`) filled best-first at each flush (not
arrival-order — a late high-scorer wins its slot), per-flush size cap 20
(`digest_max_items`), overflow suppressed **visibly** (`+N more suppressed today`
footer + funnel counter, never silently dropped). Digest-lane gate: an issue needs a
newcomer label or AI `worth_attempting=true` to enter the 70–84 lane at all.
Exactly-once via `notifications(issue_key, channel)` PK; failed instant sends
self-heal into the next digest. chat_id allowlist.

## Daily funnel report

Every day at 21:00 UTC a Telegram report: `seen → gate → hard → below-threshold →
deep-reject → setup-drop → lane-gate → ai calls → instant/digest → suppressed`, plus
per-owner seen/notified top-10 and the **config hash** (deployed vs edited configs
become visibly identical/different). This is how 70/85/caps get tuned with data.

## Quickstart

```bash
export GH_TOKEN=github_pat_...      # fine-grained, Public repos read-only
export TELEGRAM_BOT_TOKEN=...       # @BotFather
export TELEGRAM_CHAT_ID=...         # comma-separate for multiple chats
export AI_API_KEY=...               # optional (AI Studio free tier)
export DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/optyra

pip install -e ".[dev]"
python -m optyra            # bootstraps schema (+v1→v2 migration), starts all jobs
python scripts/spike.py     # report's Day-1 spike: real data, scored list, no DB
```

Config: `config/config.yaml` (every knob), `config/orgs.yaml` (watch set),
`config/ai_criteria.yaml` (LLM contract, versioned). Secrets are env-only.

## Tests

```bash
pytest                                   # SQLite: 106 passed, 3 skipped
TEST_DATABASE_URL=postgresql+asyncpg://... pytest   # real Postgres: 109 passed (CI runs this)
```

Includes an **end-to-end v2 pipeline test** (fake GitHub/AI/Telegram): sync → poll →
on-demand star gate → hard/soft filters → score → deep-check → setup policy → lane gate
→ instant + budgeted digest → suppressed footer → dedupe on second sweep → state
refresh. Plus: config validation (watch schema, pinned format, blocked owners), scoring
tables, token bucket (fake clock), GitHub client (pagination, Retry-After, rate limit,
combined repo: query), enricher (strict parse, repair, fail-open, setup defaults),
Telegram (escaping, chunking, footer, 429, allowlist), DAL (priors, funnel, reconcile,
suppression), repo sync (pinned discovery/demote-protection). The **v1→v2 migration**
(old Render DB gains `is_pinned/setup_*/suppressed_at/metrics_daily` idempotently, data
intact) is verified against real Postgres 16.

## Deployment

Friend/DevOps owns `deploy/`: `docker-compose.yml`, `.env.example` (the contract),
`runbook.md` (first deploy, updates, rollback, 90-day PAT rotation, backups, plus
**Render specifics: keep-alive pinger + DB-expiry check**), `backup.sh`. CI on every PR:
ruff + pytest (Postgres service) + Docker build; tag `v*` → multi-arch image. On Render,
healthz binds `$PORT` automatically; the advisory lock handoff makes overlapping deploys
zero-downtime. **Free-tier caveat:** a Render free web service sleeps ~15 min after the
last inbound request — set up a free UptimeRobot/cron-job.org pinger on the healthz URL,
otherwise latency promises are void. **Verify your Postgres won't expire** (Render free
Postgres does; Neon/Supabase free tiers don't).

## Known limitations (deliberate)

Crash between Telegram-200 and `sent_at` can rare-duplicate (report §16, accepted) ·
search-index lag ~1–2 min on top of poll interval (overlap absorbs) · GSoC-score column
retired-but-present (schema stability) · triage/timing proxies learn over days.
