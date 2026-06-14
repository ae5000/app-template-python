---
name: deploy-new-service
description: Use when creating a new service for the bgrx platform and deploying it to production. Triggers include "create a new service", "add a new API", "deploy a new service", "set up a new microservice", "how do I add a service to the platform", "scaffold a service".
---

# Deploying a New Service to bgrx Platform

## Overview

New services are Python FastAPI apps that self-register with the platform registry via `platform_auth.py`. Once registered, they appear in the portal, get a subdomain, and are accessible via the CLI and MCP.

---

## Step 1 — Create the service repo

Use `app-template-python` as the starting point:

```bash
# Copy the template or use it as a GitHub template repo
cp -r /path/to/app-template-python /path/to/your-service
cd your-service
```

Template contains:
- `main.py` — FastAPI app with platform auth wired in
- `platform_auth.py` — copy this into every service (no external SDK)
- `platform.yaml` — service metadata and access control
- `requirements.txt` — app dependencies (no platform-sdk needed)
- `Dockerfile` — standard Python 3.12-slim image
- `deploy-service.sh` — first-deploy helper (reads from platform-infra)

---

## Step 2 — Edit `platform.yaml`

```yaml
service:
  name: your-service-name    # must be unique, lowercase, hyphens ok
  group: engineering          # team that owns it
  description: "What this service does"
  owners:
    - your-email@example.com

runtime:
  port: 8000
  health_check: /health       # platform-registry polls this

expose:
  preset: all                 # expose all routes to platform
  access:
    require_any_group:        # who can see/call this service
      - engineering

# Optional: provision a dedicated Postgres database (see Step 4b)
# database:
#   postgres: true

# Optional: inject secrets from .env.secrets (see Step 4b)
# secrets:
#   - MY_API_KEY
#   - STRIPE_SECRET_KEY
```

`name` becomes the subdomain: `https://your-service-name.{domain}`.

---

## Step 3 — Write the service (`main.py`)

Minimal pattern:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from platform_auth import PlatformAuthMiddleware, platform_lifespan, current_user, PlatformUser

@asynccontextmanager
async def lifespan(app):
    async with platform_lifespan(app):  # registers with registry, starts heartbeat, auto-adds /health
        yield

app = FastAPI(title="your-service-name", lifespan=lifespan)
app.add_middleware(PlatformAuthMiddleware)

@app.get("/api/items", summary="List items")
async def list_items(user: PlatformUser = Depends(current_user)):
    return []
```

Key rules:
- `platform_lifespan(app)` — pass the app object so it auto-registers `/health` and sends OpenAPI schema to the registry on startup
- `PlatformAuthMiddleware` — validates `X-Platform-Auth` header on every request
- `Depends(current_user)` — injects authenticated user into protected routes
- **Do not define `/health` yourself** — `platform_lifespan(app)` registers it automatically
- Dev bypass: `DEV_MOCK_USER=email:group1,group2` skips auth with mock user

For group-restricted operations:

```python
@app.delete("/api/items/{id}")
async def delete_item(id: str, user: PlatformUser = Depends(current_user)):
    user.require_group("engineering")  # 403 if user not in group
    ...
```

## Step 3b — Expose routes to CLI + MCP

Routes are invisible to `bgrx` and Claude Desktop MCP by default. Add `openapi_extra`:

```python
@app.get(
    "/api/items",
    summary="List all items",
    openapi_extra={"x-platform": {
        "cli": {"command": "your-service list-items"}
        # "mcp" auto-derived: tool_name = "your-service_list-items"
    }},
)
```

Without this, `bgrx services` shows the service but `bgrx your-service --help` has no subcommands, and no MCP tools are registered. See `app-template-dev` skill for full `args` schema.

---

## Step 4 — Set up GitHub repo and CI

1. Create GitHub repo (in your org, not personal) under the `bgrx` org
2. Add the deploy workflow — copy `.github/workflows/deploy.yml` from `hello-service` or `auth-service`
3. Set required GitHub repo secrets/variables:

**Secrets** (set at org level or per-repo):
- `REGISTRY_NAME` — DOCR registry name (e.g. `registry.digitalocean.com/bgrx`)
- `SSH_PRIVATE_KEY` — SSH key for manager node

**Variables** (repo-level):
- `SWARM_SERVICE_NAME` — set ONLY if the repo name ≠ service name in `platform.yaml`. If repo is `my-service` and service name is `my-service`, skip this. If repo is `bgrx-my-service` but service name is `my-service`, set this to `my-service`.

CI deploys on every push to `main` via `docker service update --image ... --update-order start-first`.

---

## Step 4b — Configure database and secrets (optional)

If your service needs a Postgres database or external API secrets:

**1. In `platform.yaml`**, uncomment the relevant sections:

```yaml
database:
  postgres: true   # provisions a dedicated DB + user, stored as DATABASE_URL secret

secrets:
  - MY_API_KEY     # must have a matching entry in platform-infra/.env.secrets
  - STRIPE_SECRET_KEY
