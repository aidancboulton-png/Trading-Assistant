"""
Global News Engine — pulls RSS from 40+ international outlets, translates to English,
clusters stories by topic, and frames each side of the narrative.

Designed to feed the Jarvis script generator with raw multi-perspective intelligence.
"""
from __future__ import annotations

import re
import time
import hashlib
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)

TIMEOUT = 12
MAX_ARTICLES_PER_FEED = 8
CLUSTER_THRESHOLD = 0.28   # Jaccard similarity to merge two stories

# ── RSS Feed Registry ────────────────────────────────────────────────────────
# Format: (name, url, region, bias_label)
# bias_label: "western_mainstream" | "western_alternative" | "eastern" |
#             "middle_east" | "latin" | "african" | "asian" | "european"
FEEDS: list[tuple[str, str, str, str]] = [

    # ── United States ─────────────────────────────────────────────────────────
    ("Reuters",          "https://feeds.reuters.com/reuters/topNews",                "US",     "western_mainstream"),
    ("Associated Press", "https://rss.ap.org/article/apf-topnews",                  "US",     "western_mainstream"),
    ("NPR News",         "https://feeds.npr.org/1001/rss.xml",                       "US",     "western_mainstream"),
    ("The Hill",         "https://thehill.com/feed/",                                "US",     "western_mainstream"),
    ("Politico",         "https://www.politico.com/rss/politicotopstories.xml",       "US",     "western_mainstream"),
    ("The Intercept",    "https://theintercept.com/feed/?rss",                        "US",     "western_alternative"),
    ("Jacobin",          "https://jacobin.com/feed/",                                "US",     "western_alternative"),

    # ── United Kingdom ────────────────────────────────────────────────────────
    ("BBC News",         "http://feeds.bbci.co.uk/news/rss.xml",                     "UK",     "western_mainstream"),
    ("The Guardian",     "https://www.theguardian.com/world/rss",                    "UK",     "western_mainstream"),
    ("The Independent",  "https://www.independent.co.uk/rss",                        "UK",     "western_mainstream"),

    # ── Europe ────────────────────────────────────────────────────────────────
    ("Deutsche Welle",   "https://rss.dw.com/rdf/rss-en-world",                      "DE",     "european"),
    ("France 24",        "https://www.france24.com/en/rss",                          "FR",     "european"),
    ("Euronews",         "https://feeds.feedburner.com/euronews/en/news/",           "EU",     "european"),
    ("RFI English",      "https://www.rfi.fr/en/rss",                               "FR",     "european"),

    # ── Middle East ───────────────────────────────────────────────────────────
    ("Al Jazeera",       "https://www.aljazeera.com/xml/rss/all.xml",               "QA",     "middle_east"),
    ("Middle East Eye",  "https://www.middleeasteye.net/rss",                        "UK/ME",  "middle_east"),
    ("Arab News",        "https://www.arabnews.com/rss.xml",                         "SA",     "middle_east"),
    ("Haaretz",          "https://www.haaretz.com/cmlink/1.628762",                  "IL",     "middle_east"),
    ("Press TV",         "https://www.presstv.ir/rssFeed/2",                         "IR",     "middle_east"),

    # ── Russia / Eastern Europe ───────────────────────────────────────────────
    ("RT",               "https://www.rt.com/rss/news/",                             "RU",     "eastern"),
    ("TASS",             "https://tass.com/rss/v2.xml",                              "RU",     "eastern"),
    ("Sputnik",          "https://sputnikglobe.com/export/rss2/world/index.xml",     "RU",     "eastern"),

    # ── China / Asia-Pacific ──────────────────────────────────────────────────
    ("CGTN",             "https://www.cgtn.com/subscribe/rss/section/world.xml",     "CN",     "eastern"),
    ("Global Times",     "https://www.globaltimes.cn/rss/outbrain.xml",              "CN",     "eastern"),
    ("SCMP",             "https://www.scmp.com/rss/91/feed",                         "HK",     "asian"),
    ("NHK World",        "https://www3.nhk.or.jp/nhkworld/upld/medias/en/rss/news.xml", "JP",  "asian"),
    ("The Hindu",        "https://www.thehindu.com/feeder/default.rss",              "IN",     "asian"),
    ("NDTV",             "https://feeds.feedburner.com/ndtvnews-world-news",          "IN",     "asian"),
    ("Straits Times",    "https://www.straitstimes.com/news/world/rss.xml",           "SG",     "asian"),

    # ── Latin America ─────────────────────────────────────────────────────────
    ("TeleSUR",          "https://www.telesurenglish.net/rss/news.rss",              "VE",     "latin"),
    ("Mercopress",       "https://en.mercopress.com/rss.xml",                        "UY",     "latin"),

    # ── Africa ────────────────────────────────────────────────────────────────
    ("AllAfrica",        "https://allafrica.com/tools/headlines/rdf/world/headlines.rdf", "ZA", "african"),
    ("Daily Maverick",   "https://www.dailymaverick.co.za/dmrss/",                   "ZA",     "african"),
    ("The East African", "https://www.theeastafrican.co.ke/tea/rss",                 "KE",     "african"),

    # ── Economics / Finance specific ─────────────────────────────────────────
    ("Project Syndicate","https://www.project-syndicate.org/rss",                    "INT",    "western_mainstream"),
    ("OECD Observer",    "https://oecdobserver.org/news/fullstory.php/aid/4538/RSS_feed.html", "INT", "western_mainstream"),
]

