from __future__ import annotations

"""
Intercepts LinkedIn's internal API responses to extract post activity URNs.

Instead of parsing LinkedIn's complex SDUI entity structure (which changes
frequently), this takes a brute-force approach: scan the raw response text
for any urn:li:activity:XXXXX or urn:li:ugcPost:XXXXX patterns.

Also attempts to extract text snippets near each URN to help with
matching API URNs to DOM-extracted posts.
"""

import json
import re
from playwright.async_api import Response


class APIInterceptor:
    """Captures activity URNs from LinkedIn API responses via raw text scanning."""

    # URL patterns that indicate a LinkedIn internal API response
    API_PATTERNS = [
        "/voyager/api/",
        "/graphql",
        "/api/search",
        "search/dash",
        "searchDash",
        "/feed/",
        "/contentServing/",
    ]

    def __init__(self, debug: bool = False):
        # activity_id -> { urn, entity_type, text_context }
        self.captured_urns: dict[str, dict] = {}
        self.responses_checked: int = 0
        self.responses_matched: int = 0
        self.debug: bool = debug
        # Store first few matched response snippets for debugging
        self._debug_responses: list[dict] = []

    def _matches_api(self, url: str) -> bool:
        return any(pat in url for pat in self.API_PATTERNS)

    async def on_response(self, response: Response):
        """Playwright response handler — attach via page.on('response', interceptor.on_response)"""
        self.responses_checked += 1
        try:
            url = response.url
            if not self._matches_api(url):
                return

            content_type = response.headers.get("content-type", "")
            if "json" not in content_type and "javascript" not in content_type and "text" not in content_type:
                return

            if response.status != 200:
                return

            body = await response.text()
            if not body:
                return

            self.responses_matched += 1
            self._extract_urns_from_text(body)

            # Save debug info for first 5 matched responses
            if self.debug and len(self._debug_responses) < 5:
                self._debug_responses.append({
                    "url": url[:200],
                    "content_type": content_type,
                    "body_length": len(body),
                    "has_urn": "urn:li:activity" in body or "urn:li:ugcPost" in body,
                    "has_included": '"included"' in body,
                    "first_500_chars": body[:500],
                })

        except Exception:
            pass

    def save_debug(self, path: str = "logs/api_debug.json"):
        """Save debug info about matched API responses to a JSON file."""
        import json, os
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump({
                "responses_checked": self.responses_checked,
                "responses_matched": self.responses_matched,
                "unique_urns_captured": len(self.captured_urns),
                "captured_urns": list(self.captured_urns.keys()),
                "matched_responses": self._debug_responses,
            }, f, indent=2)
        print(f"[DEBUG] API debug info saved to {path}")

    def _extract_urns_from_text(self, body: str):
        """
        Brute-force scan: find all activity/ugcPost URNs in the raw response text.
        Also try to grab nearby text context for matching to DOM posts.
        """
        # Find all URNs
        for m in re.finditer(r'urn:li:(activity|ugcPost):(\d{15,25})', body):
            entity_type = m.group(1)
            activity_id = m.group(2)

            if activity_id in self.captured_urns:
                continue

            full_urn = f"urn:li:{entity_type}:{activity_id}"

            # Try to grab text context near this URN (helps with matching)
            # Look for text/commentary fields in the surrounding JSON
            text_context = ""
            start = max(0, m.start() - 2000)
            end = min(len(body), m.end() + 2000)
            surrounding = body[start:end]

            # Look for "text":"..." patterns near the URN
            text_matches = re.findall(r'"text"\s*:\s*"([^"]{20,200})"', surrounding)
            if text_matches:
                # Pick the longest text snippet (likely the post content)
                text_context = max(text_matches, key=len)

            # Look for author name patterns
            author_name = ""
            name_matches = re.findall(r'"name"\s*:\s*\{[^}]*"text"\s*:\s*"([^"]{2,80})"', surrounding)
            if name_matches:
                author_name = name_matches[0]
            if not author_name:
                name_matches = re.findall(r'"name"\s*:\s*"([^"]{2,80})"', surrounding)
                if name_matches:
                    author_name = name_matches[0]

            self.captured_urns[activity_id] = {
                "activity_id": activity_id,
                "urn": full_urn,
                "entity_type": entity_type,
                "text_context": text_context,
                "author_name": author_name,
            }

    def get_ordered_urns(self) -> list[dict]:
        """Return all captured URN entries, sorted by activity ID."""
        return sorted(
            self.captured_urns.values(),
            key=lambda p: p.get("activity_id", ""),
        )

    def match_by_text(self, post_text: str, used_ids: set[str]) -> dict | None:
        """Find a captured URN whose text context overlaps with the given post text."""
        if not post_text or len(post_text) < 15:
            return None

        snippet = post_text[:80].lower().strip()
        # Remove unicode formatting chars that LinkedIn uses
        snippet_clean = re.sub(r'[^\w\s]', '', snippet)[:60]

        for entry in self.captured_urns.values():
            if entry["activity_id"] in used_ids:
                continue
            ctx = (entry.get("text_context") or "").lower()
            ctx_clean = re.sub(r'[^\w\s]', '', ctx)

            # Check if a meaningful overlap exists
            if snippet_clean and len(snippet_clean) > 15 and snippet_clean[:30] in ctx_clean:
                return entry

            # Also try matching on a shorter prefix
            if snippet and len(snippet) > 20 and snippet[:25] in ctx:
                return entry

        return None

    def match_by_author(self, author_name: str, used_ids: set[str]) -> dict | None:
        """Find a captured URN by author name."""
        if not author_name:
            return None
        name_lower = author_name.lower().strip()
        for entry in self.captured_urns.values():
            if entry["activity_id"] in used_ids:
                continue
            entry_author = (entry.get("author_name") or "").lower()
            if name_lower and name_lower in entry_author:
                return entry
            if entry_author and entry_author in name_lower:
                return entry
        return None

    def stats(self) -> str:
        return (
            f"API interceptor: {self.responses_checked} responses checked, "
            f"{self.responses_matched} matched, "
            f"{len(self.captured_urns)} unique posts captured"
        )