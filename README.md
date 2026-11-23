# Gemini Studio API

A small OpenAI-style API for two local execution paths:

- **Gemini Web** is the default. It drives a dedicated signed-in Gemini browser profile.
- **Agy** is explicit. It runs the local Antigravity CLI with exact model IDs from `agy models`.

The service is optimized for occasional agent calls. FastAPI stays resident,
but Chromium and Playwright are started only when Gemini Web receives work and
are closed after 60 idle seconds.

## API

`POST /v1/chat/completions` accepts OpenAI-style `messages`.

If `backend` is omitted, the request uses Gemini Web with:

- model: `neon`
- thinking level: `Medium`

Gemini Web supports only:

- models: `hydrogen`, `neon`
- thinking levels: `Low`, `Medium`, `High`
- text and base64 `data:image/...` image inputs

Example:

```bash
curl -sS http://127.0.0.1:8000/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{
    "messages": [
      {"role": "user", "content": "Review this design and list the three biggest risks."}
    ]
  }'
```

Select Hydrogen or another reasoning level only when needed:

```json
{
  "model": "hydrogen",
  "thinking_level": "High",
  "messages": [{"role": "user", "content": "Review this quickly."}]
}
```

### Agy

Agy has a separate model namespace. List current IDs with:

```bash
agy models
```

Then pass one exact ID:

```json
{
  "backend": "agy",
  "model": "gpt-oss-120b-medium",
  "messages": [{"role": "user", "content": "Review this."}]
}
```

Do not send `thinking_level` with Agy. Reasoning is defined by the selected Agy model. Agy does not accept image attachments through this API.

Useful endpoints:

- `GET /v1/models` — Gemini Web models and defaults
- `GET /v1/backends` — backend health and current Agy model IDs
- `GET /health` — lightweight service health
- `GET /v1/diagnostics` — browser/Agy state and recent request metadata

For long calls, set `"async": true`. The submit request returns HTTP 202 with a `request_id`; poll `GET /v1/chat/completions/{request_id}`.

## Ubuntu setup

The host needs Python 3.14+ and `agy` if the Agy backend is wanted. Gemini
Web also uses X11 utilities for its visible RDP window and private no-RDP
fallback. On Ubuntu, install them once:

```bash
sudo apt install xvfb x11-utils xdotool
```

Then install the Python/browser runtime:

```bash
cd "$HOME/Gemini Studio API"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/playwright install chromium
```

Playwright uses its matching Chromium build and dedicated state under
`.browser_session/profile/`. That dedicated Chromium profile is reused across
browser launches so Google can refresh its normal login state over time. The
normal Chrome profile is never automated.

Chromium 158/159 on this Ubuntu host has one verified relaunch defect: keeping
`Default/Preferences` from the prior run makes the next launch crash with an
FD-ownership SIGTRAP. Before each cold start the worker removes only that
generated preference file plus transient lock/DevTools files. Cookies, account
databases, local storage, IndexedDB, and the rest of the dedicated profile stay
in place. Six consecutive close/relaunch cycles passed with this workaround.

On the first Gemini request, a visible Chromium window opens. If Gemini shows a
sign-in page, sign in once in that window and retry the request. The successful
retry uses the same dedicated profile on later launches.
While sign-in is required the API reports HTTP 503 and keeps that dedicated
window open for 15 minutes when a visible desktop is available. If authentication
expires while no desktop session exists, the private virtual display is released;
connect RDP and retry so the sign-in window is visible.

## Run

Foreground:

```bash
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000 --no-access-log
```

For the South Server user service:

```bash
mkdir -p ~/.config/systemd/user
cp ops/gemini-studio-api.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gemini-studio-api.service
systemctl --user status gemini-studio-api.service
```

The unit starts at boot through the user systemd manager and uses `Restart=always`.
Chromium is still lazy: when a request arrives it refreshes the current desktop
session environment before launch. If no usable desktop X11 display exists (for
example, after the RDP session has ended), the worker starts a private Xvfb only
for Chromium's lifetime and tears it down with the browser. A live desktop
display is always preferred so first-time or renewed Google sign-in remains
visible when RDP is connected. The API service is capped at 3 GiB RAM and
512 MiB swap. Chromium creates its own Linux application scope, so the worker
detects that scope, caps it separately at 2 GiB RAM and 256 MiB swap, and
retires stale browser processes/scopes on API startup. The browser process and
Playwright are fully released after 60 seconds without requests, while the
dedicated profile stays on disk for the next request. Chromium starts with a
1440×900 logical page; South Server's GNOME RDP session downscales Xwayland
windows, so the worker expands only the outer X11 surface to 3600×2100. That
produces a normal large on-screen browser while keeping the logical page and
browser memory bounded. Do not use desktop maximize: the RDP virtual root has
no stable monitor bounds and previously caused a host-memory spike. Journald owns process
logs; the project does not maintain a second supervisor or log-rotation system.

## Reliability behavior

Gemini Web uses one serialized browser worker. Each request starts from a clean chat, selects the requested visible model and thinking labels, submits once, and returns only the text obtained from Gemini's Copy control. Recognized transient Gemini failures can retry on a fresh tab, but retries keep the same model and thinking level requested by the caller.

Selectors prefer stable accessibility labels, roles, and visible text. Numeric model version labels are ignored. The current authenticated Gemini UI is the authority when Google changes the page.

Agy uses a bounded process pool. Each request runs in a sterile workspace with customizations and MCP inheritance disabled. Normal Agy requests have no tools; `use_search: true` enables only `search_web`. Prompts go through stream-json stdin, and CLI logs go to the OS null device.

## Verify

```bash
.venv/bin/python -m py_compile main.py worker_pool.py gemini_web.py gemini_models.py agy_cli.py agy_pool.py
.venv/bin/python -m unittest discover -s tests -v
curl -fsS http://127.0.0.1:8000/health
```