REGION_LABELS = {
    "western_mainstream": "Western Mainstream",
    "western_alternative": "Western Alt Media",
    "eastern":            "Eastern / State",
    "middle_east":        "Middle East",
    "european":           "European",
    "asian":              "Asia-Pacific",
    "latin":              "Latin America",
    "african":            "Africa",
}

STOP_WORDS = {
    "the","a","an","in","on","at","to","of","by","for","is","are","was","were",
    "has","have","had","will","be","been","with","from","that","this","its","it",
    "he","she","they","we","as","but","or","and","not","over","more","after",
    "amid","amid","say","says","said","report","reports",
}

# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Article:
    id: str
    source: str
    region: str
    bias: str
    title: str
    summary: str
    url: str
    published: str
    category: str = "world"

@dataclass
class StoryCluster:
    id: str
    headline: str          # best/longest title across articles
    articles: list[Article] = field(default_factory=list)
    category: str = "world"

    @property
    def sources(self) -> list[str]:
        return list({a.source for a in self.articles})

    @property
    def regions(self) -> list[str]:
        return list({a.region for a in self.articles})

    @property
    def bias_groups(self) -> dict[str, list[Article]]:
        groups: dict[str, list[Article]] = {}
        for a in self.articles:
            groups.setdefault(a.bias, []).append(a)
        return groups

    @property
    def perspective_count(self) -> int:
        return len(set(a.bias for a in self.articles))


# ── Utilities ─────────────────────────────────────────────────────────────────

def _tokens(text: str) -> set[str]:
    t = re.sub(r"[^\w\s]", " ", text.lower())
    return set(t.split()) - STOP_WORDS

def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)

def _article_id(source: str, title: str) -> str:
    return hashlib.md5(f"{source}:{title}".encode()).hexdigest()[:10]

# ── RSS parser ────────────────────────────────────────────────────────────────

NS = {
    "media": "http://search.yahoo.com/mrss/",
    "dc":    "http://purl.org/dc/elements/1.1/",
    "atom":  "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
}

def _text(el, *tags) -> str:
    for tag in tags:
        child = el.find(tag)
        if child is not None and child.text:
            return child.text.strip()
    return ""

