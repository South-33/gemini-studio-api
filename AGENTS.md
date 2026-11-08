This is the project's AGENTS.md

# Project instructions

- Production is the Windows host reached through SSH alias `southserver`.
- The server's visible identity is `SouthServer`. The existing Windows login
  name/profile path (`nyxy`, `C:\\Users\\nyxy`) is intentionally retained as
  internal legacy state because the signed-in browser, SSH, RDP, and task state
  already depend on that SID/profile. Do not rename or migrate the profile just
  for cosmetic consistency.
- Develop locally, commit and push, then deploy on production with Git only.
- The API has one queued Gemini Web worker. Do not add provider abstractions or
  multiple workers.
- Antigravity is an explicit second execution backend, selected per request with
  `backend: "agy"`; use a bounded local process pool plus overflow queue, not a
  provider framework or unbounded process spawning.
- When `backend` is omitted, route Agy-first. Use Gemini Web only as a
  pre-execution fallback when Agy cannot serve the request or its queue is full.
  Never replay a request through Gemini after an Agy model call has started.
- Explicit `backend: "agy"` and `backend: "gemini"` requests are strict and
  must not silently switch providers.
- Backend startup is independent: Gemini Web failure must not take down a
  healthy Antigravity backend, and Antigravity failure must not take down Gemini Web.
- A healthy Agy backend must not wait for Gemini Web warmup before the API starts
  serving. Warm Gemini Web in the background when Agy is ready; if Agy fails,
  Gemini Web remains the blocking startup fallback.
- Antigravity processes share one sterile runtime workspace with immutable
  `api-model` (no tools) and `api-search` (web search/read only) agents. Send
  CLI logs to the OS null device. Do not return to per-request workspaces:
  they pollute Agy's workspace conversation cache. Send prompts only through
  stream-json stdin; do not use command-line prompt transport or prompt files.
- Force UTF-8 for Antigravity subprocess pipes. Windows may otherwise use a
  legacy code page and corrupt or reject non-ASCII prompts.
- Override personal Antigravity settings per request with `--settings`. Normal
  API calls get no tools; `use_search: true` gets only web search/read tools.
  Deny writes, commands, unsandboxed execution, MCP, and browser actuation; do
  not expose local-file tools to the custom API agent; keep G1 credit fallback
  disabled.
- Fail Agy initialization when active global Antigravity hooks or enabled
  plugins are present; either can inject behavior outside the custom agent's
  tool allowlist. Disabled hooks/plugins are safe to ignore.
- Keep Antigravity on the signed-in provider by overriding `modelProvider` to
  its default for API sessions; personal API-key provider settings must not
  silently change quota or billing behavior.
- Treat `finished_reason: print_timeout` as failure even if the CLI reports
  `status: SUCCESS`; discard partial output. Refresh `agy models` once only for
  zero-token invalid-model failures on stable Gemini aliases, never exact IDs.
- The public ngrok path has a 5-minute idle timeout. Long Gemini work must use
  async submit-and-poll requests so browser thinking is not capped by the
  tunnel and a caller retry cannot duplicate a still-running request.
- Gemini Web model aliases are exactly `flash-lite`, `flash`, `pro`, and
  `argon`, with `Low`, `Medium`, and `High`. Antigravity may additionally accept
  exact model IDs returned by `agy models`; do not invent aliases for them.
- Model numbers shown by Gemini are disposable UI version labels. Ignore them
  completely for matching and API behavior; only the family names matter.
- Match Gemini's visible model family text and exact thinking labels; never use
  generated class names or menu positions.
- Treat the authenticated production Gemini UI as the source of truth for
  selectors, labels, and interaction flow. Inspect the real paid-account
  browser/session when the UI contract may have changed; web search and docs
  are context only, not UI authority. A copied browser profile is not valid UI
  evidence unless authentication and account state are confirmed unchanged.
- Return model output only through Gemini's Copy control/clipboard.
- Keep the headed-Chromium anti-throttling flags while warm. Chromium closes
  after five idle minutes and wakes on the next request; do not use memory-saver
  or other low-resource modes because Gemini can render incompletely.
- `.browser_session/` and `logs/` are private runtime state and must not be
  committed.
- Production autostart lives in the signed-in user's Explorer Startup folder as
  `Gemini Studio API.lnk` and starts `launcher.py` with the interactive desktop.
  Do not move it back to Task Scheduler or HKCU `Run`: both were observed to
  relaunch the headed browser during SSH maintenance on this host. Keep the
  launcher mutex in the Windows `Global\` namespace as a second single-owner
  guard across sessions.
- Public transport uses two tunnels to the same local API: Cloudflare Quick
  Tunnel is the preferred data plane and the fixed ngrok domain is the control
  plane plus emergency fallback. `GET /v1/endpoint` on ngrok publishes the
  current `trycloudflare.com` URL. Quick Tunnel URLs are runtime state and must
  never be hardcoded into callers or committed.
- `ops/windows/install_autostart.ps1` may use a transient interactive bootstrap
  task when invoked from SSH. It must be deleted immediately; persistent
  autostart remains only the Explorer Startup shortcut.
