"""
extractor.py
------------
Takes raw crawl content and extracts structured investment criteria
into the master.json schema.

Current mode: REAL — calls Claude API for extraction.
Requires ANTHROPIC_API_KEY set in .env file.

Flow:
  1. Receive pages from crawler.py
  2. Prioritize pages (criteria > strategy > portfolio > homepage)
  3. Clean content (strip nav, images, duplicate links)
  4. Build extraction prompt
  5. Call Claude API → get structured JSON back
  6. Merge into existing record (never overwrite confirmed fields)
  7. Set confidence level and source tags

Called by pipeline.py:
    from extractor import extract_firm
    updated_record = extract_firm(record, crawl_result)

NOTES:
  - Re-crawl logic: firms marked "stale" (last_checked_date > 90 days) re-enter
    the queue and are fully re-crawled. Criteria changes are caught on refresh.
  - Field-level provenance: currently tracked at record level via source_urls
    and last_checked_date. Future: per-field source + confirmed_date tracking
    (e.g. revenue_min_source, revenue_min_confirmed_date) — Phase 3 schema update.
"""

import os
import json
import re
import time
import random
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

EXTRACT_ENABLED   = True
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", None)
MODEL             = "claude-haiku-4-5"

# Page priority order for cleaning + prompt building
PAGE_PRIORITY = [
    "investment-criteria", "criteria", "our-criteria",
    "strategy", "approach", "portfolio", "our-companies", "companies",
]

# Max chars sent to AI — criteria page first, truncate rest
MAX_PROMPT_CHARS = 12000

# ---------------------------------------------------------------------------
# Content cleaner
# ---------------------------------------------------------------------------

SKIP_PATTERNS = [
    r"Skip to Content", r"Open Menu", r"Close Menu",
    r"\[ !\[", r"^!\[", r"squarespace-cdn", r"cdn\.",
    r"INVEST WITH US", r"Sign me up", r"Health Plan",
    r"ESG Policy", r"Privacy Policy", r"©20\d\d",
    r"^\* \* \*", r"^­$", r"Folder:", r"\[ Back \]",
    r"javascript:void", r"\.png\?format=", r"\.jpg\?format=",
    r"\.jpeg\?format=", r"\.webp\?",
]

def _clean_page(raw: str) -> str:
    """Strip nav, images, footers, duplicate links. Keep actual content."""
    lines = raw.split("\n")
    cleaned = []
    seen_links = set()

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if any(re.search(p, line) for p in SKIP_PATTERNS):
            continue
        # Deduplicate nav links
        link_match = re.match(r"^\[([^\]]+)\]\(https?://[^\)]+\)$", line)
        if link_match:
            link_text = link_match.group(1).strip().lower()
            if link_text in seen_links:
                continue
            seen_links.add(link_text)
        cleaned.append(line)

    return "\n".join(cleaned)


def _fix_encoding(text: str) -> str:
    """Fix UTF-8 mojibake from crawler output (e.g. â€™ → ')"""
    try:
        return text.encode("latin-1").decode("utf-8")
    except Exception:
        return text


# ---------------------------------------------------------------------------
# Page prioritization
# ---------------------------------------------------------------------------

# Pages to skip in extraction prompt — no investment criteria here
EXTRACTION_SKIP = [
    "/news", "/news-insights", "/press", "/press-releases", "/media", "/blog",
    "/team", "/our-team", "/meet-the-team", "/people", "/staff",
    "/careers", "/jobs",
    "/sustainability", "/esg", "/community",
    "/talent-programs", "/ceo-in-residence-program", "/propel",
]

def _prioritize_pages(pages: list[dict]) -> list[dict]:
    """Filter noise pages then sort by information density."""
    type_map = {
        "criteria": 0,
        "strategy": 1,
        "portfolio": 2,
        "pdf":       3,
        "about":     4,
        "homepage":  5,
    }

    filtered = [
        p for p in pages
        if not any(skip in p.get("url", "").lower() for skip in EXTRACTION_SKIP)
    ]

    return sorted(filtered, key=lambda p: type_map.get(p.get("type", "homepage"), 99))


def _combine_text(pages: list[dict]) -> str:
    """Clean and combine pages into a single string, respecting token budget."""
    parts = []
    total_chars = 0

    for page in pages:
        raw = page.get("content", "").strip()
        raw = _fix_encoding(raw)
        cleaned = _clean_page(raw)
        url = page.get("url", "")

        block = f"### PAGE: {url}\n\n{cleaned}"

        if total_chars + len(block) > MAX_PROMPT_CHARS:
            remaining = MAX_PROMPT_CHARS - total_chars
            if remaining > 500:  # Only add if meaningful content fits
                parts.append(block[:remaining] + "\n[truncated]")
            break

        parts.append(block)
        total_chars += len(block)

    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Public interface — called by pipeline.py
