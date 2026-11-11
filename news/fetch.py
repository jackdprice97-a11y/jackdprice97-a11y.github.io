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
import os
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
    ("telegraph", "The Telegraph", "https://www.telegraph.co.uk/rss.xml"),
    ("gbnews", "GB News", "https://www.gbnews.com/feeds/news.rss"),
    # lighter, odd-news feeds: these fill the "Lighter side" section
    ("sky", "Sky News", "https://feeds.skynews.com/feeds/rss/strange.xml", "light"),
    ("mirror", "The Mirror", "https://www.mirror.co.uk/news/weird-news/?service=rss", "light"),
    ("metro", "Metro", "https://metro.co.uk/news/weird/feed/", "light"),
    # sport: these fill the "Sport" section
    ("bbc", "BBC News", "https://feeds.bbci.co.uk/sport/rss.xml", "sport"),
    ("guardian", "The Guardian", "https://www.theguardian.com/uk/sport/rss", "sport"),
    ("sky", "Sky News", "https://www.skysports.com/rss/12040", "sport"),
    ("telegraph", "The Telegraph", "https://www.telegraph.co.uk/sport/rss.xml", "sport"),
]

# Celebrity and showbiz are left out: by section of the address, then by headline.
SKIP_URL = re.compile(
    r"/(tvshowbiz|showbiz|celebrity|celebs|celebrity-news|3am|femail|entertainment|"
    r"tv-and-radio|tv-radio|tv|soaps|reality-tv|fashion|"
    r"lifeandstyle|life-style|horoscopes?|shopping|deals|voucher|bingo|betting|puzzles)(/|$)",
    re.I,
)
SPORT_URL = re.compile(r"/sport/|skysports\.com|/football/|/rugby-union/|/cricket/", re.I)
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
SUMMARY_CHARS = 220     # summary shown on the page
SOURCE_CHARS = 600      # longer extract kept as source material for writing copy

# Straplines: two caption lines per story, as on a TV news channel.
STRAP_TOP = 43          # most characters on the top line
STRAP_BOTTOM = 75       # most characters on the bottom line
STRAP_STYLE = 4         # raise this after changing the brief: every strap is rewritten
STRAP_MODEL_MAIN = "claude-opus-5-5"    # stories several papers carry: the best writer
STRAP_MODEL_REST = "claude-sonnet-5-5"  # everything else: fast and cheaper
STRAP_BATCH_MAIN, STRAP_BATCH_REST = 10, 20  # stories per request
STRAP_MAX_MAIN, STRAP_MAX_REST = 60, 160     # new straps per run; the rest wait for the next
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
        described = child_text(node, "description", "summary")
        summary = clean(described, SUMMARY_CHARS)
        source = clean(described, SOURCE_CHARS)
        if SKIP_URL.search(link) or SKIP_TITLE.search(title.replace("’", "'")):
            continue
        if title and link.startswith("http"):
            items.append({"t": title, "u": link, "d": summary, "x": source if source != summary else "", "when": when})
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

    # second pass: join groups that the first pass split because of the order items arrived in
    merged = True
    while merged:
        merged = False
        for i, first in enumerate(stories):
            for second in stories[i + 1 :]:
                if abs((first["when"] - second["when"]).total_seconds()) > 36 * 3600:
                    continue
                if any(same_story(a, b) for a in first["words"] for b in second["words"]):
                    first["words"].extend(second["words"])
                    for version in second["versions"]:
                        if not any(v["s"] == version["s"] for v in first["versions"]):
                            first["versions"].append(version)
                    first["when"] = max(first["when"], second["when"])
                    stories.remove(second)
                    merged = True
                    break
            if merged:
                break
    return stories