```

**2. In `platform-infra/.env.secrets`** (gitignored, create from `.env.secrets.example`):

```
YOUR_SERVICE_MY_API_KEY=sk-...
YOUR_SERVICE_STRIPE_SECRET_KEY=sk_live_...
```

Key naming: `{SERVICE_NAME_WITH_UNDERSCORES_UPPERCASE}_{VAR_NAME}` where hyphens in service name become underscores.

**3. Read secrets in your service** via `platform_auth.read_secret()`:

```python
from platform_auth import read_secret

DATABASE_URL = read_secret("DATABASE_URL")     # Postgres if database.postgres: true
MY_API_KEY   = read_secret("MY_API_KEY")       # from secrets: list
```

`read_secret()` reads `/run/secrets/<name>` in production, falls back to env var in local dev.

---

## Step 5 — First deploy

**If the service has no database or secrets** (platform.yaml has none of the optional sections):

```bash
cd /path/to/platform-infra
bash scripts/deploy-service.sh your-service-name
```

**If the service uses `database.postgres: true` or `secrets:`**, use `provision-service.sh` instead — it creates the Postgres DB, Swarm secrets, and the Docker service in one pass:

```bash
cd /path/to/platform-infra
# Ensure .env has POSTGRES_ADMIN_URL set (DO managed Postgres admin URL)
# Ensure .env.secrets has values for all secrets in platform.yaml
bash scripts/provision-service.sh your-service-name --service-dir=/path/to/your-service
```

`provision-service.sh` is idempotent — safe to re-run if interrupted. It skips any resource that already exists (checks via `docker secret inspect`).

After first deploy, all future image updates happen automatically via GitHub CI on push to `main`.

---

### What `provision-service.sh` does

1. Parses `platform.yaml` from `--service-dir`
2. If `database.postgres: true`:
   - Generates a random password
   - Creates Postgres user `svc_{service_prefix}` and database `{service_prefix}` (idempotent)
   - Stores `postgresql://svc_...@host/db?sslmode=require` as Swarm secret `{prefix}_db_url`
   - Mounts it at `/run/secrets/DATABASE_URL` in the container
3. For each name in `secrets:`:
   - Reads `{SERVICE_PREFIX}_{VAR_NAME}` from `.env.secrets`
   - Creates Swarm secret `{prefix}_{var_lower}`
   - Mounts it at `/run/secrets/{VAR_NAME}` in the container
4. Creates the Docker service with `--secret` flags for all secrets

---

## Step 6 — Verify

```bash
# Check service registered and healthy in portal
open https://portal.{domain}

# Health endpoint
curl https://your-service-name.{domain}/health

# Check swarm (from platform-infra):
ssh root@{MANAGER_IP} 'docker service ls | grep your-service-name'
```

The service appears in the portal within ~30 seconds of the first successful heartbeat. If it shows as "down", the service started but `platform_lifespan(app)` isn't running or can't reach the registry.

---

## Dockerfile

Standard — don't change unless you have a specific reason:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Services run on port `8000` internally. Traefik terminates TLS externally. Never expose ports directly in the swarm service definition.

---

## Common issues

| Symptom | Cause | Fix |
|---|---|---|
| Service shows "down" in portal | Not heartbeating | `platform_lifespan(app)` not called, or registry unreachable |
| "No server available" in Traefik | Service registered but health check fails | Missing `/health` — pass `app` to `platform_lifespan(app)` so it auto-registers it |
| 403 on all routes | Auth middleware not wired | Add `app.add_middleware(PlatformAuthMiddleware)` |
| Service not visible in portal | User not in `require_any_group` groups | Add user to `platform-infra/config/users.yaml`, redeploy auth-service with new Docker config version |
| `bgrx your-service --help` has no subcommands | Routes missing `x-platform` annotation | Add `openapi_extra={"x-platform": {"cli": {"command": "..."}}}` to each route |
| CLI/MCP see no routes after adding annotations | Service deployed with old OpenAPI | Redeploy the service — registry only gets updated OpenAPI on startup registration |
| CI deploys to wrong service | Repo name ≠ service name | Set `SWARM_SERVICE_NAME` repo variable |
| 0 replicas after deploy | Image pull failed | Check DOCR auth, confirm image exists in registry |
| Docker config update fails: `AlreadyExists` | Docker configs are immutable | Create new versioned config (e.g. `users-config-v2`), update service with `--config-rm old --config-add new` |
| `read_secret("X")` returns empty string | Secret not mounted | Check Swarm secret exists: `ssh manager 'docker secret ls'`; re-run `provision-service.sh` if missing |
| `read_secret("DATABASE_URL")` returns env var in prod | `/run/secrets/DATABASE_URL` not mounted | Confirm service was created with `provision-service.sh`, not `deploy-service.sh`; check `docker service inspect` for `Secrets` |
| `psql: command not found` during provisioning | psql client not installed | `brew install libpq && brew link --force libpq` |
| `ERROR: Secret value missing from .env.secrets` | Key not in secrets file | Add `{SERVICE_PREFIX}_{VAR}=value` to `platform-infra/.env.secrets` |
| `bgrx` CLI returns HTML instead of JSON | CF Access blocking programmatic requests | CLI calls go through `cli.bgrx.win/proxy/` — check cli-service is running |
| WebSocket connects as `ws://` not `wss://` | Traefik always receives HTTP internally | Fixed in template — `wsBase` uses `location.protocol` client-side, not server-side header |