def _fetch_feed(name: str, url: str, region: str, bias: str) -> list[Article]:
    try:
        r = requests.get(url, timeout=TIMEOUT,
                         headers={"User-Agent": "Mozilla/5.0 (news-aggregator/1.0)"})
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
        # Handle both RSS 2.0 and Atom
        items = root.findall(".//item") or root.findall(".//entry")
        articles = []
        for item in items[:MAX_ARTICLES_PER_FEED]:
            title   = _text(item, "title")
            summary = (
                _text(item, "description") or
                _text(item, "summary") or
                _text(item, "{http://purl.org/rss/1.0/modules/content/}encoded") or ""
            )
            # strip HTML tags from summary
            summary = re.sub(r"<[^>]+>", " ", summary).strip()
            summary = re.sub(r"\s+", " ", summary)[:400]
            link    = _text(item, "link", "id")
            if not link:
                link_el = item.find("link")
                if link_el is not None:
                    link = link_el.get("href", "")
            pub = _text(item, "pubDate", "published", "updated", "{http://purl.org/dc/elements/1.1/}date")
            if not title:
                continue
            articles.append(Article(
                id        = _article_id(name, title),
                source    = name,
                region    = region,
                bias      = bias,
                title     = title,
                summary   = summary[:400],
                url       = link,
                published = pub,
                category  = _classify_topic(title + " " + summary),
            ))
        return articles
    except Exception as e:
        log.debug("[%s] RSS fetch failed: %s", name, e)
        return []


# ── Topic classification ──────────────────────────────────────────────────────

TOPIC_KEYWORDS: dict[str, list[str]] = {
    "war_conflict":   ["war","bomb","missile","airstrike","attack","troop","military",
                       "ceasefire","invasion","soldier","weapon","drone","killed","wounded"],
    "economics":      ["gdp","inflation","rate","fed","interest","recession","unemployment",
                       "trade","tariff","deficit","debt","imf","world bank","growth","economy"],
    "politics":       ["election","president","prime minister","vote","parliament","senate",
                       "congress","government","policy","sanction","diplomacy","minister"],
    "climate":        ["climate","temperature","hurricane","flood","wildfire","drought",
                       "carbon","emission","cop","global warming","sea level","storm"],
    "technology":     ["ai","artificial intelligence","tech","chip","semiconductor","elon",
                       "openai","google","meta","microsoft","apple","cyber","data"],
    "health":         ["pandemic","disease","outbreak","vaccine","who","fda","health",
                       "drug","treatment","hospital","cancer","virus"],
    "finance_markets":["stock","market","bitcoin","crypto","gold","oil","inflation","fed",
                       "wall street","nasdaq","dow","currency","dollar","yuan","euro"],
    "society":        ["protest","rights","refugee","immigration","poverty","crime",
                       "education","housing","food","water","hunger"],
}

def _classify_topic(text: str) -> str:
    text_lower = text.lower()
    scores = {cat: sum(1 for kw in kws if kw in text_lower)
              for cat, kws in TOPIC_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "world"


# ── Clustering ────────────────────────────────────────────────────────────────

def _cluster_articles(articles: list[Article]) -> list[StoryCluster]:
    clusters: list[StoryCluster] = []

    for article in articles:
        best_cluster = None
        best_score = 0.0
        for cluster in clusters:
            score = _jaccard(article.title, cluster.headline)
            if score > best_score:
                best_score = score
                best_cluster = cluster
        if best_cluster and best_score >= CLUSTER_THRESHOLD:
            best_cluster.articles.append(article)
            # upgrade headline to longest/most descriptive title
            if len(article.title) > len(best_cluster.headline):
                best_cluster.headline = article.title
        else:
            clusters.append(StoryCluster(
                id       = article.id,
                headline = article.title,
                articles = [article],
                category = article.category,
            ))

    # sort: most-covered stories first (most sources = most significant)
    clusters.sort(key=lambda c: (len(c.articles), c.perspective_count), reverse=True)
    return clusters


# ── Translation stub (free, no API key needed) ────────────────────────────────

def _ensure_english(text: str) -> str:
    """
    Basic detection: if text has common English stop words, skip.
    Otherwise attempt translation via MyMemory free API (1000 req/day).
    """
    if not text:
        return text
    # quick English check — if >30% of first 10 tokens are common English words
    english_indicators = {"the","is","are","was","in","on","at","of","to","and","a","an","that"}
    tokens = re.sub(r"[^\w\s]", " ", text.lower()).split()[:12]
    if not tokens:
        return text
    overlap = sum(1 for t in tokens if t in english_indicators)
    if overlap / len(tokens) >= 0.2:
        return text  # already English enough
    # attempt free translation
    try:
        r = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text[:500], "langpair": "autodetect|en"},
            timeout=8,
        )
        data = r.json()
        translated = data.get("responseData", {}).get("translatedText", "")
        if translated and len(translated) > 10:
            return translated
    except Exception:
        pass
    return text  # return original if translation fails


