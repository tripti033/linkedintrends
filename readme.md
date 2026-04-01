# LinkedIn BESS Post Scraper

A Python + Playwright scraper that collects LinkedIn posts for any keyword, extracts engagement metrics, and stores results in MongoDB Atlas with full engagement history tracking.

Built for monitoring the Battery Energy Storage System (BESS) industry on LinkedIn — but works for any topic.

## What It Does

- Searches LinkedIn for posts matching your keyword
- Extracts author info, post text, engagement counts (likes, comments, reposts), media type, timestamps
- Sorts by **relevance** (most engaged posts first) or **date** (newest first)
- Stores everything in MongoDB Atlas with **upsert logic** — run it 10 times and the same post updates instead of duplicating
- Tracks **engagement history** over time — see how likes/comments grow per post across scrapes
- Saves JSON logs locally as backup

## Sample Output

```
  1. [12h] CSIRO | ❤ 3483 💬 134 🔁 116 | Introducing the WOMBATTERY. 🔋 Today, we're proud to announce...
  2. [2d] Renewables Valuation Institute | ❤ 2758 💬 4462 🔁 35 | 📘 BESS Project Finance Model...
  3. [2d] RAJENDRA NEGI | ❤ 212 💬 13 🔁 3 | 🚀 A Proud Milestone – Inauguration of Microtek's...
  ...
  Totals: ❤ 7029  💬 4657  🔁 198
```

## Setup

### 1. Clone and create virtual environment

```bash
git clone https://github.com/yourusername/linkedin-bess-scraper.git
cd linkedin-bess-scraper

# Create virtual environment
python3 -m venv venv

# Activate it
# Linux / macOS:
source venv/bin/activate
# Windows:
venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# LinkedIn Credentials (required)
LINKEDIN_EMAIL=your_email@example.com
LINKEDIN_PASSWORD=your_password

# MongoDB Atlas (optional — scraper works without it, saves to JSON logs only)
MONGO_URI=mongodb+srv://user:pass@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
MONGO_DB=bess_linkedin

# Scraper Settings (optional — these are the defaults)
HEADLESS=false
SCROLL_COUNT=5
DELAY_MIN=2
DELAY_MAX=5
SORT_BY=relevance
```

### 4. MongoDB Atlas Setup (optional but recommended)

