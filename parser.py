from __future__ import annotations

"""
Extracts structured post data from LinkedIn's SDUI search results.

Key fixes over original:
  - Extracts post_id/post_url from rehydration <script> data + innerHTML URN scan
  - Headline extraction filters out engagement counts, connection degree, UI labels
  - Uses [data-testid="expandable-text-box"] for reliable post text
  - Uses role="listitem" as primary container selector (SDUI standard)
"""

import re
from dataclasses import dataclass, asdict
from datetime import datetime

from playwright.async_api import Page


@dataclass
class PostData:
    post_id: str = ""
    post_url: str = ""
    author_name: str = ""
    author_headline: str = ""
    author_profile_url: str = ""
    post_text: str = ""
    posted_time: str = ""
    num_likes: int = 0
    num_comments: int = 0
    num_reposts: int = 0
    media_type: str = ""
    scraped_at: str = ""
    keyword: str = ""

    def to_dict(self):
        return asdict(self)

    def summary_line(self):
        text_preview = self.post_text[:80].replace("\n", " ") if self.post_text else "(no text)"
        return (
            f"[{self.posted_time}] {self.author_name} | "
            f"\u2764 {self.num_likes} \U0001f4ac {self.num_comments} \U0001f501 {self.num_reposts} | "
            f"{text_preview}..."
        )


def _parse_reaction_count(text: str) -> int:
    if not text:
        return 0
    text = text.strip().replace(",", "")
    match = re.match(r"([\d.]+)\s*([KkMm])?", text)
    if not match:
        return 0
    num = float(match.group(1))
    suffix = (match.group(2) or "").upper()
    if suffix == "K":
        num *= 1000
    elif suffix == "M":
        num *= 1_000_000
    return int(num)


