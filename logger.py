"""
Saves scraped post data to structured JSON log files.
Each scrape session creates a new log file with timestamp and keyword.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

from config import Config
from parser import PostData


def _sanitize_filename(text: str) -> str:
    """Turn a keyword into a safe filename component."""
    return re.sub(r"[^\w\-]", "_", text.strip().lower())[:50]


def save_posts_to_log(posts: list[PostData], keyword: str) -> str:
    """
    Save a list of PostData to a JSON log file.

    Returns the path to the saved file.
    """
    os.makedirs(Config.LOG_DIR, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_keyword = _sanitize_filename(keyword)
    filename = f"{timestamp}_{safe_keyword}.json"
    filepath = os.path.join(Config.LOG_DIR, filename)

    log_data = {
        "metadata": {
            "keyword": keyword,
            "scraped_at": datetime.now().isoformat(),
            "total_posts": len(posts),
        },
        "posts": [p.to_dict() for p in posts],
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(log_data, f, indent=2, ensure_ascii=False)

    print(f"[LOG] Saved {len(posts)} posts → {filepath}")
    return filepath


def print_summary(posts: list[PostData], keyword: str):
    """Print a quick summary table to stdout."""
    print(f"\n{'='*80}")
    print(f"  SCRAPE RESULTS — keyword: \"{keyword}\"")
    print(f"  Total posts scraped: {len(posts)}")
    print(f"{'='*80}\n")

    if not posts:
        print("  (no posts found)\n")
        return

    # Sort by likes descending for the summary
    sorted_posts = sorted(posts, key=lambda p: p.num_likes, reverse=True)

    for i, post in enumerate(sorted_posts[:20], 1):  # Show top 20
        print(f"  {i:>2}. {post.summary_line()}")

    if len(posts) > 20:
        print(f"\n  ... and {len(posts) - 20} more posts (see log file)")

    total_likes = sum(p.num_likes for p in posts)
    total_comments = sum(p.num_comments for p in posts)
    total_reposts = sum(p.num_reposts for p in posts)
    print(f"\n  Totals: ❤ {total_likes}  💬 {total_comments}  🔁 {total_reposts}")
    print(f"{'='*80}\n")