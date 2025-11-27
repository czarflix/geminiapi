

import asyncio
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, AsyncGenerator

from fastapi import (
    FastAPI,
    HTTPException,
    UploadFile,
    File,
    Form,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from playwright.async_api import (
    async_playwright,
    Playwright,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)
from starlette.middleware.cors import CORSMiddleware

# =========================
# Config
# =========================

GEMINI_URL = "https://gemini.google.com/app"
BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
PROFILE_DIR = STORAGE_DIR / "playwright_profile"
SESSIONS_JSON_PATH = STORAGE_DIR / "sessions.json"

MAX_ACTIVE_SESSIONS = 5
NAVIGATION_TIMEOUT = 60_000

# Default to True (Headless) for production use, False for debugging
HEADLESS = True  # Changed to visible mode for debugging

SUPPORTED_MODELS = {
    "gemini-3.0-pro": "Thinking",
    "gemini-3.0-flash": "Fast",
    "thinking": "Thinking",
    "fast": "Fast",
}

MODEL_SELECTOR_MAP = {
    "gemini-3.0-pro": "bard-mode-option-thinkingwith3pro",
    "gemini-3.0-flash": "bard-mode-option-fast",
    "thinking": "bard-mode-option-thinkingwith3pro",
    "fast": "bard-mode-option-fast",
}


# =========================
# Utility
# =========================

def generate_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:12]}"


def html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?>.*?(</\1>)", "", html)
    text = re.sub(r"(?s)<.*?>", "", html)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def is_gemini_generating(page: Page) -> bool:
    """
    Inspect send button state to determine if Gemini is still generating.
    Returns True while the stop button is visible, False once generation finishes.
    """
    send_button = page.locator("button.send-button")

    # Method 1: button class
    try:
        classes = await send_button.get_attribute("class", timeout=1000)
        if classes and "stop" in classes:
            return True
    except Exception:
        pass

    # Method 2: aria label
    try:
        aria_label = await send_button.get_attribute("aria-label", timeout=1000)
        if aria_label and "Stop response" in aria_label:
            return True
    except Exception:
        pass

    # Method 3: stop icon presence
    try:
        stop_icon_count = await page.locator("div.blue-circle.stop-icon").count()
        if stop_icon_count > 0:
            return True
    except Exception:
        pass

    return False


# =========================
# SessionStore
# =========================

class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self._loaded = False
        self._ensure_parent_dir()

    def _ensure_parent_dir(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> None:
        if self._loaded:
            return
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.sessions = data
                else:
                    self.sessions = {}
            except Exception:
                self.sessions = {}
        else:
            self.sessions = {}
        self._loaded = True

    def _save(self) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self.sessions, f, separators=(",", ":"))

    def list_sessions(self) -> List[Dict[str, Any]]:
        self.load()
        out: List[Dict[str, Any]] = []
        for sid, meta in self.sessions.items():
            m = dict(meta)
            m["session_id"] = sid
            out.append(m)
        return out

    def exists(self, session_id: str) -> bool:
        self.load()
        return session_id in self.sessions

    def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        self.load()
        return self.sessions.get(session_id)

    def upsert_session(self, session_id: str, model: str, url: str) -> None:
        self.load()
        now = time.time()
        meta = self.sessions.get(session_id, {})
        if "created_at" not in meta:
            meta["created_at"] = now
        meta["last_used"] = now
        meta["model"] = model
        meta["url"] = url
        meta["usage_count"] = int(meta.get("usage_count", 0))
        if "active" not in meta:
            meta["active"] = True
        self.sessions[session_id] = meta
        self._save()

    def touch(self, session_id: str) -> None:
        self.load()
        meta = self.sessions.get(session_id)
        if not meta:
            return
        meta["last_used"] = time.time()
        meta["usage_count"] = int(meta.get("usage_count", 0)) + 1
        self.sessions[session_id] = meta
        self._save()

    def mark_inactive(self, session_id: str) -> None:
        self.load()
        meta = self.sessions.get(session_id)
        if not meta:
            return
        meta["active"] = False
        self.sessions[session_id] = meta
        self._save()

    def delete(self, session_id: str) -> None:
        self.load()
        if session_id in self.sessions:
            del self.sessions[session_id]
            self._save()

    def clear(self) -> None:
        self.sessions = {}
        self._save()

    def list_ids(self) -> List[str]:
        self.load()
        return list(self.sessions.keys())


