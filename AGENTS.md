This is the project's AGENTS.md

# Project AGENTS Notes

## Notes
- Server remote access is via Tailscale SSH on `100.84.17.34` (Windows user: `nyxy`, project path: `C:\Users\nyxy\Desktop\gemini-studio-api`).
- Do not use DOM text extraction as a response source for normal request handling. Use copy-button/clipboard extraction only. DOM reads may be used for diagnostics/debug context, but not to return model output.
- Burst failures can affect both workers with `unsent stuck`/`stalled generation` at once; rely on prompt/response token-estimate logs plus worker recreation events to correlate upstream stress windows.
- On Windows, keep anti-throttle launch args active (`disable-background*`, disable `CalculateNativeWinOcclusion`, disable battery saver feature) or hidden/occluded windows can stall generation despite healthy selectors.
- If Gemini endpoints and Discord both show `ERR_NAME_NOT_RESOLVED` / `getaddrinfo failed`, treat it as local DNS/network outage -> skip cross-worker retries -> refresh/retry logic alone will not recover it.
- Repeated `200` generation responses with tiny visible text (`len~50`) can still be live post-processing; use recent network activity (`net_age`) to extend tiny/no-output stall grace before killing the worker.
- For stable-response finalize attempts, try copy extraction before clicking Stop; otherwise diagnostics can get polluted with `You stopped this response` even when Gemini was still post-processing.
- Gemini 2026 UI uses `<thinking-overlay>` containing static status text (e.g., `Defining the Project Scope`) instead of legacy `button.thoughts-header-button`. Bypass progress-based stalls while it is active because these labels do not stream character-by-character.
- Thinking models require a fail-fast cooked check: if `thinking_active` is false and `response_body_len == 0` after 15s (or 360s for large prompts) of elapsed wait time, flag the generation as stalled immediately.
- Retries are total attempts, not unique-worker-only now; after recoverable stall/refresh/recreation, the same worker can be retried if the alternate worker is still busy.
- Idle maintenance is pre-request and marks all workers busy; keep it bounded and clear stale busy flags or Playwright transport failures can poison the pool with assignment timeouts.
- Persistent Chromium restores old pages that are not registered workers -> close all restored pages before pool creation, and never preserve poisoned stall pages after recreation.
- Gemini 2026 UI uses `Open/Close sidebar`, `bard-sidenav.collapsed`, `gem-menu-item`, and temp active via inner `mat-icon=close`; prefer these attributes over old label-only sidebar detection.
- API strictly aligns with Web UI model slugs: gemini-3.6-flash, gemini-3.1-pro, gemini-3.5-flash-lite. Clean version-agnostic aliases (flash, flash-lite, pro) resolve dynamically. All models default to Standard thinking level; append suffix/infix (e.g. -extended) to configure.
- Prompts >= 1500 chars (configurable via PROMPT_FILE_UPLOAD_THRESHOLD) are automatically converted into text file attachments in clipboard/uploader to prevent DOM contenteditable freezing.
