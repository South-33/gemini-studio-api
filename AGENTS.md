This is the project's AGENTS.md

- Production is South Server: Ubuntu, user `southserver`, with a systemd user service from `ops/gemini-studio-api.service`.
- Keep the service small: one FastAPI process, one serialized Gemini Web worker, and one bounded explicit Agy pool. Do not add provider frameworks, browser worker fan-out, or an in-process supervisor.
- Backend omission means Gemini Web. Its contract is exactly `hydrogen` or `neon` with `Low`, `Medium`, or `High`; defaults are `neon` and `Medium`.
- Agy is selected only with `backend: "agy"`. It accepts exact IDs from `agy models`; do not translate Gemini Web aliases into Agy IDs or apply Gemini `thinking_level` to Agy.
- Keep Gemini and Agy failure domains separate. Never silently replay a request through the other backend.
- Never point Playwright at the user's normal Chrome profile. Browser runtime profiles are disposable; do not switch back to reusing one Chromium profile across launches on this host.
- Gemini browser startup is lazy and the browser plus Playwright are fully released after idle time. Preserve the anti-background-throttling flags while the browser is warm.
- A signed-out disposable browser runtime is `auth_required`, not ready. Keep Chromium open for a short manual sign-in window, then let idle cleanup release it. Persist only filtered Google auth cookies in the ignored state directory.
- South Server's headed Chromium must use X11 with GPU disabled. The Wayland/NVIDIA path stalls before DevTools becomes ready on this host. Use a fixed initial window size; automatic maximization reproducibly exhausts host memory in the GNOME remote session.
- Launch the matched Playwright Chromium directly and attach over localhost CDP. Playwright's remote-debugging pipe crashes with SIGTRAP on this Ubuntu host; do not switch back without re-validating that failure.
- Chromium moves the browser tree into `app-org.chromium.Chromium-<pid>.scope`. Keep runtime-specific stale-scope cleanup and the 2 GiB browser-scope cap; the API service cgroup alone does not own that scope.
- Match Gemini by semantic selectors and visible labels, never generated classes or menu positions. Ignore numeric model versions.
- Treat the authenticated live Gemini page as selector/UI authority. Web docs are secondary evidence.
- Return Gemini output only through its Copy control/clipboard; DOM response text is state-detection evidence, not the API response body.
- Keep Agy prompts on stream-json stdin in the sterile runtime workspace. Preserve no-tool/search-only agents, disabled customization/MCP inheritance, denied command/write/browser access, and OS-null CLI logging.
- Runtime state (`.browser_session/`, `logs/`, caches, temporary files) must stay untracked.
