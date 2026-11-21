This is the project's AGENTS.md

# Project instructions

- The current server is **South Server**, an Ubuntu host reached through SSH alias `southserver` with Linux user `southserver`.
- This repository is not currently deployed on South Server. The previous Windows/`nyxy` production deployment is retired; do not treat its profile, RDP, Explorer Startup, Task Scheduler, or `ops/windows/` instructions as current server state.
- The launcher still contains Windows-specific production behavior. Do not claim a Linux production deployment until that path is explicitly ported and validated on South Server.
- Develop locally and use Git for any future South Server deployment.
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
- Antigravity processes share one sterile runtime workspace with static
  `api-model` (no tools) and `api-search` (web search only) agents. Send
  CLI logs to the OS null device. Do not return to per-request workspaces:
  they pollute Agy's workspace conversation cache. Send prompts only through
  stream-json stdin; do not use command-line prompt transport or prompt files.
- Force UTF-8 for Antigravity subprocess pipes. Windows may otherwise use a
  legacy code page and corrupt or reject non-ASCII prompts.
- Override personal Antigravity settings per request with `--settings`. Normal
  API calls get no tools; `use_search: true` gets only `search_web`.
  Deny writes, commands, arbitrary URL reads, unsandboxed execution, MCP, and
  browser actuation; do not expose local-file tools to the custom API agent;
  keep G1 credit fallback disabled.
- Keep both API custom agents isolated with `inheritCustomizations: false`,
  `inheritMcp: false`, and no skills. Do not make the API depend on the user's
  global Antigravity customization state.
- Keep Antigravity on the signed-in provider by overriding `modelProvider` to
  its default for API sessions; personal API-key provider settings must not
  silently change quota or billing behavior.
- Do not set `historySize` for API sessions. Headless stream-json calls do not
  need interactive history, and `historySize: 0` can clear shared CLI history.
- Disable personal `statusLine` and `title` command scripts in per-request Agy
  settings; headless API sessions must not execute CLI decoration hooks.
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
- `ops/windows/` and the Explorer Startup behavior are retained only as legacy Windows deployment code. They are not the South Server startup mechanism.
- Public transport uses two tunnels to the same local API: Cloudflare Quick
  Tunnel is the preferred data plane and the fixed ngrok domain is the control
  plane plus emergency fallback. `GET /v1/endpoint` on ngrok publishes the
  current `trycloudflare.com` URL. Quick Tunnel URLs are runtime state and must
  never be hardcoded into callers or committed.
- Do not run `ops/windows/install_autostart.ps1` on South Server.
