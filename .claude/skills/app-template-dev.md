---
name: app-template-dev
description: Use when developing features for this app-template-python project — building pages, API routes, macros, or Alpine.js components. Covers the full stack: FastAPI + Jinja2 + Alpine.js + platform SDK.
---

# app-template-python Development Guide

This project is a FastAPI app that plugs into the bgrx platform. It has a full HTML UI (server-rendered Jinja2 + Alpine.js reactive store) and a JSON API. Both share the same routes — UI routes return HTML, API routes return JSON.

## Stack

- **FastAPI** — ASGI app with Jinja2Templates and StaticFiles
- **platform_sdk** — `init_platform(app)`, `current_user` dependency, `PlatformUser` dataclass
- **Alpine.js 3** — reactive UI via `$store.app` (loaded from CDN, integrity-pinned)
- **Jinja2** — server-rendered HTML, macros in `templates/macros/`
- **WebSockets** — per-tab channels for live server-push patches

## Project Layout

```
main.py                          # FastAPI app, all routes, WS channel logic
static/
  app.js                         # Alpine store init, applyPatch, channel manager
  app.css                        # Design system CSS variables and components
templates/
  base.html                      # Shell: rail nav + left nav + topbar + content
  base_no_nav.html               # base.html variant — no left nav
  base_minimal.html              # base.html variant — no nav or topbar
  base_wide.html                 # base.html variant — full-width content
  macros/
    layout.html                  # page_header, section_header, empty_state, card
    ui.html                      # badge, live_table, card_list
    forms.html                   # text_input, select, submit_btn, create_form
  pages/
    items.html                   # List page example
    item_detail.html             # Detail page example
tests/
  conftest.py                    # TestClient fixture, reset_items/reset_channels autouse
  test_api.py                    # API route tests
  test_channel.py                # WebSocket channel tests
  test_ui.py                     # HTML route tests
```

## Platform SDK

```python
from platform_sdk import init_platform, current_user, PlatformUser

init_platform(app)  # call once after creating FastAPI app

# Dependency — injects authenticated user into any route
async def my_route(user: PlatformUser = Depends(current_user)):
    user.user_id    # str
    user.email      # str
    user.groups     # list[str]
    user.require_group("engineering")  # raises 403 if not in group
```

Set `SKIP_PLATFORM_AUTH=true` in env to bypass auth in dev/tests. Mock user gets `user_id="dev_user"`, `email="dev@local"`, `groups=["admin", "engineering"]`.

## ROOT_PATH

Apps may run at a subpath (e.g. `/my-app`) or subdomain. ROOT_PATH is set via env var:

```python
ROOT_PATH = os.getenv("ROOT_PATH", "")
app = FastAPI(title="my-service", root_path=ROOT_PATH)
```

In templates, always use `request.root_path` for links and asset URLs:
```html
<link href="{{ request.root_path }}/static/app.css?v={{ static_version }}"/>
<a href="{{ request.root_path }}/">Home</a>
```

In Alpine JS, use `window.__CONFIG__.rootPath` (not `CONFIG.rootPath` — `const` at top level does NOT attach to `window`):
```js
@click="window.location.href = window.__CONFIG__.rootPath + '/items/' + item.id"
```

## Static Cache-Busting

`_STATIC_VERSION` is computed at startup from `app.js` mtime. Append `?v={{ static_version }}` to every static asset URL in templates. This busts browser cache when files change.

```python
_STATIC_VERSION = str(int(os.path.getmtime("static/app.js")))
```

## UI Routes

Every UI route must pass these context keys:

```python
return templates.TemplateResponse(request, "pages/my_page.html", {
    "user": _user_ctx(user),          # required by base.html
    "static_version": _STATIC_VERSION, # required for cache-busting
    "debug_show_state": _DEBUG_SHOW_STATE,  # required for debug modal
    # ... your page-specific data
})
```

