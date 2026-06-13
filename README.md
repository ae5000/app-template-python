# app-template-python

Starting point for new bgrx platform services. Includes auth middleware, platform registration, WebSocket support, and a UI scaffold.

---

## Creating a new service from this template

### 1. Clone and detach from template history

```bash
git clone https://github.com/ae5000/app-template-python.git your-service-name
cd your-service-name
rm -rf .git
git init
git add .
git commit -m "Initial commit: your-service-name"
```

### 2. Update `platform.yaml`

```yaml
service:
  name: your-service-name    # must be unique — becomes the subdomain
  group: engineering
  description: "What this service does"
  owners:
    - your-email@example.com
```

### 3. Update `main.py`

Change the FastAPI title to match your service name:

```python
app = FastAPI(title="your-service-name", root_path=ROOT_PATH, lifespan=lifespan)
```

### 4. Create the GitHub repo

Go to [github.com/new](https://github.com/new) and create a new **private** repo under the `bgrx-ai` org:
- Owner: `bgrx-ai`
- Name: `your-service-name` (match `platform.yaml` exactly to avoid needing `SWARM_SERVICE_NAME`)
- No README, no .gitignore (you already have them)

Then push:

```bash
git remote add origin https://github.com/bgrx-ai/your-service-name.git
git push -u origin main
```

### 5. First deploy (manual)

CI deploys on push to `main` but requires the Swarm service to already exist. Create it once:

```bash
cd /path/to/platform-infra
bash scripts/deploy-service.sh your-service-name
```

After this, every push to `main` auto-deploys via GitHub Actions.

### 6. Verify

```bash
curl https://your-service-name.bgrx.win/health
```

Service appears in the portal within ~30s of the first successful heartbeat.

---

## How auth works

All inbound requests pass through Traefik → auth-service before reaching your service. Auth-service validates the user and injects a signed `X-Platform-Auth` header. `platform_auth.py` reads that header and makes the user available via `Depends(current_user)`.

For service-to-service calls, use `PlatformClient` — it forwards the user context automatically:

```python
from platform_auth import PlatformClient

client = PlatformClient("other-service")
resp = await client.get("/items", user=user)
```

For local development, set `DEV_MOCK_USER=email:group1,group2` to bypass auth:

```bash
DEV_MOCK_USER=dev@local:engineering,admin uvicorn main:app --reload
```

---

## Project structure

```
platform.yaml       service metadata and access control
platform_auth.py    auth middleware, registration, heartbeat, S2S client
main.py             FastAPI app — wire in platform_auth, add your routes
requirements.txt    dependencies (no private repos or build secrets needed)
Dockerfile          standard Python 3.12-slim image
.github/workflows/  CI — builds and pushes to DOCR, deploys to Swarm
templates/          Jinja2 HTML templates
static/             JS/CSS assets
```

---

## Common issues

| Symptom | Cause | Fix |
|---|---|---|
| Service shows "down" in portal | Not heartbeating | `platform_lifespan` not wired in, or registry unreachable |
| 401 on all routes | Middleware not added | `app.add_middleware(PlatformAuthMiddleware)` missing |
| CI deploy fails with "service not found" | First deploy not done | Run `deploy-service.sh` manually first |
| CI deploy fails with "Username and password required" | `DIGITALOCEAN_ACCESS_TOKEN` not set | Add secrets to repo or org |
| Repo name ≠ service name | Wrong Swarm service targeted | Set `SWARM_SERVICE_NAME` repo variable |
