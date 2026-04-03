import os
import sys
import subprocess
import requests
import pandas as pd
import streamlit as st
import plotly.express as px
import matplotlib.pyplot as plt
from pymongo import MongoClient
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# --- MongoDB connection ---
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB = os.getenv("MONGO_DB", "bess_linkedin")

if not MONGO_URI:
    st.error("MONGO_URI not set. Add it to .env or Streamlit Cloud secrets.")
    st.stop()

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
posts_collection = db["posts"]

st.set_page_config(page_title="LinkedIn Trends Dashboard", layout="wide")
st.title("LinkedIn Trends Dashboard")

# --- Sidebar ---
st.sidebar.markdown("### Data Controls")

scrape_keyword = st.sidebar.text_input(
    "Enter keywords to scrape (comma-separated)",
    placeholder="bess, solar, energy storage"
)

with st.sidebar.expander("Scraper Settings", expanded=False):
    scrape_scrolls = st.number_input("Scroll count (more = more posts)", min_value=1, max_value=50, value=5)
    scrape_sort = st.selectbox("Sort by", ["relevance", "date_posted"], index=0)
    scrape_headless = st.checkbox("Headless mode (no browser window)", value=True)
    scrape_delay_min = st.number_input("Min delay (seconds)", min_value=1, max_value=30, value=2)
    scrape_delay_max = st.number_input("Max delay (seconds)", min_value=1, max_value=60, value=5)

SCRAPER_URL = os.getenv("SCRAPER_URL", "")
SCRAPER_TOKEN = os.getenv("SCRAPER_TOKEN", "")

if st.sidebar.button("Run Scraper"):
    if not scrape_keyword.strip():
        st.sidebar.error("Please enter a keyword.")
    elif SCRAPER_URL:
        # Remote: call local machine via ngrok tunnel
        try:
            res = requests.post(
                f"{SCRAPER_URL}/scrape",
                json={
                    "keyword": scrape_keyword.strip(),
                    "scrolls": scrape_scrolls,
                    "sort": scrape_sort,
                    "headless": scrape_headless,
                    "delay_min": scrape_delay_min,
                    "delay_max": scrape_delay_max,
                },
                headers={"Authorization": f"Bearer {SCRAPER_TOKEN}"},
                timeout=10,
            )
            if "text/html" in res.headers.get("content-type", ""):
                st.sidebar.error("Local server is down. Start local_server.py on your machine.")
            elif res.ok:
                data = res.json()
                st.sidebar.success(data.get("message", "Scraper started!"))
            else:
                data = res.json()
                st.sidebar.error(data.get("error", "Scraper request failed"))
        except requests.exceptions.ConnectionError:
            st.sidebar.error("Cannot reach local server. Make sure local_server.py and ngrok are running.")
        except Exception as e:
            st.sidebar.error(f"Error: {e}")
    else:
        # Local: run scraper directly
        st.sidebar.warning("Scraper started... browser may open.")
        scraper_dir = os.path.dirname(os.path.abspath(__file__))
        cmd = [sys.executable, os.path.join(scraper_dir, "scraper.py"), scrape_keyword.strip(),
               "--scrolls", str(scrape_scrolls), "--sort", scrape_sort]
        if scrape_headless:
            cmd.append("--headless")
        subprocess.Popen(cmd, cwd=scraper_dir)
        st.sidebar.success("Scraper running in background!")

if st.sidebar.button("Refresh Data"):
    st.rerun()

st.sidebar.markdown("---")
st.sidebar.markdown("### Danger Zone")

delete_option = st.sidebar.selectbox(
    "Delete data",
    ["Select...", "All posts", "By keyword"],
    key="delete_option",
)

if delete_option == "By keyword":
    all_keywords = posts_collection.distinct("keywords")
    if all_keywords:
        delete_kw = st.sidebar.selectbox("Select keyword to delete", all_keywords, key="delete_kw")
    else:
        delete_kw = None
        st.sidebar.info("No keywords found.")

