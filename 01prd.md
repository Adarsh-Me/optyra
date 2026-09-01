# Execution & Feasibility Analysis: GSoC Issue Monitor

**Verdict up front:** This is practical, cheap ($0–6/month), and achievable as an MVP in 1–2 weeks of part-time work. The single most important correction to your spec: **webhooks are not viable for this use case** (you can't install webhooks on repos you don't admin), and **the GitHub Search API is the correct core mechanism** — one authenticated search query covers an entire organization's new issues. Everything below is built around that insight.

---

## 1. Complete Architecture

The system is a **single small background worker + database + outbound notifier**, optionally with a thin dashboard:

```
┌─────────────────────────────────────────────────────────┐
│  VPS ($0–6/mo) — one Docker Compose stack               │
│                                                         │
│  ┌──────────────────┐    ┌───────────────────────────┐  │
│  │ Scheduler/Worker  │    │ FastAPI Dashboard (opt.)  │  │
│  │ (asyncio loop)    │    │ read-only + HTMX          │  │
│  │                  │    └──────────┬────────────────┘  │
│  │ Job A: repo sync │               │                   │
│  │  (nightly)       │    ┌──────────┴────────────────┐  │
│  │ Job B: issue poll│    │ PostgreSQL (Docker)       │  │
│  │  (every 2 min)   │    │ orgs / repos / issues /   │  │
│  │ Job C: state     │    │ poll_state / notifications│  │
│  │  refresh (hourly)│    └───────────────────────────┘  │
│  └───┬──────┬───────┘                                   │
│      │      │                                           │
└──────┼──────┼───────────────────────────────────────────┘
       │      │
       ▼      ▼
  GitHub API  Telegram Bot API (outbound HTTPS only)
  (Search +   — no inbound ports, no webhook listener needed
  REST)
```

Data flow:

1. **Repo sync job (nightly):** `search/repositories` per org → filtered repo set → upsert into `repos`.
2. **Issue poll job (every ~2 min):** per org, `search/issues` with `created:>=watermark` → new issues → filter to monitored repos → hard filters → score → dedupe via DB unique key.
3. **Deep-check (only for candidates above threshold):** fetch timeline (linked PRs), optionally comments → final score → notify → record notification.
4. **State refresh job (hourly):** re-check recent high-scoring issues for assignment/taken transitions (for the dashboard, not re-notification).

Three jobs, one process, one Postgres, zero inbound traffic. That's the whole thing.

---

## 2. Exact Technology Stack

| Component | Recommendation | Why |
|---|---|---|
| Language | **Python 3.12** | Best GitHub ecosystem, fastest to iterate, you're likely solo |
| HTTP client | **httpx (async)** + retry/backoff wrapper | Async concurrency for 30 parallel-ish queries with a semaphore |
| Data layer | **SQLAlchemy (Core or 2.0) + PostgreSQL 16** | Declarative, portable, Postgres native `jsonb` for labels/assignees |
| Scheduler | **Plain asyncio loop** (not Celery/APScheduler initially) | A drift-corrected `while True: sleep(next_tick - now)` loop is enough; queues are overkill |
| Notifier | **Telegram Bot API** via plain `sendMessage` | Free, mobile push, HTML formatting, inline link buttons, outbound-only |
| Dashboard (phase 2) | **FastAPI + Jinja2 + HTMX** | No React build pipeline for a single-user tool |
| Deployment | **Docker Compose on a small VPS**, Caddy for TLS | One `docker compose up -d`, works on any $5 host or Oracle free ARM |
| Config | **YAML + env vars for secrets** | Matches your spec, git-friendly |

Alternatives considered:
- **TypeScript on Cloudflare Workers + D1 + Cron Triggers:** genuinely $0 and serverless, but free-tier CPU limits (~10–30ms/invocation) make parsing large search responses awkward, D1 has write quotas, and debugging cron jobs in a Workers runtime is slower. Choose this only if you refuse to pay anything and accept friction.
- **Go:** better runtime, worse iteration speed for a personal tool. Not worth it here.
- **Node + a framework (BullMQ, etc.):** introduces Redis and a queue for no benefit at this scale.

