from __future__ import annotations

"""
LinkedIn Post Scraper — Keyword-based search with API interception.

Two-layer approach:
  1. API Interception — raw text scan of LinkedIn's JSON responses for
     activity URNs (doesn't depend on specific JSON schema).
  2. DOM Extraction — parses rendered page for post text, author, timestamps.
  3. Merge — matches DOM posts to API URNs by text/author/position.

Usage:
    python3 scraper.py "battery energy storage"
    python3 scraper.py "BESS India" --scrolls 10
    python3 scraper.py "BESS" --sort relevance     # top/engaged posts
    python3 scraper.py "BESS" --sort date_posted    # newest first
"""

import argparse
import asyncio
import hashlib
import urllib.parse
from datetime import datetime

from playwright.async_api import async_playwright, Page

from auth import get_authenticated_context, random_delay
from config import Config
from logger import print_summary, save_posts_to_log
from parser import PostData, extract_all_posts, get_post_count
from api_interceptor import APIInterceptor


def _generate_hash_id(post: PostData) -> str:
    """Generate a deterministic hash ID from post content as last-resort fallback."""
    content = f"{post.author_name}:{post.post_text[:200]}".encode()
    return "hash:" + hashlib.blake2s(content, digest_size=8).hexdigest()


async def search_posts(page: Page, keyword: str, sort_by: str) -> str:
    """Navigate to LinkedIn search results filtered to 'Posts'."""
    encoded = urllib.parse.quote(keyword)

    if sort_by == "date_posted":
        search_url = (
            f"https://www.linkedin.com/search/results/content/"
            f"?keywords={encoded}&origin=GLOBAL_SEARCH_HEADER&sortBy=date_posted"
        )
    else:
        search_url = (
            f"https://www.linkedin.com/search/results/content/"
            f"?keywords={encoded}&origin=GLOBAL_SEARCH_HEADER"
        )

    print(f'[SCRAPER] Searching for: "{keyword}"')
    print(f"[SCRAPER] Sort: {sort_by}")
    print(f"[SCRAPER] URL: {search_url}")

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)

            print("[SCRAPER] Waiting for posts to render...")
            await asyncio.sleep(5)

            count = await get_post_count(page)
            if count > 0:
                print(f"[SCRAPER] Search page loaded — {count} containers found.")
                return page.url

            print("[SCRAPER] No containers yet, waiting longer...")
            await asyncio.sleep(5)

            count = await get_post_count(page)
            if count > 0:
                print(f"[SCRAPER] Search page loaded — {count} containers found.")
                return page.url

            print(f"[SCRAPER] Attempt {attempt}: page loaded but no containers detected.")
            if attempt < max_retries:
                await random_delay(3, 5)

        except Exception as e:
            print(f"[SCRAPER] Attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                print("[SCRAPER] Retrying in 5 seconds...")
                await random_delay(3, 5)
            else:
                raise

    return page.url


def _merge_api_into_dom(
    dom_posts: list[PostData],
    interceptor: APIInterceptor,
) -> list[PostData]:
    """
    Match each DOM-extracted post to an API-intercepted URN and
    fill in post_id + post_url. Three strategies in priority order:
      1. Text match — compare post text to text context captured near the URN
      2. Author match — match by author name
      3. Positional — assign remaining URNs by order
    """
    used_ids: set[str] = set()
    merged_count = 0

    # Skip posts that already have a real URN (from DOM innerHTML scan)
    for post in dom_posts:
        if post.post_id and post.post_id.startswith("urn:li:"):
            # Mark this ID as used so API merge doesn't double-assign
            m = __import__("re").search(r'(\d{15,25})', post.post_id)
            if m:
                used_ids.add(m.group(1))

    # Pass 1: Text matching (most reliable)
    for post in dom_posts:
        if post.post_id and post.post_id.startswith("urn:li:"):
            continue

        match = interceptor.match_by_text(post.post_text, used_ids)
        if match:
            aid = match["activity_id"]
            etype = match.get("entity_type", "activity")
            post.post_id = f"urn:li:{etype}:{aid}"
            post.post_url = f"https://www.linkedin.com/feed/update/urn:li:{etype}:{aid}/"
            used_ids.add(aid)
            merged_count += 1

    # Pass 2: Author matching
    for post in dom_posts:
        if post.post_id and post.post_id.startswith("urn:li:"):
            continue

        match = interceptor.match_by_author(post.author_name, used_ids)
        if match:
            aid = match["activity_id"]
            etype = match.get("entity_type", "activity")
            post.post_id = f"urn:li:{etype}:{aid}"
            post.post_url = f"https://www.linkedin.com/feed/update/urn:li:{etype}:{aid}/"
            used_ids.add(aid)
            merged_count += 1

    # Pass 3: Positional fallback — assign remaining URNs to remaining posts by order
    remaining_urns = [
        u for u in interceptor.get_ordered_urns()
        if u["activity_id"] not in used_ids
    ]
    remaining_posts = [
        p for p in dom_posts
        if not p.post_id or not p.post_id.startswith("urn:li:")
    ]

    for post, urn_entry in zip(remaining_posts, remaining_urns):
        aid = urn_entry["activity_id"]
        etype = urn_entry.get("entity_type", "activity")
        post.post_id = f"urn:li:{etype}:{aid}"
        post.post_url = f"https://www.linkedin.com/feed/update/urn:li:{etype}:{aid}/"
        used_ids.add(aid)
        merged_count += 1

    # Pass 4: Hash fallback for any still-unmatched posts (so every post has some ID)
    hash_count = 0
    for post in dom_posts:
        if not post.post_id or not post.post_id.startswith("urn:li:"):
            post.post_id = _generate_hash_id(post)
            post.post_url = ""  # Can't generate a valid URL from hash
            hash_count += 1

    print(f"[MERGE] Matched {merged_count} DOM posts to API URNs")
    if hash_count:
        print(f"[MERGE] {hash_count} posts got hash-based fallback IDs (no post_url for these)")

    return dom_posts


async def expand_all_posts(page: Page):
    """Click all 'see more' / '...more' buttons to expand truncated post text."""
    try:
        # Use Playwright locator to find and click all "more" buttons reliably
        more_buttons = page.locator('button:text("…more"), button:text("...more"), button:text("see more")')
        count = await more_buttons.count()
        clicked = 0
        for i in range(count):
            try:
                btn = more_buttons.nth(i)
                if await btn.is_visible():
                    await btn.click(timeout=1000)
                    clicked += 1
            except Exception:
                pass

        # Also try span-based "more" triggers
        more_spans = page.locator('span[role="button"]:text("…more"), span[role="button"]:text("see more")')
        span_count = await more_spans.count()
        for i in range(span_count):
            try:
                span = more_spans.nth(i)
                if await span.is_visible():
                    await span.click(timeout=1000)
                    clicked += 1
            except Exception:
                pass

        if clicked > 0:
            print(f"[SCRAPER] Expanded {clicked} truncated posts")
            await asyncio.sleep(1)  # wait for full text to render
    except Exception as e:
        print(f"[SCRAPER] Expand posts error (non-fatal): {e}")


async def extract_post_urls_via_menu(page: Page, posts: list[PostData]) -> list[PostData]:
    """
    Click the three-dot menu on each post missing a URL,
    then click 'Copy link to post' to capture the URL from the clipboard.
    """
    missing = [i for i, p in enumerate(posts) if not p.post_url or "/feed/update/" not in p.post_url]
    if not missing:
        return posts

    print(f"\n[ENRICH] Clicking three-dot menus for {len(missing)} posts without URLs...")
    enriched = 0

    # Find all three-dot menu buttons on the page
    menu_buttons = page.locator('[role="listitem"] button[aria-label*="More actions"], [role="listitem"] button[aria-label*="Open control menu"]')
    menu_count = await menu_buttons.count()

    for idx in missing:
        if idx >= menu_count:
            break

        try:
            menu_btn = menu_buttons.nth(idx)
            if not await menu_btn.is_visible():
                continue

            # Click the three-dot menu
            await menu_btn.click(timeout=2000)
            await asyncio.sleep(0.5)

            # Look for "Copy link to post" option
            copy_link = page.locator('div[role="menu"] span:text("Copy link to post"), div[role="menu"] span:text("Copy link")')
            if await copy_link.count() > 0:
                # Before clicking, get the href from the menu item's parent
                link_url = await page.evaluate(r"""
                () => {
                    const items = document.querySelectorAll('div[role="menu"] [role="menuitem"]');
                    for (const item of items) {
                        const text = (item.textContent || '').trim();
                        if (text.includes('Copy link')) {
                            // Check for data attributes with URL
                            const href = item.getAttribute('href') || '';
                            if (href.includes('/feed/update/')) return href;

                            // Check onclick or data attrs
                            for (const attr of item.attributes) {
                                if (attr.value.includes('/feed/update/')) return attr.value;
                                const m = attr.value.match(/urn:li:(?:activity|ugcPost):\d{15,25}/);
                                if (m) return 'https://www.linkedin.com/feed/update/' + m[0] + '/';
                            }

                            // Check parent element attributes
                            const parent = item.closest('[data-urn], [data-control-urn]');
                            if (parent) {
                                for (const attr of parent.attributes) {
                                    const m = attr.value.match(/urn:li:(?:activity|ugcPost):\d{15,25}/);
                                    if (m) return 'https://www.linkedin.com/feed/update/' + m[0] + '/';
                                }
                            }
                        }
                    }

                    // Also scan the menu itself for URNs
                    const menu = document.querySelector('div[role="menu"]');
                    if (menu) {
                        const html = menu.innerHTML || '';
                        const m = html.match(/urn:li:(?:activity|ugcPost):\d{15,25}/);
                        if (m) return 'https://www.linkedin.com/feed/update/' + m[0] + '/';
                    }
                    return null;
                }
                """)

                if link_url and '/feed/update/' in link_url:
                    post = posts[idx]
                    post.post_url = link_url
                    # Also extract post_id from URL
                    urn_match = re.search(r'(urn:li:(?:activity|ugcPost):\d{15,25})', link_url)
                    if urn_match:
                        post.post_id = urn_match.group(1)
                    enriched += 1

            # Close the menu by pressing Escape
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)

        except Exception:
            # Close any open menu
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    print(f"[ENRICH] Found {enriched} URLs via three-dot menus")
    return posts