**IMPORTANT:** `TemplateResponse` takes `request` as first positional arg (Starlette ≥ 0.21). Old signature `TemplateResponse(name, context)` is deprecated and will warn.

## Template Blocks

`base.html` exposes these blocks to override in child templates:

| Block | Purpose |
|---|---|
| `page_title` | `<title>` content |
| `nav_title` | Left nav header text |
| `left_nav` | Left nav item list (Alpine, reads `$store.app`) |
| `topbar_title` | Topbar app name |
| `topbar_breadcrumb` | Topbar breadcrumb (after app name) |
| `topbar_right` | Topbar right side (badges, actions) |
| `body` | Main page content |
| `nav_panel` | Override to remove left nav entirely |
| `topbar_panel` | Override to remove topbar entirely |
| `extra_head` | Extra `<head>` content |

## Macros

Jinja2 does NOT support `{% from "file.html" import * %}`. Every macro must be named explicitly.

**layout.html:** `page_header`, `section_header`, `empty_state`, `card`
**ui.html:** `badge`, `live_table`, `card_list`
**forms.html:** `text_input`, `select`, `submit_btn`, `create_form`

```jinja
{% from "macros/layout.html" import page_header, section_header, empty_state %}
{% from "macros/ui.html" import badge, live_table %}
{% from "macros/forms.html" import create_form %}
```

## Alpine Store

Shape defined in `static/app.js` — `window.initAppStore()` is called by `base.html` before Alpine loads:

```js
{
  ui:      { navItem: null, navCollapsed: false, progress: null, debugOpen: false },
  user:    { id, name, initials, email },   // populated from window.__USER__ (SSR'd)
  items:   [],                               // populated via fetch on init
  channel: { id: null, wsStatus: "disconnected" },
}
```

To add a new store key for your resource, add it to the store object in `initAppStore()` and populate it with a `fetch` call alongside `items`.

### Mutating the store — CRITICAL

**All mutations must go through `Alpine.store("app")`** (the reactive Proxy). Never capture the raw store object in a closure and mutate that.

```js
// WRONG — breaks reactivity, DOM won't update
const store = Alpine.store("app");
store.items = newItems;   // ← raw closure reference, not the proxy

// CORRECT
Alpine.store("app").items = newItems;
applyPatch(Alpine.store("app"), { op: "set", path: "items", value: newItems });
```

This applies to ALL async callbacks: `ws.onmessage`, `Promise.then`, `setTimeout`, etc.

## WebSocket Channels (Per-Tab Live Push)

Each browser tab gets its own WS channel. The server pushes JSON patches to the tab that triggered the mutation.

**Flow:**
1. Page load → `createChannel()` → `POST /ws/channel` → returns `channel_id`
2. `openChannelWS(channelId)` → `WS /ws/channel/{channel_id}`
3. API calls include `X-Channel-Id: <id>` header
4. Server calls `await push(channel_id, patch)` to send patches

**Adding push to an API route:**

```python
@app.post("/api/things")
async def create_thing(
    thing: Thing,
    user: PlatformUser = Depends(current_user),
    channel_id: str | None = Header(None, alias="X-Channel-Id"),
):
    thing_id = str(uuid.uuid4())[:8]
    _things[thing_id] = thing.model_dump()
    result = {"id": thing_id, **_things[thing_id]}
    await push(channel_id, {"op": "add", "path": "things", "value": result})
    return result
```

**`applyPatch` operations:**

| op | effect |
|---|---|
| `set` | `parent[field] = value` |
| `add` | append to array, or merge into object |
| `update` | find by `id` (or `key`) in array, merge `value` |
| `remove` | filter out by `id` (or `key`) from array |
| `merge` | shallow merge into object |
| `inc` | increment number by `by` (default 1) |
| `prepend` | prepend `value` to array |
| `append-log` | append to array, keep last 500 |

## Detail Page Pattern

