"""
crawler.py
----------
Handles all website crawling for the PE/VC research pipeline.
Uses Crawl4AI AdaptiveCrawler for real crawls.

Current mode: REAL (Crawl4AI 0.8.x)
To switch to mock: set CRAWL_ENABLED = False

Priority pages crawled per firm (in order):
  1. Homepage
  2. /investment-criteria or /our-criteria
  3. /strategy or /approach
  4. /portfolio (reverse-engineer criteria from deals)
  5. Any linked PDFs (fund fact sheets, brochures)

Called by pipeline.py:
    from crawler import crawl_firm
    result = crawl_firm(record)
"""

import time
import random
import json
import asyncio
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CRAWL_ENABLED       = True    # False = mock mode
CRAWL_STATE_DIR     = Path(__file__).parent.parent / "crawl_states"
KB_DIR              = Path(__file__).parent.parent / "kb"

# Crawl4AI AdaptiveConfig parameters
CRAWL_CONFIG = {
    "confidence_threshold": 0.75,
    "max_pages":            15,
    "top_k_links":          4,
    "save_state":           True,
}

# Priority page patterns to look for in nav/sitemap
PRIORITY_PATHS = [
    "/investment-criteria",
    "/our-criteria",
    "/criteria",
    "/strategy",
    "/approach",
    "/portfolio",
    "/investments",
    "/about",
]

CRAWL_QUERY = (
    "investment criteria EBITDA revenue minimum maximum "
    "sectors buyout portfolio deal size check"
)


# ---------------------------------------------------------------------------
# Public interface — called by pipeline.py
# ---------------------------------------------------------------------------

def crawl_firm(record: dict) -> dict:
    """
    Main entry point. Takes a firm record, returns crawl result dict.

    Returns:
        {
            "success":        bool,
            "pages":          [{"url": ..., "content": ..., "type": ...}],
            "failure_reason": str | None,
            "source_urls":    [str],
        }
    """
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
# Real crawl — Crawl4AI 0.8.x
# ---------------------------------------------------------------------------

def _real_crawl(website: str, slug: str, firm_name: str) -> dict:
    """Real Crawl4AI crawl using AdaptiveConfig + digest() API (v0.8.x)."""
    try:
        from crawl4ai import AsyncWebCrawler, AdaptiveCrawler, AdaptiveConfig  # type: ignore

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

        pages       = []
        source_urls = []

        # get_relevant_content returns top pages ranked by relevance score
        relevant = adaptive.get_relevant_content(top_k=CRAWL_CONFIG["max_pages"])
        for page in relevant:
            content = page.get("content", "") or page.get("markdown", "")
            url     = page.get("url", "")
            if content and url:
                pages.append({
                    "url":     url,
                    "content": content,
                    "type":    _classify_page(url),
                })
                source_urls.append(url)

        if not pages:
            return _failure("no_content_extracted")

        # Use Crawl4AI's built-in KB export
        kb_path = str(KB_DIR / f"{slug}.jsonl")
        adaptive.export_knowledge_base(kb_path)

        _save_state(slug, {
            "crawled_urls": list(state.crawled_urls),
            "website":      website,
            "confidence":   adaptive.confidence,
        })

        print(f"    [crawler] OK — {len(pages)} pages | confidence={adaptive.confidence:.0%}")
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
# Mock crawl — simulates realistic crawl results for testing
# ---------------------------------------------------------------------------

MOCK_CRITERIA_TEMPLATES = [
    # Template A — explicit criteria page
    """
    Investment Criteria | {name}

    We focus on acquiring established lower middle market businesses.

    Target Company Profile:
    - Revenue: $10 million to $75 million
    - EBITDA: $2 million to $10 million
    - Enterprise Value: $15 million to $100 million
    - Deal Types: Buyout, Recapitalization, Management Buyout
    - Geography: United States, with Midwest focus
    - Sectors: Industrials, Business Services, Healthcare Services
    - We do not invest in: startups, real estate, financial services
    """,

    # Template B — strategy page, less explicit
    """
    Our Strategy | {name}

    {name} is a private equity firm focused on the lower middle market.
    We partner with founders and family-owned businesses seeking liquidity
    or growth capital. Our typical investment is between $5M and $30M in
    companies with EBITDA of $3M to $15M.

    We are industry agnostic but have deep expertise in:
    - Manufacturing and distribution
    - Healthcare services
    - Technology-enabled business services

    Portfolio companies: Acme Manufacturing (2022), Beta Health (2021),
    Delta Services (2023 - exited 2025)
    """,

    # Template C — thin site, minimal info
    """
    {name} | Private Equity

    We are a private equity firm headquartered in Chicago, Illinois.
    We invest in middle market companies across North America.

    Contact us to learn more about partnership opportunities.
    """,
]