def build(feed_results, now):
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)
    order = {}
    for feed in FEEDS:
        order.setdefault(feed[0], len(order))
    items, by_id = [], {}
    for source_id, name, kind, result in feed_results:
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
                kept.append({**item, "when": when, "s": source_id, "kind": kind})
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
                "light": any(v["kind"] == "light" for v in versions),
                "sport": any(v["kind"] == "sport" or SPORT_URL.search(v["u"]) for v in versions),
                "versions": [
                    {
                        "s": v["s"],
                        "t": v["t"],
                        "u": v["u"],
                        "d": v["d"],
                        **({"x": v["x"]} if v.get("x") else {}),
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
- top: at most {top} characters. A punchy headline: who or what, and what is happening.
- bottom: at most {bottom} characters. One flowing sentence that carries the story on: the detail a viewer needs after reading the top line (who, where, how many, why, what happens next, or the response).

House style:
- Present tense and active voice wherever it applies ("PM announces", "Police arrest man", "Court hears"). Use the past only for a completed event that needs it ("after crash killed three").
- Each line is a single clear statement, direct and punchy, in the manner of a rolling news channel strap. No semicolons, ever. Do not bolt two fragments together: join ideas with "as", "after", "with", "amid" or a comma, or drop the lesser fact.
- The bottom line gives the context that makes the top line make sense. It says who the people are and what the organisations are, in a few words (the NFL great, Championship club Birmingham City, the Little Britain comedian), and adds the key detail. Assume the viewer has never heard of anyone in the story. A bare label such as "Championship club" or "the firm" with no name is not enough: name it.
- The bottom line must not simply restate the top line's point in other words. It may name the subject again where that is the clearest way to give context.
- Clarity comes first. A viewer glancing up with no background must understand each line at once. Say plainly what was said, done or decided: write "says UK should rejoin EU in the long term", never "goes further than the PM". No comparisons, allusions or references that only make sense if you already know the story.
- The bottom line must stand on its own. Never refer back to the top line's subject with a pronoun (he, she, they, it, his, her, their): use a describing word instead, as a news writer would, such as the comedian, the author, the minister, the 41-year-old, the club or the company, or use the name again.
- Give people their role the first time they appear ("Scottish Labour leader Michael Marra"), and do not put a second person in the line unless the story is about them too.
- One idea per line. The bottom line gives the single most important further fact, or two closely linked ones. Never cram in a list of dates, fixtures, scores, figures or places. Choose why it matters, what is at stake or what happens next over logistics such as dates and schedules.
- In sport, make clear which sport, team or competition the story is about.
- Be specific: names, places, numbers, ages. No vague filler ("row erupts", "questions raised", "what we know"), no puns, no questions, no teasing.
- Accuracy beats everything, including punch. Every claim in a strap must be stated directly in the headlines or summary given. Never infer, guess or imply anyone's position, motive or reaction: no "unlike", "despite", "in a blow to", "defying" or similar contrasts unless the source states that contrast in terms. If the source says someone was urged to do something, say they were urged, not that they refused or disagree. When unsure, leave it out and use another fact from the source.
- Use only the facts given. Attribute anything alleged, claimed or unproven (police say, court hears, he denies); never state an allegation as fact. Do not call someone a victim or offender unless a court has found it.
- Write the United States as U.S. (with full stops), never US.
- Prime Minister can be shortened to PM when space is tight.
- Headline style: never open a line with "The", "A" or "An" (write "Comedian denies claims", not "The comedian denies claims"), and drop articles elsewhere wherever a headline would.
- Plain sentence case, British spelling, numerals for numbers, no full stop at the end, no emoji. Use a name a viewer will recognise.
- Count every character including spaces. Never go over either limit. Shorter is fine.

Reply with only a JSON array, one object per story: [{{"n": 0, "top": "...", "bottom": "..."}}]

Stories:
{stories}"""


STRAP_FIX_PROMPT = """These TV news straplines break house rules (too long, or they use a semicolon). Rewrite each one so that top is at most {top} characters and bottom is at most {bottom} characters, counting spaces. Cut words or rephrase; use no semicolons, keep each line a single flowing statement, never use a pronoun (he, she, they, it) for the subject, never open a line with "The", "A" or "An", keep the present tense, the facts, the attribution of anything alleged, and U.S. written with full stops, and keep the two lines from repeating each other. A line already within its limit can stay as it is. Keep each number as given.

Reply with only a JSON array: [{{"n": 0, "top": "...", "bottom": "..."}}]

{straps}"""


def house_style(text):
    text = re.sub(r"\s+", " ", str(text)).strip()
    text = re.sub(r"\bUS\b(?!\.)", "U.S.", text)  # house style: U.S., never US
    opening = re.match(r"(?:The|A|An) (?=\S)", text)  # headline style: no article to open a line
    if opening:
        rest = text[opening.end():]
        text = rest[0].upper() + rest[1:]
    return text[:-1] if text.endswith(".") and not text.endswith("U.S.") else text


def claude(key, prompt, model):
    body = {"model": model, "max_tokens": 16000, "messages": [{"role": "user", "content": prompt}]}
    if model == STRAP_MODEL_MAIN:
        # let it reason briefly: it checks facts and counts characters far better
        body["thinking"] = {"type": "adaptive"}
        body["output_config"] = {"effort": "low"}
    else:
        body["thinking"] = {"type": "between_tools"}  # answer directly
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        reply = json.loads(response.read())
    text = "".join(block.get("text", "") for block in reply.get("content", []))
    found = re.search(r"\[.*\]", text, re.S)
    if not found:
        raise ValueError(f"no list in reply (stop: {reply.get('stop_reason')})")
    try:
        entries = json.loads(found.group(0), strict=False)
    except ValueError:  # one bad entry must not lose the batch: read them one at a time
        entries = []
        for piece in re.findall(r"\{[^{}]*\}", found.group(0)):
            try:
                entries.append(json.loads(piece, strict=False))
            except ValueError:
                pass
    out = {}
    for entry in entries:
        if not isinstance(entry, dict) or "n" not in entry:
            continue
        top, bottom = house_style(entry.get("top", "")), house_style(entry.get("bottom", ""))
        if top and bottom:
            out[int(entry["n"])] = {"top": top, "bottom": bottom}
    return out


def too_long(strap):
    """True when a strap needs sending back: over length, or uses a semicolon."""
    if ";" in strap["top"] or ";" in strap["bottom"]:
        return True
    return len(strap["top"]) > STRAP_TOP or len(strap["bottom"]) > STRAP_BOTTOM


DANGLING = {
    "a", "an", "the", "of", "to", "in", "on", "at", "for", "and", "as", "after", "over", "his", "her",
    "their", "its", "by", "with", "near", "is", "are", "was", "were", "from", "amid", "into", "that", "who", "he", "she", "it",
}


def trim(text, limit):
    """Last resort for a line still over the limit: cut at a word, drop dangling words."""
    text = fit(text.replace(";", ","), limit)
    words_left = text.split(" ")
    while len(words_left) > 2 and words_left[-1].lower().strip(",") in DANGLING:
        words_left.pop()
    return " ".join(words_left).rstrip(" ,")


def source_lines(batch):
    lines = []
    for n, story in enumerate(batch):
        versions = story["versions"]
        summary = next((v["d"] for v in versions if v["d"]), "")
        lines.append(f"{n}. HEADLINES: " + " | ".join(v["t"] for v in versions[:4]) + f"\n   SUMMARY: {summary}")
    return lines


def shorten(key, straps, model):
    """Send back any strap that is over length or uses a semicolon, with real counts."""
    for _ in range(3):
        over = {n: strap for n, strap in straps.items() if too_long(strap)}
        if not over:
            return
        notes = []
        for n, strap in over.items():
            notes.append(
                f'{n}. top ({len(strap["top"])} characters, limit {STRAP_TOP}): {strap["top"]}\n'
                f'   bottom ({len(strap["bottom"])} characters, limit {STRAP_BOTTOM}): {strap["bottom"]}'
            )
        try:
            fixed = claude(key, STRAP_FIX_PROMPT.format(top=STRAP_TOP - 2, bottom=STRAP_BOTTOM - 3, straps="\n".join(notes)), model)
        except Exception:
            continue
        straps.update({n: strap for n, strap in fixed.items() if n in over})


def ask_claude(key, batch, model):
    lines = source_lines(batch)
    # ask for a little under the real limits: the model miscounts by a few characters
    straps = claude(key, STRAP_PROMPT.format(top=STRAP_TOP - 4, bottom=STRAP_BOTTOM - 6, stories="\n".join(lines)), model)
    straps = {n: strap for n, strap in straps.items() if 0 <= n < len(batch)}
    shorten(key, straps, model)

    for strap in straps.values():  # never over, whatever happened above
        strap["top"], strap["bottom"] = trim(strap["top"], STRAP_TOP), trim(strap["bottom"], STRAP_BOTTOM)
    return straps


def add_straps(data, target):
    """Give every story a strapline: reuse saved ones, write new ones, else trim."""
    import os

    saved = {}
    try:
        for story in json.loads(target.read_text(encoding="utf-8"))["stories"]:
            strap = story.get("strap")
            if strap and not strap.get("auto") and strap.get("v") == STRAP_STYLE:
                for version in story["versions"]:
                    saved[version["u"]] = strap
    except (OSError, ValueError, KeyError):
        pass

    main_todo, rest_todo = [], []
    for story in data["stories"]:
        strap = next((saved[v["u"]] for v in story["versions"] if v["u"] in saved), None)
        is_main = len(story["versions"]) > 1
        if strap:
            strap["top"], strap["bottom"] = house_style(strap["top"]), house_style(strap["bottom"])
            story["strap"] = strap
            if is_main and strap.get("by") != "main":
                main_todo.append(story)  # now a main story: give it the best writer
        else:
            story["strap"] = rough_strap(story)
            (main_todo if is_main else rest_todo).append(story)

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        print("No ANTHROPIC_API_KEY set: straplines are trimmed headlines")
        return
    written = 0
    plans = [
        (main_todo[:STRAP_MAX_MAIN], STRAP_BATCH_MAIN, STRAP_MODEL_MAIN, "main"),
        (rest_todo[:STRAP_MAX_REST], STRAP_BATCH_REST, STRAP_MODEL_REST, "rest"),
    ]
    for todo, size, model, label in plans:
        for start in range(0, len(todo), size):
            batch = todo[start : start + size]
            try:
                for n, strap in ask_claude(key, batch, model).items():
                    batch[n]["strap"] = {**strap, "by": label, "v": STRAP_STYLE}
                    written += 1
            except Exception as error:  # keep what they had and carry on
                print(f"FAIL  straplines ({label}): {type(error).__name__}: {error}", file=sys.stderr)
                break
    print(f"Wrote {written} new straplines")


PAPERS_INDEX = "https://www.bbc.co.uk/news/blogs/the_papers"

FRONT_PROMPT = """Below is a review of today's British newspaper front pages, then a numbered list of news stories.

For each numbered story that the review says is on a newspaper's front page, list the newspapers that carry it there. Match on the story itself, not on a loose theme: the review must describe that same event or announcement as being on that paper's front page. Leave out any story the review does not place on a front page.

Use short newspaper names: Times, Telegraph, Guardian, Mail, Express, Mirror, Sun, i, FT, Star, Metro, Independent.

Reply with only a JSON array: [{{"n": 0, "papers": ["Mail", "Times"]}}]. If nothing matches, reply [].

REVIEW
{review}

STORIES
{stories}"""


def front_pages(data, target, key):
    """Flag stories that are on newspaper front pages, using the BBC's nightly review."""
    previous = {}
    try:
        previous = json.loads(target.read_text(encoding="utf-8")).get("front", {})
    except (OSError, ValueError):
        pass
    flags, review_url = previous.get("flags", {}), previous.get("url", "")
    review_date = previous.get("date", "")
    try:
        index = fetch(PAPERS_INDEX).decode("utf-8", "replace")
        links = re.findall(r'href="(/news/articles/[a-z0-9]+)"', index)
        print(f"front pages: {len(links)} article links on the papers page")
        # the newest review is not always the first link: read the dates of the first few
        best = None
        for link in list(dict.fromkeys(links))[:4]:
            url = "https://www.bbc.co.uk" + link
            known = parse_date(review_date) if url == review_url else None
            if known:  # already read this one: no need to fetch it again
                if best is None or known > best[0]:
                    best = (known, url, None)
                continue
            page = fetch(url).decode("utf-8", "replace")
            stamp = re.search(r'"datePublished":"([^"]+)"', page) or re.search(r'<time[^>]*datetime="([^"]+)"', page)
            when = parse_date(stamp.group(1)) if stamp else None
            print(f"front pages: {url} published {when}")
            if when and (best is None or when > best[0]):
                best = (when, url, page)
        if best and (datetime.now(timezone.utc) - best[0]).total_seconds() > 30 * 3600:
            print("front pages: newest review is over a day old, clearing flags")
            flags, review_url, best = {}, "", None
        if best and best[2] and key:
            when, url, page = best
            if True:
                paragraphs = [clean(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", page, re.S)]
                review = " ".join(p for p in paragraphs if len(p) > 40)[:9000]
                title = clean(" ".join(re.findall(r"<h1[^>]*>(.*?)</h1>", page, re.S)[:1]))
                print(f"front pages: review {url} | {title!r} | {len(review)} characters")
                if len(review) > 600:
                    stories = [s for s in data["stories"] if not s.get("sport")][:220]
                    listing = "\n".join(
                        f"{n}. " + " | ".join(v["t"] for v in story["versions"][:3]) for n, story in enumerate(stories)
                    )
                    body = {
                        "model": STRAP_MODEL_MAIN,
                        "max_tokens": 16000,
                        "thinking": {"type": "adaptive"},
                        "output_config": {"effort": "low"},
                        "messages": [{"role": "user", "content": FRONT_PROMPT.format(review=review, stories=listing)}],
                    }
                    request = urllib.request.Request(
                        "https://api.anthropic.com/v1/messages",
                        data=json.dumps(body).encode(),
                        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=300) as response:
                        reply = json.loads(response.read())
                    text = "".join(block.get("text", "") for block in reply.get("content", []))
                    found = re.search(r"\[.*\]", text, re.S)
                    flags = {}
                    for entry in json.loads(found.group(0), strict=False) if found else []:
                        n, papers = int(entry["n"]), [str(p)[:20] for p in entry.get("papers", [])][:8]
                        if 0 <= n < len(stories) and papers:
                            for version in stories[n]["versions"]:
                                flags[version["u"]] = papers
                    review_url, review_date = url, when.isoformat(timespec="minutes")
                    print(f"front pages: flagged {len({tuple(v) for v in flags.values()})} stories")
    except Exception as error:  # the page works without the flags
        print(f"FAIL  front pages: {type(error).__name__}: {error}", file=sys.stderr)

    for story in data["stories"]:
        papers = next((flags[v["u"]] for v in story["versions"] if v["u"] in flags), None)
        if papers:
            story["front"] = papers
    data["front"] = {"url": review_url, "date": review_date, "flags": flags}


def main():
    now = datetime.now(timezone.utc)
    results = []
    for source_id, name, url, *kind in FEEDS:
        kind = kind[0] if kind else ""
        try:
            results.append((source_id, name, kind, parse_feed(fetch(url))))
            print(f"ok    {name}: {len(results[-1][3])} items")
        except Exception as error:  # one broken feed must not stop the rest
            results.append((source_id, name, kind, f"{type(error).__name__}: {error}"[:160]))
            print(f"FAIL  {name}: {error}", file=sys.stderr)

    data = build(results, now)
    target = Path(__file__).with_name("data.json")
    if not data["stories"] and target.exists():
        print("No stories fetched; keeping the previous data.json", file=sys.stderr)
        return 1
    add_straps(data, target)
    front_pages(data, target, os.environ.get("ANTHROPIC_API_KEY", "").strip())
    # stamp the finish time: writing straplines can take minutes, and the page shows this
    data["updated"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
    data["brief"] = STRAP_PROMPT.format(top=STRAP_TOP - 4, bottom=STRAP_BOTTOM - 6, stories="{stories}")
    target.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {len(data['stories'])} stories to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
