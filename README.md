# Gemini Custom Wrapper

A FastAPI-based wrapper for Google Gemini's web interface that provides a RESTful API for programmatic access to Gemini AI models.

## Features

- 🚀 **RESTful API** - Simple HTTP endpoints for chat interactions
- 🔄 **Session Management** - Persistent browser sessions for multi-turn conversations
- 📎 **File Upload Support** - Attach files to prompts (images, documents, etc.)
- 🎯 **Model Selection** - Support for Gemini Pro (Thinking) and Flash (Fast) models
- 📡 **Streaming Responses** - Real-time token streaming for progressive output
- 🔥 **Pre-warmed Sessions** - Background session pool for faster response times
- 🕶️ **Stealth Mode** - Advanced detection evasion using headless Chrome
- 💾 **Session Persistence** - Sessions saved to disk and restored across restarts

## Prerequisites

- Python 3.8+
- Google account with Gemini access
- Chrome/Chromium browser

## Installation

1. Clone the repository:

```bash
git clone https://github.com/czarflix/geminiapi
cd GeminiCustomWrapper
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Install Playwright browsers:

```bash
playwright install chromium
```

## Setup

1. **Login to Gemini** - Run the login helper to authenticate:

```bash
python login_helper.py
```

This opens a browser window where you log in to your Google account. Cookies are saved for future use.

2. **Start the API server**:

```bash
python main.py
```

The API will be available at `http://localhost:8000`

## API Usage

### Send a Prompt

**Endpoint:** `POST /ask`

**Parameters:**

- `prompt` (required) - The text prompt to send
- `model` (optional) - Model to use: `thinking`, `fast`, `gemini-3.0-pro`, or `gemini-3.0-flash` (default: `thinking`)
- `session_mode` (optional) - `new` or `same` (default: `new`)
- `session_id` (optional) - Required when `session_mode=same`
- `stream` (optional) - Enable streaming responses (default: `false`)
- `files` (optional) - Array of files to upload

**Example (cURL):**

```bash
# Simple prompt
curl -X POST "http://localhost:8000/ask" \
  -F "prompt=What is the capital of France?" \
  -F "model=thinking"

# With file upload
curl -X POST "http://localhost:8000/ask" \
  -F "prompt=Describe this image" \
  -F "model=fast" \
  -F "files=@image.jpg"

# Continue existing session
curl -X POST "http://localhost:8000/ask" \
  -F "prompt=Tell me more" \
  -F "session_mode=same" \
  -F "session_id=sess_abc123def456"
```

**Response:**

```json
{
  "session_id": "sess_abc123def456",
  "model_requested": "thinking",
  "model_used": "thinking",
  "response_text": "The capital of France is Paris.",
  "latency_ms": 2847
}
```

### List Sessions

**Endpoint:** `GET /sessions`

```bash
curl "http://localhost:8000/sessions"
```

**Response:**

```json
[
  {
    "session_id": "sess_abc123def456",
    "model": "thinking",
    "created_at": 1732675200.0,
    "last_used": 1732675800.0,
    "usage_count": 5,
    "active": true,
    "url": "https://gemini.google.com/app/..."
  }
]
```

### Delete Session

**Endpoint:** `DELETE /session/{session_id}`

```bash
curl -X DELETE "http://localhost:8000/session/sess_abc123def456"
```

### Delete All Sessions

**Endpoint:** `DELETE /sessions/all`

```bash
curl -X DELETE "http://localhost:8000/sessions/all"
```

## Configuration

Edit `main12.py` to customize settings:

```python
HEADLESS = True  # Run in headless mode (set False for debugging)
MAX_ACTIVE_SESSIONS = 5  # Maximum concurrent browser sessions
NAVIGATION_TIMEOUT = 60_000  # Page load timeout in milliseconds
```

## Session Management

- **NEW mode** - Creates a fresh session with no conversation history
- **SAME mode** - Continues an existing session for multi-turn conversations
- Sessions are automatically evicted when limit is reached (oldest first)
- Session data persists in `storage/sessions.json`
- Browser profiles stored in `storage/playwright_profile/`

## Supported Models

| Model ID                       | Display Name | Description                             |
| ------------------------------ | ------------ | --------------------------------------- |
| `thinking` or `gemini-3.0-pro` | Thinking     | Most capable, slower responses          |
| `fast` or `gemini-3.0-flash`   | Fast         | Faster responses, good for simple tasks |

## File Upload Support

Supported file types:

- Images (JPG, PNG, WebP, etc.)
- Documents (PDF, TXT, etc.)
- Other formats supported by Gemini

Files are automatically processed and attached to your prompt.

## Architecture

- **FastAPI** - Modern async web framework
- **Playwright** - Browser automation with persistent context
- **Session Pool** - Pre-warmed browser tabs for faster response times
- **Stealth Techniques** - WebGL spoofing, WebDriver hiding, headless detection prevention

## Troubleshooting

**"Gemini appears logged out"**

- Run `python login_helper.py` to re-authenticate
- Check that cookies are saved in `storage/playwright_profile/`

**Slow first response**

- The session pool pre-warms tabs in the background
- First request may take longer while pool initializes

**Upload failures**

- Ensure files are under Gemini's size limits
- Check file format is supported

**Rate limiting**

- Gemini may rate limit requests
- Reduce concurrent requests or add delays

## Development

Run in visible mode for debugging:

```python
HEADLESS = False  # in main12.py
```

View logs in the console to track:

- Session creation/restoration
- Model selection
- File uploads
- Response timing

## Testing

Run the test script:

```bash
bash test_gemini_wrapper.sh
```

Or use the Python test files:

```bash
python test.py
python test2.py
# ... etc
```

## License

This project is for educational and personal use. Respect Google's Terms of Service when using Gemini.

## Credits

Built with:

- [FastAPI](https://fastapi.tiangolo.com/)
- [Playwright](https://playwright.dev/)
- [Uvicorn](https://www.uvicorn.org/)

---

**Note:** This wrapper automates the Gemini web interface. It is not an official Google API and may break if Google updates their UI.
