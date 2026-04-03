"""
Scrape all posts from a specific LinkedIn author's profile page.

Usage:
  python3 author_scraper.py "https://www.linkedin.com/in/ankitmttl"
  python3 author_scraper.py "Tripti Verma" --company "Ingro Energy"

Stores results in a separate `author_posts` collection in MongoDB.
"""

import asyncio
import argparse
import os
import re
import sys
from datetime import datetime, timezone

from playwright.async_api import async_playwright, Page
from dotenv import load_dotenv

load_dotenv()

from auth import get_authenticated_context, random_delay
from config import Config


async def find_author_profile(page: Page, name: str, company: str = "") -> str:
    """Search LinkedIn for an author and return their profile URL."""
    query = name
    if company:
        query += f" {company}"

    search_url = f"https://www.linkedin.com/search/results/people/?keywords={query}&origin=GLOBAL_SEARCH_HEADER"
    await page.goto(search_url)
    await asyncio.sleep(3)

    # Get first matching profile URL
    profile_url = await page.evaluate(r"""
    () => {
        const links = document.querySelectorAll('a[href*="/in/"]');
        for (const link of links) {
            const href = link.getAttribute('href') || '';
            if (href.includes('/in/') && !href.includes('/search/')) {
                return href.split('?')[0];
            }
        }
        return null;
    }
    """)

    if profile_url and not profile_url.startswith("http"):
        profile_url = "https://www.linkedin.com" + profile_url

    return profile_url


