#!/usr/bin/env python3
"""Collect today's headlines from free British news sites into news/data.json.

Run by .github/workflows/news.yml every hour. Standard library only.
Stores headlines, short summaries and links only; the articles stay on
the papers' own sites.

To add or fix a paper, edit FEEDS below.
"""
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# id, display name, feed address. Order = default order of the paper buttons.
FEEDS = [
    ("bbc", "BBC News", "https://feeds.bbci.co.uk/news/rss.xml"),
    ("guardian", "The Guardian", "https://www.theguardian.com/uk/rss"),
    ("sky", "Sky News", "https://feeds.skynews.com/feeds/rss/home.xml"),
    ("independent", "The Independent", "https://www.independent.co.uk/news/uk/rss"),
    ("mail", "Daily Mail", "https://www.dailymail.co.uk/news/index.rss"),
    ("standard", "The Standard", "https://www.standard.co.uk/news/rss"),
    ("mirror", "The Mirror", "https://www.mirror.co.uk/news/?service=rss"),
    ("metro", "Metro", "https://metro.co.uk/feed/"),
    ("express", "Daily Express", "https://www.express.co.uk/posts/rss/1/uk"),
    # lighter, odd-news feeds: these fill the "Lighter side" section
    ("sky", "Sky News", "https://feeds.skynews.com/feeds/rss/strange.xml", "light"),
    ("mirror", "The Mirror", "https://www.mirror.co.uk/news/weird-news/?service=rss", "light"),
    ("metro", "Metro", "https://metro.co.uk/news/weird/feed/", "light"),
]

# Celebrity and showbiz are left out: by section of the address, then by headline.
SKIP_URL = re.compile(
    r"/(tvshowbiz|showbiz|celebrity|celebs|celebrity-news|3am|femail|entertainment|"
    r"tv-and-radio|tv-radio|tv|soaps|reality-tv|fashion|"
    r"lifeandstyle|life-style|horoscopes?|shopping|deals|voucher|bingo|betting|puzzles)(/|$)",
    re.I,
)
SKIP_TITLE = re.compile(
    r"\b(love island|strictly|i'm a celeb|im a celeb|big brother|bake off|traitors|"
    r"towie|x factor|eastenders|coronation street|corrie|emmerdale|hollyoaks|"
    r"kardashian|jenner|red carpet|showbiz|celebrity|celeb|reality star|influencer|"
    r"bikini|flaunts|sizzles|stuns in|shows off|horoscope|best deals|black friday)\b",
    re.I,
)

MAX_AGE_HOURS = 30      # drop anything older than this
MAX_PER_FEED = 60       # newest items kept per paper
SUMMARY_CHARS = 220
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

STOPWORDS = set(
    """the and for with from that this have has had are was were will would could
    should after before over under into about amid says said say new his her
    their its our your who what when where why how not but out off than then
    they them she him you all more most just been being can may might must
    also live latest news update updates video watch pictures breaking first
    year years day days week man woman people uk british britain""".split()
)


def local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def child_text(node, *names):
    for child in node:
        if local(child.tag) in names and (child.text or "").strip():
            return child.text.strip()
    return ""


def clean(text, limit=None):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:.-") + "…"
    return text


def parse_date(text):
    if not text:
        return None
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def parse_feed(data):
    """Return [{t, u, d, when}] from RSS or Atom bytes."""
    root = ET.fromstring(data)
    items = []
    for node in root.iter():
        if local(node.tag) not in ("item", "entry"):
            continue
        title = clean(child_text(node, "title"))
        link = child_text(node, "link")
        if not link:  # Atom keeps the address in an attribute
            for child in node:
                if local(child.tag) == "link" and child.get("href"):
                    if child.get("rel", "alternate") == "alternate":
                        link = child.get("href")
                        break
        when = parse_date(child_text(node, "pubdate", "published", "updated", "date"))
        summary = clean(child_text(node, "description", "summary"), SUMMARY_CHARS)
        if SKIP_URL.search(link) or SKIP_TITLE.search(title.replace("’", "'")):
            continue
        if title and link.startswith("http"):
            items.append({"t": title, "u": link, "d": summary, "when": when})
    return items


def fetch(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read()


def words(title):
    out = set()
    for word in re.findall(r"[a-z0-9]+", title.lower().replace("’", "").replace("'", "")):
        if len(word) < 3 or word in STOPWORDS:
            continue
        if len(word) > 4 and word.endswith("s"):
            word = word[:-1]
        out.add(word)
    return out


def same_story(a, b):
    shared = len(a & b)
    if shared < 3:
        return False
    return shared / min(len(a), len(b)) >= 0.5


def cluster(items):
    """Group items from different papers that cover the same story."""
    stories = []
    for item in sorted(items, key=lambda i: i["when"], reverse=True):
        item_words = words(item["t"])
        home = None
        for story in stories:
            if abs((story["when"] - item["when"]).total_seconds()) > 36 * 3600:
                continue
            if any(same_story(item_words, w) for w in story["words"]):
                home = story
                break
        if home is None:
            home = {"when": item["when"], "words": [], "versions": []}
            stories.append(home)
        home["words"].append(item_words)
        # one version per paper: keep the newest, which arrives first
        if not any(v["s"] == item["s"] for v in home["versions"]):
            home["versions"].append(item)
    return stories


def build(feed_results, now):
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)
    order = {}
    for feed in FEEDS:
        order.setdefault(feed[0], len(order))
    items, by_id = [], {}
    for source_id, name, light, result in feed_results:
        source = by_id.setdefault(source_id, {"id": source_id, "name": name, "ok": False, "count": 0})
        if isinstance(result, str):
            source.setdefault("error", result)
            continue
        source["ok"] = True
        source.pop("error", None)
        kept = []
        for item in result:
            when = item["when"] or now
            if when > now:
                when = now
            if when >= cutoff:
                kept.append({**item, "when": when, "s": source_id, "light": light})
        kept.sort(key=lambda i: i["when"], reverse=True)
        kept = kept[:MAX_PER_FEED]
        items.extend(kept)
        source["count"] += len(kept)
    sources = list(by_id.values())

    stories = cluster(items)
    out = []
    for story in stories:
        versions = sorted(story["versions"], key=lambda v: order[v["s"]])
        out.append(
            {
                "time": max(v["when"] for v in versions).isoformat(timespec="minutes"),
                "light": any(v["light"] for v in versions),
                "versions": [
                    {
                        "s": v["s"],
                        "t": v["t"],
                        "u": v["u"],
                        "d": v["d"],
                        "p": v["when"].isoformat(timespec="minutes"),
                    }
                    for v in versions
                ],
            }
        )
    # stories carried by the most papers lead; ties go to the newest
    out.sort(key=lambda s: (len(s["versions"]), s["time"]), reverse=True)
    return {"updated": now.isoformat(timespec="minutes"), "sources": sources, "stories": out}


def main():
    now = datetime.now(timezone.utc)
    results = []
    for source_id, name, url, *kind in FEEDS:
        light = bool(kind)
        try:
            results.append((source_id, name, light, parse_feed(fetch(url))))
            print(f"ok    {name}: {len(results[-1][3])} items")
        except Exception as error:  # one broken feed must not stop the rest
            results.append((source_id, name, light, f"{type(error).__name__}: {error}"[:160]))
            print(f"FAIL  {name}: {error}", file=sys.stderr)

    data = build(results, now)
    target = Path(__file__).with_name("data.json")
    if not data["stories"] and target.exists():
        print("No stories fetched; keeping the previous data.json", file=sys.stderr)
        return 1
    target.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {len(data['stories'])} stories to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
