"""
preprocess_capiq.py
-------------------
Converts a CapIQ financial buyer export (TSV or CSV) into master.json
for the PE/VC research pipeline.

Usage:
    python preprocess_capiq.py --input capiq_export.tsv --output master.json
    python preprocess_capiq.py --input capiq_export.csv --output master.json --delimiter ","

Handles two export formats:
  Format A (basic):   Entity Name, CIQ ID, Buyer Type, Primary Industry, Geography, Business Description
  Format B (enriched): SP_ENTITY_NAME, SP_ENTITY_ID, SP_WEBSITE, SP_YEAR_INCORPORATED,
                        SNL_AUM, SP_AUM, SNL_NUM_COMPANY_EMPLOYEES,
                        SP_TOTAL_INVESTMENTS, SP_TOTAL_ACTIVE_INVESTMENTS, SP_TOTAL_LTM_INVESTMENTS,
                        SP_FUND_SECTOR_EMPHASIS, (optional) Business Description / Geography
"""

import json
import re
import csv
import argparse
from datetime import date, datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Column name aliases — maps both export formats to internal names
# ---------------------------------------------------------------------------

COL = {
    "name":         ["Entity Name",    "SP_ENTITY_NAME"],
    "ciq_id":       ["Entity ID",      "CIQ ID", "SP_ENTITY_ID"],
    "website":      ["Web Address",    "SP_WEBSITE"],
    "geography":    ["Geography"],
    "description":  ["Business Description"],
    "firm_type":    ["Primary Industry (CIQ/GICS) or Firm Type", "Firm Type"],
    "year_inc":     ["Year Incorporated", "SP_YEAR_INCORPORATED"],
    "aum_snl":      ["Assets Under Management\n($000)", "SNL_AUM"],
    "aum_sp":       ["SP_AUM"],
    "employees":    ["Number of Company Employees\n(actual)", "SNL_NUM_COMPANY_EMPLOYEES"],
    "total_inv":    ["Total Investments\n(actual)", "SP_TOTAL_INVESTMENTS"],
    "active_inv":   ["Total Active Investments\n(actual)", "SP_TOTAL_ACTIVE_INVESTMENTS"],
    "ltm_inv":      ["Total LTM Investments\n(actual)", "SP_TOTAL_LTM_INVESTMENTS"],
    "sector_emph":  ["Sector Emphasis", "SP_FUND_SECTOR_EMPHASIS"],
}

def get_col(row: dict, key: str, default="") -> str:
    """Try each alias in order, return first non-empty hit."""
    for alias in COL.get(key, []):
        val = row.get(alias, "").strip()
        if val and val.upper() != "NA":
            return val
    return default


# ---------------------------------------------------------------------------
# Slug generator
# ---------------------------------------------------------------------------

def make_slug(name: str) -> str:
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)
    return slug


# ---------------------------------------------------------------------------
# Geography parser  "New York, NY" → ("New York", "NY")
# ---------------------------------------------------------------------------

def parse_geography(geo: str) -> tuple[str, str]:
    if not geo:
        return ("", "")
    parts = [p.strip() for p in geo.split(",")]
    if len(parts) >= 2:
        return (parts[0], parts[-1])
    return (parts[0], "")


# ---------------------------------------------------------------------------
# AUM parser  "2,012,977" (thousands) → 2012.977 (USD millions)
# ---------------------------------------------------------------------------

def parse_aum(raw: str) -> float | None:
    if not raw or raw.upper() == "NA":
        return None
    try:
        val = float(raw.replace(",", "").replace("$", "").strip())
        return round(val / 1000, 2)  # convert $000s to $millions
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Integer parser for investment counts
# ---------------------------------------------------------------------------

def parse_int(raw: str) -> int | None:
    if not raw or raw.upper() == "NA":
        return None
    try:
        return int(float(raw.replace(",", "")))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Activity tier inference from LTM + active investment counts
# ---------------------------------------------------------------------------

def infer_activity_tier(ltm: int | None, active: int | None, total: int | None) -> str | None:
    if ltm is not None:
        if ltm >= 1:
            return "Active"
        elif active and active > 0:
            return "Slowing"
        else:
            return "Dormant"
    if active is not None:
        if active > 0:
            return "Slowing"
        return "Dormant"
    return None  # not enough data — crawler will determine


