This is the project's AGENTS.md

- Production is South Server: Ubuntu, user `southserver`, with a systemd user service from `ops/gemini-studio-api.service`.
- Keep the service small: one Starlette API process, one serialized Gemini Web worker, and one bounded explicit Agy pool. Do not add provider frameworks, browser worker fan-out, or an in-process supervisor.
- Backend omission means Gemini Web. Its contract is exactly `hydrogen` or `neon` with `Low`, `Medium`, or `High`; defaults are `neon` and `Medium`.
- Agy is selected only with `backend: "agy"`. It accepts exact IDs from `agy models`; do not translate Gemini Web aliases into Agy IDs or apply Gemini `thinking_level` to Agy.
- Keep Gemini and Agy failure domains separate. Never silently replay a request through the other backend.
- Never point Playwright at the user's normal Chrome profile. Reuse the ignored dedicated `.browser_session/profile/` so Google can refresh its session normally. Before every launch remove only `Default/Preferences` plus transient Chromium lock/DevTools files: on this Ubuntu host retaining that generated Preferences file reproducibly causes the next Chromium 158/159 launch to SIGTRAP, while six sanitized relaunches passed.
- Gemini browser startup is lazy and the browser plus Playwright are fully released after idle time. Preserve the anti-background-throttling flags while the browser is warm.
- Cold-start profiling on South Server shows Chromium/CDP startup is sub-second and authenticated `/app` headers arrive in about 150–190ms, but Chromium may take roughly 15–30s to finish the ~934KB document body. Headless mode, forcing HTTP/2 instead of HTTP/3, and device-bound-session/keyring flags did not reliably improve it; optimize only with new evidence.
- A signed-out dedicated browser is `auth_required`, not ready. Keep Chromium open for a short manual sign-in window, then let idle cleanup release the process while preserving the dedicated profile.
- South Server's headed Chromium must use X11 with GPU disabled. Native Wayland stalls before DevTools becomes ready in the GNOME RDP session. Keep a 1440×900 logical browser and resize only the outer X11 window to 3600×2100 with xdotool; GNOME/RDP otherwise displays Xwayland at roughly 40% size. Do not maximize against the monitor-less RDP root.
- Prefer a live desktop X11 display, but do not depend on RDP staying connected: when no usable display exists, start a private Xvfb lazily with Chromium and stop it at browser teardown. Do not keep a permanent virtual display alive.
- Launch the matched Playwright Chromium directly and attach over localhost CDP so the worker owns the browser PID and can cap its transient systemd app scope. The SIGTRAP root cause is the retained `Default/Preferences` file, not CDP itself.
- Chromium moves the browser tree into `app-org.chromium.Chromium-<pid>.scope`. Keep runtime-specific stale-scope cleanup and the 2 GiB browser-scope cap; the API service cgroup alone does not own that scope.
- Match Gemini by semantic selectors and visible labels, never generated classes or menu positions. Ignore numeric model versions.
- Treat the authenticated live Gemini page as selector/UI authority. Web docs are secondary evidence.
- Return Gemini output only through its Copy control/clipboard; DOM response text is state-detection evidence, not the API response body.
- Keep Agy prompts on stream-json stdin in the sterile runtime workspace. Preserve no-tool/search-only agents, disabled customization/MCP inheritance, denied command/write/browser access, and OS-null CLI logging.
- Runtime state (`.browser_session/`, `logs/`, caches, temporary files) must stay untracked.