# ── Main aggregator ───────────────────────────────────────────────────────────

def aggregate(max_workers: int = 12, translate: bool = True) -> dict:
    """
    Fetch all feeds in parallel, cluster stories, return structured result.
    """
    all_articles: list[Article] = []
    feed_status: dict[str, str] = {}

    print(f"[newsengine] Fetching {len(FEEDS)} feeds…")
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_fetch_feed, name, url, region, bias): name
            for name, url, region, bias in FEEDS
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                articles = future.result()
                all_articles.extend(articles)
                feed_status[name] = f"ok ({len(articles)} articles)"
            except Exception as e:
                feed_status[name] = f"error: {e}"

    print(f"[newsengine] Got {len(all_articles)} raw articles")

    # Translate non-English content
    if translate:
        for a in all_articles:
            a.title   = _ensure_english(a.title)
            a.summary = _ensure_english(a.summary)

    # Cluster into stories
    clusters = _cluster_articles(all_articles)
    print(f"[newsengine] Clustered into {len(clusters)} stories")

    # Serialize
    serialized = []
    for c in clusters[:60]:  # top 60 stories
        groups = c.bias_groups
        perspectives = []
        for bias, arts in groups.items():
            # pick the most detailed article per bias group
            best = max(arts, key=lambda a: len(a.summary))
            perspectives.append({
                "bias":    bias,
                "label":   REGION_LABELS.get(bias, bias),
                "source":  best.source,
                "region":  best.region,
                "title":   best.title,
                "summary": best.summary,
                "url":     best.url,
            })
        serialized.append({
            "id":              c.id,
            "headline":        c.headline,
            "category":        c.category,
            "source_count":    len(c.articles),
            "perspective_count": c.perspective_count,
            "sources":         c.sources,
            "perspectives":    perspectives,
        })

    # Category breakdown
    cat_counts: dict[str, int] = {}
    for c in clusters:
        cat_counts[c.category] = cat_counts.get(c.category, 0) + 1

    return {
        "ts":            datetime.now(timezone.utc).isoformat(),
        "total_articles": len(all_articles),
        "total_stories":  len(clusters),
        "feed_count":    sum(1 for v in feed_status.values() if v.startswith("ok")),
        "by_category":   cat_counts,
        "stories":       serialized,
        "feed_status":   feed_status,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json, sys
    result = aggregate()
    top = result["stories"][:5]
    print(f"\n{'═'*80}")
    print(f"  GLOBAL NEWS BRIEF  |  {len(result['stories'])} stories from {result['feed_count']} feeds")
    print(f"{'═'*80}\n")
    for story in top:
        print(f"  [{story['category'].upper()}]  {story['headline'][:70]}")
        print(f"  Covered by {story['source_count']} sources | {story['perspective_count']} distinct perspectives")
        for p in story["perspectives"]:
            print(f"    [{p['label']:25s}] {p['source']:20s} — {p['summary'][:80]}")
        print()