# ---------------------------------------------------------------------------
# Sector emphasis parser  "All,Health Care,Industrials,..." → tier1 list
# ---------------------------------------------------------------------------

CAPIQ_TO_TIER1 = {
    "technology": "Technology",
    "information technology": "Technology",
    "software": "Technology",
    "health care": "Healthcare",
    "healthcare": "Healthcare",
    "pharmaceuticals": "Healthcare",
    "industrials": "Industrials",
    "capital goods": "Industrials",
    "financials": "Financial Services",
    "financial services": "Financial Services",
    "insurance": "Financial Services",
    "consumer": "Consumer",
    "retail": "Consumer",
    "energy": "Energy",
    "energy and utilities": "Energy",
    "utilities": "Energy",
    "real estate": "Real Estate",
    "media": "Media",
    "telecommunications": "Media",
    "infrastructure": "Infrastructure",
    "materials": "Industrials",
    "transportation": "Infrastructure",
}

def parse_sector_emphasis(raw: str) -> list[str]:
    """Parse CapIQ sector emphasis CSV string into tier1 list."""
    if not raw:
        return []
    items = [s.strip().lower() for s in raw.split(",")]
    seen = set()
    tier1 = []
    for item in items:
        if item == "all":
            continue
        mapped = CAPIQ_TO_TIER1.get(item)
        if mapped and mapped not in seen:
            seen.add(mapped)
            tier1.append(mapped)
        if len(tier1) >= 3:
            break
    return tier1


# ---------------------------------------------------------------------------
# Numeric extractor — regex first pass from Business Description
# "between $50 million and $300 million" → (50.0, 300.0)
# If regex fails, returns (None, None) — AI/crawler fills it from raw string
# ---------------------------------------------------------------------------

MILLION_PATTERN = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)\s*(?:million|mm|m)\b",
    re.IGNORECASE,
)

def extract_range(text: str, keywords: list[str]) -> tuple[float | None, float | None]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for sentence in sentences:
        if any(kw.lower() in sentence.lower() for kw in keywords):
            matches = MILLION_PATTERN.findall(sentence)
            values = [float(m.replace(",", "")) for m in matches]
            if len(values) >= 2:
                return (min(values), max(values))
            elif len(values) == 1:
                return (values[0], None)
    return (None, None)


# ---------------------------------------------------------------------------
# Firm type mapper
# ---------------------------------------------------------------------------

FIRM_TYPE_MAP = {
    "private equity or venture capital": "PE",
    "venture capital": "VC",
    "private equity": "PE",
    "family office": "Family Office",
    "mezzanine": "Mezzanine",
    "search fund": "Search Fund",
    "fundless sponsor": "Fundless Sponsor",
}

def map_firm_type(raw: str) -> str:
    if not raw:
        return "Other"
    lower = raw.lower()
    for key, val in FIRM_TYPE_MAP.items():
        if key in lower:
            return val
    return "Other"


# ---------------------------------------------------------------------------
# Deal type extractor
# ---------------------------------------------------------------------------

DEAL_TYPE_SIGNALS = {
    "Buyout":        ["buyout", "leveraged buyout", "lbo", "management buyout", "mbo"],
    "Growth Equity": ["growth equity", "growth capital", "growth financing"],
    "Minority":      ["minority stake", "minority investment", "minority position"],
    "Recap":         ["recapitalization", "recap"],
    "Distressed":    ["distressed", "turnaround", "vulture", "restructuring"],
    "Venture":       ["venture capital", "series a", "series b", "seed", "early stage"],
    "Mezzanine":     ["mezzanine", "subordinated debt", "unitranche"],
}

def extract_deal_types(description: str) -> list[str]:
    found = []
    desc_lower = description.lower()
    for deal_type, signals in DEAL_TYPE_SIGNALS.items():
        if any(sig in desc_lower for sig in signals):
            found.append(deal_type)
    return found


# ---------------------------------------------------------------------------
# Sector tier 1 — fallback from Business Description if no sector emphasis
# ---------------------------------------------------------------------------