---

## 3. GitHub API Requirements — the most important section

### Webhooks vs. events vs. polling — decisive answer

| Mechanism | Verdict |
|---|---|
| **Webhooks** | ❌ **Impossible.** Creating a webhook requires admin access to the repo. You cannot install webhooks on `apache/kafka`, `kubernetes/kubernetes`, etc. Full stop. Remove this from the design. |
| **Public Events API** (`/repos/{o}/{r}/events`, `/orgs/{org}/events`) | ❌ Capped at ~300 events / 90 days per resource. On `kubernetes/kubernetes`, 300 events pass in minutes — you'll miss issues. Useless for targeted monitoring. |
| **Search API polling** | ✅ **The correct approach.** One query per org covers *all* its repos, filtered by issue creation time server-side. |
| **REST polling per repo** | ✅ Fallback for repos whose org you don't monitor; scales well with ETags (below). |
| **GraphQL** | ⚠️ Optional middle tier for repo-level monitoring at scale. Skip for MVP. |

### The core query (memorize this)

```
GET /search/issues?q=org:apache is:issue is:open created:>=2025-01-15T10:00:00Z
  &sort=created&order=desc&per_page=100
```

- `is:issue` is **mandatory** — `/search/issues` returns PRs too.
- `no:assignee` can be added to pre-filter server-side.
- Covers every repo under `apache` — thousands of repos in **one call**.
- Add a 60-second overlap window on every poll (query `created:>=watermark - 60s`) and dedupe via the DB primary key. This gives at-least-once detection, exactly-once notification.

Repo discovery is also one query per org:

```
GET /search/repositories?q=org:apache stars:>=2000 archived:false&sort=stars&per_page=100
```

### Authentication

- **Recommended: Fine-grained PAT** with access = "Public repositories (read-only)". Least privilege (you literally cannot write anything), and fine-grained PATs currently get **15,000 requests/hour** (classic PATs: 5,000/hr).
- Fallback: classic PAT with **no scopes at all** — a zero-scope classic PAT can read public repo data.
- No OAuth app, no GitHub App needed (GitHub Apps only matter if you wanted webhooks on your *own* repos or per-installation limits — not this use case).

### Rate limits (the real numbers)

| Limit | Value |
|---|---|
| Search API | **30 requests/minute** authenticated |
| REST API | 5,000/hr (classic PAT) / 15,000/hr (fine-grained PAT) |
| GraphQL | 5,000 points/hour |
| Search results | Max **1,000 results per query** (page via `Link` header; split time windows if you exceed it — only relevant in catch-up scenarios) |
| Conditional requests | **304 responses do NOT count against your rate limit** — critical for the scaling strategy (§8) |
| Secondary limits | Don't burst; respect `Retry-After` on 403s; keep concurrency ≤ 5–10 |

### Pagination
`per_page=100` everywhere; follow `Link` response headers. For search, page until `created` timestamps fall below your watermark (usually 1 page).

**Known caveat:** GitHub's search index has a small indexing lag — typically seconds to ~2 minutes, occasionally more. REST per-repo polling is authoritative but 1 call/repo. For your 1–5 minute target, search polling is acceptable; just know the floor is not exactly 0 seconds.

---

## 4. Free vs. Paid

| Item | Cost | Notes |
|---|---|---|
| GitHub API | **$0** | Free forever at this usage; no paid tier needed |
| Telegram Bot API | **$0** | Unlimited for personal use |
| Compute | $0 (Oracle Cloud Always Free ARM: 4 OCPU/24GB, or home PC, or Cloudflare Workers) **or** ~$4–6/mo VPS (Hetzner CX22 ~€4.5, RackNerd ~$11/yr promos) | See §5 for honest free-tier caveats |
| PostgreSQL | **$0** (local Docker) or $0 hosted (Neon free ~0.5GB with cold starts; Supabase free 500MB with 7-day-inactivity pause) | Local Docker on the same VPS = no cold starts, no limits |
| Backups | **$0** | Nightly `pg_dump` → Backblaze B2 (10GB free) |
| Uptime monitor | **$0** | Healthchecks.io free (dead-man switch) |
| Domain | $0 (Tailscale/IP + DuckDNS) or ~$10/yr | Optional |
| LLM (phase 2+) | $0–3/mo | ~10–50 candidate issues/day on a Haiku-class model |