# ---------------------------------------------------------------------------

def extract_firm(record: dict, crawl_result: dict) -> dict:
    """
    Main entry point. Takes existing record + crawl result.
    Returns updated record with extracted fields merged in.

    Never overwrites a field that already has a confirmed value.
    Always preserves capiq_* shadow fields.
    """
    firm_name = record.get("firm_name", "Unknown")
    print(f"  [extractor] {firm_name}")

    if not crawl_result.get("success"):
        print(f"    [extractor] Skipping — crawl failed")
        return _mark_failed(record, crawl_result.get("failure_reason", "unknown"))

    pages = crawl_result.get("pages", [])
    if not pages:
        return _mark_failed(record, "no_pages")

    # Prioritize and combine
    ordered = _prioritize_pages(pages)
    combined_text = _combine_text(ordered)

    if EXTRACT_ENABLED:
        extracted = _real_extract(combined_text, record)
    else:
        extracted = _mock_extract(combined_text, record)

    if not extracted:
        return _mark_failed(record, "extraction_returned_null")

    updated = _merge(record, extracted, crawl_result.get("source_urls", []))
    return updated


# ---------------------------------------------------------------------------
# Real extraction — Claude API
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """You are extracting investment criteria for a PE/VC database.

Firm: {firm_name}

Extract all fields you can confirm from the content below.
Set unconfirmed fields to null. Never guess or fabricate data.

Return ONLY a valid JSON object. No explanation, no markdown fences.

SCHEMA:
{{
  "firm_type": null,
  "hq_city": null,
  "hq_state": null,
  "hq_country": null,
  "us_investments": null,
  "revenue_min": null,
  "revenue_max": null,
  "ebitda_min": null,
  "ebitda_max": null,
  "enterprise_value_min": null,
  "enterprise_value_max": null,
  "check_size_min": null,
  "check_size_max": null,
  "deal_types": [],
  "sector_tier1": [],
  "sector_tier2": [],
  "sector_tier3": [],
  "sector_keywords": [],
  "sector_exclusions": [],
  "last_known_deal_date": null,
  "fund_name": null,
  "fund_vintage_year": null,
  "fund_number": null,
  "aum_usd_millions": null,
  "activity_tier": null,
  "confidence": null,
  "needs_review": false,
  "notes": ""
}}

RULES:
- All dollar figures → USD millions as numbers (e.g. 10.0 not "$10M")
- firm_type → one of: PE | VC | Family Office | Search Fund | Fundless Sponsor | Mezzanine | Other
- deal_types → array from: Buyout, Growth Equity, Minority, Recap, Distressed, Venture, Other
  - Buyout = control acquisition, typically leveraged
  - Growth Equity = minority stake, no control, growth stage company
  - Only use Growth Equity if the firm explicitly states minority/non-control growth investments
  - "buy-and-build", "growth-oriented", "growth capital" language alone does NOT mean Growth Equity
  - A firm doing control buyouts of growing companies is still Buyout, not Growth Equity
- sector_tier1 → max 3, from: Technology | Healthcare | Industrials | Financial Services | Consumer |
  Real Estate | Energy | Business Services | Media | Infrastructure | Generalist
- sector_keywords → lowercase, no spaces (e.g. "hvac", "saas", "foodprocessing")
- us_investments → "true", "primarily", or "false"
- confidence → "High" if criteria explicit on site | "Medium" if inferred | "Low" if minimal data
- activity_tier → "Active" | "Slowing" | "Dormant"
- needs_review → true if data is contradictory, ambiguous, or confidence is Low
- notes → brief summary of what was found and any data gaps
- If you see a link to a PDF that appears to contain investment criteria or fund information,
  note the full URL in notes like: "criteria PDF found: [url] — not parsed"

SOURCE PAGES:
{text}
"""