async def scrape_author_posts(page: Page, profile_url: str, scroll_count: int = 10) -> list[dict]:
    """Navigate to author's 'Recent Activity' / posts page and scrape all posts."""
    # Navigate to their posts page
    posts_url = profile_url.rstrip("/") + "/recent-activity/all/"
    print(f"[AUTHOR] Navigating to: {posts_url}")
    await page.goto(posts_url)
    await asyncio.sleep(4)

    # Get author name from profile
    author_name = await page.evaluate(r"""
    () => {
        const h1 = document.querySelector('h1');
        return h1 ? h1.textContent.trim() : '';
    }
    """)
    print(f"[AUTHOR] Author: {author_name}")

    all_posts = []
    seen_ids = set()

    for scroll_num in range(1, scroll_count + 1):
        print(f"[AUTHOR] Scroll {scroll_num}/{scroll_count}...")

        # Click all "see more" buttons
        expanded = await page.evaluate(r"""
        () => {
            let count = 0;
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
            while (walker.nextNode()) {
                const el = walker.currentNode;
                const tag = el.tagName.toLowerCase();
                if (!['button', 'span'].includes(tag)) continue;
                const text = (el.textContent || '').trim().toLowerCase().replace(/\s+/g, '');
                if (text === '…more' || text === '...more' || text === 'seemore') {
                    el.click();
                    count++;
                }
            }
            return count;
        }
        """)
        if expanded > 0:
            await asyncio.sleep(0.5)

        # Extract posts
        posts = await page.evaluate(r"""
        () => {
            const results = [];
            // Author's activity page uses role="article" or .feed-shared-update-v2
            const containers = document.querySelectorAll(
                '[role="article"].feed-shared-update-v2, [role="article"][data-urn], .feed-shared-update-v2'
            );

            for (const post of containers) {
                const result = {
                    post_id: '', post_url: '', post_text: '', posted_time: '',
                    num_likes: 0, num_comments: 0, num_reposts: 0, media_type: 'text'
                };

                // Post ID from data-urn
                const dataUrn = post.getAttribute('data-urn') || '';
                if (dataUrn) {
                    const m = dataUrn.match(/urn:li:(activity|ugcPost):(\d{15,25})/);
                    if (m) {
                        result.post_id = m[0];
                        result.post_url = 'https://www.linkedin.com/feed/update/' + m[0] + '/';
                    }
                }

                // Post text
                const cleanText = (t) => t.trim()
                    .replace(/…\s*more\s*$/i, '').replace(/\.\.\.\s*more\s*$/i, '')
                    .replace(/\s*see\s*less\s*$/i, '').trim().substring(0, 5000);

                const textSelectors = [
                    '[data-testid="expandable-text-box"]',
                    '.feed-shared-text .break-words',
                    '.update-components-text .break-words',
                    '.break-words[dir="ltr"]',
                ];
                for (const sel of textSelectors) {
                    const el = post.querySelector(sel);
                    if (el) {
                        const text = cleanText(el.textContent);
                        if (text.length > (result.post_text || '').length) {
                            result.post_text = text;
                        }
                    }
                }

                // Fallback: longest text
                if (!result.post_text || result.post_text.length < 20) {
                    let longest = '';
                    const textEls = post.querySelectorAll('span[dir="ltr"], span[dir="auto"], p, div.break-words');
                    for (const el of textEls) {
                        const text = el.textContent.trim();
                        if (text.length > longest.length && text.length > 20) longest = text;
                    }
                    if (longest.length > (result.post_text || '').length) {
                        result.post_text = cleanText(longest);
                    }
                }

                // Posted time
                const fullText = (post.innerText || '');
                const timeMatch = fullText.match(/(\d+)(m|h|d|w|mo|yr)(?:\s|·|•)/);
                if (timeMatch) {
                    result.posted_time = timeMatch[1] + timeMatch[2];
                }

                // Engagement
                const allSpans = post.querySelectorAll('span');
                for (const span of allSpans) {
                    const text = span.textContent.trim().toLowerCase();
                    if (text.match(/^\d[\d,]*\s*reactions?$/) && result.num_likes === 0) {
                        const m = text.match(/^(\d[\d,]*)/);
                        if (m) result.num_likes = parseInt(m[1].replace(/,/g, ''));
                    }
                    if (text.match(/^\d[\d,]*\s*comments?$/) && result.num_comments === 0) {
                        const m = text.match(/^(\d[\d,]*)/);
                        if (m) result.num_comments = parseInt(m[1].replace(/,/g, ''));
                    }
                    if (text.match(/^\d[\d,]*\s*(reposts?|shares?)$/) && result.num_reposts === 0) {
                        const m = text.match(/^(\d[\d,]*)/);
                        if (m) result.num_reposts = parseInt(m[1].replace(/,/g, ''));
                    }
                }

                // Media type
                if (post.querySelector('video')) result.media_type = 'video';
                else if (post.querySelector('article, a[href*="/pulse/"]')) result.media_type = 'article';
                else {
                    const imgs = post.querySelectorAll('img');
                    for (const img of imgs) {
                        const src = img.getAttribute('src') || '';
                        if (src.includes('feedshare') || src.includes('/D4') || src.includes('/D5')) {
                            result.media_type = 'image';
                            break;
                        }
                    }
                }

                if (result.post_text && result.post_text.length > 10) {
                    results.push(result);
                }
            }
            return results;
        }
        """)

        new_count = 0
        for p in posts:
            pid = p.get("post_id") or p.get("post_text", "")[:100]
            if pid not in seen_ids:
                seen_ids.add(pid)
                all_posts.append(p)
                new_count += 1

        print(f"[AUTHOR] Found {len(posts)} posts, {new_count} new (total: {len(all_posts)})")

        # Scroll down
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await random_delay()
        await asyncio.sleep(2)

    return all_posts, author_name


