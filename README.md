# Gemini Studio API

A local API that automates Google AI Studio to provide OpenAI-compatible endpoints. Use Gemini 2.5 Pro/Flash with thinking levels directly from Roo Code, Cursor, or any OpenAI-compatible tool.

## Features

- **OpenAI Compatible** - Works with Cursor, Roo Code, Continue, etc.
- **Thinking Levels** - Control via model name suffix: `-minimal`, `-low`, `-medium`, `-high`
- **Model Selection** - gemini-3-flash-preview, gemini-3-pro-preview
- **Markdown Extraction** - Properly extracts formatted responses via clipboard
- **Session Persistence** - Login once, stays authenticated

## Quick Start

```bash
# Install dependencies
pip install fastapi uvicorn playwright python-dotenv

# Install browser
playwright install chromium

# Run
python main.py
```

## Configuration (.env)

```env
WORKER_COUNT=1
HEADLESS=false
LOW_MEMORY_MODE=false
```

## Roo Code / Cursor Setup

1. **Provider**: OpenAI Compatible
2. **Base URL**: `http://localhost:8001/v1`
3. **API Key**: `anything` (ignored)
4. **Model**: `gemini-3-flash-preview-minimal`
5. **IMPORTANT**: Disable "Enable streaming" ⚠️

## Model Names → Thinking Levels

| Model | Thinking |
|-------|----------|
| `gemini-3-flash-preview` | High (default) |
| `gemini-3-flash-preview-minimal` | Minimal |
| `gemini-3-flash-preview-low` | Low |
| `gemini-3-flash-preview-medium` | Medium |
| `gemini-3-pro-preview` | High |

## Endpoints

- `GET /v1/models` - List available models
- `POST /v1/chat/completions` - OpenAI-compatible chat
- `POST /v1/chat` - Simple direct chat
- `GET /health` - Health check

## Test Chat UI

Open `chat.html` in your browser for a quick test interface.

## Notes

- First run opens a browser for manual Google login
- Session saved to `.aistudio_data/` folder
- Streaming not supported (disable in client)
