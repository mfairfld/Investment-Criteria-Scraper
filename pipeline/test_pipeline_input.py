"""
test_pipeline_input.py

Shows exactly what the AI extractor would receive for a given firm.
Run this before writing extractor.py to validate the data pipeline.

Usage:
    python pipeline/test_pipeline_input.py --slug borgman-capital-llc
    python pipeline/test_pipeline_input.py --slug borgman-capital-llc --all-pages
    python pipeline/test_pipeline_input.py --slug borgman-capital-llc --send-to-ai
"""

import json
import re
import argparse
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────────────────────────

KB_DIR = Path("kb")
MASTER_FILE = Path("data/master.json")

# Page priority order — AI reads these first, stops when confident
PAGE_PRIORITY = [
    "investment-criteria",
    "criteria",
    "our-criteria",
    "strategy",
    "approach",
    "portfolio",
    "our-companies",
    "companies",
]

# ─── CLEANER ─────────────────────────────────────────────────────────────────

SKIP_PATTERNS = [
    r"Skip to Content",
    r"Open Menu",
    r"Close Menu",
    r"\[ !\[",           # image links like [ ![alt](img) ](url)
    r"^!\[",            # standalone images
    r"squarespace-cdn",
    r"cdn\.",
    r"INVEST WITH US",
    r"Sign me up",
    r"Health Plan",
    r"ESG Policy",
    r"Privacy Policy",
    r"©20\d\d",
    r"^\* \* \*",
    r"^­$",             # soft hyphens
    r"Folder:",
    r"\[ Back \]",
    r"javascript:void",
    r"\.png\?format=",
    r"\.jpg\?format=",
    r"\.jpeg\?format=",
]


def clean_page(raw: str) -> str:
    """Strip nav, images, footers, duplicate links. Keep actual content."""
    lines = raw.split("\n")
    cleaned = []
    seen_links = set()

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Drop noise lines
        if any(re.search(p, line) for p in SKIP_PATTERNS):
            continue

        # Deduplicate nav links (same URL text appears 2-3x per page due to mobile/desktop nav)
        link_match = re.match(r"^\[([^\]]+)\]\(https?://[^\)]+\)$", line)
        if link_match:
            link_text = link_match.group(1).strip().lower()
            if link_text in seen_links:
                continue
            seen_links.add(link_text)

        cleaned.append(line)

    return "\n".join(cleaned)


# ─── PAGE PRIORITIZER ─────────────────────────────────────────────────────────

def prioritize_pages(pages: list[dict]) -> list[dict]:
    """Sort pages so criteria/strategy pages come first."""
    def priority_score(page):
        url = page.get("url", "").lower()
        for i, keyword in enumerate(PAGE_PRIORITY):
            if keyword in url:
                return i
        return len(PAGE_PRIORITY)  # homepage and others go last

    return sorted(pages, key=priority_score)


# ─── PROMPT BUILDER ──────────────────────────────────────────────────────────

