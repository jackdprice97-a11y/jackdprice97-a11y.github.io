#!/usr/bin/env python3
"""Collect today's headlines from free British news sites into news/data.json.

Run by .github/workflows/news.yml every hour. Standard library only.
Stores headlines, short summaries and links only; the articles stay on
the papers' own sites.

To add or fix a paper, edit FEEDS below.
"""
import gzip
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
    ("independent", "The Independent", "https://www.independent.co.uk/rss"),
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
    r"bikini|flaunts|sizzles|stuns in|shows off|horoscope|best deals|black friday|"
    # dating and relationship gossip
    r"dating|romance|romantic|loved-up|loved up|new beau|new flame|new man|new woman|"
    r"pda|cosy|cosies up|date night|split from|splits from|break-up|breakup|"
    r"rekindles?|reunites? with ex|marriage woes|love life|love triangle|heartbreak|"
    r"baby bump|baby joy|engagement ring|honeymoon)\b",
    re.I,
)

MAX_AGE_HOURS = 36      # drop anything older than this
MAX_PER_FEED = 100      # newest items kept per paper
SUMMARY_CHARS = 220

# Straplines: two caption lines per story, as on a TV news channel.
STRAP_TOP = 43          # most characters on the top line
STRAP_BOTTOM = 75       # most characters on the bottom line
STRAP_MODEL = "claude-sonnet-5-5"
STRAP_BATCH = 20        # stories per request
STRAP_MAX_PER_RUN = 160 # new stories written per run; the rest wait for the next
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
        data = response.read()
    if data[:2] == b"\x1f\x8b":  # some sites send gzip without being asked
        data = gzip.decompress(data)
    return data.lstrip(b"\xef\xbb\xbf \t\r\n")


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


def fit(text, limit):
    """Shorten to the limit at a word boundary. Never goes over."""
    text = re.sub(r"\s+", " ", text or "").strip().rstrip("…").strip()
    if len(text) <= limit:
        return text
    cut = text[: limit + 1].rsplit(" ", 1)[0][:limit]
    return cut.rstrip(" ,;:-–—'‘\"")


def rough_strap(story):
    """Fallback when no AI key is set: trimmed headline and summary."""
    first = story["versions"][0]
    options = [first["d"]] + [v["t"] for v in story["versions"][1:]] + [first["t"]]
    bottom = next(text for text in options if text)
    bottom = re.split(r"(?<=[.!?])\s", bottom)[0].rstrip(".")
    return {"top": fit(first["t"], STRAP_TOP), "bottom": fit(bottom, STRAP_BOTTOM), "auto": True}


STRAP_PROMPT = """You are the output editor on a British TV news channel, writing straplines: the two-line captions at the bottom of the screen.

For each numbered story below, write:
- top: at most {top} characters. Who or what, and what is happening: the story in one line.
- bottom: at most {bottom} characters. The next most important facts: the detail a viewer needs after reading the top line (who, where, how many, why, what happens next, or the response).

House style:
- Present tense and active voice wherever it applies ("PM announces", "Police arrest man", "Court hears"). Use the past only for a completed event that needs it ("after crash killed three").
- The two lines must not repeat each other's words or facts. Read together, they tell the whole story to someone who has not heard it.
- Be specific: names, places, numbers, ages. No vague filler ("row erupts", "questions raised", "what we know"), no puns, no questions, no teasing.
- Use only the facts given. Attribute anything alleged, claimed or unproven (police say, court hears, he denies); never state an allegation as fact. Do not call someone a victim or offender unless a court has found it.
- Write the United States as U.S. (with full stops), never US.
- Plain sentence case, British spelling, numerals for numbers, no full stop at the end, no emoji. Use a name a viewer will recognise.
- Count every character including spaces. Never go over either limit. Shorter is fine.

Reply with only a JSON array, one object per story: [{{"n": 0, "top": "...", "bottom": "..."}}]

Stories:
{stories}"""


STRAP_FIX_PROMPT = """These TV news straplines are too long. Rewrite each one shorter so that top is at most {top} characters and bottom is at most {bottom} characters, counting spaces. Cut words or rephrase; keep the present tense, the facts, the attribution of anything alleged, and U.S. written with full stops, and keep the two lines from repeating each other. A line already within its limit can stay as it is. Keep each number as given.

Reply with only a JSON array: [{{"n": 0, "top": "...", "bottom": "..."}}]

{straps}"""


