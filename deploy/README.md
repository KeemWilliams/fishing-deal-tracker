# Deploying the Fishing Gear Deal Tracker

This app is not deployed yet. This doc describes the setup to create later,
mirroring the existing `govcon-scraper` Coolify app (same host, same pattern:
idle container + Coolify Scheduled Tasks, no long-running server process).

## Coolify application (mirrors `govcon-scraper`)

Read live from Coolify (`get_application`, uuid `tkl1n4f8fu1ysopv7g3vv5tg`) on
2026-09-12. Use these settings as the template for the new app:

| Setting | `govcon-scraper` value | This app |
|---|---|---|
| Server / destination | `apps-ovh-1` (10.50.1.120), Coolify standalone-docker destination `xq798qcq10vkvm0w7715qdo4` | same host |
| Git repository | `ssh://git@gitlab.helixstax.net:2289/wakeem/helix-stax.git` | same monorepo |
| Git branch | `feat/govcon-lead-discovery` (its feature branch) | this app's own branch, then `main` once merged |
| Base directory | `/apps/govcon-scraper` | `/apps/fishing-price-tracker` |
| Build pack | `dockerfile` | `dockerfile` |
| Dockerfile location | `/Dockerfile` | `/Dockerfile` (relative to base directory) |
| Start command | `tail -f /dev/null` | same — the container stays idle; Scheduled Tasks (below) `docker exec` into it to run `tick`/`export` |
| Health check | disabled | disabled (no HTTP server to probe) |
| Ports exposed | none | none |

Do not set `docker_registry_image_name`/`static_image` — build from the
Dockerfile in this directory, same as `govcon-scraper`.

## Environment variables (names and purpose only — no values here)

| Var | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string read by `fpt/db.py` and `deploy/migrate.py`. If unset, `tick` runs in dry-run/print mode instead of failing. |
| `FPT_SNAPSHOT_DIR` | Where raw HTTP response snapshots are written. Defaults to `/var/lib/fpt/snapshots` inside the image (already created for the non-root user in the Dockerfile) — override only if a mounted volume is wired up for snapshot retention. |
| `EXPORT_STORAGE_BACKEND` | `local` or `s3`, read by `fpt/export/storage.py::build_storage_from_env`. Use `s3` in production. |
| `EXPORT_LOCAL_DIR` | Only used when `EXPORT_STORAGE_BACKEND=local`. Not needed in production. |
| `EXPORT_S3_BUCKET` | Required when `EXPORT_STORAGE_BACKEND=s3`. The bucket the published feed (`meta.json`, `deals.json`, product pages) is written to. |
| `EXPORT_S3_ENDPOINT_URL` | S3-compatible endpoint (Cloudflare R2 or MinIO — see architecture doc §3.4). Optional for real AWS S3. |
| `EXPORT_S3_REGION` | Region for the bucket, if the endpoint requires one. |
| `EXPORT_S3_ACCESS_KEY_ID` / `EXPORT_S3_ACCESS_KEY_ID` fallback `AWS_ACCESS_KEY_ID` | R2/S3 access key. |
| `EXPORT_S3_SECRET_ACCESS_KEY` fallback `AWS_SECRET_ACCESS_KEY` | R2/S3 secret key. |
| `EXPORT_S3_KEY_PREFIX` | Optional key prefix inside the bucket, if the bucket is shared with other exports. |
| `PUBLIC_FEED_BASE_URL` | **Site-side, not this container.** Read by `site/src/lib/feed.ts` at build time (Astro `import.meta.env`). Required in the Cloudflare Pages production environment — see below. |

**Note on DB roles** — `db/migrations/011_roles_grants.up.sql` documents two
intended least-privilege roles, `fpt_job` (this container's tick/export
process) and `fpt_api` (a future public-facing API process), each meant to
connect with its own login/password provisioned outside migrations. The
current application code (`fpt/db.py`) only reads a single `DATABASE_URL`
env var. Until the app code is split to support per-role connection strings,
`DATABASE_URL` should point at a connection using the `fpt_job` role once
that role has `LOGIN` + a password set (see the `ALTER ROLE` commands in that
migration file). Flagging this rather than resolving it — `fpt/db.py` is
outside this task's scope.