SECTOR_SIGNALS = {
    "Technology":         ["software", "saas", "technology", "tech", "semiconductor", "cybersecurity", "fintech", "ai", "data"],
    "Healthcare":         ["healthcare", "health care", "biotech", "pharma", "medical", "life science", "diagnostics"],
    "Industrials":        ["industrial", "manufacturing", "aerospace", "defense", "chemicals", "packaging", "engineering"],
    "Financial Services": ["financial services", "insurance", "banking", "payments", "asset management"],
    "Consumer":           ["consumer", "retail", "food", "beverage", "restaurant", "e-commerce"],
    "Business Services":  ["business services", "outsourcing", "staffing", "logistics", "distribution"],
    "Energy":             ["energy", "oil and gas", "renewables", "power generation", "utilities"],
    "Real Estate":        ["real estate", "reit", "property"],
    "Media":              ["media", "entertainment", "publishing", "telecommunications", "telecom"],
    "Infrastructure":     ["infrastructure", "transportation", "airports", "ports"],
}

def extract_sector_from_description(description: str) -> list[str]:
    desc_lower = description.lower()
    scores = {}
    for sector, signals in SECTOR_SIGNALS.items():
        count = sum(1 for sig in signals if sig in desc_lower)
        if count > 0:
            scores[sector] = count
    return sorted(scores, key=scores.get, reverse=True)[:3]


# ---------------------------------------------------------------------------
# US investments flag
# ---------------------------------------------------------------------------

def extract_us_investments(description: str) -> str:
    desc_lower = description.lower()
    us_signals   = ["united states", "north america", "u.s.", "us-based", "domestic"]
    intl_signals = ["globally", "worldwide", "asia", "europe", "latin america", "africa"]
    has_us   = any(sig in desc_lower for sig in us_signals)
    has_intl = any(sig in desc_lower for sig in intl_signals)
    if has_us and not has_intl:
        return "true"
    elif has_us and has_intl:
        return "primarily"
    return "false"


# ---------------------------------------------------------------------------
# Normalize website URL
# ---------------------------------------------------------------------------

def normalize_website(raw: str) -> str | None:
    if not raw:
        return None
    raw = raw.strip().lower()
    if not raw.startswith("http"):
        raw = "https://" + raw
    return raw


# ---------------------------------------------------------------------------
# Main record builder
# ---------------------------------------------------------------------------