Detail pages read from the store (for live updates) with SSR fallback for items not yet loaded or paginated out:

```jinja
<div x-data='{
  get item() {
    return $store.app.items.find(i => i.id === {{ item.id | tojson }})
           || {{ item | tojson }};
  }
}'>
  <p x-text="item.name"></p>
</div>
```

The `x-for` variable name and the Jinja template variable MUST be different to avoid confusion. Use `it` or `entry` if the Jinja variable is `item`:

```jinja
<template x-for="it in $store.app.items" :key="it.id">
  <div :class="{'active': it.id === {{ item.id | tojson }}}"
       @click="window.location.href = window.__CONFIG__.rootPath + '/items/' + it.id">
    <span x-text="it.name"></span>
  </div>
</template>
```

## `create_form` Macro

Renders a complete create form backed by the `platformForm` Alpine component. Automatically sends `X-Channel-Id` header when a WS channel is open.

```jinja
{{ create_form(
    action="/things",
    fields=[
        {"name": "name",  "label": "Name",  "placeholder": "Enter name"},
        {"name": "value", "label": "Value", "type": "text"},
    ],
    submit_label="Create Thing"
) }}
```

The `action` is relative to `apiBase` (`/api`), so `/things` → `POST /api/things`.

## CSS Variables

All variables are defined in `static/app.css` `:root`. Only these exist — do not reference others:

| Category | Variables |
|---|---|
| Backgrounds | `--bg0` (darkest) → `--bg5` (lightest) |
| Borders | `--bd`, `--bdh`, `--bdhl` (light) |
| Text | `--t0` (brightest) → `--t3` (dimmest) |
| Colors | `--green`, `--red`, `--blue`, `--orange`, `--purple`, `--yellow`, `--pink` |
| Typography | `--f` (Outfit, sans), `--mono` (JetBrains Mono) |
| Radius | `--r` (8px), `--rlg` (12px), `--rxl` (16px) |
| Layout | `--rail` (52px), `--nav` (236px) |
| Animation | `--ease` (0.16s ease) |

**There is no `--accent`, `--primary`, `--surface`, or any other variable.** Using an undefined variable silently renders as transparent/initial — see Gotcha #11.

## Testing

Run tests with the venv that has `platform_sdk` installed:

```bash
SKIP_PLATFORM_AUTH=true .venv312/bin/pytest tests/ -q
```

The `conftest.py` has:
- `client` fixture — `TestClient(app)` with lifespan
- `reset_items` / `reset_channels` — `autouse=True` fixtures that clear in-memory stores before each test

Tests import `main.py` which imports `platform_sdk`. Set `SKIP_PLATFORM_AUTH=true` before import to inject mock user; `conftest.py` does this in module scope:

```python
import os
os.environ["SKIP_PLATFORM_AUTH"] = "true"
```

---

## Known Bugs / Critical Gotchas

### 1. `x-data` double-quote attribute break (MOST COMMON)

**Symptom:** Alpine errors `Unexpected token '}'`, `form is not defined`, component silently broken.

**Cause:** `x-data="..."` uses double-quote delimiters. Any `{{ var | tojson }}` inside injects JSON with double-quotes, terminating the attribute early.

```html
<!-- BROKEN: HTML parser stops at first " inside the attribute -->
<div x-data="{ item: {{ item | tojson }} }">

<!-- FIXED: single-quote outer attribute -->
<div x-data='{ item: {{ item | tojson }} }'>
```

This applies to ANY `x-data` that contains Jinja-interpolated JSON. Also applies to `create_form` macro — it uses single-quote outer attribute for this reason.

### 2. Alpine reactivity bypass

**Symptom:** Store data changes (API call succeeds, WS patch received) but DOM never updates.

**Cause:** Capturing raw store object before the reactive Proxy is ready, or passing raw object to async callbacks.

