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
- Keep the headed-Chromium anti-throttling flags while warm. Chromium closes
  after five idle minutes and wakes on the next request; do not use memory-saver
  or other low-resource modes because Gemini can render incompletely.
- `.browser_session/` and `logs/` are private runtime state and must not be
  committed.
- Production autostart lives in the signed-in user's HKCU `Run` key and starts
  `launcher.py` with the interactive desktop. Do not move it back to Task
  Scheduler: task stop/relaunch events can interrupt the headed browser, while
  SSH logons do not execute the HKCU Run entry. Keep the launcher mutex in the
  Windows `Global\` namespace as a second single-owner guard across sessions.