if delete_option != "Select...":
    confirm = st.sidebar.checkbox("I confirm I want to delete this data", key="delete_confirm")
    if st.sidebar.button("Delete", type="primary"):
        if not confirm:
            st.sidebar.error("Please confirm first.")
        elif delete_option == "All posts":
            count = posts_collection.count_documents({})
            posts_collection.delete_many({})
            st.sidebar.success(f"Deleted all {count} posts.")
            st.rerun()
        elif delete_option == "By keyword" and delete_kw:
            # Remove keyword from posts that have multiple keywords
            posts_collection.update_many(
                {"keywords": delete_kw, "keywords.1": {"$exists": True}},
                {"$pull": {"keywords": delete_kw}},
            )
            # Delete posts where this was the only keyword
            result = posts_collection.delete_many({"keywords": {"$size": 0}})
            remaining = posts_collection.delete_many({"keywords": delete_kw})
            total = result.deleted_count + remaining.deleted_count
            st.sidebar.success(f"Deleted {total} posts for \"{delete_kw}\".")
            st.rerun()

# --- Load data ---
@st.cache_data(ttl=60)
def load_posts():
    data = list(posts_collection.find({}, {"_id": 0}))
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["likes"] = df.get("num_likes", 0)
    df["comments"] = df.get("num_comments", 0)
    df["reposts"] = df.get("num_reposts", 0)
    df["total_engagement"] = df["likes"] + df["comments"] + df["reposts"]
    df["author_name"] = df["author_name"].fillna("").replace("", "Unknown")

    # Convert posted_time_raw (e.g. "3d", "2w", "1mo") to hours for sorting
    def time_to_hours(t):
        if not t or not isinstance(t, str):
            return 99999
        t = t.strip().lower()
        import re as _re
        # Match longer units first: mo/yr before m/y
        m = _re.match(r'(\d+)\s*(mo|yr|w|d|h|m)', t)
        if not m:
            return 99999
        num = int(m.group(1))
        unit = m.group(2)
        return {"m": num / 60, "h": num, "d": num * 24, "w": num * 168, "mo": num * 720, "yr": num * 8760}.get(unit, 99999)

    if "posted_time_raw" in df.columns:
        df["time_hours"] = df["posted_time_raw"].apply(time_to_hours)
    else:
        df["time_hours"] = 99999
    df["display"] = (
        df["author_name"]
        + " - "
        + df["post_text"].fillna("").str[:80]
    )
    return df


df = load_posts()

if df.empty:
    st.warning("No posts in database yet. Run the scraper to collect data.")
    st.stop()

# --- Tabs ---
tab1, tab2, tab3, tab4 = st.tabs(["Top Engaged Posts", "Engagement Insights", "Keyword Trends", "Author Analysis"])