```js
// BROKEN — `store` is the raw object, mutations don't trigger Alpine
const store = Alpine.store("app");
ws.onmessage = (e) => { applyPatch(store, msg); };

// FIXED — always re-read Alpine.store() inside the callback
ws.onmessage = (e) => { applyPatch(Alpine.store("app"), msg); };
```

### 3. `const CONFIG` not on `window`

**Symptom:** `CONFIG is not defined` in Alpine expressions or inline event handlers.

**Cause:** `const CONFIG = {...}` at top of `app.js` does NOT attach to `window`, unlike `var`. Alpine expressions and inline `@click` handlers cannot see it.

**Fix:** Use `window.__CONFIG__` (which IS set via `window.__CONFIG__ = {...}` in `base.html`):

```js
// In Alpine expressions:
@click="window.location.href = window.__CONFIG__.rootPath + '/items/' + it.id"
```

### 4. Jinja `import *` not supported

**Symptom:** `TemplateSyntaxError: expected token 'name', got '*'`

**Fix:** Import every macro by name:
```jinja
{% from "macros/layout.html" import page_header, section_header, empty_state %}
```

### 5. Wrong macro file

`empty_state` and `section_header` live in `layout.html`, NOT `ui.html`. `badge`, `live_table`, `card_list` are in `ui.html`. Importing from the wrong file causes `UndefinedError` at render time.

### 6. WS channel cleanup must be in `finally`

```python
# WRONG — leak if websocket errors without WebSocketDisconnect
try:
    while True:
        await websocket.receive_text()
except WebSocketDisconnect:
    _channels.pop(channel_id, None)

# CORRECT
try:
    while True:
        await websocket.receive_text()
except WebSocketDisconnect:
    pass
finally:
    _channels.pop(channel_id, None)
```

### 7. Detail page missing `left_nav` block

**Symptom:** Nav sidebar is empty on detail pages.

**Fix:** Detail pages must define `{% block left_nav %}` with the items list. The list page's `left_nav` block is NOT inherited automatically — each child template must define it.

### 8. `x-for` variable shadows Jinja template variable

When the Jinja template variable and the Alpine `x-for` variable have the same name (e.g., both called `item`), it's confusing even though there's no actual conflict (Jinja runs server-side, Alpine runs client-side). Use a distinct name in `x-for`:

```jinja
{# item = Jinja variable for current item #}
<template x-for="it in $store.app.items">  {# use "it" not "item" #}
  <div :class="{'active': it.id === {{ item.id | tojson }}}">
```

### 9. `TemplateResponse` deprecated signature

```python
# DEPRECATED (warns in Starlette ≥ 0.21)
return templates.TemplateResponse("page.html", {"request": request, ...})

# CORRECT
return templates.TemplateResponse(request, "page.html", {...})
```

### 11. Undefined CSS variable renders silently invisible

**Symptom:** Element has correct size and position but is invisible (e.g., progress bar fill, badge background, icon color). No browser error.

**Cause:** CSS `var(--undefined-name)` falls back to `initial`, which for `background` is `transparent` and for `color` is inherited. No warning anywhere.

**Common victim:** `background:var(--accent)` — `--accent` does NOT exist in this project. Use `--blue`, `--green`, etc. from the variables table above.

```css
/* WRONG — --accent is not defined, renders transparent */
background: var(--accent);

/* CORRECT — use a real variable */
background: var(--blue);
```

When a visual element disappears unexpectedly: check the variable name against the CSS variables table before debugging Alpine or JS.

### 10. `wsBase` XSS via Host header

Never interpolate `request.headers.get("host")` directly into a `<script>` block. Wrap the full expression in `| tojson`:

```jinja
{# WRONG #}
wsBase: "ws://{{ request.headers.get('host') }}{{ request.root_path }}",

{# CORRECT — tojson escapes the whole string #}
wsBase: {{ ("ws://" + request.headers.get("host", "localhost") + request.root_path) | tojson }},
```
