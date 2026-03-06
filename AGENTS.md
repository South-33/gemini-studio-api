# Project AGENTS Notes

## Notes
- Do not use DOM text extraction as a response source for normal request handling. Use copy-button/clipboard extraction only. DOM reads may be used for diagnostics/debug context, but not to return model output.
- Burst failures can affect both workers with `unsent stuck`/`stalled generation` at once; rely on prompt/response token-estimate logs plus worker recreation events to correlate upstream stress windows.
- On Windows, keep anti-throttle launch args active (`disable-background*`, disable `CalculateNativeWinOcclusion`, disable battery saver feature) or hidden/occluded windows can stall generation despite healthy selectors.
- If Gemini endpoints and Discord both show `ERR_NAME_NOT_RESOLVED` / `getaddrinfo failed`, treat it as local DNS/network outage -> skip cross-worker retries -> refresh/retry logic alone will not recover it.