async def scroll_and_collect(
    page: Page,
    keyword: str,
    scroll_count: int,
    interceptor: APIInterceptor,
) -> list[PostData]:
    """Scroll through search results and extract posts."""
    all_posts: list[PostData] = []
    seen_keys: set[str] = set()

    for scroll_num in range(1, scroll_count + 1):
        print(f"[SCRAPER] Scroll {scroll_num}/{scroll_count}...")

        # Click all "...more" / "see more" buttons to expand truncated posts
        await expand_all_posts(page)

        page_posts = await extract_all_posts(page, keyword)

        new_in_scroll = 0
        for post in page_posts:
            if post.post_id and post.post_id.startswith("urn:li:"):
                dedup_key = post.post_id
            else:
                dedup_key = f"{post.author_name}:{post.post_text[:100]}"

            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            all_posts.append(post)
            new_in_scroll += 1

        print(
            f"[SCRAPER] Found {len(page_posts)} posts, "
            f"{new_in_scroll} new (total: {len(all_posts)})"
        )
        print(f"[SCRAPER] {interceptor.stats()}")

        # Scroll to bottom to trigger lazy loading + new API calls
        await page.evaluate("""
            () => {
                const main = document.querySelector('[role="main"]') || document.body;
                main.scrollTop = main.scrollHeight;
                window.scrollTo(0, document.body.scrollHeight);
            }
        """)
        await random_delay()
        await asyncio.sleep(3)

        # Try clicking "Load more" buttons
        try:
            for text in ["Show more results", "See more results", "Load more"]:
                btn = page.locator(f"button:has-text('{text}')").first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    print(f"[SCRAPER] Clicked '{text}'")
                    await random_delay(1, 3)
                    break
        except Exception:
            pass

    return all_posts