def _real_extract(text: str, record: dict) -> dict | None:
    """Call Claude API and return extracted JSON."""
    try:
        import anthropic

        if not ANTHROPIC_API_KEY:
            print("    [extractor] ERROR: ANTHROPIC_API_KEY not set")
            return None

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        prompt = EXTRACTION_PROMPT.format(
            firm_name=record.get("firm_name", "Unknown"),
            text=text,
        )

        message = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = message.content[0].text.strip()

        # Strip markdown fences if model adds them anyway
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        extracted = json.loads(raw)
        print(f"    [extractor] confidence={extracted.get('confidence')} ✓")
        return extracted

    except ImportError:
        print("    [extractor] ERROR: pip install anthropic")
        return None
    except json.JSONDecodeError as e:
        print(f"    [extractor] ERROR: JSON parse failed: {e}")
        return None
    except Exception as e:
        print(f"    [extractor] ERROR: {e}")
        return None


# ---------------------------------------------------------------------------
# Mock extraction (fallback for testing without API)
# ---------------------------------------------------------------------------

MOCK_EXTRACTIONS = [
    {
        "firm_type": "PE", "hq_city": "Chicago", "hq_state": "IL",
        "us_investments": "primarily", "revenue_min": 10.0, "revenue_max": 75.0,
        "ebitda_min": 2.0, "ebitda_max": 10.0, "enterprise_value_min": 15.0,
        "enterprise_value_max": 100.0, "check_size_min": 5.0, "check_size_max": 30.0,
        "deal_types": ["Buyout", "Recap"],
        "sector_tier1": ["Industrials", "Business Services", "Healthcare"],
        "sector_tier2": ["Manufacturing", "Distribution"],
        "sector_tier3": ["HVAC", "Food Processing"],
        "sector_keywords": ["manufacturing", "distribution", "hvac"],
        "sector_exclusions": ["Real Estate", "Startups"],
        "confidence": "High", "notes": "Mock: explicit criteria found.",
    },
    {
        "firm_type": "PE", "hq_city": "New York", "hq_state": "NY",
        "us_investments": "primarily", "revenue_min": None, "revenue_max": None,
        "ebitda_min": 3.0, "ebitda_max": 15.0, "enterprise_value_min": None,
        "enterprise_value_max": None, "check_size_min": 5.0, "check_size_max": None,
        "deal_types": ["Buyout", "Growth Equity"],
        "sector_tier1": ["Healthcare", "Technology"],
        "sector_tier2": ["Healthcare Services", "Software"],
        "sector_tier3": [], "sector_keywords": ["healthcare", "saas"],
        "sector_exclusions": [], "confidence": "Medium",
        "notes": "Mock: inferred from strategy page.",
    },
    {
        "firm_type": "PE", "hq_city": None, "hq_state": None,
        "us_investments": "primarily", "revenue_min": None, "revenue_max": None,
        "ebitda_min": None, "ebitda_max": None, "enterprise_value_min": None,
        "enterprise_value_max": None, "check_size_min": None, "check_size_max": None,
        "deal_types": [], "sector_tier1": [], "sector_tier2": [], "sector_tier3": [],
        "sector_keywords": [], "sector_exclusions": [], "confidence": "Low",
        "notes": "Mock: thin website, minimal criteria found.",
    },
]

def _mock_extract(text: str, record: dict) -> dict:
    time.sleep(random.uniform(0.1, 0.3))
    roll = random.random()
    if roll < 0.60:
        result = MOCK_EXTRACTIONS[0].copy()
    elif roll < 0.85:
        result = MOCK_EXTRACTIONS[1].copy()
    else:
        result = MOCK_EXTRACTIONS[2].copy()
    print(f"    [extractor] MOCK confidence={result.get('confidence')}")
    return result

def _calculate_confidence(record: dict) -> str:
    """
    Calculate confidence based on how many key fields are populated.
    Objective — not dependent on AI self-assessment or content quality.
    
    High   → 3+ core criteria fields populated
    Medium → 1-2 core criteria fields populated  
    Low    → no core criteria fields populated
    """
    core_fields = [
        "ebitda_min", "ebitda_max",
        "revenue_min", "revenue_max",
        "check_size_min", "check_size_max",
        "enterprise_value_min", "enterprise_value_max",
    ]
    sector_fields = [
        "sector_tier1", "sector_tier2",
        "sector_keywords",
    ]
    deal_fields = ["deal_types"]

    core_score   = sum(1 for f in core_fields if record.get(f))
    sector_score = sum(1 for f in sector_fields if record.get(f))
    deal_score   = sum(1 for f in deal_fields if record.get(f))

    total = core_score + sector_score + deal_score

    if core_score >= 2:             return "High"
    if core_score >= 1:             return "Medium"
    if sector_score + deal_score >= 2: return "Medium"
    return "Low"

