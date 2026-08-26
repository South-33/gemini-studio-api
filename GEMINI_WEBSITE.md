# Gemini Website Contract

This is the maintained inventory of Gemini Web behavior used in production.
It records semantic contracts, not Angular classes or numeric model versions.
Last checked against the signed-in UI on 2026-08-26.

## Readiness and modes

Chat lives at `/app`; Spark lives at `/spark`. A page is ready when its prompt
composer is visible and it is not a Google error page. Some layouts omit the
Chat/Spark switcher, so a ready composer on `/app` is valid Chat too.

The signed-in account menu itself links to `accounts.google.com`. Only a visible
control whose text or accessible label is exactly `Sign in` means authentication
is required.

| Purpose | Stable signal | Safe fallback |
| --- | --- | --- |
| Chat | Button named `Chat`; current test id `app-tab-chat` | Ready composer on `/app` when mode tabs are absent |
| Spark | Button named `Spark`; current test id `app-tab-agent` | `/spark` URL |
| New chat | Link/control named `New chat` targeting `/app` | `Ctrl+Shift+O`, then prove the conversation is empty |
| Temporary chat | Button named `Temporary chat` | Clean regular chat if the control is unavailable |
| Composer | Textbox labeled `Enter a prompt for Gemini` | Visible editable textbox with prompt/enter semantics |
| Model picker | `Open mode picker, currently …` | Current test id `bard-mode-menu-button` |
| Upload | Button named `Upload & tools` | Local file input exposed after opening that menu |
| Send | Accessible label containing `Send` | Prove user bubble, Stop, or response change after activation |
| Stop | Accessible label containing `Stop` | State signal only; do not blindly click it |
| Copy result | `Copy` inside the latest model response | Clipboard is the only production response source |

## Model and reasoning menu

The current menu exposes labeled items such as `3.5 Flash-Lite`, `3.7 Flash`,
`3.1 Pro`, and `Extended thinking`. Selection is family-based:

- `Flash` + `Lite` means `flash-lite`;
- `Pro` means `pro`;
- `Flash` means `flash`.

Ignore numeric versions and descriptive copy. Select only a visible item whose
text matches the requested family. Never use hashed IDs or menu positions; a
wrong model is worse than a clear failure.

Reasoning has two API states: `Standard` and `Extended`. Prefer the labeled
`Extended thinking` toggle. A legacy submenu may use labeled Standard/Extended
items, but never their index. Re-read the picker after selection.

## Uploads and prompts

`Upload & tools` currently opens `Upload files` (documents, data, code) and
`Add from Drive`, plus creative/deep-research tools. Production uses only local
file upload. Images are pasted/uploaded as attachments. Long text prompts are
written to `prompt.txt` and uploaded to avoid freezing the contenteditable.
Gemini often skips Search when the request exists only inside that attachment,
so prompts containing the standalone words `google`, `search`, or `web` leave a
short search instruction in the composer. `use_search: true` forces that hint;
short prompts use only the explicit flag and are not keyword-guessed.

For text-only API calls, keep the anti-image instruction. Do not activate
Create image/video/music, Canvas, or Deep research as an automation fallback.

## Request lifecycle

Every request must:

1. prove Chat mode from an active Chat tab, or from a ready `/app` composer
   when Spark is not active (some layouts expose no stable selected-tab state);
2. prove the conversation is empty (no user query, response, or Stop state);
3. enter temporary chat when available;
4. select and re-read model/reasoning;
5. enter prompt/attachments and capture pre-send counters;
6. send and prove generation started;
7. wait using Stop, response, thinking, and network state;
8. copy the latest response through Gemini's Copy control.

After Copy succeeds, prepare a fresh Temporary Chat before releasing the queue
lock or returning the result. The next queued request must always inherit this
known-ready state. Do not repeat a readiness wait already proved by the reset
action. If reset fails, replace the page once and prepare that page.

`thinking-overlay` may expose a short changing summary while Extended reasoning
is active, but Gemini leaves an empty overlay mounted after completion. Count
only a visible animation or non-empty summary as liveness, and never require
reasoning text for success. Sample local DOM state every 200 ms so completion
is returned promptly; this does not create Gemini network requests. A changed
summary or growing response resets the progress clock; static reasoning fails
after 120 seconds, or 180 seconds while relevant Gemini network traffic remains
live.

`Ctrl+Shift+S` toggles Chat/Spark and is not idempotent. Use it only when state
already proves Spark is active and always verify afterward. `Ctrl+Shift+O`
creates a Chat conversation. The visible New chat control is preferable when
available. The Dictate button currently advertises `Ctrl+Shift+D`; it is not
used by the API.

An unsent draft may be cleared. An accepted user bubble must not be silently
discarded. Never refresh a healthy idle page because of age or request count.

## Failure and recovery

- Google 500, dead page, retained Stop, or verified stall: capture diagnostics,
  recreate the tab, and retry once.
- DNS/network outage: report it immediately; tab recreation cannot fix DNS.
- Missing selector: report selector/action, URL, UI state, visible model label,
  request ID, queue wait, and recovery outcome.
- Startup: close Chromium-restored pages, create one managed page, and become
  healthy only after its composer is ready.

When Gemini changes, inspect the signed-in DOM, update only behavior this API
uses, and add or adjust a focused regression test.
