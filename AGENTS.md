# Project instructions

- Production is the Windows host reached through SSH alias `southserver`.
- Develop locally, commit and push, then deploy on production with Git only.
- The API has one queued Gemini Web worker. Do not add provider abstractions or
  multiple workers.
- API models are exactly `flash-lite` and `pro`. Thinking levels are exactly
  `Low`, `Medium`, and `High`. Do not add aliases. `flash` remains recognizable
  internally only so the worker can switch away from that UI state.
- Match Gemini's visible model family text and exact thinking labels; never use
  generated class names or menu positions.
- Return model output only through Gemini's Copy control/clipboard.
- Keep the headed-Chromium anti-throttling flags and launcher process ownership.
- `.browser_session/` and `logs/` are private runtime state and must not be
  committed.
