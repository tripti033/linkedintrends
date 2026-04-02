from __future__ import annotations

"""
MongoDB Atlas storage with smart upsert logic.

Deduplication strategy:
  - Each post gets a unique `post_id` (LinkedIn URN or deterministic hash)
  - First scrape → INSERT with all fields + engagement snapshot
  - Repeat scrape → UPDATE engagement counts, push new snapshot to history,
    merge keywords, bump scrape_count and last_scraped_at
  - This gives you engagement growth tracking over time

Collection: `posts`
Index: unique on `post_id`
"""

import os
from datetime import datetime, timezone

from pymongo import MongoClient, UpdateOne
from pymongo.errors import BulkWriteError

from config import Config


def get_db():
    """Get MongoDB database connection."""
    uri = os.getenv("MONGO_URI", "")
    db_name = os.getenv("MONGO_DB", "bess_linkedin")

    if not uri:
        raise ValueError(
            "MONGO_URI not set in .env file. "
            "Get your connection string from MongoDB Atlas → Connect → Drivers."
        )

    client = MongoClient(uri)
    db = client[db_name]

    # Ensure indexes exist
    db.posts.create_index("post_id", unique=True)
    db.posts.create_index("keywords")
    db.posts.create_index("author_name")
    db.posts.create_index("last_scraped_at")
    db.posts.create_index([("num_likes", -1)])  # for sorting by engagement

    return db


def upsert_posts(posts: list[dict], keyword: str) -> dict:
    """
    Upsert scraped posts into MongoDB.

    Returns stats: { inserted: N, updated: N, unchanged: N }
    """
    db = get_db()
    collection = db.posts
    now = datetime.now(timezone.utc)

    stats = {"inserted": 0, "updated": 0, "unchanged": 0, "errors": 0}
    operations = []

    for post in posts:
        pid = post.get("post_id", "")
        if not pid:
            continue

        # Current engagement snapshot
        snapshot = {
            "scraped_at": now,
            "num_likes": post.get("num_likes", 0),
            "num_comments": post.get("num_comments", 0),
            "num_reposts": post.get("num_reposts", 0),
        }

        # Build the upsert operation
        # $setOnInsert: only set these on first insert
        # $set: always update these
        # $addToSet: add keyword without duplicates
        # $push: append engagement snapshot
        # $inc: bump scrape count
        operation = UpdateOne(
            {"post_id": pid},
            {
                "$setOnInsert": {
                    "post_id": pid,
                    "author_name": post.get("author_name", ""),
                    "post_text": post.get("post_text", ""),
                    "media_type": post.get("media_type", "text"),
                    "first_scraped_at": now,
                    "posted_time_raw": post.get("posted_time", ""),
                },
                "$set": {
                    "num_likes": post.get("num_likes", 0),
                    "num_comments": post.get("num_comments", 0),
                    "num_reposts": post.get("num_reposts", 0),
                    "last_scraped_at": now,
                    "author_headline": post.get("author_headline", ""),
                    "author_profile_url": post.get("author_profile_url", ""),
                    **({"post_url": post["post_url"]}
                       if post.get("post_url") and "/feed/update/" in post.get("post_url", "")
                       else {}),
                },
                "$addToSet": {
                    "keywords": keyword,
                },
                "$push": {
                    "engagement_history": {
                        "$each": [snapshot],
                        "$slice": -100,  # keep last 100 snapshots max
                    }
                },
                "$inc": {
                    "scrape_count": 1,
                },
            },
            upsert=True,
        )
        operations.append(operation)

    if not operations:
        print("[DB] No posts to upsert.")
        return stats

    try:
        result = collection.bulk_write(operations, ordered=False)
        stats["inserted"] = result.upserted_count
        stats["updated"] = result.modified_count
        stats["unchanged"] = len(operations) - result.upserted_count - result.modified_count
        print(
            f"[DB] Upserted {len(operations)} posts → "
            f"{stats['inserted']} new, "
            f"{stats['updated']} updated, "
            f"{stats['unchanged']} unchanged"
        )
    except BulkWriteError as e:
        stats["errors"] = len(e.details.get("writeErrors", []))
        stats["inserted"] = e.details.get("nInserted", 0)
        stats["updated"] = e.details.get("nModified", 0)
        print(f"[DB] Bulk write partially failed: {stats['errors']} errors")
        print(f"[DB] First error: {e.details['writeErrors'][0]['errmsg']}")

    return stats


def get_collection_stats(keyword: str = None) -> dict:
    """Get quick stats about what's in the database."""
    db = get_db()
    collection = db.posts

    query = {}
    if keyword:
        query["keywords"] = keyword

    total = collection.count_documents(query)
    if total == 0:
        return {"total": 0}

    # Aggregate stats
    pipeline = [
        {"$match": query} if query else {"$match": {}},
        {
            "$group": {
                "_id": None,
                "total": {"$sum": 1},
                "total_likes": {"$sum": "$num_likes"},
                "total_comments": {"$sum": "$num_comments"},
                "total_reposts": {"$sum": "$num_reposts"},
                "avg_likes": {"$avg": "$num_likes"},
                "max_likes": {"$max": "$num_likes"},
                "unique_authors": {"$addToSet": "$author_name"},
                "avg_scrape_count": {"$avg": "$scrape_count"},
            }
        },
    ]

    result = list(collection.aggregate(pipeline))
    if not result:
        return {"total": 0}

    r = result[0]
    return {
        "total_posts": r["total"],
        "total_likes": r["total_likes"],
        "total_comments": r["total_comments"],
        "total_reposts": r["total_reposts"],
        "avg_likes": round(r["avg_likes"], 1),
        "max_likes": r["max_likes"],
        "unique_authors": len(r["unique_authors"]),
        "avg_scrape_count": round(r["avg_scrape_count"], 1),
    }