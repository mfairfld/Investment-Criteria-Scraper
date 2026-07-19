"""
crawler.py
----------
Handles all website crawling for the PE/VC research pipeline.
Uses Crawl4AI AdaptiveCrawler for real crawls.

Current mode: REAL (Crawl4AI 0.8.x)
To switch to mock: set CRAWL_ENABLED = False

Anti-403 strategy:
  Step 1 — Crawl4AI gets whatever it can (homepage + any pages it can validate)
  Step 2 — Parse internal links from homepage content we already have
            Filter to priority-looking ones (strategy, portfolio, criteria etc.)
            Plain requests.get() with browser headers on each real link
            200 → keep | 403/404/other → skip
            No JS render, no Wayback, no blind path guessing

Called by pipeline.py:
    from crawler import crawl_firm
    result = crawl_firm(record)

KB record format (per page):
  {
    "url":          str,
    "content":      str,
    "type":         str,   # criteria | strategy | portfolio | homepage | pdf
    "source":       str,   # live | crawl4ai
    "wayback_date": str,   # always null, kept for schema compatibility
  }
"""

import re
import time
import random
import json
import asyncio
import requests
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CRAWL_ENABLED   = True
CRAWL_STATE_DIR = Path(__file__).parent.parent / "crawl_states"
KB_DIR          = Path(__file__).parent.parent / "kb"

CRAWL_CONFIG = {
    "confidence_threshold": 0.75,
    "max_pages":            15,
    "top_k_links":          4,
    "save_state":           True,
}

# Keywords that indicate a priority page worth fetching
PRIORITY_KEYWORDS = [
    # Explicit criteria pages
    "investment-criteria", "our-criteria", "criteria",
    "target-sectors-investment-criteria",
    "investment-strategy",
    # Strategy/approach pages  
    "strategy", "approach", "our-approach", "focus",
    "our-thesis", "thesis", "principles",
    "mission-statement-values",
    # Portfolio/investments
    "portfolio", "investments", "our-companies",
    "our-companies-current", "current-investments",
    "real-assets",
    # About/firm pages
    "about", "our-firm", "who-we-are", "firm",
    "about-broadwing-private-equity-firm",
]

# Keywords that indicate pages to skip
SKIP_KEYWORDS = [
    "privacy", "terms", "legal", "disclaimer", "cookie",
    "careers", "jobs",
    "news", "news-insights", "press", "press-releases",
    "media", "blog",
    "team", "our-team", "meet-the-team",
    "ridgewood-infrastructure-team",
    "people", "staff",
    "login", "signin", "signup", "register", "password",
    "twitter", "linkedin", "facebook", "instagram",
    "sustainability", "esg", "community",
    "talent-programs", "ceo-in-residence-program",
    "propel",
    ".jpg", ".png", ".zip", "mailto:",
]

