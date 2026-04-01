"""
LinkedIn authentication with session persistence.
Saves browser state (cookies, localStorage) so you only need to log in once.
Subsequent runs reuse the saved session.
"""

from __future__ import annotations

import asyncio
import os
import random

from playwright.async_api import BrowserContext, Page, Playwright

from config import Config


STATE_FILE = os.path.join(Config.STATE_DIR, "linkedin_state.json")


async def random_delay(min_s: float = None, max_s: float = None):
    """Human-like random delay between actions."""
    lo = min_s or Config.DELAY_MIN
    hi = max_s or Config.DELAY_MAX
    await asyncio.sleep(random.uniform(lo, hi))


async def _do_login(page: Page) -> bool:
    """Perform the actual login flow."""
    print("[AUTH] Navigating to LinkedIn login...")
    await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=60000)
    await random_delay(1, 2)

    # Fill credentials
    email_input = page.locator('input#username')
    await email_input.fill(Config.LINKEDIN_EMAIL)
    await random_delay(0.5, 1.5)

    password_input = page.locator('input#password')
    await password_input.fill(Config.LINKEDIN_PASSWORD)
    await random_delay(0.5, 1.0)

    # Click sign in
    await page.locator('button[type="submit"]').click()
    print("[AUTH] Credentials submitted, waiting for redirect...")

    # Wait for either successful redirect or challenge page
    try:
        await page.wait_for_url("**/feed/**", timeout=30000)
        print("[AUTH] Login successful — landed on feed.")
        return True
    except Exception:
        # Could be a security challenge (CAPTCHA, verification, etc.)
        current_url = page.url
        print(f"[AUTH] Did not reach feed. Current URL: {current_url}")

        if "checkpoint" in current_url or "challenge" in current_url:
            print("[AUTH] Security challenge detected!")
            print("[AUTH] Please complete the challenge manually in the browser.")
            print("[AUTH] Waiting up to 120 seconds...")

            try:
                await page.wait_for_url("**/feed/**", timeout=120000)
                print("[AUTH] Challenge passed — landed on feed.")
                return True
            except Exception:
                print("[AUTH] Timed out waiting for challenge completion.")
                return False
        else:
            print("[AUTH] Login may have failed. Check credentials.")
            return False


async def get_authenticated_context(
    playwright: Playwright,
) -> tuple[BrowserContext, bool]:
    """
    Returns an authenticated browser context.

    - If a saved session exists, it reuses it.
    - If not, performs a fresh login and saves the session.

    Returns:
        (context, success) — context is the browser context, success indicates auth state
    """
    Config.validate()

    launch_args = {
        "headless": Config.HEADLESS,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    }

    browser = await playwright.chromium.launch(**launch_args)

    # Try to reuse existing session
    if os.path.exists(STATE_FILE):
        print("[AUTH] Found saved session, attempting reuse...")
        try:
            context = await browser.new_context(
                storage_state=STATE_FILE,
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            # Verify session is still valid
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
            await random_delay(1, 2)

            if "/feed" in page.url and "login" not in page.url:
                print("[AUTH] Saved session is valid.")
                await page.close()
                return context, True
            else:
                print("[AUTH] Saved session expired, performing fresh login...")
                await page.close()
                await context.close()
        except Exception as e:
            print(f"[AUTH] Session verification failed: {e}")
            print("[AUTH] Deleting stale session, will do fresh login...")
            try:
                await page.close()
                await context.close()
            except Exception:
                pass
            os.remove(STATE_FILE)

    # Fresh login
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    )
    page = await context.new_page()
    success = await _do_login(page)

    if success:
        # Save session for next time
        await context.storage_state(path=STATE_FILE)
        print(f"[AUTH] Session saved to {STATE_FILE}")

    await page.close()
    return context, success