**Cost does not scale with repository count — it scales with compute comfort.** Monitoring 1,000 repos vs. 100 repos costs the same in dollars; only your API strategy changes (§8).

At 100 / 500 / 1,000+ repos: **the monthly cost is identical ($0–6)**, because org-level search makes repos nearly free, and ETag-based REST polling makes repo-level checks nearly free. The constraint is API quota design, not money.

---

## 5. Hosting/Deployment — honest free-tier reality (2024–2025)

| Option | Real status |
|---|---|
| Heroku / Railway / Fly.io free tiers | **Gone.** Don't plan around them. |
| Render free | Web services **sleep after 15 min**; background workers aren't free; free cron jobs run ≤15 min on long intervals. ❌ for a 2-min poller. |
| Vercel/Netlify serverless crons | Hobby-tier crons run **daily at best**. ❌ |
| **GitHub Actions cron** | `$0` on a public repo (unlimited standard runner minutes), min interval 5 min, **but dispatch is routinely delayed 5–30 min** at peak. Fails your latency goal; viable as a $0 fallback if you accept 10–30 min latency. |
| **Oracle Cloud Always Free (ARM)** | Genuinely free, generous (4 OCPU/24GB). Caveats: needs a card for signup, capacity often unavailable in popular regions, idle instances can be reclaimed (keep the worker busy; accept the risk). |
| **Cloudflare Workers + D1 + Cron Triggers** | Genuinely $0, cron down to every minute. Caveats: ~10–30ms CPU/invocation, D1 write quotas, more constrained dev experience. Viable "hardcore free" option. |
| **Home PC / Raspberry Pi** | $0 (pennies of power), fully capable. Requires reliable home internet/power. |
| **~$5/mo VPS (Hetzner/RackNerd)** | ✅ **The recommended baseline for reliability.** One Docker Compose stack, 99.9% uptime, no games. |

**Recommendation:** Oracle free ARM or home PC if you want $0 and accept the risk; Hetzner CX22 (~€4.5/mo) the moment you want to stop thinking about it. A stale monitor that misses issues while you sleep is worse than $5/month.

---

## 6. Database