def _mock_crawl(website: str, slug: str, firm_name: str) -> dict:
    """Returns fake crawl content for pipeline testing."""
    time.sleep(random.uniform(0.2, 0.6))

    roll = random.random()

    if roll < 0.10:
        reason = random.choice(["timeout", "404_not_found", "js_wall", "connection_refused"])
        print(f"    [crawler] FAILED — {reason}")
        return _failure(reason)

    if roll < 0.70:
        template = MOCK_CRITERIA_TEMPLATES[0]
    elif roll < 0.90:
        template = MOCK_CRITERIA_TEMPLATES[1]
    else:
        template = MOCK_CRITERIA_TEMPLATES[2]

    content = template.format(name=firm_name, slug=slug)

    pages = [
        {"url": website,                          "content": content, "type": "homepage"},
        {"url": website + "/investment-criteria", "content": content, "type": "criteria"},
    ]

    if random.random() > 0.5:
        pages.append({
            "url":     website + "/portfolio",
            "content": f"Portfolio | {firm_name}\n\nCurrent investments:\n- Company A (Healthcare, 2023)\n- Company B (Industrials, 2022)",
            "type":    "portfolio",
        })

    source_urls = [p["url"] for p in pages]
    print(f"    [crawler] OK — {len(pages)} pages, {sum(len(p['content']) for p in pages)} chars")

    _save_state(slug, {"pages": pages, "website": website})
    _save_kb(slug, pages)

    return {
        "success":        True,
        "pages":          pages,
        "failure_reason": None,
        "source_urls":    source_urls,
    }


# ---------------------------------------------------------------------------
# Page classifier
# ---------------------------------------------------------------------------

def _classify_page(url: str) -> str:
    url_lower = url.lower()
    if any(p in url_lower for p in ["/criteria", "/investment-criteria"]):
        return "criteria"
    if any(p in url_lower for p in ["/portfolio", "/investments", "/companies"]):
        return "portfolio"
    if any(p in url_lower for p in ["/strategy", "/approach", "/focus"]):
        return "strategy"
    if url_lower.endswith(".pdf"):
        return "pdf"
    return "homepage"


# ---------------------------------------------------------------------------
# State persistence
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


# ---------------------------------------------------------------------------
# Failure helper
# ---------------------------------------------------------------------------

def _failure(reason: str) -> dict:
    return {
        "success":        False,
        "pages":          [],
        "failure_reason": reason,
        "source_urls":    [],
    }


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== crawler.py test ===")
    print(f"Mode: {'REAL (Crawl4AI)' if CRAWL_ENABLED else 'MOCK'}\n")

    test_record = {
        "firm_slug": "borgman-capital-llc",
        "firm_name": "Borgman Capital LLC",
        "website":   "https://www.borgmancapital.com",
    }

    print("Test 1 — crawl Borgman Capital")
    result = crawl_firm(test_record)
    print(f"  Success:  {result['success']}")
    print(f"  Pages:    {len(result['pages'])}")
    print(f"  Sources:  {result['source_urls']}")
    if result["pages"]:
        print(f"\n  First page preview ({result['pages'][0]['type']}):")
        print("  " + result["pages"][0]["content"][:300].strip().replace("\n", "\n  "))

    print("\nTest 2 — no website")
    result = crawl_firm({"firm_slug": "no-site", "firm_name": "No Site LLC", "website": None})
    print(f"  Success: {result['success']} | Reason: {result['failure_reason']}")

    print("\nTest 3 — check files written")
    state_file = CRAWL_STATE_DIR / "borgman-capital-llc.json"
    kb_file    = KB_DIR / "borgman-capital-llc.jsonl"
    print(f"  crawl_states/borgman-capital-llc.json: {'✓' if state_file.exists() else '✗'}")
    print(f"  kb/borgman-capital-llc.jsonl:          {'✓' if kb_file.exists() else '✗'}")