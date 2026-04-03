"""
Local API server for triggering the LinkedIn scraper remotely.

Usage:
  1. SCRAPER_TOKEN=tripti-secret-2026 python3 local_server.py
  2. In another terminal: ngrok http 5001
  3. Copy the ngrok URL and set it as SCRAPER_URL in Streamlit Cloud secrets
"""

import subprocess
import os
import sys
import threading
import time
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

API_TOKEN = os.getenv("SCRAPER_TOKEN", "changeme-to-a-secret")

jobs = {}
queue = []
queue_lock = threading.Lock()
queue_running = False


def check_auth(req):
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    return token == API_TOKEN


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


def run_scraper_for_keyword(keyword, scrolls=None, sort=None, headless=True, delay_min=None, delay_max=None):
    """Run the scraper for a single keyword. Returns job info."""
    scraper_path = os.path.join(os.path.dirname(__file__), "scraper.py")
    cmd = [sys.executable, scraper_path, keyword]

    if headless:
        cmd.append("--headless")
    if scrolls:
        cmd.extend(["--scrolls", str(int(scrolls))])
    if sort:
        cmd.extend(["--sort", sort])

    # Pass delay settings via environment variables
    env = os.environ.copy()
    if delay_min:
        env["DELAY_MIN"] = str(delay_min)
    if delay_max:
        env["DELAY_MAX"] = str(delay_max)

    process = subprocess.Popen(
        cmd,
        cwd=os.path.dirname(__file__),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )

    job_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    jobs[job_id] = {
        "process": process,
        "keyword": keyword,
        "started_at": datetime.now().isoformat(),
    }
    return job_id


def process_queue():
    """Process queued keywords one by one."""
    global queue_running
    queue_running = True

    while True:
        with queue_lock:
            if not queue:
                queue_running = False
                return
            item = queue.pop(0)

        job_id = run_scraper_for_keyword(**item)
        # Wait for this job to finish before starting next
        jobs[job_id]["process"].wait()

    queue_running = False


@app.route("/scrape", methods=["POST"])
def scrape():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    keyword_input = data.get("keyword", "").strip()

    if not keyword_input:
        return jsonify({"error": "Keyword is required"}), 400

    # Split comma-separated keywords
    keywords = [kw.strip() for kw in keyword_input.split(",") if kw.strip()]

    if not keywords:
        return jsonify({"error": "No valid keywords provided"}), 400

    # Extract settings
    settings = {
        "scrolls": data.get("scrolls"),
        "sort": data.get("sort"),
        "headless": data.get("headless", True),
        "delay_min": data.get("delay_min"),
        "delay_max": data.get("delay_max"),
    }

    with queue_lock:
        for kw in keywords:
            queue.append({"keyword": kw, **settings})

    # Start queue processor if not already running
    if not queue_running:
        threading.Thread(target=process_queue, daemon=True).start()

    sort_label = f", sort={settings['sort']}" if settings['sort'] else ""
    scrolls_label = f", scrolls={settings['scrolls']}" if settings['scrolls'] else ""

    return jsonify({
        "message": f"Queued {len(keywords)} keyword(s): {', '.join(keywords)}{scrolls_label}{sort_label}",
        "keywords": keywords,
        "queue_size": len(queue),
    })


@app.route("/scrape/author", methods=["POST"])
def scrape_author():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    name = data.get("name", "").strip()
    company = data.get("company", "").strip()
    profile_url = data.get("profile_url", "").strip()
    scrolls = data.get("scrolls", 10)

    if not name and not profile_url:
        return jsonify({"error": "Author name or profile URL is required"}), 400

    scraper_path = os.path.join(os.path.dirname(__file__), "author_scraper.py")
    cmd = [sys.executable, scraper_path]

    if profile_url:
        cmd.append(profile_url)
    else:
        cmd.append(name)
        if company:
            cmd.extend(["--company", company])

    cmd.extend(["--scrolls", str(int(scrolls)), "--headless"])

    process = subprocess.Popen(
        cmd,
        cwd=os.path.dirname(__file__),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    job_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    jobs[job_id] = {
        "process": process,
        "keyword": f"author:{name or profile_url}",
        "started_at": datetime.now().isoformat(),
    }

    return jsonify({
        "message": f"Scraping author \"{name or profile_url}\"...",
        "job_id": job_id,
    })


@app.route("/scrape/status", methods=["GET"])
def scrape_status():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401

    active = []
    completed = []

    for job_id, job in jobs.items():
        poll = job["process"].poll()
        info = {
            "job_id": job_id,
            "keyword": job["keyword"],
            "started_at": job["started_at"],
        }
        if poll is None:
            info["status"] = "running"
            active.append(info)
        else:
            info["status"] = "completed" if poll == 0 else "failed"
            info["exit_code"] = poll
            completed.append(info)

    return jsonify({"active": active, "completed": completed[-5:]})


if __name__ == "__main__":
    print("=" * 50)
    print("  LinkedIn Scraper API Server")
    print("=" * 50)
    print(f"  Token: {API_TOKEN}")
    print(f"  Server: http://localhost:5001")
    print()
    print("  Next: run 'ngrok http 5001' in another terminal")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5001, debug=False)