1. Go to [cloud.mongodb.com](https://cloud.mongodb.com) → create a free M0 cluster
2. **Database Access** → create a user with read/write permissions
3. **Network Access** → add your IP (or `0.0.0.0/0` for development)
4. **Connect → Drivers → Python** → copy the connection string
5. Paste into `.env` as `MONGO_URI`, replacing `<password>` with your actual password

## Usage

### Basic scrape

```bash
python3 scraper.py "battery energy storage"
```

### With options

```bash
# More scrolls = more posts (each scroll loads ~3-6 new posts)
python3 scraper.py "BESS India" --scrolls 10

# Sort by newest instead of most engaged
python3 scraper.py "BESS" --sort date_posted

# Run without browser window (headless)
python3 scraper.py "energy storage" --headless

# Combine all options
python3 scraper.py "renewable energy" --scrolls 15 --sort relevance --headless
```

### First run

The first run opens a browser window and logs into LinkedIn. You may need to:
- Complete a CAPTCHA manually
- Approve a security verification (email/phone code)

After successful login, the session is saved to `state/linkedin_state.json`. All subsequent runs reuse this session automatically — no login needed.

If the session expires, delete `state/linkedin_state.json` and run again.

## How Deduplication Works

Every post gets a unique `post_id`:
- **LinkedIn URN** (e.g., `urn:li:ugcPost:7443138149037555713`) — extracted from the page when available
- **Content hash** (e.g., `hash:020fce3ce00eb389`) — deterministic hash of author + text, used as fallback

When the scraper encounters a post that already exists in MongoDB:

| Field | First scrape | Subsequent scrapes |
|---|---|---|
| `author_name`, `post_text` | Set | Unchanged |
| `num_likes`, `num_comments`, `num_reposts` | Set | **Overwritten** with latest |
| `engagement_history` | First snapshot | **Appended** with new snapshot |
| `keywords` | `["BESS"]` | **Merged** → `["BESS", "battery energy storage"]` |
| `scrape_count` | 1 | **Incremented** |
| `first_scraped_at` | Set | Unchanged |
| `last_scraped_at` | Set | **Updated** to now |

This means running the scraper on a cron (e.g., twice daily) builds up engagement growth data over time — the foundation for trending analysis.

### MongoDB document example

```json
{
  "post_id": "hash:020fce3ce00eb389",
  "author_name": "Neeraj Kumar Singal",
  "author_headline": "Founder @ Semco Group...",
  "post_text": "Who REALLY profits in the #EnergyStorage industry...",
  "num_likes": 55,
  "num_comments": 9,
  "num_reposts": 1,
  "keywords": ["battery energy storage", "BESS"],
  "scrape_count": 3,
  "first_scraped_at": "2026-04-01T08:30:00Z",
  "last_scraped_at": "2026-04-01T20:00:00Z",
  "engagement_history": [
    { "scraped_at": "2026-04-01T08:30:00Z", "num_likes": 49, "num_comments": 8, "num_reposts": 0 },
    { "scraped_at": "2026-04-01T14:00:00Z", "num_likes": 52, "num_comments": 9, "num_reposts": 1 },
    { "scraped_at": "2026-04-01T20:00:00Z", "num_likes": 55, "num_comments": 9, "num_reposts": 1 }
  ]
}
```

## Project Structure

```
├── scraper.py            # Main entry point — orchestrates the full scrape pipeline
├── parser.py             # DOM extraction — pulls post data from LinkedIn's rendered HTML
├── api_interceptor.py    # Network interception — captures URNs from LinkedIn API responses
├── auth.py               # Session management — login, cookie persistence, challenge handling
├── config.py             # Configuration — loads .env, validates settings
├── db.py                 # MongoDB Atlas — upsert logic, engagement history, collection stats
├── logger.py             # JSON log files — local backup of each scrape session
├── debug_page.py         # Diagnostic tool — saves page HTML + screenshot for debugging
├── debug_engagement.py   # Diagnostic tool — tests engagement count extraction
├── requirements.txt
├── .env.example
├── .gitignore
├── logs/                 # JSON log files (one per scrape session)
└── state/                # Saved browser session (cookies + localStorage)
```

### How the modules connect

```
scraper.py
  ├── auth.py              → gets an authenticated browser context
  ├── api_interceptor.py   → attaches to page, listens for API responses
  ├── parser.py            → extracts posts from rendered DOM
  ├── logger.py            → saves JSON log locally
  └── db.py                → upserts into MongoDB Atlas
```

## Architecture

The scraper uses a two-layer extraction approach to work around LinkedIn's SDUI (Server-Driven UI) architecture:

**Layer 1 — DOM Extraction** (`parser.py`):  
Runs JavaScript inside the browser to walk LinkedIn's rendered HTML. Extracts author name/headline/profile from `aria-label` attributes, post text from `[data-testid="expandable-text-box"]`, engagement counts from span text matching patterns like `"49 reactions"`, and post URNs from `innerHTML` when present.

**Layer 2 — API Interception** (`api_interceptor.py`):  
Listens to all network responses via `page.on('response')` and scans response bodies for `urn:li:activity:XXXXX` patterns. When found, these URNs are matched to DOM posts by text similarity or author name to fill in `post_id` and `post_url`.

**Merge** (`scraper.py`):  
After scrolling completes, matches each DOM post to an API-captured URN. Posts that can't be matched get a deterministic hash ID as fallback.

## Debugging

If posts aren't being extracted correctly:

```bash
# Save the raw page HTML + screenshot for inspection
python3 debug_page.py "battery energy storage"
# Check: logs/debug_page.html and logs/debug_screenshot.png

# Test engagement extraction specifically
python3 debug_engagement.py "BESS"
```

If the API interceptor captures 0 URNs (expected with LinkedIn's current SDUI), a debug file is auto-saved to `logs/api_debug.json` showing what API responses were received.

## Limitations

- **Rate limiting**: LinkedIn may temporarily block scraping if you run too aggressively. The built-in random delays (2-5s between actions) help, but avoid running more than a few times per hour.
- **Session expiry**: The saved session typically lasts a few days. If you get redirect errors, delete `state/linkedin_state.json`.
- **Post URLs**: LinkedIn's SDUI architecture doesn't expose post permalinks in the DOM for most posts. ~10% of posts get real URLs; the rest use hash-based IDs. This doesn't affect engagement tracking or analytics.
- **CAPTCHA**: First login may require manual CAPTCHA completion. The browser window stays open for 120 seconds to allow this.

## Upcoming

- Next.js analytics dashboard with engagement rankings, keyword analysis, author segmentation
- Engagement growth tracking and trending post detection
- Automated scheduled scraping via cron

## License

MIT