CRAWL_QUERY = (
    "investment criteria EBITDA revenue minimum maximum "
    "sectors buyout portfolio deal size check"
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

DELAY_NORMAL = 0.5


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def crawl_firm(record: dict) -> dict:
    firm_name = record.get("firm_name", "Unknown")
    website   = record.get("website")
    slug      = record.get("firm_slug", "unknown")

    print(f"  [crawler] {firm_name}")

    if not website:
        return _failure("no_website")

    if CRAWL_ENABLED:
        return _real_crawl(website, slug, firm_name)
    else:
        return _mock_crawl(website, slug, firm_name)


# ---------------------------------------------------------------------------
# Real crawl
# ---------------------------------------------------------------------------

def _real_crawl(website: str, slug: str, firm_name: str) -> dict:
    try:
        from crawl4ai import AsyncWebCrawler, AdaptiveCrawler, AdaptiveConfig

        async def _crawl():
            state_path = str(CRAWL_STATE_DIR / f"{slug}.json")
            config = AdaptiveConfig(
                confidence_threshold = CRAWL_CONFIG["confidence_threshold"],
                max_pages            = CRAWL_CONFIG["max_pages"],
                top_k_links          = CRAWL_CONFIG["top_k_links"],
                save_state           = CRAWL_CONFIG["save_state"],
                state_path           = state_path,
            )
            async with AsyncWebCrawler() as crawler:
                adaptive = AdaptiveCrawler(crawler, config)
                state    = await adaptive.digest(
                    start_url = website,
                    query     = CRAWL_QUERY,
                )
                return state, adaptive

        state, adaptive = asyncio.run(_crawl())

        pages            = []
        source_urls      = []
        homepage_content = ""

        # Step 1 — collect what Crawl4AI got
        relevant = adaptive.get_relevant_content(top_k=CRAWL_CONFIG["max_pages"])

        for page in relevant:
            content = page.get("content", "") or page.get("markdown", "")
            url     = page.get("url", "")
            if not url or not content or len(content.strip()) < 200:
                continue
            pages.append({
                "url":          url,
                "content":      content,
                "type":         _classify_page(url),
                "source":       "crawl4ai",
                "wayback_date": None,
            })
            source_urls.append(url)
            if _is_homepage(url, website):
                homepage_content = content

        # Step 2 — parse homepage links, fetch priority ones
        if homepage_content:
            already_fetched = {p["url"].rstrip("/") for p in pages}
            priority_links  = _extract_priority_links(
                homepage_content, website, already_fetched
            )

            for url in priority_links:
                time.sleep(DELAY_NORMAL)
                print(f"    [crawler] Fetching: {url}")
                content = _fetch_plain(url)

                if content and len(content.strip()) >= 200:
                    pages.append({
                        "url":          url,
                        "content":      content,
                        "type":         _classify_page(url),
                        "source":       "live",
                        "wayback_date": None,
                    })
                    source_urls.append(url)
                    print(f"    [crawler] Captured: {url}")
                else:
                    print(f"    [crawler] Skipped (no content): {url}")

        if not pages:
            return _failure("no_content_extracted")

        # Step 3 — export KB
        kb_path = KB_DIR / f"{slug}.jsonl"
        KB_DIR.mkdir(exist_ok=True)
        with open(kb_path, "w", encoding="utf-8") as f:
            for p in pages:
                f.write(json.dumps(p) + "\n")
        print(f"    Exported {len(pages)} documents to {kb_path}")

        _save_state(slug, {
            "crawled_urls": list(state.crawled_urls),
            "website":      website,
            "confidence":   adaptive.confidence,
        })

        sources = {}
        for p in pages:
            sources[p["source"]] = sources.get(p["source"], 0) + 1
        source_summary = " | ".join(f"{k}:{v}" for k, v in sources.items())

        print(f"    [crawler] OK — {len(pages)} pages | confidence={adaptive.confidence:.0%} | {source_summary}")
        return {
            "success":        True,
            "pages":          pages,
            "failure_reason": None,
            "source_urls":    source_urls,
        }

    except ImportError:
        print("  ERROR: crawl4ai not installed. Run: pip install crawl4ai")
        return _failure("crawl4ai_not_installed")
    except Exception as e:
        print(f"  ERROR: Crawl failed: {e}")
        return _failure(str(e))


# ---------------------------------------------------------------------------
# Homepage link extractor
# ---------------------------------------------------------------------------

def _extract_priority_links(
    homepage_content: str,
    website: str,
    already_fetched: set,
) -> list[str]:
    """
    Parse all internal links from homepage markdown.
    Return only priority ones we haven't fetched yet.
    """
    base_domain = _get_domain(website)

    # Extract markdown links [text](url)
    all_links = re.findall(r'\[([^\]]*)\]\((https?://[^\s\)\"]+)', homepage_content)

    priority = []
    seen     = set()

    for link_text, url in all_links:
        # Normalize — strip query params, anchors, trailing slash
        url_clean = url.rstrip("/").split("?")[0].split("#")[0]

        if url_clean in already_fetched or url_clean in seen:
            continue

        # Must be same domain
        if base_domain not in url_clean:
            continue

        url_lower = url_clean.lower()

        # Skip noise
        if any(skip in url_lower for skip in SKIP_KEYWORDS):
            continue

        # Extract path
        path = url_lower
        for prefix in [f"https://www.{base_domain}", f"https://{base_domain}"]:
            path = path.replace(prefix, "")

        # Only priority paths
        if any(kw in path for kw in PRIORITY_KEYWORDS):
            priority.append(url_clean)
            seen.add(url_clean)

    print(f"    [crawler] Found {len(priority)} priority links in homepage")
    for url in priority:
        print(f"      → {url}")
    return priority


def _get_domain(website: str) -> str:
    domain = website.replace("https://", "").replace("http://", "")
    domain = domain.replace("www.", "").rstrip("/")
    return domain.split("/")[0]


def _is_homepage(url: str, website: str) -> bool:
    clean_url     = url.rstrip("/")
    clean_website = website.rstrip("/")
    domain        = _get_domain(website)
    return clean_url in [
        clean_website,
        clean_website.replace("www.", ""),
        f"https://www.{domain}",
        f"https://{domain}",
    ]


# ---------------------------------------------------------------------------
# Plain fetch
# ---------------------------------------------------------------------------

def _fetch_plain(url: str) -> str:
    try:
        from markdownify import markdownify as md
        response = requests.get(url, headers=BROWSER_HEADERS, timeout=10)
        if response.status_code == 200:
            return md(response.text, heading_style="ATX", strip=["script", "style", "head"])
        print(f"    [crawler] HTTP {response.status_code}: {url}")
        return ""
    except Exception as e:
        print(f"    [crawler] Fetch error ({e}): {url}")
        return ""


# ---------------------------------------------------------------------------
# Mock crawl
# ---------------------------------------------------------------------------

MOCK_CRITERIA_TEMPLATES = [
    """
    Investment Criteria | {name}
    - Revenue: $10 million to $75 million
    - EBITDA: $2 million to $10 million
    - Deal Types: Buyout, Recapitalization, Management Buyout
    - Geography: United States, with Midwest focus
    - Sectors: Industrials, Business Services, Healthcare Services
    """,
    """
    Our Strategy | {name}
    Lower middle market PE firm. Typical investment $5M-$30M equity.
    EBITDA $3M to $15M. Manufacturing, distribution, healthcare services.
    """,
    """
    {name} | Private Equity
    We invest in middle market companies across North America.
    """,
]

def _mock_crawl(website: str, slug: str, firm_name: str) -> dict:
    time.sleep(random.uniform(0.2, 0.6))
    roll = random.random()
    if roll < 0.10:
        reason = random.choice(["timeout", "404_not_found", "js_wall"])
        print(f"    [crawler] FAILED — {reason}")
        return _failure(reason)
    template = MOCK_CRITERIA_TEMPLATES[0] if roll < 0.70 else (
               MOCK_CRITERIA_TEMPLATES[1] if roll < 0.90 else
               MOCK_CRITERIA_TEMPLATES[2])
    content  = template.format(name=firm_name)
    pages    = [
        {"url": website,                          "content": content, "type": "homepage", "source": "live", "wayback_date": None},
        {"url": website + "/investment-criteria", "content": content, "type": "criteria", "source": "live", "wayback_date": None},
    ]
    source_urls = [p["url"] for p in pages]
    print(f"    [crawler] MOCK OK — {len(pages)} pages")
    _save_state(slug, {"pages": pages, "website": website})
    _save_kb(slug, pages)
    return {"success": True, "pages": pages, "failure_reason": None, "source_urls": source_urls}


# ---------------------------------------------------------------------------
# Page classifier
# ---------------------------------------------------------------------------

def _classify_page(url: str) -> str:
    url_lower = url.lower()
    if any(p in url_lower for p in [
        "/criteria", "/investment-criteria", "/target-sectors-investment-criteria"
    ]):
        return "criteria"
    if any(p in url_lower for p in [
        "/strategy", "/approach", "/our-approach", "/focus",
        "/investment-strategy", "/our-thesis", "/thesis",
        "/principles", "/mission-statement-values"
    ]):
        return "strategy"
    if any(p in url_lower for p in [
        "/portfolio", "/investments", "/our-companies",
        "/our-companies-current", "/current-investments", "/real-assets"
    ]):
        return "portfolio"
    if any(p in url_lower for p in [
        "/about", "/our-firm", "/who-we-are", "/firm"
    ]):
        return "about"
    if url_lower.endswith(".pdf"):
        return "pdf"
    return "homepage"

# ---------------------------------------------------------------------------
# State + KB persistence
# ---------------------------------------------------------------------------

def _save_state(slug: str, data: dict) -> None:
    CRAWL_STATE_DIR.mkdir(exist_ok=True)
    path = CRAWL_STATE_DIR / f"{slug}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({**data, "saved_at": datetime.now().isoformat()}, f, indent=2)

def _load_state(slug: str) -> dict | None:
    path = CRAWL_STATE_DIR / f"{slug}.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None

def _save_kb(slug: str, pages: list[dict]) -> None:
    KB_DIR.mkdir(exist_ok=True)
    path = KB_DIR / f"{slug}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for page in pages:
            f.write(json.dumps(page) + "\n")

def _failure(reason: str) -> dict:
    return {"success": False, "pages": [], "failure_reason": reason, "source_urls": []}


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== crawler.py test ===")
    print(f"Mode: {'REAL (Crawl4AI)' if CRAWL_ENABLED else 'MOCK'}\n")

    print("Test 1 — Tilia (blocked site, homepage link parser)")
    result = crawl_firm({
        "firm_slug": "tilia-holdings-llc",
        "firm_name": "Tilia Holdings, LLC",
        "website":   "https://www.tiliallc.com",
    })
    print(f"  Pages: {len(result['pages'])}")
    for p in result["pages"]:
        print(f"    {p['source']:10} | {p['type']:10} | {p['url']}")

    print("\nTest 2 — Borgman (open site, Crawl4AI gets everything)")
    result = crawl_firm({
        "firm_slug": "borgman-capital-llc",
        "firm_name": "Borgman Capital LLC",
        "website":   "https://www.borgmancapital.com",
    })
    print(f"  Pages: {len(result['pages'])}")
    for p in result["pages"]:
        print(f"    {p['source']:10} | {p['type']:10} | {p['url']}")

    print("\nTest 3 — no website")
    result = crawl_firm({"firm_slug": "x", "firm_name": "X", "website": None})
    print(f"  Success: {result['success']} | {result['failure_reason']}")