def save_to_db(posts: list[dict], author_name: str, profile_url: str):
    """Save author posts to the author_posts collection."""
    from pymongo import MongoClient, UpdateOne
    from pymongo.errors import BulkWriteError

    uri = os.getenv("MONGO_URI", "")
    db_name = os.getenv("MONGO_DB", "bess_linkedin")
    if not uri:
        print("[DB] MONGO_URI not set — skipping.")
        return

    client = MongoClient(uri)
    db = client[db_name]
    collection = db["author_posts"]

    # Ensure indexes
    collection.create_index("post_id", unique=True)
    collection.create_index("author_name")
    collection.create_index([("num_likes", -1)])

    now = datetime.now(timezone.utc)
    operations = []

    for post in posts:
        pid = post.get("post_id", "")
        if not pid:
            continue

        snapshot = {
            "scraped_at": now,
            "num_likes": post.get("num_likes", 0),
            "num_comments": post.get("num_comments", 0),
            "num_reposts": post.get("num_reposts", 0),
        }

        operation = UpdateOne(
            {"post_id": pid},
            {
                "$setOnInsert": {
                    "post_id": pid,
                    "media_type": post.get("media_type", "text"),
                    "first_scraped_at": now,
                    "posted_time_raw": post.get("posted_time", ""),
                },
                "$set": {
                    "author_name": author_name,
                    "author_profile_url": profile_url,
                    "num_likes": post.get("num_likes", 0),
                    "num_comments": post.get("num_comments", 0),
                    "num_reposts": post.get("num_reposts", 0),
                    "last_scraped_at": now,
                    **({"post_text": post["post_text"]} if post.get("post_text") else {}),
                    **({"post_url": post["post_url"]}
                       if post.get("post_url") and "/feed/update/" in post.get("post_url", "")
                       else {}),
                },
                "$push": {
                    "engagement_history": {
                        "$each": [snapshot],
                        "$slice": -100,
                    }
                },
                "$inc": {"scrape_count": 1},
            },
            upsert=True,
        )
        operations.append(operation)

    if not operations:
        print("[DB] No posts to save.")
        return

    try:
        result = collection.bulk_write(operations, ordered=False)
        print(
            f"[DB] author_posts: {result.upserted_count} new, "
            f"{result.modified_count} updated"
        )
    except BulkWriteError as e:
        print(f"[DB] Bulk write error: {e.details.get('writeErrors', [{}])[0].get('errmsg', '')}")


async def run(name: str, company: str = "", profile_url: str = "", scroll_count: int = 10, headless: bool = True):
    if headless:
        Config.HEADLESS = True

    async with async_playwright() as pw:
        context, auth_ok = await get_authenticated_context(pw)
        if not auth_ok:
            print("[AUTHOR] Auth failed.")
            return

        page = await context.new_page()

        try:
            # If no profile URL given, search for the author
            if not profile_url:
                print(f"[AUTHOR] Searching for: {name} {company}")
                profile_url = await find_author_profile(page, name, company)
                if not profile_url:
                    print("[AUTHOR] Could not find author profile.")
                    return
                print(f"[AUTHOR] Found profile: {profile_url}")

            posts, author_name = await scrape_author_posts(page, profile_url, scroll_count)

            if posts:
                print(f"\n[AUTHOR] Total posts scraped: {len(posts)}")
                with_url = sum(1 for p in posts if p.get("post_url") and "/feed/" in p.get("post_url", ""))
                print(f"[AUTHOR] Posts with URLs: {with_url}/{len(posts)}")

                save_to_db(posts, author_name or name, profile_url)
            else:
                print("[AUTHOR] No posts found.")

        finally:
            await page.close()
            await context.close()


def main():
    parser = argparse.ArgumentParser(description="LinkedIn Author Post Scraper")
    parser.add_argument("name", help="Author name or LinkedIn profile URL")
    parser.add_argument("--company", default="", help="Company name (helps find the right profile)")
    parser.add_argument("--scrolls", type=int, default=10, help="Number of scrolls (default: 10)")
    parser.add_argument("--headless", action="store_true", help="Run headless")
    parser.add_argument("--url", default="", help="Direct LinkedIn profile URL")
    args = parser.parse_args()

    profile_url = args.url or (args.name if args.name.startswith("http") else "")
    name = args.name if not args.name.startswith("http") else ""

    asyncio.run(run(name, args.company, profile_url, args.scrolls, args.headless))


if __name__ == "__main__":
    main()
