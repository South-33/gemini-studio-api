This is the project's AGENTS.md

- `GEMINI_WEBSITE.md` is the source of truth for production UI behavior; update it with focused selector/behavior tests when Gemini changes.
- Production uses one queued Gemini Web worker. Do not reintroduce provider abstractions, round-robin workers, or time/count-based page refreshes.
- Match model families by visible `Flash`, `Lite`, and `Pro` text; never select models/reasoning by hashed IDs or menu position.
- Return responses only through Gemini's Copy control/clipboard; DOM text is diagnostic state, not response content.
- Preserve Windows anti-throttle Chromium flags and capture diagnostics before recreating a failed page.
- `.browser_session`, `.env`, prompts, screenshots, and runtime logs are private local state and must not be committed.