# =================== TAB 1: TOP POSTS ===================
with tab1:
    st.subheader("Top Engaged Posts")

    # --- Filters ---
    filtered_df = df.copy()

    fc1, fc2, fc3 = st.columns(3)

    # Keyword filter
    with fc1:
        if "keywords" in df.columns:
            all_keywords = sorted({
                kw
                for kws in df["keywords"]
                for kw in (kws if isinstance(kws, list) else [])
            })
            search_term = st.text_input("Search keyword", "", placeholder="e.g. solar, bess")
            if search_term.strip():
                search_lower = search_term.lower()
                filtered_df = filtered_df[
                    filtered_df["keywords"].apply(
                        lambda kws: any(
                            search_lower in kw.lower()
                            for kw in (kws if isinstance(kws, list) else [])
                        )
                    )
                ]

    # Author filter
    with fc2:
        authors = sorted(df["author_name"].dropna().unique())
        selected_author = st.selectbox("Filter by author", ["All"] + authors)
        if selected_author != "All":
            filtered_df = filtered_df[filtered_df["author_name"] == selected_author]

    # Posted time filter
    with fc3:
        if "posted_time_raw" in df.columns:
            time_options = ["All", "< 24h", "< 1 week", "< 1 month"]
            selected_time = st.selectbox("Filter by time", time_options)
            if selected_time != "All":
                max_hours = {
                    "< 24h": 24,
                    "< 1 week": 168,
                    "< 1 month": 720,
                }[selected_time]
                filtered_df = filtered_df[filtered_df["time_hours"] <= max_hours]

    # Sort option
    sc1, sc2 = st.columns(2)
    with sc1:
        sort_by = st.selectbox("Sort by", ["Total Engagement", "Likes", "Comments", "Reposts", "Most Recent"])
        sort_col = {
            "Total Engagement": "total_engagement",
            "Likes": "likes",
            "Comments": "comments",
            "Reposts": "reposts",
            "Most Recent": "time_hours",
        }[sort_by]
    with sc2:
        if sort_by == "Most Recent":
            sort_order = st.selectbox("Order", ["Newest first", "Oldest first"])
            ascending = sort_order == "Newest first"  # smallest hours = newest
        else:
            sort_order = st.selectbox("Order", ["Highest first", "Lowest first"])
            ascending = sort_order == "Lowest first"

    if "keywords" in df.columns and all_keywords:
        st.caption("Available keywords: " + ", ".join(all_keywords[:15]))

    # Stats cards
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Posts", len(filtered_df))
    col2.metric("Total Likes", f"{filtered_df['likes'].sum():,}")
    col3.metric("Total Comments", f"{filtered_df['comments'].sum():,}")
    col4.metric("Total Reposts", f"{filtered_df['reposts'].sum():,}")

    # Sorted posts
    df_sorted = filtered_df.sort_values(sort_col, ascending=ascending)

    for _, row in df_sorted.iterrows():
        with st.expander(
            f"{row['display']} | Likes: {row['likes']}  Comments: {row['comments']}  "
            f"Reposts: {row['reposts']}  (Total: {row['total_engagement']})"
        ):
            post_url = row.get("post_url", "")
            if post_url and "/feed/update/" in str(post_url):
                st.markdown(f"[View Post]({post_url})")

            author_url = row.get("author_profile_url", "")
            author_display = row.get("author_name", "Unknown")
            if author_url:
                st.markdown(f"**Author:** [{author_display}]({author_url})")
            else:
                st.markdown(f"**Author:** {author_display}")

            st.markdown(f"**Posted:** {row.get('posted_time_raw', 'N/A')}")
            st.markdown(f"**Post:**\n{row.get('post_text', '')}")

            # Engagement history chart
            history = row.get("engagement_history", [])
            if isinstance(history, list) and len(history) > 1:
                st.write("**Engagement History:**")
                first, latest = history[0], history[-1]
                st.markdown(
                    f"- Likes: {first.get('num_likes', 0)} → {latest.get('num_likes', 0)}\n"
                    f"- Comments: {first.get('num_comments', 0)} → {latest.get('num_comments', 0)}\n"
                    f"- Reposts: {first.get('num_reposts', 0)} → {latest.get('num_reposts', 0)}"
                )
                fig, ax = plt.subplots(figsize=(5, 3))
                dates = list(range(len(history)))
                for key, label in [("num_likes", "Likes"), ("num_comments", "Comments"), ("num_reposts", "Reposts")]:
                    ax.plot(dates, [s.get(key, 0) for s in history], marker="o", label=label)
                ax.set_xlabel("Scrape #")
                ax.set_title("Engagement Over Time", fontsize=10)
                ax.legend(fontsize=8)
                st.pyplot(fig)
                plt.close(fig)
            else:
                st.info("Only one snapshot available yet.")