EXTRACT_ALL_POSTS_JS = r"""
() => {
    // ========================================================================
    // Step 1: Extract ALL activity URNs from the rehydration script data.
    // LinkedIn embeds these in <script id="rehydrate-data"> as escaped JSON.
    // They appear as e.g. "reactionState-urn:li:activity:7444259422253621248"
    // ========================================================================
    const urnMap = {};  // activityId -> full URN
    const orderedUrns = [];  // preserve order for positional fallback

    for (const script of document.querySelectorAll('script')) {
        const text = script.textContent || '';
        if (!text.includes('urn:li:activity') && !text.includes('urn:li:ugcPost')) continue;

        for (const m of text.matchAll(/urn:li:(activity|ugcPost):(\d{15,25})/g)) {
            const fullUrn = 'urn:li:' + m[1] + ':' + m[2];
            if (!urnMap[m[2]]) {
                urnMap[m[2]] = fullUrn;
                orderedUrns.push(fullUrn);
            }
        }
    }

    // Deduplicate ordered URNs
    const uniqueUrns = [...new Set(orderedUrns)];

    // ========================================================================
    // Step 2: Find post containers — try multiple strategies
    // LinkedIn changes these frequently
    // ========================================================================
    let containers = [];

    // Strategy A: role="article" with feed-shared-update-v2 (current as of Apr 2026)
    if (containers.length === 0) {
        containers = [...document.querySelectorAll('[role="article"].feed-shared-update-v2')];
    }

    // Strategy B: role="article" with data-urn
    if (containers.length === 0) {
        containers = [...document.querySelectorAll('[role="article"][data-urn]')];
    }

    // Strategy C: feed-shared-update-v2 class
    if (containers.length === 0) {
        containers = [...document.querySelectorAll('.feed-shared-update-v2')];
    }

    // Strategy D: role="listitem" (old LinkedIn layout)
    if (containers.length === 0) {
        containers = [...document.querySelectorAll('[role="listitem"]')];
    }

    // Strategy E: data-display-contents (SDUI fallback)
    if (containers.length === 0) {
        const allDC = document.querySelectorAll('[data-display-contents="true"]');
        for (const c of allDC) {
            let parent = c.parentElement, isNested = false;
            while (parent) {
                if (parent.hasAttribute && parent.hasAttribute('data-display-contents')) {
                    isNested = true; break;
                }
                parent = parent.parentElement;
            }
            if (!isNested) containers.push(c);
        }
    }

    const posts = [];
    let urnIdx = 0;

    for (const post of containers) {
        const fullText = (post.innerText || '').trim();

        // Skip small containers, feedback cards, ads
        if (fullText.length < 50) continue;
        if (fullText.startsWith('Are these results helpful')) continue;

        // Must look like a feed post
        const hasFollow = post.querySelector('button[aria-label*="Follow"]');
        const hasFeedPost = fullText.includes('Feed post');
        const hasTime = /\d+[mhdwMy]\s*[•·]/.test(fullText) || /\b\d+[mhdwMy]\b/.test(fullText);
        if (!hasFollow && !hasFeedPost && !hasTime) continue;

        const result = {
            author_name: '', author_headline: '', author_profile_url: '',
            post_text: '', posted_time: '', post_url: '', post_id: '',
            num_likes: 0, num_comments: 0, num_reposts: 0, media_type: 'text'
        };

        // ==== AUTHOR NAME ====
        const followBtn = post.querySelector('button[aria-label*="Follow"]');
        if (followBtn) {
            const label = followBtn.getAttribute('aria-label') || '';
            const m = label.match(/Follow\s+(.+)/i);
            if (m) result.author_name = m[1].trim();
        }

        // ==== AUTHOR PROFILE URL ====
        const profileLinks = post.querySelectorAll('a[href*="/in/"], a[href*="/company/"]');
        for (const link of profileLinks) {
            const href = link.getAttribute('href') || '';
            if (href.includes('/in/') || href.includes('/company/')) {
                let cleanUrl = href.split('?')[0].replace(/\/posts\/?$/, '').replace(/\/+$/, '');
                result.author_profile_url = cleanUrl;
                if (!result.author_name) {
                    const text = link.textContent.trim().split('\n')[0].trim();
                    if (text && text !== 'Follow' && text.length > 1 && text.length < 80) {
                        result.author_name = text;
                    }
                }
                break;
            }
        }

        // ==== AUTHOR HEADLINE ====
        // Strategy: find <p> tags in the author info section.
        // The SDUI structure is: author link -> div with name, then p with headline
        // We scan <p> tags after the author name, skipping known non-headline content.
        const HEADLINE_SKIP = new Set([
            'Follow', 'Like', 'Comment', 'Repost', 'Send', 'Feed post',
            'Show translation', 'Report', 'Copy link', 'Save'
        ]);
        const HEADLINE_SKIP_REGEX = /^(\s*[•·]\s*\d+(st|nd|rd|th)\+?\s*$|\d+[mhdwMy]|\d+\s*(reactions?|comments?|reposts?|shares?|likes?)$)/i;

        if (result.author_name) {
            const allPs = post.querySelectorAll('p');
            let nameFound = false;
            for (const p of allPs) {
                const text = p.textContent.trim();
                if (!text) continue;

                // Check if this p contains the author name
                if (text.includes(result.author_name)) {
                    nameFound = true;
                    continue;
                }

                if (nameFound && text.length > 3 && text.length < 300) {
                    if (HEADLINE_SKIP.has(text)) continue;
                    if (HEADLINE_SKIP_REGEX.test(text)) continue;
                    // Skip connection degree patterns like "• 3rd+" or " • 3rd+"
                    if (/^\s*[•·]\s*\d/.test(text)) continue;
                    // Skip time + globe patterns like "3m •"
                    if (/^\d+[mhdwMy]\s*[•·]/.test(text)) continue;
                    // Skip very short generic text
                    if (text.length < 5) continue;

                    result.author_headline = text;
                    break;
                }
            }
        }

        // ==== POSTED TIME ====
        const pTags = post.querySelectorAll('p');
        for (const p of pTags) {
            const text = p.textContent.trim();
            const m = text.match(/^(\d+[mhdwMy](?:o|r|in|our|ay|eek|onth|ear)?s?)\s*[•·]/);
            if (m) { result.posted_time = m[1]; break; }
        }
        if (!result.posted_time) {
            const tw = document.createTreeWalker(post, NodeFilter.SHOW_TEXT);
            while (tw.nextNode()) {
                const t = tw.currentNode.textContent.trim();
                const m1 = t.match(/^(\d+[mhdwMy](?:o|r|in|our|ay|eek|onth|ear)?s?)\b/i);
                if (m1) { result.posted_time = m1[1]; break; }
                const m2 = t.match(/(\d+[mhdwMy](?:o|r)?s?)\s*[•·]/);
                if (m2) { result.posted_time = m2[1]; break; }
            }
        }

        // ==== POST TEXT ====
        // Clean function to strip "more" / "see less" artifacts
        const cleanText = (t) => t.trim()
            .replace(/…\s*more\s*$/i, '')
            .replace(/\.\.\.\s*more\s*$/i, '')
            .replace(/\s*see\s*less\s*$/i, '')
            .replace(/\s*show\s*less\s*$/i, '')
            .replace(/\s*…\s*$/i, '')
            .trim()
            .substring(0, 5000);

        // Try multiple selectors — LinkedIn changes these frequently
        const textSelectors = [
            '[data-testid="expandable-text-box"]',
            '[data-ad-dom-id] .break-words',
            '.feed-shared-update-v2__description .break-words',
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

        // Also try aria-expanded containers (LinkedIn reveals full text here)
        if (!result.post_text || result.post_text.length < 50) {
            const expanded = post.querySelector('[aria-expanded="true"] .break-words');
            if (expanded) {
                const text = cleanText(expanded.textContent);
                if (text.length > (result.post_text || '').length) {
                    result.post_text = text;
                }
            }
        }

        // Fallback: find the longest text block in the post container
        // This is the most resilient approach — doesn't depend on selectors
        if (!result.post_text || result.post_text.length < 50) {
            let longest = result.post_text || '';
            const SKIP = new Set([
                'Follow', 'Like', 'Comment', 'Repost', 'Send', 'Report',
                'Copy link', 'Save', 'Not interested', 'Feed post',
                'Show translation', '…more', '...more', 'see more', 'see less'
            ]);

            const textEls = post.querySelectorAll('span[dir="ltr"], span[dir="auto"], span[lang], p, div.break-words');
            for (const el of textEls) {
                const text = el.textContent.trim();
                if (text.length <= longest.length || text.length < 20) continue;
                if (text === result.author_name || text === result.author_headline) continue;
                if (SKIP.has(text)) continue;
                // Skip engagement count text
                if (/^\d[\d,]*\s*(reactions?|comments?|reposts?|likes?|shares?)$/i.test(text)) continue;
                longest = text;
            }
            if (longest.length > (result.post_text || '').length) {
                result.post_text = cleanText(longest);
            }
        }

        // ==== POST URL / POST ID ====
        // Strategy 0: Check data-urn attribute on the container itself
        // (current LinkedIn layout puts data-urn="urn:li:activity:XXX" on [role="article"])
        const dataUrn = post.getAttribute('data-urn') || '';
        if (dataUrn) {
            const dm = dataUrn.match(/urn:li:(activity|ugcPost):(\d{15,25})/);
            if (dm) {
                result.post_id = dm[0];
                result.post_url = 'https://www.linkedin.com/feed/update/' + dm[0] + '/';
            }
        }

        // Also check parent elements for data-urn
        if (!result.post_id) {
            let parent = post.parentElement;
            for (let i = 0; i < 5 && parent; i++) {
                const pu = parent.getAttribute('data-urn') || '';
                const pm = pu.match(/urn:li:(activity|ugcPost):(\d{15,25})/);
                if (pm) {
                    result.post_id = pm[0];
                    result.post_url = 'https://www.linkedin.com/feed/update/' + pm[0] + '/';
                    break;
                }
                parent = parent.parentElement;
            }
        }

        // Strategy A: Scan this container's innerHTML for activity URNs
        if (!result.post_id) {
            const html = post.innerHTML || '';
            let urnMatch = html.match(/urn:li:activity:(\d{15,25})/);
            if (urnMatch) {
                result.post_id = 'urn:li:activity:' + urnMatch[1];
                result.post_url = 'https://www.linkedin.com/feed/update/urn:li:activity:' + urnMatch[1] + '/';
            }
            if (!result.post_id) {
                urnMatch = html.match(/urn:li:ugcPost:(\d{15,25})/);
                if (urnMatch) {
                    result.post_id = 'urn:li:ugcPost:' + urnMatch[1];
                    result.post_url = 'https://www.linkedin.com/feed/update/urn:li:ugcPost:' + urnMatch[1] + '/';
                }
            }
        }

        // Strategy B: Check element attributes directly
        if (!result.post_id) {
            const allEls = post.querySelectorAll('*');
            outer:
            for (const el of allEls) {
                for (const attr of el.attributes) {
                    const val = attr.value || '';
                    let m = val.match(/activity[:\-](\d{15,25})/);
                    if (m) {
                        result.post_id = 'urn:li:activity:' + m[1];
                        result.post_url = 'https://www.linkedin.com/feed/update/urn:li:activity:' + m[1] + '/';
                        break outer;
                    }
                    m = val.match(/ugcPost[:\-](\d{15,25})/);
                    if (m) {
                        result.post_id = 'urn:li:ugcPost:' + m[1];
                        result.post_url = 'https://www.linkedin.com/feed/update/urn:li:ugcPost:' + m[1] + '/';
                        break outer;
                    }
                }
            }
        }

        // Strategy C: Positional fallback from rehydration URNs
        if (!result.post_id && urnIdx < uniqueUrns.length) {
            result.post_id = uniqueUrns[urnIdx];
            result.post_url = 'https://www.linkedin.com/feed/update/' + uniqueUrns[urnIdx] + '/';
            urnIdx++;
        }

        // ==== ENGAGEMENT ====
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

        // Fallback: button aria-labels
        if (result.num_likes === 0 && result.num_comments === 0) {
            const buttons = post.querySelectorAll('button[aria-label]');
            for (const btn of buttons) {
                const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                if (label.includes('reaction') || label.includes('like')) {
                    const m = label.match(/(\d[\d,]*)\s*(?:reaction|like)/);
                    if (m) result.num_likes = parseInt(m[1].replace(/,/g, ''));
                }
                if (label.includes('comment') && !label.includes('add a comment')) {
                    const m = label.match(/(\d[\d,]*)\s*comment/);
                    if (m) result.num_comments = parseInt(m[1].replace(/,/g, ''));
                }
                if (label.includes('repost') || label.includes('share')) {
                    const m = label.match(/(\d[\d,]*)\s*(?:repost|share)/);
                    if (m) result.num_reposts = parseInt(m[1].replace(/,/g, ''));
                }
            }
        }

        // ==== MEDIA TYPE ====
        if (post.querySelector('video, [data-vjs-player]')) {
            result.media_type = 'video';
        } else if (post.querySelector('a[href*="lnkd.in"], article, a[href*="/pulse/"]')) {
            result.media_type = 'article';
        } else {
            const imgs = post.querySelectorAll('img');
            for (const img of imgs) {
                const src = img.getAttribute('src') || '';
                if (src.includes('profile-displayphoto') || src.includes('px.ads') ||
                    src.includes('static.licdn') || src.includes('reactions') ||
                    src.includes('aero') || src.includes('feed-assets')) continue;
                if (src.includes('feedshare') || src.includes('/D4') || src.includes('/D5')) {
                    result.media_type = 'image';
                    break;
                }
                const w = img.naturalWidth || img.width || 0;
                if (w > 100) { result.media_type = 'image'; break; }
            }
        }

        if (result.author_name || result.post_text.length > 20) {
            posts.push(result);
        }
    }

    return posts;
}
"""


