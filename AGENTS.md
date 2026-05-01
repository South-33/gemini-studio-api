This is the project's AGENTS.md

# Project AGENTS Notes

## Notes
- Do not use DOM text extraction as a response source for normal request handling. Use copy-button/clipboard extraction only. DOM reads may be used for diagnostics/debug context, but not to return model output.
- Burst failures can affect both workers with `unsent stuck`/`stalled generation` at once; rely on prompt/response token-estimate logs plus worker recreation events to correlate upstream stress windows.
- On Windows, keep anti-throttle launch args active (`disable-background*`, disable `CalculateNativeWinOcclusion`, disable battery saver feature) or hidden/occluded windows can stall generation despite healthy selectors.
- If Gemini endpoints and Discord both show `ERR_NAME_NOT_RESOLVED` / `getaddrinfo failed`, treat it as local DNS/network outage -> skip cross-worker retries -> refresh/retry logic alone will not recover it.
- Repeated `200` generation responses with tiny visible text (`len~50`) can still be live post-processing; use recent network activity (`net_age`) to extend tiny/no-output stall grace before killing the worker.
- For stable-response finalize attempts, try copy extraction before clicking Stop; otherwise diagnostics can get polluted with `You stopped this response` even when Gemini was still post-processing.
- Retries are total attempts, not unique-worker-only now; after recoverable stall/refresh/recreation, the same worker can be retried if the alternate worker is still busy.
- Gemini last-turn diagnostics can track thinking separately via `button.thoughts-header-button`; treat assistant `button[aria-label="Copy"]` inside the latest `model-response` as response-ready even if `Stop response` is still visible, and ignore user `Copy prompt` buttons.
- Idle maintenance is pre-request and marks all workers busy; keep it bounded and clear stale busy flags or Playwright transport failures can poison the pool with assignment timeouts.