# =================== TAB 2: ENGAGEMENT INSIGHTS ===================
with tab2:
    st.subheader("Engagement Insights")

    # --- Top Authors ---
    st.write("### Top Authors by Engagement")

    # Build author stats with profile URLs
    author_url_map = df.groupby("author_name")["author_profile_url"].first().to_dict() if "author_profile_url" in df.columns else {}

    author_stats = (
        df.groupby("author_name")
        .agg(
            post_count=("post_text", "count"),
            total_likes=("likes", "sum"),
            total_comments=("comments", "sum"),
            total_reposts=("reposts", "sum"),
            avg_likes=("likes", "mean"),
        )
        .reset_index()
    )
    author_stats["total_engagement"] = (
        author_stats["total_likes"]
        + author_stats["total_comments"]
        + author_stats["total_reposts"]
    )
    author_stats = author_stats.sort_values("total_engagement", ascending=False)

    # Pie chart top 10
    top_authors = author_stats.head(10)
    fig_authors = px.pie(
        top_authors,
        values="total_engagement",
        names="author_name",
        hole=0.3,
        title="Top 10 Authors by Total Engagement",
    )
    st.plotly_chart(fig_authors, width="stretch")

    # Author table with clickable profile links
    st.write("**Top 20 Authors**")
    for _, row in author_stats.head(20).iterrows():
        name = row["author_name"]
        url = author_url_map.get(name, "")
        link = f"[{name}]({url})" if url else name
        st.markdown(
            f"{link} — **{int(row['post_count'])}** posts, "
            f"**{int(row['total_likes']):,}** likes, "
            f"**{int(row['total_comments']):,}** comments, "
            f"**{int(row['total_reposts']):,}** reposts "
            f"(avg {row['avg_likes']:.0f} likes/post)"
        )

    # --- Engagement Distribution ---
    st.write("### Engagement Distribution")
    fig_hist = px.histogram(
        df,
        x="total_engagement",
        nbins=20,
        title="Distribution of Post Engagement Scores",
        labels={"total_engagement": "Total Engagement"},
    )
    st.plotly_chart(fig_hist, width="stretch")

    # --- Posts per keyword pie ---
    if "keywords" in df.columns:
        st.write("### Posts per Keyword")
        df_exploded = df.explode("keywords")
        kw_counts = df_exploded["keywords"].value_counts().head(15).reset_index()
        kw_counts.columns = ["Keyword", "Posts"]
        fig_kw_pie = px.pie(
            kw_counts,
            values="Posts",
            names="Keyword",
            hole=0.4,
            title="Post Distribution by Keyword",
        )
        st.plotly_chart(fig_kw_pie, width="stretch")


# =================== TAB 3: KEYWORD TRENDS ===================
with tab3:
    st.subheader("Keyword Trends")

    if "keywords" not in df.columns:
        st.info("No keyword data available.")
    else:
        df_exploded = df.explode("keywords")

        # --- Keyword engagement bar chart ---
        st.write("### Keyword Engagement Comparison")
        kw_engagement = (
            df_exploded.groupby("keywords")
            .agg(
                post_count=("post_text", "count"),
                total_likes=("likes", "sum"),
                total_comments=("comments", "sum"),
                total_engagement=("total_engagement", "sum"),
                avg_engagement=("total_engagement", "mean"),
            )
            .reset_index()
            .sort_values("total_engagement", ascending=False)
            .head(15)
        )

        metric = st.selectbox(
            "Select metric",
            ["Total Engagement", "Post Count", "Total Likes", "Average Engagement"],
        )
        metric_map = {
            "Total Engagement": "total_engagement",
            "Post Count": "post_count",
            "Total Likes": "total_likes",
            "Average Engagement": "avg_engagement",
        }
        fig_bar = px.bar(
            kw_engagement,
            x="keywords",
            y=metric_map[metric],
            title=f"Keywords by {metric}",
            labels={"keywords": "Keyword", metric_map[metric]: metric},
        )
        st.plotly_chart(fig_bar, width="stretch")

        # --- Top posts per keyword ---
        st.write("### Top Posts for a Keyword")
        selected_kw = st.selectbox(
            "Select keyword", df_exploded["keywords"].unique()
        )
        if selected_kw:
            kw_posts = df_exploded[df_exploded["keywords"] == selected_kw].sort_values(
                "total_engagement", ascending=False
            ).head(10)
            kw_posts["short_text"] = kw_posts["post_text"].apply(
                lambda x: (x[:50] + "...") if isinstance(x, str) and len(x) > 50 else x
            )
            fig_kw_posts = px.bar(
                kw_posts,
                x="short_text",
                y="total_engagement",
                title=f"Top Posts for '{selected_kw}'",
                labels={"short_text": "Post", "total_engagement": "Engagement"},
            )
            fig_kw_posts.update_xaxes(tickangle=45)
            st.plotly_chart(fig_kw_posts, width="stretch")

        # --- Engagement timeline from history ---
        st.write("### Engagement Growth Over Time")
        history_data = []
        for _, row in df.iterrows():
            history = row.get("engagement_history", [])
            if isinstance(history, list):
                for snap in history:
                    scraped_at = snap.get("scraped_at")
                    if scraped_at:
                        history_data.append({
                            "date": scraped_at.strftime("%Y-%m-%d") if hasattr(scraped_at, "strftime") else str(scraped_at)[:10],
                            "likes": snap.get("num_likes", 0),
                            "comments": snap.get("num_comments", 0),
                            "reposts": snap.get("num_reposts", 0),
                        })

        if history_data:
            df_timeline = pd.DataFrame(history_data)
            df_timeline = df_timeline.groupby("date").sum().reset_index().sort_values("date")
            fig_timeline = px.line(
                df_timeline,
                x="date",
                y=["likes", "comments", "reposts"],
                markers=True,
                title="Total Engagement Across Scrape Sessions",
                labels={"value": "Count", "date": "Date"},
            )
            st.plotly_chart(fig_timeline, width="stretch")
        else:
            st.info("Run the scraper multiple times to see engagement trends over time.")


