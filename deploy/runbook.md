# Optyra Runbook (DevOps)

One Linux box, one Docker Compose stack, outbound-only traffic. You own everything in
`deploy/` and the box; app code lives in the same repo but is not your concern. The
contract between the two roles is `.env` (see `deploy/.env.example`).

## 1. First deploy (~30 min)

1. Provision the box: Oracle Always Free ARM (try a couple of regions) or Hetzner CX22.
   Ubuntu 24.04 LTS, then: `curl -fsSL https://get.docker.com | sh`.
2. Get the code + config onto the box:
   ```bash
   sudo mkdir -p /opt/optyra && sudo chown $USER /opt/optyra
   # copy deploy/ and config/ from the repo (or clone the repo and symlink)
   cp deploy/.env.example /opt/optyra/.env && chmod 600 /opt/optyra/.env
   # edit /opt/optyra/.env: OPTYRA_IMAGE, POSTGRES_PASSWORD, GH_TOKEN, TELEGRAM_*
   cp -r config /opt/optyra/config   # curate config/orgs.yaml (tiers + gsoc_years)
   ```
3. Point `OPTYRA_IMAGE` at your GHCR image (pushed by the release workflow when you tag
   `v0.1.0`). If the repo is private, `docker login ghcr.io` with a PAT that has
   `read:packages`.
4. Bring it up: `cd /opt/optyra && docker compose up -d` (compose file lives in
   `deploy/`, so: `docker compose -f deploy/docker-compose.yml up -d`).
5. Verify: `curl -s localhost:8080/healthz | jq` — expect `"status": "ok"` and a
   `last_sweep_at` that advances every few minutes. Check logs:
   `docker compose logs -f worker`.
6. Telegram: message your bot once from your account, then confirm instant alerts
   arrive for a high-scoring issue (or lower `notify.instant_threshold` temporarily in
   `config/config.yaml` to test).
7. Watchdog: create a free Healthchecks.io check with a ~15 min period, paste the ping
   URL into `.env` as `HEALTHCHECK_URL`, `docker compose up -d` again.

## 2. Updates (on version tags)

- Preferred: self-hosted runner (`.github/workflows/deploy.yml` runs on tag push):
  register a runner on the box (`Settings → Actions → Runners → New self-hosted runner`,
  label `optyra`), and deploys become `git tag v0.x.y && git push --tags` from the app side.
- Manual equivalent:
  ```bash
  cd /opt/optyra
  docker compose pull worker && docker compose up -d worker
  curl -s localhost:8080/healthz
  ```
- Rollback: set `OPTYRA_IMAGE=ghcr.io/<owner>/optyra:<previous-tag>` in `.env`, then
  `docker compose up -d worker`. Postgres schema is additive; rolling back one version is safe.

## 3. Backups

- Nightly: `deploy/backup.sh` (cron line in the script header) dumps `pg_dump | gzip`
  with retention. Do a **monthly restore drill** into a scratch database — a backup you
  haven't restored is a rumor.
- Optional off-site: rclone + Backblaze B2 (`OPTYRA_RCLONE_REMOTE`).

## 4. Token rotation (calendar this)

| Token | Where | Rotation |
|---|---|---|
| GitHub fine-grained PAT | `.env` `GH_TOKEN` | 90 days; create new, swap, restart worker, revoke old |
| Telegram bot token | `.env` `TELEGRAM_BOT_TOKEN` | only if leaked; rotate in @BotFather |
| Gemini API key | `.env` `AI_API_KEY` | only if leaked; free tier |
| Postgres password | `.env` `POSTGRES_PASSWORD` | set at install; rotate with a maintenance window |

After any `.env` change: `docker compose up -d` (recreates the worker with new env).

## 5. Health & troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| `/healthz` down | `docker compose ps`, `docker compose logs worker` | worker crash → `docker compose up -d worker`; check Postgres healthy |
| `last_sweep_polled: 0` for long | worker logs | all orgs breaker-tripped? look for rate-limit warnings |
| `github_rate_remaining` low/0 | logs for `Retry-After` / reset sleeps | expected during catch-up; if persistent, raise tier2 interval |
| Telegram silent | logs `telegram send rejected` | wrong chat_id/token; bot must be started by the user first |
| DB filling up | `docker compose exec postgres psql -U optyra -c '\l+'` | pruning runs daily (90d retention); check `maintenance` logs |
| "another optyra worker already holds the advisory lock" | two workers running | `docker compose ps` — scale worker to exactly 1 |

Security posture: Postgres has no published port; healthz is loopback-only (reach over
Tailscale/SSH); secrets only in `.env` (chmod 600); all traffic is outbound HTTPS.
