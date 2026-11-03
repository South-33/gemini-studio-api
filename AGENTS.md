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
- API models are exactly `flash-lite`, `flash`, `pro`, and `argon`. Thinking
  levels are exactly `Low`, `Medium`, and `High`. Do not add aliases.
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
- Keep the headed-Chromium anti-throttling flags and launcher process ownership.
- Do not enable Chromium memory-saver or other low-resource modes; Gemini can
  render incompletely. Reduced motion is allowed only after live UI verification.
- `.browser_session/` and `logs/` are private runtime state and must not be
  committed.