## Scheduled tasks (create later, do not create now)

Two Coolify Scheduled Tasks, both targeting the idle container above via
`docker exec`. **Every task command must be wrapped in both `timeout` and
`flock -n`** — a stuck `run_once` execution overlapping with the next
scheduled run has already caused duplicate/overlapping HTTP requests to
retailers in this codebase's history (see `govcon-scraper`'s equivalent
lesson). Neither guard alone is suffient: `flock -n` prevents two runs of the
*same* task overlapping each other; `timeout` prevents a hung run from
holding that lock forever and starving every future scheduled invocation.

| Task | Schedule | Command (illustrative — adjust paths once the container is running) |
|---|---|---|
| `tick` | every 15 minutes | `flock -n /tmp/fpt-tick.lock timeout 600 python run.py tick` |
| `export` | every 15 minutes, offset to run after `tick` | `flock -n /tmp/fpt-export.lock timeout 300 python run.py export` |

`python run.py export --help` was verified against the built image on
2026-09-12 (`--dry-run` and `--force` flags exist; `--dry-run` builds and
validates the feed without writing to storage — useful for a pre-launch
smoke test that doesn't touch `EXPORT_S3_BUCKET`).

## Infisical paths to create

Following the per-agent secrets convention (`/agents/<name>` for
agent-scoped credentials, `/secret/<NAME>` for shared ones): create
`/agents/fishing-price-tracker` in the Helix Stax Infisical project holding:

- `DATABASE_URL`
- `EXPORT_S3_ACCESS_KEY_ID`
- `EXPORT_S3_SECRET_ACCESS_KEY`

Non-secret config (`EXPORT_STORAGE_BACKEND`, `EXPORT_S3_BUCKET`,
`EXPORT_S3_ENDPOINT_URL`, `EXPORT_S3_REGION`, `EXPORT_S3_KEY_PREFIX`) can be
set directly as plain Coolify app env vars — they are not secrets, and
keeping them out of Infisical means one less indirection to keep in sync
when the bucket/endpoint changes.

## Cloudflare Pages (site/)

- **Root directory**: `apps/fishing-price-tracker/site`
- **Build command**: `npm run build` (Astro — confirm against
  `site/package.json` `scripts.build` once the site package is finalized)
- **Output directory**: `dist` (Astro default)
- **Required production env var**: `PUBLIC_FEED_BASE_URL` — must point at
  wherever `EXPORT_S3_BUCKET`'s `latest/` prefix is publicly served (a public
  R2 bucket URL, a CDN in front of it, or a custom domain). The site fails
  closed without it: `site/src/lib/feed.ts` reads it via
  `import.meta.env.PUBLIC_FEED_BASE_URL`, and `PUBLIC_` is Astro's public-env
  prefix, so this value is exposed client-side by design — it must be a
  public read-only feed URL, never a credentialed one.

## Pre-launch checklist

- [ ] Security review HIGH findings closed
- [ ] `fpt_job` and `fpt_api` DB roles have `LOGIN` + password set (see role
      note above) and `DATABASE_URL` uses `fpt_job`, not a superuser
- [ ] `deploy/migrate.py` run successfully against the target database
      (`python deploy/migrate.py --status` shows all migrations applied)
- [ ] Infisical path `/agents/fishing-price-tracker` populated
- [ ] `EXPORT_S3_BUCKET` created and reachable from the Coolify host
      (apps-ovh-1 — confirm datacenter egress to the chosen R2/S3 endpoint)
- [ ] `PUBLIC_FEED_BASE_URL` set in the Cloudflare Pages **production**
      environment (not just preview)
- [ ] Scheduled tasks created with `flock -n` + `timeout` as documented above
- [ ] Confirm `fpt/export/runner.py`'s actual CLI invocation before wiring
      the `export` scheduled task (owned by a peer coder, in progress as of
      2026-09-12)
- [ ] Manual `tick --max-discovery-pages 1` dry run against production config
      before enabling the schedule