def house_style(text):
    text = re.sub(r"\s+", " ", str(text)).strip()
    text = re.sub(r"\bUS\b(?!\.)", "U.S.", text)  # house style: U.S., never US
    return text[:-1] if text.endswith(".") and not text.endswith("U.S.") else text


def claude(key, prompt):
    body = {
        "model": STRAP_MODEL,
        "max_tokens": 6000,
        "thinking": {"type": "between_tools"},  # answer directly, no reasoning first
        "messages": [{"role": "user", "content": prompt}],
    }
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        reply = json.loads(response.read())
    text = "".join(block.get("text", "") for block in reply.get("content", []))
    found = re.search(r"\[.*\]", text, re.S)
    if not found:
        raise ValueError(f"no list in reply (stop: {reply.get('stop_reason')})")
    out = {}
    for entry in json.loads(found.group(0)):
        top, bottom = house_style(entry.get("top", "")), house_style(entry.get("bottom", ""))
        if top and bottom:
            out[int(entry["n"])] = {"top": top, "bottom": bottom}
    return out


def too_long(strap):
    return len(strap["top"]) > STRAP_TOP or len(strap["bottom"]) > STRAP_BOTTOM


def ask_claude(key, batch):
    lines = []
    for n, story in enumerate(batch):
        versions = story["versions"]
        summary = next((v["d"] for v in versions if v["d"]), "")
        lines.append(f"{n}. HEADLINES: " + " | ".join(v["t"] for v in versions[:4]) + f"\n   SUMMARY: {summary}")
    # ask for a little under the real limits: the model miscounts by a few characters
    straps = claude(key, STRAP_PROMPT.format(top=STRAP_TOP - 4, bottom=STRAP_BOTTOM - 6, stories="\n".join(lines)))

    for _ in range(2):  # send back any that are still over, with the real counts
        over = {n: strap for n, strap in straps.items() if too_long(strap)}
        if not over:
            break
        notes = []
        for n, strap in over.items():
            notes.append(
                f'{n}. top ({len(strap["top"])} characters, limit {STRAP_TOP}): {strap["top"]}\n'
                f'   bottom ({len(strap["bottom"])} characters, limit {STRAP_BOTTOM}): {strap["bottom"]}'
            )
        try:
            straps.update(claude(key, STRAP_FIX_PROMPT.format(top=STRAP_TOP, bottom=STRAP_BOTTOM, straps="\n".join(notes))))
        except Exception:
            break
    for strap in straps.values():  # last resort: never go over
        strap["top"], strap["bottom"] = fit(strap["top"], STRAP_TOP), fit(strap["bottom"], STRAP_BOTTOM)
    return straps


def add_straps(data, target):
    """Give every story a strapline: reuse saved ones, write new ones, else trim."""
    import os

    saved = {}
    try:
        for story in json.loads(target.read_text(encoding="utf-8"))["stories"]:
            strap = story.get("strap")
            if strap and not strap.get("auto"):
                for version in story["versions"]:
                    saved[version["u"]] = strap
    except (OSError, ValueError, KeyError):
        pass

    todo = []
    for story in data["stories"]:
        strap = next((saved[v["u"]] for v in story["versions"] if v["u"] in saved), None)
        if strap:
            story["strap"] = strap
        else:
            story["strap"] = rough_strap(story)
            todo.append(story)

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        print("No ANTHROPIC_API_KEY set: straplines are trimmed headlines")
        return
    todo = todo[:STRAP_MAX_PER_RUN]  # stories are already ordered most-covered first
    written = 0
    for start in range(0, len(todo), STRAP_BATCH):
        batch = todo[start : start + STRAP_BATCH]
        try:
            for n, strap in ask_claude(key, batch).items():
                if 0 <= n < len(batch):
                    batch[n]["strap"] = strap
                    written += 1
        except Exception as error:  # keep the trimmed versions and carry on
            print(f"FAIL  straplines: {type(error).__name__}: {error}", file=sys.stderr)
            break
    print(f"Wrote {written} new straplines")


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
    add_straps(data, target)
    target.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {len(data['stories'])} stories to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