# =========================
# BrowserManager
# =========================

class BrowserManager:
    def __init__(self) -> None:
        self.playwright: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None

    async def startup(self) -> None:
        if self.playwright is not None:
            return

        self.playwright = await async_playwright().start()

        # --- STEALTH ARGUMENTS ---
        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--enable-features=NetworkService,NetworkServiceInProcess",
            "--window-size=1920,1080", # Force window size to prevent detection
        ]

        # THE FIX: "New Headless" Mode
        # We set Playwright's internal headless to FALSE but pass "--headless=new"
        # to Chrome. This renders a full browser (good for Auth/Cookies) but hides the UI.
        launch_headless = False 
        if HEADLESS:
            browser_args.append("--headless=new")

        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=launch_headless, # ALWAYS False to allow '--headless=new' to work
            args=browser_args,
            # Spoof User Agent to match a real Mac/Windows desktop
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
        )

        # --- STEALTH INJECTIONS ---
        await self.context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Inc.';
                if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                return getParameter(parameter);
            };
            """
        )

    async def shutdown(self) -> None:
        if self.context is not None:
            await self.context.close()
            self.context = None
        if self.playwright is not None:
            await self.playwright.stop()
            self.playwright = None

    async def _is_login_page(self, page: Page) -> bool:
        url = page.url or ""
        if "accounts.google.com" in url or "signin" in url:
            return True
        try:
            if await page.locator("text='Sign in'").count() > 0 and "gemini.google.com" not in url:
                return True
            if await page.locator("text='Next'").count() > 0 and "accounts.google.com" in url:
                return True
        except Exception:
            pass
        return False

browser_manager = BrowserManager()
session_store = SessionStore(SESSIONS_JSON_PATH)


# =========================
# Session Management
# =========================

class ChatSession:
    def __init__(self, page: Page, default_model: str):
        self.page: Page = page
        self.lock = asyncio.Lock()
        self.last_used: float = time.time()
        self.default_model: str = default_model


class SessionMode:
    NEW = "new"
    SAME = "same"


class SessionManager:
    def __init__(self, browser_mgr: BrowserManager, store: SessionStore):
        self.browser_mgr = browser_mgr
        self.store = store
        self.sessions: Dict[str, ChatSession] = {}
        self._dict_lock = asyncio.Lock()
        
        # Pre-warm session pool optimization
        self.warm_pool: asyncio.Queue = asyncio.Queue(maxsize=6)
        self._warmer_task: Optional[asyncio.Task] = None
        self._warming_enabled = True

    async def _ensure_context(self) -> None:
        if self.browser_mgr.context is None:
            raise RuntimeError("Browser context is not initialized.")

    async def _evict_if_needed(self) -> None:
        async with self._dict_lock:
            while len(self.sessions) >= MAX_ACTIVE_SESSIONS:
                oldest_sid: Optional[str] = None
                oldest_time = time.time()

                for sid, sess in self.sessions.items():
                    if sess.last_used < oldest_time:
                        oldest_sid = sid
                        oldest_time = sess.last_used

                if oldest_sid is None:
                    return

                sess = self.sessions.pop(oldest_sid, None)
                if sess:
                    try:
                        await sess.page.close()
                    except Exception:
                        pass
                # Eviction only marks inactive, doesn't delete history
                self.store.mark_inactive(oldest_sid)

    async def _warm_session_pool(self):
        """
        Background task that maintains 2-3 pre-loaded browser pages.
        These pages have Gemini UI already loaded and ready to use.
        """
        print("🔥 Pre-warm session pool started")
        
        while self._warming_enabled:
            try:
                current_warm = self.warm_pool.qsize()
                
                if current_warm < 4:  # Maintain at least 4 warm sessions
                    if self.browser_mgr.context is None:
                        await asyncio.sleep(5)
                        continue
                    
                    # Create pre-warmed page
                    page = await self.browser_mgr.context.new_page()
                    await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
                    
                    # Verify not a login page
                    if not await self.browser_mgr._is_login_page(page):
                        await self.warm_pool.put(page)
                        print(f"   ✅ Pre-warmed session added (pool size: {self.warm_pool.qsize()})")
                    else:
                        await page.close()
                        print("   ⚠️ Login required, skipping warm session")
                
                await asyncio.sleep(3)  # Check every 3 seconds
                
            except Exception as e:
                print(f"   ⚠️ Warm pool error: {e}")
                await asyncio.sleep(15)
        
        print("🔥 Pre-warm session pool stopped")

    async def start_warming(self):
        """Start the background warming task"""
        if self._warmer_task is None or self._warmer_task.done():
            self._warmer_task = asyncio.create_task(self._warm_session_pool())

    async def stop_warming(self):
        """Stop the background warming task and close all warm pages"""
        self._warming_enabled = False
        if self._warmer_task:
            self._warmer_task.cancel()
            try:
                await self._warmer_task
            except asyncio.CancelledError:
                pass
        
        # Close all warm pages
        while not self.warm_pool.empty():
            try:
                page = self.warm_pool.get_nowait()
                await page.close()
            except Exception:
                pass

    async def _create_new_session(self, model: str) -> Tuple[str, ChatSession]:
        await self._ensure_context()
        await self._evict_if_needed()

        # Try to get a pre-warmed page first
        page = None
        try:
            page = await asyncio.wait_for(self.warm_pool.get(), timeout=0.5)
            print(f"   ⚡ Using pre-warmed session (saved ~3s)")
            print(f"   📄 Pre-warmed page URL: {page.url}")
        except asyncio.TimeoutError:
            # No warm page available, create new one
            print(f"   🐌 Creating new session (no warm sessions available)")
            page = await self.browser_mgr.context.new_page()
            await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
            print(f"   📄 New page URL: {page.url}")

        # Verify login status
        if await self.browser_mgr._is_login_page(page):
            await page.close()
            raise HTTPException(
                status_code=401, 
                detail="Gemini appears logged out. Please run login_helper.py again."
            )

        new_id = generate_session_id()
        sess = ChatSession(page=page, default_model=model)

        async with self._dict_lock:
            self.sessions[new_id] = sess

        url = page.url
        print(f"   💾 Storing session {new_id} with URL: {url}")
        session_store.upsert_session(new_id, model=model, url=url)
        return new_id, sess

    async def _restore_session_tab(self, session_id: str) -> ChatSession:
        meta = self.store.get(session_id)
        if not meta:
            raise RuntimeError(f"Cannot restore session '{session_id}': no metadata.")

        await self._ensure_context()
        await self._evict_if_needed()

        url = meta.get("url") or GEMINI_URL
        model = meta.get("model") or "thinking"

        page = await self.browser_mgr.context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)

        if await self.browser_mgr._is_login_page(page):
            await page.close()
            raise HTTPException(
                status_code=401, 
                detail="Gemini appears logged out."
            )

        sess = ChatSession(page=page, default_model=model)
        async with self._dict_lock:
            self.sessions[session_id] = sess
        return sess

    async def resolve_session(
        self,
        session_mode: str,
        requested_session_id: Optional[str],
        requested_model: str,
    ) -> Tuple[str, ChatSession]:
        mode = session_mode or SessionMode.SAME

        if mode == SessionMode.NEW:
            return await self._create_new_session(requested_model)

        if not requested_session_id:
            raise HTTPException(status_code=400, detail="session_id required for same mode")

        sid = requested_session_id

        if not self.store.exists(sid):
            raise HTTPException(status_code=400, detail=f"Unknown session_id '{sid}'")

        async with self._dict_lock:
            active_sess = self.sessions.get(sid)

        if active_sess is not None:
            active_sess.last_used = time.time()
            self.store.touch(sid)
            return sid, active_sess

        restored = await self._restore_session_tab(sid)
        restored.last_used = time.time()
        self.store.touch(sid)
        return sid, restored

    async def cleanup_all(self) -> None:
        async with self._dict_lock:
            items = list(self.sessions.items())
            self.sessions.clear()
        for _, sess in items:
            try:
                await sess.page.close()
            except Exception:
                pass
                
    async def close_session(self, session_id: str) -> None:
        """
        Explicitly closes and DELETES the session from storage.
        """
        async with self._dict_lock:
            sess = self.sessions.pop(session_id, None)
        if sess:
            try:
                await sess.page.close()
            except Exception:
                pass
        # FIX: Actually delete the data from JSON so tests pass
        self.store.delete(session_id)


session_manager = SessionManager(browser_manager, session_store)


# =========================
# Gemini UI Automation Helpers
# =========================

async def reset_ui_state(page: Page):
    """Only reset UI if there are visible overlays"""
    backdrop = page.locator(".cdk-overlay-backdrop")
    if await backdrop.is_visible():
        await page.keyboard.press("Escape")
        # Wait only if backdrop was visible
        await asyncio.sleep(0.1)
    # Single escape is usually enough
    else:
        try:
            # Quick check for any modals
            if await page.locator("mat-dialog-container, .cdk-overlay-pane").is_visible():
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.1)
        except Exception:
            pass


async def ensure_model_selected(page: Page, model_key: str) -> str:
    """
    Ensure the correct Gemini model is selected.
    Uses proper data-test-id selectors and fast clicking to prevent menu timeout.
    """
    target_id = MODEL_SELECTOR_MAP.get(model_key, "bard-mode-option-thinkingwith3pro")
    target_label = "Thinking" if "thinking" in target_id else "Fast"
    
    print(f"🎯 Selecting model: {model_key} -> {target_label}")
    
    menu_trigger = page.locator("[data-test-id='bard-mode-menu-button']")
    old_menu_trigger = page.locator("[data-test-id='desktop-nested-mode-menu']")

    # Check current model to avoid unnecessary switching
    try:
        menu_text = await menu_trigger.inner_text(timeout=2000)
        if target_label in menu_text:
            print(f"   ✅ Already on {target_label} model")
            return model_key
    except Exception:
        pass

    # Open model menu
    print(f"   🔄 Opening model menu...")
    try:
        if await menu_trigger.is_visible():
            await menu_trigger.click(timeout=3_000)
        elif await old_menu_trigger.is_visible():
            await old_menu_trigger.click(timeout=3_000)
        else:
            print(f"   ⚠️ No menu trigger visible, skipping model selection")
            return model_key
    except Exception as e:
        print(f"   ⚠️ Failed to open menu: {e}")
        return model_key

    # FAST click - menu closes quickly, so we need to be fast
    await asyncio.sleep(0.2)  # Minimal wait for menu to appear
    
    option_locator = page.locator(f"button[data-test-id='{target_id}']")
    
    try:
        await option_locator.first.click(timeout=2000)
        print(f"   ✅ Clicked {target_label} option")
        await asyncio.sleep(0.3)  # Let menu close
    except Exception as e:
        print(f"   ⚠️ Could not click option: {e}")
        # Try text fallback
        try:
            text_locator = page.locator(f"button:has-text('{target_label}')")
            await text_locator.first.click(timeout=1500)
            print(f"   ✅ Clicked {target_label} by text (fallback)")
        except Exception:
            print(f"   ❌ Model selection failed, continuing anyway")

    return model_key


async def prepare_file_payloads(files: List[UploadFile]) -> List[Dict[str, Any]]:
    """
    Read all uploaded files into memory.
    This is pure I/O with no browser interaction, safe to parallelize.
    """
    payloads = []
    for uf in files:
        await uf.seek(0)
        content = await uf.read()
        payloads.append({
            "name": uf.filename,
            "mimeType": uf.content_type or "application/octet-stream",
            "buffer": content,
        })
    return payloads


async def attach_files_to_prompt(page: Page, payloads: List[Dict[str, Any]]):
    """
    Optimized file upload with intelligent UI-based completion detection.
    
    Now expects payloads to be pre-prepared (files already read into memory).
    This allows file reading to happen in parallel with model selection.
    
    IMPORTANT: This function is called AFTER model selection completes,
    so there is NO conflict between model menu and upload menu.
    """
    if not payloads:
        return

    num_files = len(payloads)

    # Open upload menu
    fab_button = page.locator("button.upload-card-button").first
    upload_trigger_btn = page.locator(
        "button[data-test-id='local-images-files-uploader-button']"
    ).first
    fallback_trigger_btn = page.locator(
        "span:has-text('Upload files'), div:has-text('Upload files')"
    ).first

    # Smart toggle - check if menu is already open
    aria_label = await fab_button.get_attribute("aria-label") or ""
    is_menu_open_according_to_fab = "Close" in aria_label

    if is_menu_open_according_to_fab:
        if not await upload_trigger_btn.is_visible() and not await fallback_trigger_btn.is_visible():
            await fab_button.click(force=True)
            await asyncio.sleep(0.2)
            await fab_button.click(force=True)
    else:
        await fab_button.click(force=True)

    # Wait for upload button to be visible
    target_btn = upload_trigger_btn
    try:
        await upload_trigger_btn.wait_for(state="visible", timeout=3000)
    except:
        try:
            await fallback_trigger_btn.wait_for(state="visible", timeout=2000)
            target_btn = fallback_trigger_btn
        except:
            return

    # Upload files
    async with page.expect_file_chooser() as fc_info:
        await target_btn.click(force=True)

    file_chooser = await fc_info.value
    await file_chooser.set_files(payloads)

    # ⚡ OPTIMIZATION: Intelligent completion detection
    start_time = time.time()
    max_wait = 15  # 15 seconds max timeout
    
    while (time.time() - start_time) < max_wait:
        # Count loading vs completed file previews
        loading_count = await page.locator("[data-test-id='file-loading-preview']").count()
        completed_count = await page.locator("[data-test-id='file-preview']").count()
        
        # Check if all files are done processing
        if loading_count == 0 and completed_count == num_files:
            # Verify no spinners remain (additional safety check)
            spinner_count = await page.locator("mat-spinner").count()
            if spinner_count > 0:
                await asyncio.sleep(0.2)
                continue
            
            # Brief stability wait
            await asyncio.sleep(0.3)
            
            # Verify editor is ready (not disabled)
            editor = page.locator('div.ql-editor.textarea.new-input-ui')
            try:
                is_disabled = await editor.get_attribute('aria-disabled')
                if is_disabled == 'true':
                    # Editor still disabled, wait a bit more
                    await asyncio.sleep(0.5)
            except:
                pass
            
            return
        
        await asyncio.sleep(0.2)
    
    # Timeout fallback - log warning but proceed
    loading_count = await page.locator("[data-test-id='file-loading-preview']").count()
    completed_count = await page.locator("[data-test-id='file-preview']").count()
    
    if loading_count > 0:
        # Some files still processing - give one more second
        await asyncio.sleep(1)
    else:
        # All converted to file-preview, but editor might need moment
        await asyncio.sleep(0.5)


async def send_prompt_to_gemini(page: Page, prompt: str) -> None:
    editor = page.locator(
        'div.ql-editor.textarea.new-input-ui[contenteditable="true"][aria-label="Enter a prompt here"]'
    )
    
    try:
        await editor.click(timeout=3000)
    except Exception:
        await reset_ui_state(page)
        await editor.click(timeout=3000, force=True)

    await editor.fill(prompt)
    # No wait needed - fill is synchronous

    try:
        await editor.press("Enter")
    except Exception:
        try:
            send_btn = page.locator("button[data-test-id='send-button']").first
            await send_btn.click(timeout=3000)
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to submit prompt.")


async def wait_for_final_response_html(page: Page, timeout_ms: int = 60_000) -> str:
    """
    Wait for Gemini to finish streaming using definitive UI completion signals.
    """
    response_locator = page.locator(
        "message-content .markdown.markdown-main-panel, "
        ".model-response-text .markdown.markdown-main-panel"
    ).last

    try:
        await response_locator.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        raise HTTPException(status_code=504, detail="Timed out waiting for Gemini response.")

    start_time = time.time()
    max_wait_seconds = timeout_ms / 1000

    while (time.time() - start_time) < max_wait_seconds:
        if not await is_gemini_generating(page):
            await asyncio.sleep(0.2)  # brief stability buffer
            if not await is_gemini_generating(page):
                return await response_locator.inner_html()
        await asyncio.sleep(0.05)

    return await response_locator.inner_html()


async def stream_response_chunks(page: Page) -> AsyncGenerator[str, None]:
    locator = page.locator(
        "message-content .markdown.markdown-main-panel, "
        ".model-response-text .markdown.markdown-main-panel"
    ).last

    try:
        await locator.wait_for(state="visible", timeout=60_000)
    except PlaywrightTimeoutError:
        yield json.dumps({"error": "Timed out waiting for first token"}) + "\n"
        return

    last_text = ""

    while True:
        try:
            html = await locator.inner_html()
        except Exception:
            break

        text = html_to_text(html)

        if text != last_text:
            if text.startswith(last_text):
                delta = text[len(last_text):]
            else:
                delta = text
            last_text = text
            chunk = {"delta": delta, "full": text, "partial": True}
            yield json.dumps(chunk) + "\n"

        if not await is_gemini_generating(page):
            await asyncio.sleep(0.1)
            if not await is_gemini_generating(page):
                break

        await asyncio.sleep(0.05)

    yield json.dumps({"done": True}) + "\n"


# =========================
# FastAPI App (Lifespan)
# =========================

@asynccontextmanager
async def lifespan(app: FastAPI):
    await browser_manager.startup()
    
    # Start pre-warming sessions in background
    await session_manager.start_warming()
    
    yield
    
    # Cleanup on shutdown
    await session_manager.stop_warming()
    await session_manager.cleanup_all()
    await browser_manager.shutdown()

app = FastAPI(title="Gemini Web UI Wrapper (Version R-Fixed)", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/ask")
async def ask(
    prompt: str = Form(...),
    model: str = Form("thinking"),  # Default to thinking model for testing
    session_mode: str = Form(SessionMode.NEW),
    session_id: Optional[str] = Form(None),
    stream: bool = Form(False),
    files: List[UploadFile] = File(default_factory=list),
):
    if model not in SUPPORTED_MODELS:
        raise HTTPException(status_code=400, detail=f"Unsupported model '{model}'")

    print(f"\n{'='*60}")
    print(f"📨 NEW REQUEST")
    print(f"{'='*60}")
    print(f"   Prompt: {prompt[:50]}...")
    print(f"   Model: {model}")
    print(f"   Mode: {session_mode}")
    print(f"   Session ID: {session_id}")
    
    sid, chat_session = await session_manager.resolve_session(
        session_mode=session_mode,
        requested_session_id=session_id,
        requested_model=model,
    )
    page = chat_session.page
    
    print(f"   Using session: {sid}")
    print(f"   Page URL: {page.url}")
    print(f"   Page title: {await page.title()}")

    async with chat_session.lock:
        start_ts = time.time()
        
        # Only reset UI if there are visible overlays (saves 200-400ms)
        backdrop = page.locator(".cdk-overlay-backdrop")
        if await backdrop.is_visible():
            print(f"   🧹 Resetting UI (overlay visible)")
            await reset_ui_state(page)
        
        # Parallelize file I/O with model selection
        if files:
            print(f"   📎 Processing {len(files)} files...")
            model_task = ensure_model_selected(page, model)
            file_task = prepare_file_payloads(files)
            
            model_used, payloads = await asyncio.gather(model_task, file_task)
            
            print(f"   📤 Uploading files...")
            await attach_files_to_prompt(page, payloads)
        else:
            print(f"   🔧 Selecting model (no files)...")
            model_used = await ensure_model_selected(page, model)

        print(f"   💬 Sending prompt...")
        await send_prompt_to_gemini(page, prompt)

        if stream:
            async def streamer():
                async for chunk in stream_response_chunks(page):
                    yield chunk.encode("utf-8")
            return StreamingResponse(streamer(), media_type="application/json")

        print(f"   ⏳ Waiting for response...")
        html = await wait_for_final_response_html(page)
        text = html_to_text(html)
        latency_ms = int((time.time() - start_ts) * 1000)
        session_store.touch(sid)

        print(f"   ✅ Response received ({latency_ms}ms)")
        print(f"{'='*60}\n")

        return JSONResponse({
            "session_id": sid,
            "model_requested": model,
            "model_used": model_used,
            "response_text": text,
            "latency_ms": latency_ms
        })

@app.get("/sessions")
async def list_sessions():
    return session_store.list_sessions()

@app.delete("/session/{session_id}")
async def delete_session(session_id: str):
    await session_manager.close_session(session_id)
    return {"status": "ok", "session_id": session_id}

@app.delete("/sessions/all")
async def delete_all_sessions():
    await session_manager.cleanup_all()
    session_store.clear()
    return {"status": "ok", "cleared": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)