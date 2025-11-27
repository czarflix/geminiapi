import asyncio
import os
from playwright.async_api import async_playwright

GEMINI_URL = "https://gemini.google.com/app"
PROFILE_DIR = os.path.join(os.path.dirname(__file__), "storage", "playwright_profile")
os.makedirs(PROFILE_DIR, exist_ok=True)


async def main():
    async with async_playwright() as p:

        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--start-maximized",
        ]

        context = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            args=browser_args,
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        )

        page = await context.new_page()
        await page.goto(GEMINI_URL)

        print(
            "\n⚠️  IMPORTANT ⚠️\n"
            "➡ A REAL Chrome-like browser window is now open.\n"
            "➡ Log in normally (email + password + 2FA).\n"
            "➡ Stay until you see the Gemini chat UI.\n"
            "➡ RETURN HERE and press ENTER.\n"
        )

        input("\n>>> Press ENTER ONLY AFTER Gemini loads successfully... ")

        print("\nSaving session... Closing browser...\n")
        await context.close()
        print("🎉 Login stored successfully!")


if __name__ == "__main__":
    asyncio.run(main())