async def enrich_post_urls(page: Page, posts: list[PostData]) -> list[PostData]:
    """
    Try to get URLs for posts missing them by clicking the three-dot menu
    and extracting the 'Copy link to post' option which contains the URL.
    """
    missing = [i for i, p in enumerate(posts) if not p.post_url or "/feed/update/" not in p.post_url]
    if not missing:
        return posts

    print(f"\n[ENRICH] Attempting to find URLs for {len(missing)} posts without links...")

    # Use JavaScript to scan all dropdown menus, share buttons, and copy-link elements
    # that might contain post URLs after they've been loaded
    enriched_urls = await page.evaluate(r"""
    () => {
        const results = {};
        // Scan all elements for post URLs in href/data attributes
        const allLinks = document.querySelectorAll('a[href*="/feed/update/"]');
        for (const link of allLinks) {
            const href = link.getAttribute('href') || '';
            const m = href.match(/\/feed\/update\/(urn:li:(?:activity|ugcPost):\d{15,25})/);
            if (m) {
                results[m[1]] = 'https://www.linkedin.com/feed/update/' + m[1] + '/';
            }
        }

        // Also scan for data attributes containing activity URNs
        const allEls = document.querySelectorAll('[data-urn], [data-activity-urn]');
        for (const el of allEls) {
            for (const attr of el.attributes) {
                const val = attr.value || '';
                const m = val.match(/(urn:li:(?:activity|ugcPost):\d{15,25})/);
                if (m && !results[m[1]]) {
                    results[m[1]] = 'https://www.linkedin.com/feed/update/' + m[1] + '/';
                }
            }
        }

        // Scan clipboard copy buttons that might have post URLs
        const copyBtns = document.querySelectorAll('[data-copy-text], [data-clipboard-text]');
        for (const btn of copyBtns) {
            const text = btn.getAttribute('data-copy-text') || btn.getAttribute('data-clipboard-text') || '';
            if (text.includes('/feed/update/')) {
                const m = text.match(/(urn:li:(?:activity|ugcPost):\d{15,25})/);
                if (m) results[m[1]] = text;
            }
        }

        return results;
    }
    """)

    # Match enriched URLs back to posts
    enriched_count = 0
    for i in missing:
        post = posts[i]
        # Check if we found a URL matching this post's hash-based ID
        if post.post_id and post.post_id.startswith("urn:li:"):
            url = enriched_urls.get(post.post_id)
            if url:
                post.post_url = url
                enriched_count += 1
        else:
            # Try matching by checking all found URNs against post text
            for urn, url in enriched_urls.items():
                if urn not in {p.post_id for p in posts}:
                    post.post_id = urn
                    post.post_url = url
                    enriched_count += 1
                    break

    print(f"[ENRICH] Found {enriched_count} additional URLs")
    return posts