def build_extraction_prompt(firm_name: str, pages: list[dict], capiq_description: str = None) -> str:
    """Build the full prompt that would be sent to the AI extractor."""

    # Build page content block
    page_blocks = []
    for page in pages:
        url = page["url"]
        content = page["cleaned_content"]
        char_count = len(content)
        page_blocks.append(f"### PAGE: {url} ({char_count} chars)\n\n{content}")

    pages_text = "\n\n---\n\n".join(page_blocks)

    # CapIQ fallback block
    capiq_block = ""
    if capiq_description:
        capiq_block = f"""
## CAPIQ DESCRIPTION (fallback source)
{capiq_description}
"""

    prompt = f"""You are extracting investment criteria for a PE/VC database.

Firm: {firm_name}

Extract all fields you can confirm from the content below.
Set unconfirmed fields to null. Never guess or fabricate data.

Return ONLY a valid JSON object matching this schema. No explanation, no markdown fences.

SCHEMA:
{{
  "firm_name": "{firm_name}",
  "website": null,
  "firm_type": null,
  "hq_country": null,
  "hq_state": null,
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
- revenue_min, revenue_max, ebitda_min, ebitda_max, enterprise_value_min, enterprise_value_max,
  check_size_min, check_size_max, aum_usd_millions → USD millions as numbers (e.g. 10.0 not "$10M")
- firm_type → one of: PE | VC | Family Office | Search Fund | Fundless Sponsor | Mezzanine | Other
- deal_types → array from: Buyout, Growth Equity, Minority, Recap, Distressed, Venture, Other
- sector_tier1 → max 3, from: Technology | Healthcare | Industrials | Financial Services | Consumer |
  Real Estate | Energy | Business Services | Media | Infrastructure | Generalist
- sector_keywords → lowercase, no spaces (e.g. "hvac", "saas", "foodprocessing")
- us_investments → "true", "primarily", or "false"
- confidence → "High" if criteria explicit on site | "Medium" if inferred | "Low" if minimal data
- activity_tier → "Active" | "Slowing" | "Dormant"
- notes → brief summary of what was found and any data gaps

SOURCE PAGES:
{pages_text}
{capiq_block}
"""
    return prompt


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def run_test(slug: str, show_all_pages: bool = False, send_to_ai: bool = False):
    kb_file = KB_DIR / f"{slug}.jsonl"

    if not kb_file.exists():
        print(f"❌ KB file not found: {kb_file}")
        print(f"   Run the crawler first: python pipeline/pipeline.py --slug {slug}")
        return

    # Load master record for this firm
    firm_record = {}
    if MASTER_FILE.exists():
        with open(MASTER_FILE) as f:
            master = json.load(f)
            if isinstance(master, list):
                firm_record = next((f for f in master if f.get("firm_slug") == slug), {})
            else:
                firm_record = master.get(slug, {})

    firm_name = firm_record.get("firm_name", slug)
    capiq_description = firm_record.get("capiq_description")

    # Load all pages from KB
    pages = []
    with open(kb_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            raw_content = record.get("content", "")
            cleaned = clean_page(raw_content)
            pages.append({
                "url": record.get("url", "unknown"),
                "raw_content": raw_content,
                "cleaned_content": cleaned,
                "raw_chars": len(raw_content),
                "clean_chars": len(cleaned),
            })

    # Sort by priority
    pages = prioritize_pages(pages)

    # ── REPORT ──
    print("=" * 70)
    print(f"PIPELINE INPUT REPORT — {firm_name}")
    print("=" * 70)
    print(f"Slug:       {slug}")
    print(f"KB file:    {kb_file}")
    print(f"Pages found: {len(pages)}")
    print()

    total_raw = sum(p["raw_chars"] for p in pages)
    total_clean = sum(p["clean_chars"] for p in pages)
    reduction = (1 - total_clean / total_raw) * 100 if total_raw else 0

    print("PAGE SUMMARY (priority order):")
    print(f"{'#':<3} {'URL':<55} {'Raw':>6} {'Clean':>6} {'Cut':>5}")
    print("-" * 80)
    for i, page in enumerate(pages):
        url_short = page["url"].replace("https://www.", "").replace("https://", "")
        cut = (1 - page["clean_chars"] / page["raw_chars"]) * 100 if page["raw_chars"] else 0
        print(f"{i+1:<3} {url_short:<55} {page['raw_chars']:>6,} {page['clean_chars']:>6,} {cut:>4.0f}%")

    print("-" * 80)
    print(f"{'TOTAL':<58} {total_raw:>6,} {total_clean:>6,} {reduction:>4.0f}%")
    print(f"\nToken estimate (raw):   ~{total_raw // 4:,}")
    print(f"Token estimate (clean): ~{total_clean // 4:,}")
    print(f"Token savings:          ~{(total_raw - total_clean) // 4:,}")

    # ── SHOW CLEANED CONTENT ──
    print()
    if show_all_pages:
        pages_to_show = pages
    else:
        # Default: show only the top-priority page
        pages_to_show = pages[:1]
        print(f"(Showing top-priority page only. Use --all-pages to see all.)")

    for page in pages_to_show:
        url_short = page["url"].replace("https://www.", "")
        print()
        print(f"{'─' * 70}")
        print(f"CLEANED CONTENT → {url_short}")
        print(f"{'─' * 70}")
        print(page["cleaned_content"])

    # ── BUILD AND SHOW PROMPT ──
    print()
    print("=" * 70)
    print("EXTRACTION PROMPT (what AI would receive)")
    print("=" * 70)

    # For prompt, use top 3 pages by priority
    prompt_pages = pages[:3]
    prompt = build_extraction_prompt(firm_name, prompt_pages, capiq_description)
    prompt_tokens = len(prompt) // 4

    print(f"Prompt length: {len(prompt):,} chars (~{prompt_tokens:,} tokens)")
    print()
    print(prompt[:3000] + f"\n\n... [truncated for display — full prompt is {len(prompt):,} chars] ..." if len(prompt) > 3000 else prompt)

    # ── OPTIONALLY CALL AI ──
    if send_to_ai:
        print()
        print("=" * 70)
        print("SENDING TO AI EXTRACTOR...")
        print("=" * 70)
        try:
            import anthropic
            client = anthropic.Anthropic()
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}]
            )
            raw_response = response.content[0].text
            print("RAW AI RESPONSE:")
            print(raw_response)
            print()
            try:
                extracted = json.loads(raw_response)
                print("✅ PARSED JSON:")
                print(json.dumps(extracted, indent=2))
            except json.JSONDecodeError as e:
                print(f"⚠️  JSON parse failed: {e}")
                print("Raw response above — may need prompt adjustment")
        except ImportError:
            print("❌ anthropic package not installed: pip install anthropic")
        except Exception as e:
            print(f"❌ API call failed: {e}")


# ─── ENTRY POINT ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test the extraction pipeline input for a firm")
    parser.add_argument("--slug", required=True, help="Firm slug, e.g. borgman-capital-llc")
    parser.add_argument("--all-pages", action="store_true", help="Show cleaned content for all pages")
    parser.add_argument("--send-to-ai", action="store_true", help="Actually call the AI and show extracted JSON")
    args = parser.parse_args()

    run_test(
        slug=args.slug,
        show_all_pages=args.all_pages,
        send_to_ai=args.send_to_ai,
    )