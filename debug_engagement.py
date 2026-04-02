from __future__ import annotations

"""
Debug engagement buttons and social counts in LinkedIn's SDUI DOM.
"""

import asyncio
import sys
import urllib.parse

from playwright.async_api import async_playwright

from auth import get_authenticated_context, random_delay
from config import Config


async def debug_engagement(keyword: str):
    Config.validate()

    async with async_playwright() as pw:
        context, auth_ok = await get_authenticated_context(pw)
        if not auth_ok:
            return

        page = await context.new_page()

        encoded = urllib.parse.quote(keyword)
        url = (
            f"https://www.linkedin.com/search/results/content/"
            f"?keywords={encoded}&origin=GLOBAL_SEARCH_HEADER&sortBy=date_posted"
        )

        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(8)
        await page.evaluate("window.scrollBy(0, 800)")
        await asyncio.sleep(3)

        # 1. Dump ALL buttons with aria-labels inside post containers
        print("\n[DEBUG] === All buttons with aria-label inside post containers ===\n")
        buttons = await page.evaluate("""
            () => {
                const containers = document.querySelectorAll('[data-display-contents="true"]');
                const results = [];
                let postIdx = 0;
                for (const post of containers) {
                    const fullText = (post.innerText || '').trim();
                    if (fullText.length < 30) continue;
                    if (!fullText.includes('Follow') && !fullText.match(/\\d+[mhdw]/)) continue;

                    postIdx++;
                    if (postIdx > 3) break; // Only inspect first 3 posts

                    const btns = post.querySelectorAll('button');
                    for (const btn of btns) {
                        const ariaLabel = btn.getAttribute('aria-label') || '(none)';
                        const text = btn.textContent.trim().substring(0, 80);
                        const cls = typeof btn.className === 'string' ? btn.className.substring(0, 80) : '';
                        results.push({
                            post: postIdx,
                            ariaLabel: ariaLabel.substring(0, 150),
                            text: text,
                            cls: cls
                        });
                    }
                }
                return results;
            }
        """)
        for b in buttons:
            print(f"  Post #{b['post']} | aria-label: \"{b['ariaLabel']}\"")
            print(f"           text: \"{b['text']}\"")
            print()

        # 2. Look for ANY elements with reaction/like/comment counts
        print("\n[DEBUG] === Elements containing numbers near social actions ===\n")
        social = await page.evaluate("""
            () => {
                const containers = document.querySelectorAll('[data-display-contents="true"]');
                const results = [];
                let postIdx = 0;
                for (const post of containers) {
                    const fullText = (post.innerText || '').trim();
                    if (fullText.length < 30 || (!fullText.includes('Follow') && !fullText.match(/\\d+[mhdw]/))) continue;

                    postIdx++;
                    if (postIdx > 3) break;

                    // Find all spans/divs that contain just a number or "X reactions" etc
                    const els = post.querySelectorAll('span, div, a, button');
                    for (const el of els) {
                        const text = el.textContent.trim();
                        // Match: "1", "23", "1,234", "2 reactions", "5 comments", etc.
                        if (text.match(/^\\d[\\d,]*\\s*(reaction|like|comment|repost|share)?s?$/i) ||
                            text.match(/^\\d+$/) && text !== '0') {
                            const parent = el.parentElement;
                            const parentText = parent ? parent.textContent.trim().substring(0, 100) : '';
                            const tag = el.tagName;
                            const ariaLabel = el.getAttribute('aria-label') || '';
                            const href = el.getAttribute('href') || '';
                            results.push({
                                post: postIdx,
                                tag: tag,
                                text: text,
                                ariaLabel: ariaLabel.substring(0, 100),
                                href: href.substring(0, 100),
                                parentText: parentText
                            });
                        }
                    }
                }
                return results;
            }
        """)
        if social:
            for s in social:
                print(f"  Post #{s['post']} | <{s['tag']}> text=\"{s['text']}\" aria-label=\"{s['ariaLabel']}\"")
                print(f"           href=\"{s['href']}\" parentText=\"{s['parentText'][:80]}\"")
                print()
        else:
            print("  (none found)")

        # 3. Dump the bottom section of first post (where Like/Comment/Repost buttons live)
        print("\n[DEBUG] === Bottom section HTML of first post (social bar) ===\n")
        bottom = await page.evaluate("""
            () => {
                const containers = document.querySelectorAll('[data-display-contents="true"]');
                for (const post of containers) {
                    const fullText = (post.innerText || '').trim();
                    if (fullText.length < 30 || !fullText.includes('Follow')) continue;

                    // Get the last 2000 chars of the outer HTML (social bar is at bottom)
                    const html = post.outerHTML;
                    return html.substring(Math.max(0, html.length - 3000));
                }
                return '(no post found)';
            }
        """)
        print(bottom[:2000])

        await page.close()
        await context.close()


if __name__ == "__main__":
    keyword = sys.argv[1] if len(sys.argv) > 1 else "battery energy storage"
    asyncio.run(debug_engagement(keyword))