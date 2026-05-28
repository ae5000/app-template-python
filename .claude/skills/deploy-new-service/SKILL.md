---
name: deploy-new-service
description: Use when creating a new service for the bgrx platform and deploying it to production. Triggers include "create a new service", "add a new API", "deploy a new service", "set up a new microservice", "how do I add a service to the platform", "scaffold a service".
---

# Deploying a New Service to bgrx Platform

## Overview

New services are Python FastAPI apps that self-register with the platform registry via `platform_sdk`. Once registered, they appear in the portal, get a subdomain, and are accessible via the CLI.

---

## Step 1 — Create the service repo

Use `app-template-python` as the starting point:

```bash
# Copy the template or use it as a GitHub template repo
cp -r /path/to/app-template-python /path/to/your-service
cd your-service
```

Template contains:
- `main.py` — FastAPI app with platform SDK wired in
- `platform.yaml` — service metadata and access control
- `requirements.txt` — includes `platform-sdk`
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
```

`name` becomes the subdomain: `https://your-service-name.{domain}`.

---

## Step 3 — Write the service (`main.py`)

Minimal pattern:

```python
from fastapi import FastAPI, Depends
from platform_sdk import init_platform, current_user, PlatformUser

app = FastAPI(title="your-service-name")
init_platform(app)  # registers with platform-registry, wires auth middleware

@app.get("/items", operation_id="list_items", summary="List items")
async def list_items(user: PlatformUser = Depends(current_user)):
    return []
```

Key rules:
- Always call `init_platform(app)` — this handles heartbeating and auth middleware
- Always use `Depends(current_user)` on protected routes
- Always set explicit `operation_id` on every route — this becomes the CLI command name. Auto-generated IDs are ugly (`list_items_items_get`)
- `/health` is provided by the SDK automatically — don't define it yourself

For group-restricted operations:

```python
@app.delete("/items/{id}", operation_id="delete_item")
async def delete_item(id: str, user: PlatformUser = Depends(current_user)):
    user.require_group("engineering")  # 403 if user not in group
    ...
```

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

## Step 5 — First deploy

First deploy must be done manually via the platform-infra deploy script (CI can't create a service that doesn't exist yet):

```bash
cd /path/to/platform-infra
bash scripts/deploy-service.sh your-service-name
```

This script:
1. Reads manager IP and registry from terraform outputs
2. Creates the Docker Swarm service on the `platform_platform-internal` network
3. Connects to `platform_platform-egress` network (for outbound internet access)
4. Passes `PLATFORM_TOKEN`, `PLATFORM_REGISTRY_URL`, `PLATFORM_DOMAIN` as env vars

After this, all future deploys happen automatically via GitHub CI on push to `main`.

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

The service appears in the portal within ~30 seconds of the first successful heartbeat. If it shows as "down", the service started but `init_platform(app)` isn't running or can't reach the registry.

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
| Service shows "down" in portal | Not heartbeating | `init_platform(app)` not called, or registry unreachable |
| 403 on all routes | Auth middleware not wired | `init_platform(app)` must be called before routes are added |
| Routes not accessible | Service not in `require_any_group` | Check `platform.yaml` access config |
| CI deploys to wrong service | Repo name ≠ service name | Set `SWARM_SERVICE_NAME` repo variable |
| Service not visible in portal | Not in user's groups | User must be in one of `require_any_group` groups |
| 0 replicas after deploy | Image pull failed | Check DOCR auth, confirm image exists in registry |
| Health check failing | `/health` returning non-200 | platform_sdk provides `/health` automatically — don't override it |