def build_record(row: dict) -> dict:
    name        = get_col(row, "name")
    ciq_id      = get_col(row, "ciq_id")
    description = get_col(row, "description")
    geo         = get_col(row, "geography")
    firm_type_r = get_col(row, "firm_type")
    website_raw = get_col(row, "website")
    year_inc    = get_col(row, "year_inc")
    sector_emph = get_col(row, "sector_emph")

    # AUM — prefer SP_AUM (calendar year), fall back to SNL_AUM (fiscal year)
    aum_sp  = parse_aum(get_col(row, "aum_sp"))
    aum_snl = parse_aum(get_col(row, "aum_snl"))
    aum     = aum_sp if aum_sp is not None else aum_snl

    # Investment counts
    total_inv  = parse_int(get_col(row, "total_inv"))
    active_inv = parse_int(get_col(row, "active_inv"))
    ltm_inv    = parse_int(get_col(row, "ltm_inv"))

    city, state = parse_geography(geo)

    # Sector: prefer CapIQ sector emphasis field; fall back to description NLP
    sector_tier1 = parse_sector_emphasis(sector_emph)
    if not sector_tier1 and description:
        sector_tier1 = extract_sector_from_description(description)

    # Numeric ranges from description (regex first pass)
    # Raw description ALSO preserved for AI/crawler to re-extract if needed
    ebitda_min,  ebitda_max  = extract_range(description, ["ebitda", "operating profit"])
    revenue_min, revenue_max = extract_range(description, ["revenue", "sales value", "annual revenue"])
    ev_min,      ev_max      = extract_range(description, ["enterprise value", "ev between", "ev of"])
    check_min,   check_max   = extract_range(description, ["invests between", "investment between", "equity investment"])

    return {
        # --- IDENTITY ---
        "firm_slug":      make_slug(name),
        "firm_name":      name,
        "capiq_ids":      [ciq_id] if ciq_id else [],   # array — dupes append here
        "website":        normalize_website(website_raw),
        "website_source": "capiq" if website_raw else None,
        "firm_type":      map_firm_type(firm_type_r),
        "hq_country":     "USA",
        "hq_state":       state,
        "hq_city":        city,
        "year_incorporated": int(year_inc) if year_inc.isdigit() else None,
        "us_investments": extract_us_investments(description) if description else None,

        # --- DEAL SIZING (regex pass — null = not found, AI/crawler will fill) ---
        "revenue_min":           revenue_min,
        "revenue_max":           revenue_max,
        "ebitda_min":            ebitda_min,
        "ebitda_max":            ebitda_max,
        "enterprise_value_min":  ev_min,
        "enterprise_value_max":  ev_max,
        "check_size_min":        check_min,
        "check_size_max":        check_max,
        "deal_types":            extract_deal_types(description) if description else [],

        # --- SECTOR COVERAGE ---
        "sector_tier1":      sector_tier1,
        "sector_tier2":      [],
        "sector_tier3":      [],
        "sector_keywords":   [],
        "sector_exclusions": [],

        # --- ACTIVITY SIGNALS ---
        "last_known_deal_date": None,
        "fund_name":            None,
        "fund_vintage_year":    None,
        "fund_number":          None,
        "aum_usd_millions":     aum,
        "total_investments":    total_inv,
        "active_investments":   active_inv,
        "ltm_investments":      ltm_inv,
        "num_employees":        parse_int(get_col(row, "employees")),
        "activity_tier":        infer_activity_tier(ltm_inv, active_inv, total_inv),

        # --- RAW SOURCE DATA (preserved for AI extraction pass) ---
        "capiq_description":    description if description else None,
        "capiq_sector_emphasis": sector_emph if sector_emph else None,

        # --- DATA QUALITY ---
        "last_checked_date": None,
        "source_urls":       [],
        "confidence":        None,
        "needs_review":      False,
        "notes":             (
            "Seeded from CapIQ export. "
            "Numeric fields are regex first-pass — crawler will confirm. "
            "capiq_description preserved for AI extraction fallback."
        ),
        "status": "pending",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Preprocess CapIQ export to master.json")
    parser.add_argument("--input",     required=True,          help="Path to CapIQ TSV or CSV export")
    parser.add_argument("--output",    default="master.json",  help="Output path for master.json")
    parser.add_argument("--delimiter", default="\t",           help="Delimiter: \\t for TSV (default), ',' for CSV")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}")
        return

    records    = []
    slug_index = {}   # slug → index in records list

    with open(input_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=args.delimiter)
        for row in reader:
            name = get_col(row, "name")
            if not name:
                continue

            ciq_id = get_col(row, "ciq_id")
            slug   = make_slug(name)

            if slug in slug_index:
                # Duplicate slug — append CIQ ID to existing record, don't re-crawl
                existing = records[slug_index[slug]]
                if ciq_id and ciq_id not in existing["capiq_ids"]:
                    existing["capiq_ids"].append(ciq_id)
                    print(f"  DUPE merged: {name} → added CIQ ID {ciq_id} to {slug}")
            else:
                record = build_record(row)
                slug_index[slug] = len(records)
                records.append(record)

    # Summary stats
    has_website     = sum(1 for r in records if r["website"])
    has_aum         = sum(1 for r in records if r["aum_usd_millions"])
    has_activity    = sum(1 for r in records if r["activity_tier"])
    active_count    = sum(1 for r in records if r["activity_tier"] == "Active")
    slowing_count   = sum(1 for r in records if r["activity_tier"] == "Slowing")
    dormant_count   = sum(1 for r in records if r["activity_tier"] == "Dormant")
    multi_id_count  = sum(1 for r in records if len(r["capiq_ids"]) > 1)

    output_path = Path(args.output)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, default=str)

    print(f"\n✓ Processed {len(records)} unique firms → {output_path}")
    print(f"\n  Website populated:    {has_website}/{len(records)}")
    print(f"  AUM populated:        {has_aum}/{len(records)}")
    print(f"  Activity inferred:    {has_activity}/{len(records)}")
    print(f"    Active:             {active_count}")
    print(f"    Slowing:            {slowing_count}")
    print(f"    Dormant:            {dormant_count}")
    print(f"  Multi-CIQ-ID slugs:   {multi_id_count}")

    print(f"\nSample record ({records[0]['firm_name']}):")
    sample = {k: v for k, v in records[0].items() if k != "capiq_description"}
    print(json.dumps(sample, indent=2))


if __name__ == "__main__":
    main()