# =================== TAB 4: AUTHOR ANALYSIS ===================
author_collection = db["author_posts"]

@st.cache_data(ttl=60)
def load_author_posts():
    data = list(author_collection.find({}, {"_id": 0}))
    if not data:
        return pd.DataFrame()
    adf = pd.DataFrame(data)
    adf["likes"] = adf.get("num_likes", 0)
    adf["comments"] = adf.get("num_comments", 0)
    adf["reposts"] = adf.get("num_reposts", 0)
    adf["total_engagement"] = adf["likes"] + adf["comments"] + adf["reposts"]
    adf["author_name"] = adf["author_name"].fillna("").replace("", "Unknown")
    return adf

with tab4:
    st.subheader("Author Analysis")

    ac1, ac2 = st.columns(2)
    with ac1:
        author_search = st.text_input("Author name", placeholder="e.g. Tripti Verma")
    with ac2:
        company_search = st.text_input("Company (optional)", placeholder="e.g. Ingro Energy")

    asc1, asc2 = st.columns(2)
    with asc1:
        author_profile_url = st.text_input("LinkedIn profile URL (optional)", placeholder="e.g. https://www.linkedin.com/in/username")
    with asc2:
        author_scrolls = st.number_input("Scroll count", min_value=1, max_value=50, value=10, key="author_scrolls")

    # Scrape author's posts button
    if st.button("Scrape Author Posts"):
        if not author_search.strip() and not author_profile_url.strip():
            st.error("Please enter an author name or profile URL.")
        else:
            try:
                if SCRAPER_URL:
                    res = requests.post(
                        f"{SCRAPER_URL}/scrape/author",
                        json={
                            "name": author_search.strip(),
                            "company": company_search.strip(),
                            "profile_url": author_profile_url.strip(),
                            "scrolls": author_scrolls,
                        },
                        headers={"Authorization": f"Bearer {SCRAPER_TOKEN}"},
                        timeout=10,
                    )
                    if "text/html" in res.headers.get("content-type", ""):
                        st.error("Local server is down. Start local_server.py on your machine.")
                    elif res.ok:
                        st.success(res.json().get("message", "Scraping started!") + " Refresh in ~2 min.")
                    else:
                        st.error(res.json().get("error", "Failed"))
                else:
                    scraper_dir = os.path.dirname(os.path.abspath(__file__))
                    cmd = [sys.executable, os.path.join(scraper_dir, "author_scraper.py")]
                    if author_profile_url.strip():
                        cmd.append(author_profile_url.strip())
                    else:
                        cmd.append(author_search.strip())
                        if company_search.strip():
                            cmd.extend(["--company", company_search.strip()])
                    cmd.extend(["--scrolls", str(author_scrolls), "--headless"])
                    subprocess.Popen(cmd, cwd=scraper_dir)
                    st.success("Scraping author posts... Refresh in ~2 min.")
            except requests.exceptions.ConnectionError:
                st.error("Cannot reach local server. Make sure local_server.py and ngrok are running.")
            except Exception as e:
                st.error(f"Error: {e}")

    st.markdown("---")

    # Load author posts from separate collection
    author_posts_df = load_author_posts()

    # Determine which data to show
    author_df = pd.DataFrame()

    if not author_posts_df.empty:
        existing_authors = sorted(author_posts_df["author_name"].unique())

        if author_search.strip():
            search_lower = author_search.strip().lower()
            author_df = author_posts_df[
                author_posts_df["author_name"].str.lower().str.contains(search_lower, na=False)
            ]
        else:
            selected = st.selectbox(
                "Or select from scraped authors",
                ["Select..."] + existing_authors,
                key="author_analysis_select",
            )
            if selected != "Select...":
                author_df = author_posts_df[author_posts_df["author_name"] == selected]
    elif not author_search.strip():
        st.info("No author data yet. Enter a name and click 'Scrape Author Posts'.")

    if not author_df.empty:
        author_name = author_df["author_name"].iloc[0]
        profile = author_df["author_profile_url"].iloc[0] if "author_profile_url" in author_df.columns else ""
        if profile:
            st.markdown(f"### [{author_name}]({profile})")
        else:
            st.markdown(f"### {author_name}")

        # --- Stats ---
        mc1, mc2, mc3, mc4, mc5 = st.columns(5)
        mc1.metric("Total Posts", len(author_df))
        mc2.metric("Total Likes", f"{author_df['likes'].sum():,}")
        mc3.metric("Total Comments", f"{author_df['comments'].sum():,}")
        mc4.metric("Total Reposts", f"{author_df['reposts'].sum():,}")
        mc5.metric("Avg Engagement", f"{author_df['total_engagement'].mean():.0f}")

        # --- Engagement per post bar chart ---
        st.write("### Engagement per Post")
        chart_df = author_df.copy()
        chart_df["short_text"] = chart_df["post_text"].fillna("").apply(
            lambda x: x[:50] + "..." if len(x) > 50 else x
        )
        chart_df = chart_df.sort_values("total_engagement", ascending=False).head(20)
        fig_eng = px.bar(
            chart_df,
            x="short_text",
            y=["likes", "comments", "reposts"],
            title=f"Engagement Breakdown — {author_name}",
            labels={"value": "Count", "short_text": "Post"},
            barmode="stack",
        )
        fig_eng.update_xaxes(tickangle=45)
        st.plotly_chart(fig_eng, width="stretch")

        # --- Media type breakdown ---
        if "media_type" in author_df.columns:
            st.write("### Content Types")
            media_counts = author_df["media_type"].value_counts().reset_index()
            media_counts.columns = ["Type", "Count"]
            fig_media = px.pie(media_counts, values="Count", names="Type", hole=0.4,
                               title=f"Content Types — {author_name}")
            st.plotly_chart(fig_media, width="stretch")

        # --- Post list ---
        st.write("### All Posts")
        for _, row in author_df.sort_values("total_engagement", ascending=False).iterrows():
            with st.expander(
                f"{row.get('posted_time_raw', '')} | Likes: {row['likes']}  Comments: {row['comments']}  "
                f"Reposts: {row['reposts']}  (Total: {row['total_engagement']})"
            ):
                post_url = row.get("post_url", "")
                if post_url and "/feed/update/" in str(post_url):
                    st.markdown(f"[View Post]({post_url})")
                st.markdown(f"**Posted:** {row.get('posted_time_raw', 'N/A')}")
                st.markdown(f"**Post:**\n{row.get('post_text', '')}")

    elif author_search.strip():
        st.info(f"No posts found for \"{author_search}\" in author database. Click 'Scrape Author Posts' to fetch them.")


# --- Footer ---
st.markdown("---")
st.caption("Data collected by LinkedIn Trends Scraper")