# ---------------------------------------------------------------------------
# Merge extracted fields into existing record
# ---------------------------------------------------------------------------

def _merge(record: dict, extracted: dict, source_urls: list[str]) -> dict:
    """
    Merges extracted data into the existing record.
    - Extracted value wins over null
    - Extracted value wins over CapIQ seed (but capiq_ shadow preserved)
    - Contradiction with CapIQ seed → flag needs_review
    - Null extracted values never overwrite existing confirmed values
    """
    updated = record.copy()
    today = date.today().isoformat()

    scalar_fields = [
        "firm_type", "hq_city", "hq_state", "hq_country", "us_investments",
        "revenue_min", "revenue_max", "ebitda_min", "ebitda_max",
        "enterprise_value_min", "enterprise_value_max",
        "check_size_min", "check_size_max",
        "last_known_deal_date", "fund_name", "fund_vintage_year",
        "fund_number", "aum_usd_millions",
    ]

    list_fields = [
        "deal_types", "sector_tier1", "sector_tier2",
        "sector_tier3", "sector_keywords", "sector_exclusions",
    ]

    needs_review = record.get("needs_review", False)
    notes_parts = [record.get("notes", "")]

    for field in scalar_fields:
        new_val = extracted.get(field)
        if new_val is None:
            continue
        # Flag contradictions with CapIQ seed
        capiq_val = record.get(f"capiq_{field}")
        if capiq_val is not None and new_val != capiq_val:
            needs_review = True
            notes_parts.append(f"CONFLICT: {field} website={new_val} vs capiq={capiq_val}")
        updated[field] = new_val

    for field in list_fields:
        new_val = extracted.get(field)
        if new_val:
            updated[field] = new_val

    updated["confidence"] = _calculate_confidence(updated)
    updated["needs_review"]      = needs_review or (extracted.get("confidence") == "Low")
    updated["last_checked_date"] = today
    updated["source_urls"]       = list(set(record.get("source_urls", []) + source_urls))

    extractor_note = extracted.get("notes", "")
    if extractor_note:
        notes_parts.append(f"[extractor] {extractor_note}")
    updated["notes"] = " | ".join(filter(None, notes_parts))
    updated["status"] = "complete"

    return updated


# ---------------------------------------------------------------------------
# Mark failed record
# ---------------------------------------------------------------------------

def _mark_failed(record: dict, reason: str) -> dict:
    updated = record.copy()
    updated["status"]            = "needs_review"
    updated["confidence"]        = "Low"
    updated["needs_review"]      = True
    updated["last_checked_date"] = date.today().isoformat()
    updated["notes"]             = (
        f"{record.get('notes', '')} | [extractor] FAILED: {reason}"
    ).strip(" |")
    print(f"    [extractor] marked needs_review — {reason}")
    return updated


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== extractor.py test ===")
    print(f"Mode: {'REAL (Claude API)' if EXTRACT_ENABLED else 'MOCK'}\n")

    test_record = {
        "firm_slug":   "borgman-capital-llc",
        "firm_name":   "Borgman Capital LLC",
        "website":     "https://www.borgmancapital.com",
        "ebitda_min":  None,
        "ebitda_max":  None,
        "sector_tier1": [],
        "needs_review": False,
        "notes":       "Seeded from CapIQ.",
        "source_urls": [],
        "status":      "in_progress",
    }

    test_crawl = {
        "success": True,
        "pages": [{
            "url":     "https://www.borgmancapital.com/investment-criteria",
            "content": "Revenue: $10M-$100M. EBITDA: $2M-$15M. Sectors: Industrials, Business Services, Consumer. Midwest focus. Buyout and recapitalization.",
            "type":    "criteria",
        }],
        "failure_reason": None,
        "source_urls": ["https://www.borgmancapital.com/investment-criteria"],
    }

    print("Test 1 — successful extraction")
    updated = extract_firm(test_record, test_crawl)
    print(f"  Status:      {updated['status']}")
    print(f"  Confidence:  {updated['confidence']}")
    print(f"  EBITDA:      {updated.get('ebitda_min')} - {updated.get('ebitda_max')}")
    print(f"  Sectors:     {updated.get('sector_tier1')}")
    print(f"  Notes:       {updated.get('notes', '')[:100]}")

    print("\nTest 2 — failed crawl")
    failed = {"success": False, "pages": [], "failure_reason": "timeout", "source_urls": []}
    updated = extract_firm(test_record, failed)
    print(f"  Status:      {updated['status']}")
    print(f"  needs_review: {updated['needs_review']}")