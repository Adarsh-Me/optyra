# Doubt Resolution & Final Decisions

All seven points resolved below. Short version: you don't need Aiven, Render's free tier cannot honestly run this bot, a token on your **main** account is the right call, 100 orgs forces a tiered polling design, the LLM layer is effectively $0, and you and your friend should split by **concern (app vs. infra), not by branches**. Total cost: **$0–5/month**.

---

## 1. GitHub Authentication

**Can you run with no account/token?** Technically yes for public data, but it's not viable:

| Limit | Unauthenticated | Authenticated (PAT) |
|---|---|---|
| Search API | **10 req/min per IP** | 30 req/min |
| REST API | **60 req/hr per IP** | 5,000/hr (classic) / **15,000/hr (fine-grained, public repos)** |
| GraphQL | ❌ unavailable | ✅ |

At your scale (50–100 orgs), 10 search/min puts you at the absolute ceiling with zero headroom, and **60 REST req/hr kills repo discovery, timeline deep-checks, and state refresh entirely**. Worse: on shared PaaS infrastructure (like Render), the per-IP unauthenticated limit is shared with other tenants on the same egress IP. Verdict: **token is mandatory, no exceptions.**

**Dummy account vs. main account — use your MAIN account with a fine-grained PAT.**

- A fine-grained PAT scoped to **"Public repositories (read-only)"** with zero additional permissions cannot read your private repos, cannot write anything, cannot act as you. The worst-case leak is: someone burns your rate limit reading public data. Blast radius ≈ zero.
- A "dummy personal account" is a ToS gray area (GitHub permits **machine accounts** for automation, but a second personal account to hold a read-only token buys you nothing here).
- 15,000 REST req/hr is so far beyond your usage that there's no rate-limit contention with your normal GitHub activity.
- Rotation is trivial: fine-grained PATs have a max 1-year expiry — set 90 days, put rotation in the runbook (it's an env var change + restart).

**Decision:** Fine-grained PAT on your main account → "Public repositories (read-only)" → no additional permissions → stored as `GH_TOKEN` env var, rotated every 90 days. If this ever becomes a multi-user product, switch to a machine account (a one-line config change).

---

## 2. Deployment: Render + Aiven — Does It Actually Work?

**Render free tier — the honest reality** (verify current docs before committing; their tiers shift):

| Render component | Free tier behavior |
|---|---|
| Web service | 0.1 CPU / 512MB, **sleeps after 15 min without inbound HTTP**, ~30–60s cold start, 750 instance-hours/month (≈ exactly one always-on service, no headroom for deploy overlaps) |
| **Background worker** | ❌ **Paid only** (~$7/mo Starter) — this is what our poller actually is |
| **Cron job** | ❌ **Paid only** |
| Free Postgres | ❌ **Deleted after 30 days** (trial, not a tier) |

So the "$0 Render" path requires embedding the poller inside a *web service* and keeping it awake with an external pinger (cron-job.org/UptimeRobot hitting it every ~10 min). That's a known hack, and it works — but:

1. Your **core feature's uptime now depends on a third-party pinger**. Pinger dies → bot sleeps → you miss issues.
2. Every deploy/restart adds a cold-start gap.
3. You're using the free tier against its intent; policy risk.
4. 750 hrs/month ≈ 744 hrs for a 31-day month — borderline, no margin.

**The architecturally correct Render deployment** (Background Worker, ~$7/mo) + **Aiven free Postgres** (single-node, always-on, no autosuspend, modest connection limits — genuinely fine for our ~5-connection pool) **does work reliably.** It's just $7/month for 512MB/0.5CPU — a Hetzner VPS at €4.5 gives you 2 vCPU/4GB *and* removes the external DB dependency.

**Comparison:**

| Setup | Cost | 24/7 reliable? | Notes |
|---|---|---|---|
| Render free + keep-awake hack + Aiven free | $0 | ⚠️ fragile | Cold starts, pinger dependency, policy risk |
| Render Background Worker + Aiven free | ~$7/mo | ✅ | Works, but dominated by a cheaper VPS |
| **Oracle Always Free ARM + local Postgres** | **$0** | ✅ | 4 ARM OCPU / 24GB RAM, 200GB disk; needs card at signup, capacity may require region retries; rare reclaim of *idle* instances (your poller is never idle); arm64 images all fine |
| Hetzner CX22 + local Postgres | ~€4.5/mo | ✅ | Bulletproof fallback; 2vCPU/4GB |
| GitHub Actions cron (public repo) | $0 | runs, but | 5–30+ min scheduling jitter — fails your latency goal; keep as a free failover scanner |
| Home PC | $0 | mostly | Fine if your home internet is reliable |

**Decision:** **One box, everything in Docker Compose: Oracle Always Free ARM as primary; Hetzner CX22 if Oracle signup/capacity fails.** Since your friend's entire role is DevOps, a single Linux box he owns is the natural and *simplest* deployment target — no PaaS abstractions to fight. Skip Render+Aiven entirely. (Exception: if your friend specifically wants to *learn Render*, the paid Background Worker + Aiven free path is architecturally sound — just accept $7/mo for a worse machine than a €4.5 VPS.)

---

## 3. Scale: 50–100 Orgs × ~100 Repos — Real API Math

You're right that repo count alone doesn't determine usage. With **org-level search, one query covers every repo in the org** — monitoring 10,000 repos costs the same as monitoring 50. Repo count only affects: (a) nightly discovery-sync pagination, (b) per-candidate deep-check REST calls, (c) any future per-repo polling.

**The only hard constraint: Search API = 30 req/min (authenticated). Sustained usage should stay ≤ ~20/min.**

| Orgs | Cycle | Search req/min | Verdict |
|---|---|---|---|
| 50 | 2 min | 25 | ❌ too close to the 30/min ceiling |
| 50 | 3 min | ~17 | ⚠️ OK with even spreading |
| 50 | 5 min | 10 | ✅ comfortable |
| 100 | 5 min | 20 | ✅ OK if spread evenly (1 query every 3s) |
| 100 | 10 min | 10 | ✅ very safe, but ~11 min latency |

Key conclusion: **"1–5 min detection for all 100 orgs" is mathematically impossible on one account via search.** The fix is tiering, not more polling:

**Recommended: tiered polling**
- **Tier 1 (priority): top 25 orgs → every 3 min** = 8.3 req/min
- **Tier 2: remaining 75 orgs → every 12 min** = 6.3 req/min
- **Total ≈ 15 search req/min** — safe margin under 30, even spreading via a token-bucket scheduler, per-org watermarks.

Resulting latency: **priority orgs ~2–5 min** (interval + query spread + GitHub search indexing lag of ~0.5–2 min), **long tail ~10–12 min**. If you'd rather have uniform coverage, run all 100 orgs at a 5-min cycle (~5–7 min latency for everything).

**REST budget (irrelevant at this scale):** nightly discovery sync ≈ 200–400 calls/day, deep-checks ≈ 50–300/day, hourly state refresh ≈ 200–1,000/day → **well under 5% of the 15,000/hr fine-grained quota.**

Two implementation details that matter:
1. Search results include issues from *all* repos in the org — **post-filter against your monitored-repo whitelist** (a DB set lookup) since issue search can't express a stars threshold.
2. If an org produces >100 new issues in one watermark window (only during catch-up after downtime), follow the `Link` header; otherwise it's 1 page.

**Decision:** Tiered search polling (25 orgs @ 3 min / 75 @ 12 min), token-bucket ≤ 20 search/min, org tiers and intervals in `orgs.yaml`. Uniform 5-min is the acceptable simpler default.

---

## 4. AI Pipeline — Cheapest Practical Design

**When to call the LLM (this is the whole cost story):** only for issues that pass hard filters AND rule-score ≥ notification threshold AND haven't been enriched before — i.e., **20–80 calls/day at your scale, called inline before notification (adds 2–5s)**. Never before the threshold; never for new/assigned/label checks (those are metadata — an LLM there is pure waste).

**Model: Gemini Flash via a Google AI Studio API key.** The free tier runs ~10–15 req/min and several hundred to ~1,500 requests/day depending on version — your 20–80/day uses <10% of it. **$0.** Design the client so the model is an env var (`AI_MODEL`) so you can swap to Claude Haiku / GPT-mini class (~$0.003–0.005/call → **<$4/mo worst case**) without code changes.

**Pipeline:**

```
poll → hard filters → whitelist → rule score ≥ threshold
     → LLM enrich (timeout 20s, 2 retries, strict JSON)
     → store {summary, verdict} in DB (cached, one call per issue)
     → Telegram message includes the 2-line summary + verdict
```

**Prompt contract (criteria versioned in a config file, not hardcoded):**

```json
// input: repo, stars, language, labels, assignee, linked_pr,
//        title, body[:3000]
// output schema:
{
  "summary": "≤2 lines, plain text",
  "worth_attempting": true|false,
  "reason_codes": ["good-fit"|"env-heavy"|"proprietary-tool"|
                   "huge-setup"|"unclear"|"docs-only"|"too-complex"],
  "difficulty": "easy|medium|hard|unclear"
}
```

System prompt contains your personal no-go criteria (huge local builds, GPU/cluster requirements, proprietary SDKs/hardware, Windows-only, your language preferences) — tunable without touching code.

**Guardrails (non-negotiable):**
- **Fail open:** if the LLM times out or returns garbage after retries, send the notification *without* the summary. AI must never gate or delay discovery.
- JSON repair retry once; then give up gracefully.
- One call per issue, cached in DB.
- Treat `worth_attempting: false` as a **demotion/annotation, not a drop** — LLM verdicts are noisy; always keep the issue link visible.

**Decision:** Gemini Flash free tier, called only on threshold-passing candidates, inline pre-notification, strict JSON, fail-open, criteria in config. Expected cost: $0.

---

## 5. Realistic Monthly Cost at Your Scale (50–100 orgs)

| Component | $0 / Free | Potentially paid |
|---|---|---|
| GitHub API | ✅ entirely free | never |
| Telegram | ✅ | never |
| AI (Gemini Flash free tier) | ✅ at 20–80 calls/day | $2–4/mo only if you outgrow free tier |
| PostgreSQL | ✅ (local Docker on your box) | — (Aiven/Neon/Supabase free also sufficient if you take the Render path) |
| Compute | ✅ Oracle Always Free ARM | ~€4.5 Hetzner / $7 Render worker |
| Backups (Backblaze B2, 10GB) | ✅ | — |
| Watchdog (Healthchecks.io) | ✅ | — |
| CI/CD (GitHub Actions + GHCR, public repo) | ✅ | — |
| Tailscale (dashboard/SSH access) | ✅ | — |
| Domain (optional) | — | ~$10/yr |

**Bottom line: $0/month on Oracle, or ~$5/month on Hetzner — and this number does not change between 50 and 100 orgs.** Your scale affects only API scheduling, never dollars. The single paid decision is "which box," and you only need one box until you're monitoring tens of thousands of orgs, which will never happen.

---

## 6. Two-Person Workflow: You (code + agent) + Friend (DevOps)

**Verdict: yes, this split is far better than A/B code branches.** A/B branches with one real coder buys you nothing but merge conflicts, two divergent half-tested designs, and a review burden on a friend who isn't deep in the code. **Split by concern, not by codebase.**

**Cleanest structure:**

**One app repo (public — free Actions minutes + free GHCR + trivial collaborator access):**
- `main` is always deployable; you + the coding agent work in short-lived branches → PRs.
- CI gate on every PR: `ruff` + `pytest` + Docker build. This is *essential* when a coding agent writes most of the code — the gate, not you, is what keeps `main` green.
- Tag releases (`v0.1.3`); CI builds and pushes the image to GHCR on tags.

**`/deploy/` directory in the same repo (or a separate infra repo if your friend prefers):**
- `docker-compose.yml`, backup script, Caddy/Tailscale config, `runbook.md` (restart, token rotation, DB restore), and `.env.example`.
- **The `.env.example` file is the entire contract between your two roles.** You define which env vars the app needs (`GH_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DATABASE_URL`, `AI_API_KEY`, `CONFIG_PATH`); he provides them. As long as that contract is stable, you never block each other.

**Deployment: self-hosted GitHub Actions runner on the box** (free, registered to your repo). Deploy workflow on a version tag: pull image → `docker compose up -d` → curl `/healthz` → auto-rollback to the previous tag on failure. This avoids SSH keys and secrets in CI entirely. (Lazy alternative: Watchtower auto-pulling the latest image — fine for v0, less control later.)

**Ownership split:**
- **You:** everything in the app repo — poller, scoring, AI, Telegram, dashboard, tests.
- **Friend:** the box (OS updates, Docker), the runner, `.env` on the box (chmod 600, never in git), nightly `pg_dump` → B2 + a monthly restore drill, Healthchecks watchdog, Tailscale, token rotation calendar.
- **Rule:** if he ever touches app code, it's a PR that *you* review (even in 2 minutes); if you ever touch `/deploy/`, it's a PR that *he* reviews.

No A/B branches, no shared long-lived branches, no both-of-you-in-main-daily.

---

## 7. Final Recommended Architecture (one, decisive)

```
ONE BOX: Oracle Always Free ARM (fallback: Hetzner CX22 ~€4.5/mo)
Docker Compose — all arm64 images
┌────────────────────────────────────────────────────────────┐
│ worker (Python 3.12, asyncio, httpx)                       │
│  ├─ Job A (nightly): search/repositories per org           │
│  │    → whitelist (stars/activity filters) → repos table   │
│  ├─ Job B (tiered): search/issues per org                  │
│  │    priority 25 orgs @ 3 min / 75 orgs @ 12 min          │
│  │    (token bucket ≤ 20 search/min, per-org watermarks,   │
│  │     60s overlap, DB dedupe)                             │
│  │    → hard filters → whitelist → rule score              │
│  ├─ deep-check (candidates only): timeline REST →          │
│  │    linked PR / assignee confirmation                    │
│  ├─ enrich (candidates only): Gemini Flash (free)          │
│  │    → 2-line summary + worth-attempting verdict (JSON,   │
│  │      fail-open, cached)                                 │
│  ├─ notify: Telegram (instant ≥85, digest 70–84,           │
│  │    chat_id allowlist)                                   │
│  └─ Job C (hourly): state refresh of recent high-score     │
│       issues (assignment/taken transitions)                │
├─ postgres:16 (local volume, ~30–60MB/mo, prune >90d,       │
│               nightly pg_dump → Backblaze B2)              │
├─ dashboard (phase 2): FastAPI + HTMX, via Tailscale only   │
├─ github-actions-runner (self-hosted) → deploys on tags      │
└─ healthcheck ping each cycle → Healthchecks.io (free)      │
                                                             
Secrets: .env on box only — fine-grained GH PAT (public,
read-only, 90-day rotation), Telegram bot token, Gemini key.
```

Repos: **one public app repo** (you + agent, CI-gated, tags) with **`/deploy/` owned by your friend**, deployed by a self-hosted runner.

---

## Misconceptions Explicitly Flagged

1. **"Render free can run the bot 24/7" — wrong.** Free tier = web services only, which sleep after 15 min; background workers and cron jobs are paid-only; free Postgres is a 30-day trial. The free path is a keep-awake hack with a third-party single point of failure on your *core* feature.
2. **"Aiven is needed" — not in the recommended setup.** Aiven free is a decent DB, but running Postgres in Docker on the same box removes an external dependency, a network hop, and connection limits — and it's equally free.
3. **"A dummy GitHub account is safer" — unnecessary.** A fine-grained public read-only PAT has near-zero blast radius; a second personal account is a ToS gray area and one more thing to maintain.
4. **"1–5 min detection for all orgs at 100 orgs" — infeasible on one token.** Search caps at 30 req/min; tier it (fast lane for priority orgs) or accept ~5–12 min for the long tail.
5. **"LLM per issue" — wasteful.** Only threshold-passing candidates (20–80/day) get LLM treatment, and it must fail-open so AI never blocks a notification.
6. **"Split the code A/B between two people" — wrong model.** One coder + one infra owner; split by concern, keep `main` deployable, interface via `.env` contract.
7. **(Correct instinct, confirmed):** repo count does *not* drive search cost — org-level queries make 10,000 monitored repos as cheap as 50. Repo count only touches nightly sync, deep-checks, and any future per-repo polling.

---

## Quick Reference

- **Recommended setup:** Oracle Always Free ARM (or Hetzner CX22) · Docker Compose (worker + local Postgres) · fine-grained PAT on main account (public read-only) · tiered search polling (25 orgs @ 3 min / 75 @ 12 min) · Gemini Flash free tier for candidate summaries · Telegram notify · self-hosted Actions runner deploys · Healthchecks watchdog + B2 backups.
- **Monthly cost:** **$0** (Oracle) or **~$5** (Hetzner). AI $0 within free tier. Nothing else costs money.
- **Expected latency:** priority orgs ~2–5 min; remaining orgs ~10–12 min (uniform 5-min cycle gives ~5–7 min everywhere, if you prefer simplicity over tiering).
- **Exact first steps, in order:**
  1. **Friend (today):** provision the box (try Oracle ARM across a couple of regions; fall back to Hetzner), install Docker, register a self-hosted Actions runner.
  2. **You (today):** create the fine-grained PAT; run the 100-line spike from the previous plan against 5 orgs to validate discovery + search + scoring output.
  3. **Both (this week):** write `.env.example` + `docker-compose.yml` + `runbook.md`, push the app repo skeleton, deploy `v0.1` — then you build the poller/scoring/Telegram pipeline against a live box from day one, and your friend never waits on your code to set up infra.