async def extract_all_posts(page: Page, keyword: str) -> list[PostData]:
    """Extract all posts from current page in one JS evaluation."""
    try:
        raw_posts = await page.evaluate(EXTRACT_ALL_POSTS_JS)
        if not raw_posts:
            return []

        posts = []
        for data in raw_posts:
            post = PostData(
                post_id=data.get("post_id", ""),
                post_url=data.get("post_url", ""),
                author_name=data.get("author_name", ""),
                author_headline=data.get("author_headline", ""),
                author_profile_url=data.get("author_profile_url", ""),
                post_text=data.get("post_text", ""),
                posted_time=data.get("posted_time", ""),
                num_likes=data.get("num_likes", 0),
                num_comments=data.get("num_comments", 0),
                num_reposts=data.get("num_reposts", 0),
                media_type=data.get("media_type", "text"),
                scraped_at=datetime.now().isoformat(),
                keyword=keyword,
            )
            posts.append(post)

        return posts
    except Exception as e:
        print(f"[PARSER] Error extracting posts: {e}")
        return []


async def get_post_count(page: Page) -> int:
    """Count potential post containers."""
    return await page.evaluate("""
        () => {
            const listitems = document.querySelectorAll('[role="listitem"]');
            if (listitems.length > 0) return listitems.length;
            return document.querySelectorAll('[data-display-contents="true"]').length;
        }
    """)