**PostgreSQL.** Not SQLite (you want `jsonb`, real constraints, and concurrent dashboard reads while the worker writes — SQLite would work for MVP but you'll outgrow it within weeks). Not MongoDB (no benefit, worse tooling for relational issue state).

- **Free hosted is enough?** Yes. Storage math: an issue row (title, URL, labels as `jsonb`, scores, truncated raw payload) ≈ 1–2 KB. Even a heavy set of 30 orgs producing ~1,000 qualifying issues/day ≈ **30–60 MB/month**. With a 90-day retention/pruning job you stay under ~200MB forever.
- **But:** Neon free autosuspends (cold-start hiccup on first query), Supabase pauses after 7 idle days. If you're on a VPS anyway, run **Postgres in Docker locally** — zero cold starts, zero limits, `pg_dump` for backup. If you go serverless (CF Workers), use D1.

Core schema:

```sql
orgs(login PK, gsoc_years int[], tier smallint, created_at);
repos(github_id PK, org_login, full_name, stars int, language,
      archived bool, monitored bool, pushed_at, last_synced_at);
issues(repo_full_name, number,           -- composite PK = dedupe
       title, state, author, created_at,
       labels jsonb, assignees jsonb,
       linked_pr bool, score int, gsoc_score int,
       notified bool, raw jsonb,
       first_seen_at, last_checked_at);
notifications(issue_key, channel, sent_at, PRIMARY KEY(issue_key, channel));
poll_state(scope PK, watermark timestamptz,
           last_ok timestamptz, consecutive_failures int);
```

`poll_state` is the reliability backbone (§16).

---

## 7. Monitoring Frequency — the math for 1–5 minute detection

**Design: poll every 2 minutes, one search query per org, plus 60s overlap.**

- Search limit: 30 req/min. 25 orgs ÷ 2-min cycle = **12.5 req/min average** — comfortable margin.
- Worst-case detection latency = time-since-last-poll (≤2 min) + query time (queries run with concurrency 4–5; 25 queries ≈ 10–20 s) + processing (<1 s).
- **Result: P50 ≈ 60–90 s, P95 ≈ 2.5–3 min, worst case ~4 min.** Meets your spec.
- Rule of thumb: keep `orgs ≤ 25` for a 2-min cycle; 25–50 orgs → 3–4 min cycle; >50 orgs → you're out of single-account search budget for low latency, move high-volume orgs to ETag REST polling (§8) or accept 5-min cycles.

Rules that keep you safe:
- Never exceed ~20 search req/min sustained (leave headroom below 30).
- No faster than a 60-second cycle (secondary rate limits flag aggressive bursts).
- On `403` with `Retry-After`: back off, pause the affected job, log it.
- On `X-RateLimit-Remaining: 0`: sleep until `X-RateLimit-Reset`.

---

## 8. Scalability — 100 / 500 / 1,000+ repositories

The key insight: **if you monitor at org level, repository count is irrelevant to cost.** One search query covers Apache's ~2,000 repos as cheaply as one repo. Repos only matter when they belong to orgs you *don't* monitor.

| Scale | Strategy | Quota math | Latency |
|---|---|---|---|
| ≤25 orgs (covers ~100–2,000+ repos) | Search per org, 2-min cycle | ~12 search/min | 1–4 min |
| ~100 individually-monitored repos | Search per repo (`repo:o/r`), or 25-query batches | 100 queries / 2 min = 50/min ❌ → use 3.5-min cycle (28/min) ✅, or REST+ETag | ~4–5 min |
| 500–1,000+ repos | **REST polling with conditional requests**: `GET /repos/{o}/{r}/issues?state=open&sort=created&direction=desc&per_page=5` + `If-None-Match` | 304 responses **don't count** against the rate limit. Quota consumption ∝ *repos with changes*, not repo count. 1,000 repos × 3-min cycle with ~5–10% returning 200 = 1–2k counted req/hr — fine under 5k (classic) or 15k (fine-grained) | 3–5 min |
| Optional middle tier | GraphQL with aliases (batch ~100 repos' latest issues per query, ~100 points/query) | 5,000 points/hr → ~6–12 min cycles at 500–1,000 repos | 6–12 min (worse than ETag REST — use only if ETags misbehave) |

Additional scaling measures, in order:
1. Watermark overlap + DB dedupe already gives correctness; scaling is purely about cycle time.
2. Prune: drop repos with no issues in 90 days from the poll set automatically.
3. If you ever truly outgrow one account: a **GitHub App** (5,000/hr per installation) for repos you own, and honestly, at that point you're building a product, not a personal tool.
4. **Do not** shard across multiple personal accounts — dubious under GitHub's ToS ("one person may not maintain more than one free account") and unnecessary given the above.

---

## 9. Notifications

| Channel | Free? | Effort | Notes |
|---|---|---|---|
| **Telegram bot** ✅ recommended | Yes, unlimited | ~30 min | `@BotFather` → token; `getUpdates` to find your `chat_id`; `sendMessage` with HTML + inline URL button. **Enforce a chat_id allowlist** so strangers who find your bot get ignored. Outbound HTTPS only — no server, no ports. |
| Discord incoming webhook | Yes | ~10 min | Even simpler (just a URL), but rate-limited to ~30 msgs/min and weaker mobile push. Good second channel. |
| Email (SMTP/Gmail) | Yes | ~1 hr | Gmail app-password, ≤500/day. Slower to notice. Phase 2. |
| Web dashboard | Hosting cost only | Phase 2 | Not a *notification* — a browsing surface. |

**Anti-spam design (critical — get this wrong and you'll mute the bot within two days):**
- Default: notify only for score ≥ threshold (e.g., 70) **and** unassigned **and** unlinked PR.
- Offer a **digest mode**: batch qualifying issues every 15–30 min into one message, ranked. Instant mode for score ≥ 85 only. In practice digest mode is what you'll keep enabled.
- One notification per issue, enforced by `notifications` PK.

---

## 10. Issue Detection & Ranking — what's reliable and what isn't

| Signal | Source | Reliability |
|---|---|---|
| Newly created | Search `created:>=watermark` + DB dedupe | ✅ Exact |
| Assigned | `assignees` in search result | ✅ Exact, real-time |
| Labels | `labels` in search result | ✅ Exact |
| Closed | `state` | ✅ Exact |
| **Linked PR** | `GET /repos/{o}/{r}/issues/{n}/timeline` → look for `cross-referenced` events from PRs, or GraphQL `timelineItems(CROSS_REFERENCED_EVENT)` | ✅ Reliable, but costs 1 call per candidate — apply **only to issues that already passed the score threshold** (a handful per hour). Skip in MVP if needed; assignee+labels cover 80%. |
| "Someone is working on it" (comments like "I'll take this") | Fetch comments, keyword/heuristic match | ⚠️ Low precision. Phase 3 at best; label it "possible contention," never a hard filter. |
| Duplicate-of-another-issue | Labels (`duplicate`) or closing reference | ✅ Via labels only; true dedupe is hard, don't attempt in v1. |
| Beginner-friendly | Labels: `good first issue`, `first-timers-only`, `help wanted`, `beginner`, `easy`, `starter` — configurable map | ✅ Exact, but conventions differ per org (hence configurable) |
| Negative | Labels: `question`, `support`, `invalid`, `duplicate`, `wontfix`, `security`; author = bot accounts; body < 50 chars | ✅ Good heuristics |

---

## 11. Scoring System — concrete v1

**Hard filters (applied before scoring — score = 0, never notified):** closed, assigned, `question/support/invalid/duplicate/wontfix/security` label, repo not monitored, age > `max_age_hours`.

**Contribution Opportunity Score (0–100):**

| Factor | Points |
|---|---|
| Recency: ≤30 min **+25** / ≤2 h +20 / ≤6 h +12 / ≤24 h +6 | 25 |
| Unassigned (post deep-check) | 20 |
| No linked open PR | 15 |
| Labels: `good first issue`/`first-timers-only` **+15** / `help wanted` +10 / `beginner`,`easy`,`starter` +5 / `bug`,`enhancement` +3 | 15 |
| Repo pushed within 30 days | 10 |
| Stars: >10k +5 / >5k +3 / >2k +2 | 5 |
| Body ≥ 200 chars AND contains a code block, error text, or numbered steps | 5 |

Cap at 100. All weights in `config.yaml`. This is deliberately deterministic, debuggable, and tunable — no ML needed for 90% of the value.

**GSoC Relevance Score (org-level, computed nightly, cached on each issue):**

| Factor | Points |
|---|---|
| GSoC participations in last 6 years: 6 → 40, 4–5 → 30, 2–3 → 20, 1 → 10, 0 → 0 | 40 |
| Org has repos >10k stars actively maintained | 20 |
| Ratio of `good first issue`-labeled issues opened per month (proxy for newcomer-friendliness) | 20 |
| Median maintainer response: presence of triage within 48h on recent issues (proxy, from your own data) | 20 |

Seeded from the public GSoC org archive lists (community-maintained JSON on GitHub exists; or hand-curate 30 orgs once — 30 minutes of work, done).

---

## 12. MVP — exact scope

**In (target: 1–2 weeks part-time):**
1. `config.yaml`: orgs, min stars, label map, thresholds, Telegram settings.
2. Fine-grained PAT (public read-only).
3. Nightly repo sync via `search/repositories` → filtered `repos` table.
4. 2-minute search poller per org with watermark + overlap + DB dedupe.
5. Hard filters + v1 scoring.
6. Timeline deep-check for candidates ≥ threshold (linked-PR detection).
7. Telegram notification (instant for ≥85, digest for ≥70) with allowlist.
8. `poll_state` watermark recovery + retry/backoff + structured logs.
9. Docker Compose deploy (worker + Postgres), `/healthz`, Healthchecks.io ping.

**Explicitly out of MVP:** dashboard, CLI, LLM analysis, comment mining, historical analytics, multi-user, GitHub App, webhooks, search UI. Telegram + psql is your interface for the first two weeks.

---

## 13. Development Roadmap

| Phase | Time | Deliverables |
|---|---|---|
| **0 — Spike (Day 1)** | 1 day | Token, config, 100-line script: discover repos for 3 orgs + query last 24h of issues + print scored list to console. Proves the whole core loop. |
| **1 — MVP (Week 1)** | 5–8 days | DB schema, watermark poller, dedupe, scoring, Telegram, deploy to VPS, reliability (retries, resume, health ping). |
| **2 — Usability (Weeks 2–3)** | +5–8 days | FastAPI+HTMX dashboard (filter by org/label/score/age), hourly state-refresh job (assignment changes), digest mode, basic analytics page, CLI wrapper, pruning job, backups. |
| **3 — Intelligence** | +1–2 weeks | LLM enrichment of candidates only (§17), comment-contention heuristic, GSoC score refinement, multi-channel (Discord), GSoC org auto-curation. |

---

## 14. Complexity Assessment

| Area | Difficulty | Notes |
|---|---|---|
| Repo discovery, metadata, filters | 🟢 Easy | ~200 lines |
| Search poller + dedupe + watermark | 🟢 Easy | The architecture does the work |
| Telegram bot | 🟢 Easy | An afternoon |
| Scoring v1 | 🟢 Easy | Pure functions + config |
| 24/7 reliability (resume, backoff, prune, rename/archive handling) | 🟡 Medium | Where real bugs live; budget a full day |
| Linked-PR detection via timeline | 🟡 Medium | Straightforward but per-issue calls |
| Dashboard + filters | 🟡 Medium | Time-consuming, not hard |
| "Someone's already working on it" via comments | 🔴 Hard/low-precision | Heuristic at best |
| LLM issue-quality analysis that's actually *right* | 🔴 Hard | Easy to build badly; hallucinated difficulty ratings will mislead you |
| 1,000+ repos at low latency | 🟡 Medium | ETag strategy works but needs careful testing |
| **Overall** | **~25–40 hrs for MVP** for a competent dev | |

---

## 15. Security

- **GitHub token:** fine-grained PAT, "Public repositories (read-only)." Stored in env var / `.env` (chmod 600, gitignored) injected by Docker Compose. Never in config files or logs (scrub it in your HTTP client's log formatter — this is the #1 self-doxxing vector).
- **Telegram bot token:** same handling. Allowlist your `chat_id` — otherwise anyone who discovers the bot can read your feed.
- **Dashboard:** single-user → put it behind **Tailscale** (free, zero public exposure) or HTTP Basic auth + Caddy TLS. Never expose it raw.
- **Webhook security:** N/A (no webhooks). One less attack surface.
- **Database:** bound to `localhost`/Docker network only, strong password or peer auth, no public port.
- **Backups:** nightly `pg_dump | gzip` → rclone to B2 (10GB free). Test a restore once.
- **Secrets rotation:** trivial — one GitHub token, one Telegram token.

---

## 16. Reliability

- **Dedup notifications:** PK on `(issue, channel)`; insert-then-send; mark `sent` after success. A crash between send and mark can produce one rare duplicate — acceptable; document it.
- **Missed events / downtime recovery:** `poll_state.watermark` per org. On restart: resume from watermark minus 60s overlap; if watermark is older than `max_catchup` (e.g., 72h — or beyond the search 1,000-result practicality), do one catch-up scan in time-sliced windows, then reset to now. This makes the system **self-healing with zero manual intervention**.
- **Retries/backoff:** exponential + jitter on 5xx; honor `Retry-After` and `X-RateLimit-Reset` on 403/429; circuit-break an org after N consecutive failures (log, don't crash the worker).
- **Renamed/moved repos:** GitHub API returns 301 — follow it, update `full_name`. 404/410 → mark unmonitored. Archived → nightly sync handles it.
- **Watchdog:** worker pings Healthchecks.io (free) every cycle; if pings stop for 15 min you get an email/Telegram — this is how you find out your $5 VPS or Oracle instance died at 3am.
- **Overlapping runs:** single asyncio loop is naturally serialized; if you ever add parallel workers, use Postgres advisory locks.

---

## 17. AI/LLM Usage — where it helps and where it's waste

**Wasteful (do NOT use an LLM):** deciding whether an issue is *new* (timestamps), assignment (metadata), labels (metadata), recency, repository stats. All of this is deterministic metadata — an LLM adds latency, cost, and errors.

**Actually useful (Phase 3, candidates only — ~10–50 issues/day pass your threshold):**
- **Clarity classification:** "Is this issue well-specified enough to act on?" (replaces/extends the crude body-length heuristic).
- **Summarization:** compress a 3,000-word issue into 2 lines for the Telegram digest. High value, low risk.
- **Difficulty/skills estimate:** useful *only if clearly labeled as a prediction with evidence* — and expect it to be wrong often. Keep it out of the score; show it as advisory text.
- **Soft dedupe:** "this looks like issue #4812 from last week" — advisory only.

Cost at candidate volumes with a Haiku-class model: **$0.5–3/month.** Use your heuristic score to decide *which* issues deserve LLM treatment — never the reverse.

---

## 18. Realistic Cost Analysis

| Setup | Monthly | Components |
|---|---|---|
| **Personal, $0 tier** | $0 | Cloudflare Workers+D1 (constrained) or home PC or Oracle free ARM (risk of reclaim) + Telegram + local/D1 storage + free backups |
| **Personal, recommended** | **$4–6** | Hetzner CX22 (or RackNerd ~$11/YEAR = ~$1/mo) + Docker Postgres + Telegram + B2 backups + Healthchecks.io |
| **Personal + dashboard + LLM** | $5–9 | Above + $1–3 LLM |
| **Scaled (1,000+ repos, heavier jobs)** | $6–15 | Bigger VPS (4–8GB) + LLM; GitHub API still $0 |

One-time: domain ~$10/yr (optional). That's the entire cost universe of this project.

---

## 19. Recommended Final Architecture (v1)

```
                    ┌──────────────────────────────┐
                    │  VPS · Docker Compose        │
                    │  ┌────────────────────────┐  │
   Nightly job ────▶│  search/repositories     │  │  filtered repo set
                    │  per org (stars/activity) │  │
                    └───────────┬────────────────┘  │
                                ▼                   ▼
                    ┌────────────────────────┐  ┌───────────────┐
   Every 2 min ────▶│ search/issues per org │─▶│  PostgreSQL   │
                    │ created:>=watermark-60s│  │  (dedupe,     │
                    │ is:issue is:open       │  │   state,      │
                    └───────────┬────────────┘  │   scores)     │
                                ▼               └───────┬───────┘
                    hard filters → score ≥70           │
                                ▼                      │
                    ┌────────────────────────┐        │
                    │ timeline deep-check    │        │
                    │ (linked PR, assignee)  │        │
                    └───────────┬────────────┘        │
                                ▼                     ▼
                     Telegram (≥85 instant,        FastAPI+HTMX
                     ≥70 digest) + allowlist       dashboard (opt.)
                                │
                     Healthchecks.io watchdog (free)
```

No queues, no microservices, no Redis, no Kubernetes. One process, one database, three jobs, outbound-only.

---

## 20. Exact Starting Point

**Build first (today, in this order):**
1. Fine-grained PAT (public, read-only) + `config.yaml` with **5 orgs** (e.g., `apache`, `kubernetes`, `llvm`, `rust-lang`, `nodejs`).
2. ~100-line spike script: repo discovery query → print monitored repos; issues search for last 24h → apply hard filters + v1 score → print ranked list. **This validates the entire core loop in one file.**
3. Add the DB (schema above) + watermark + dedupe → wrap in a 2-min loop → run for 48h on your laptop, tuning thresholds by watching real output.
4. Telegram bot + allowlist.
5. Deploy: `docker compose up -d` on the VPS + Healthchecks.io ping. **MVP is live.**
6. Only then: dashboard, timeline deep-check, state refresh, analytics.

**Avoid initially:** webhooks (impossible), GraphQL (premature), LLM analysis (premature), dashboards-first (Telegram is your UI for week 1), multi-user/accounts, queues/Celery, React, and any "smart" comment parsing. Each of these adds 1–3 days and zero discovery latency improvement.

---

# Final Summary

**Recommended Stack:** Python 3.12 + httpx (async) + SQLAlchemy + PostgreSQL 16 (Docker) + asyncio poller + Telegram Bot API + FastAPI/HTMX dashboard (phase 2) + Docker Compose on Hetzner CX22 (~€4.5/mo) or Oracle free ARM ($0), behind Tailscale.

**Free/Paid Breakdown:** GitHub API $0 · Telegram $0 · Postgres $0 · backups $0 (B2 10GB) · watchdog $0 · compute $0–6 (your only real cost) · LLM optional $1–3. **Total: $0 (risky) or ~$5/month (solid).**

**MVP Architecture:** Nightly `search/repositories` repo sync → 2-minute `search/issues` poller per org (`is:issue is:open created:>=watermark-60s`) → Postgres dedupe → hard filters → rule-based score → timeline linked-PR check for candidates → Telegram notify (instant ≥85, digest ≥70) → watermark-based crash recovery. No webhooks (impossible for third-party repos), no queues, no inbound ports.

**Approximate Monthly Cost:** $0–6 personal; $6–15 at 1,000+ repos. Costs do not scale with repo count.

**Expected Monitoring Latency:** P50 ≈ 60–90 s, P95 ≈ 2.5–3 min, worst-case ~4–5 min for ≤25 orgs on a 2-min cycle (plus occasional GitHub search-indexing lag). Comfortably within your 1–5 min target.

**Implementation Roadmap:** Day 1 — spike script proving discovery+search+scoring in 100 lines. Week 1 — DB, poller, dedupe, Telegram, deploy (MVP live). Weeks 2–3 — dashboard, state refresh, digest, analytics, backups. Phase 3 — LLM enrichment, contention heuristics, GSoC scoring refinement.

**Main Technical Risks:**
1. Search API secondary rate limits if you burst or over-poll (mitigate: ≤20 search/min, respect `Retry-After`).
2. GitHub search-indexing lag (seconds to minutes, occasionally worse).
3. "Already being worked on" detection is only as good as assignee/labels/linked-PR metadata — comment-based detection stays unreliable.
4. Free-hosting fragility (Oracle reclaims, Actions cron jitter 5–30 min, Render sleep).
5. Notification fatigue — without digest mode and a strict threshold, you'll mute the bot in two days.
6. Rule-based scoring will mislabel some docs-only/spam issues — expect tuning, not perfection, in v1.

**Exact First Step:** Create the fine-grained PAT, write the one-file spike: call `GET /search/repositories?q=org:apache+stars:%3E%3D2000+archived:false` and `GET /search/issues?q=org:apache+is:issue+is:open+created:%3E%3D{24h-ago}&sort=created&order=desc&per_page=100`, apply your hard filters and v1 score, and print the top 20 issues to your terminal. If that output looks like something you'd act on — and it will — the entire project is validated, and everything after it is engineering, not research.