async def run_scraper(
    keyword: str,
    scroll_count: int,
    headless: bool | None = None,
    sort_by: str | None = None,
):
    """Main scraper entry point."""
    if headless is not None:
        Config.HEADLESS = headless
    if sort_by is not None:
        Config.SORT_BY = sort_by

    print(f"\n{'='*60}")
    print(f"  LinkedIn Post Scraper (API Interception + DOM)")
    print(f'  Keyword:  "{keyword}"')
    print(f"  Scrolls:  {scroll_count}")
    print(f"  Sort by:  {Config.SORT_BY}")
    print(f"  Headless: {Config.HEADLESS}")
    print(f"  Time:     {datetime.now().isoformat()}")
    print(f"{'='*60}\n")

    interceptor = APIInterceptor(debug=True)

    async with async_playwright() as pw:
        context, auth_ok = await get_authenticated_context(pw)

        if not auth_ok:
            print("[SCRAPER] Authentication failed. Exiting.")
            return

        page = await context.new_page()

        # Attach API interceptor BEFORE navigating
        page.on("response", interceptor.on_response)
        print("[SCRAPER] API interceptor attached — listening for LinkedIn API responses\n")

        try:
            await search_posts(page, keyword, Config.SORT_BY)

            await asyncio.sleep(1)
            print(f"[SCRAPER] After page load: {interceptor.stats()}\n")

            posts = await scroll_and_collect(page, keyword, scroll_count, interceptor)

            if posts:
                # Merge API URN data into DOM posts
                print(
                    f"\n[MERGE] Starting merge — "
                    f"{len(posts)} DOM posts, "
                    f"{len(interceptor.captured_urns)} API URNs"
                )
                posts = _merge_api_into_dom(posts, interceptor)

                # Try to find URLs for posts that are still missing them
                posts = await enrich_post_urls(page, posts)
                posts = await extract_post_urls_via_menu(page, posts)

                log_path = save_posts_to_log(posts, keyword)
                print_summary(posts, keyword)
                print(f"\n[SCRAPER] Log saved: {log_path}")

                # Stats
                with_urn = sum(1 for p in posts if p.post_id.startswith("urn:li:"))
                with_hash = sum(1 for p in posts if p.post_id.startswith("hash:"))
                with_url = sum(1 for p in posts if p.post_url and "/feed/update/" in p.post_url)
                print(f"[SCRAPER] Posts with LinkedIn URN:   {with_urn}/{len(posts)}")
                print(f"[SCRAPER] Posts with hash fallback:  {with_hash}/{len(posts)}")
                print(f"[SCRAPER] Posts with valid post_url: {with_url}/{len(posts)}")

                # Save debug info if API interception didn't capture enough
                if len(interceptor.captured_urns) < len(posts) // 2:
                    interceptor.save_debug()

                # Push to MongoDB Atlas (if MONGO_URI is configured)
                import os
                if os.getenv("MONGO_URI"):
                    try:
                        from db import upsert_posts, get_collection_stats
                        post_dicts = [p.to_dict() for p in posts]
                        upsert_posts(post_dicts, keyword)
                        stats = get_collection_stats()
                        print(
                            f"[DB] Collection totals: {stats['total_posts']} posts, "
                            f"{stats['unique_authors']} authors, "
                            f"avg {stats['avg_scrape_count']} scrapes/post"
                        )
                    except Exception as e:
                        print(f"[DB] MongoDB error: {e}")
                        print("[DB] Posts saved to log file — MongoDB push failed.")
                else:
                    print("[DB] MONGO_URI not set — skipping MongoDB. Posts saved to log file only.")
            else:
                print("[SCRAPER] No posts were extracted.")
                print(f"[SCRAPER] {interceptor.stats()}")

        except Exception as e:
            print(f"[SCRAPER] Error during scraping: {e}")
            raise
        finally:
            await page.close()
            await context.close()


def main():
    parser = argparse.ArgumentParser(description="LinkedIn Post Scraper")
    parser.add_argument("keyword", help="Search keyword(s)")
    parser.add_argument(
        "--scrolls", type=int, default=None,
        help=f"Number of scroll iterations (default: {Config.SCROLL_COUNT})",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run in headless mode",
    )
    parser.add_argument(
        "--sort", type=str, default=None, choices=["relevance", "date_posted"],
        help="Sort order: 'relevance' for top posts, 'date_posted' for newest (default: relevance)",
    )
    args = parser.parse_args()

    scroll_count = args.scrolls or Config.SCROLL_COUNT
    headless = True if args.headless else None
    asyncio.run(run_scraper(args.keyword, scroll_count, headless, args.sort))


if __name__ == "__main__":
    main()