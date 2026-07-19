"""
pipeline.py
-----------
Main orchestration loop for the PE/VC research pipeline.
Reads master.json, processes each pending/stale firm, writes back after each.

Usage:
    python pipeline.py                    # process all pending firms
    python pipeline.py --limit 5          # process first 5 only (testing)
    python pipeline.py --slug borgman-capital-llc  # process one firm by slug
    python pipeline.py --summary          # just print status summary, no processing

Phase tracking:
    Phase 1 — single firm end-to-end (use --slug or --limit 1)
    Phase 2 — pilot batch 25-50 firms (use --limit 25)
    Phase 3 — refinement (full run, fix failure modes)
    Phase 4 — scale + refresh loop
"""

import argparse
import time
import sys
from datetime import date
from pathlib import Path

# Add pipeline/ to path so imports work when run from project root
sys.path.insert(0, str(Path(__file__).parent))

from writer          import load_master, get_pending, save_record, set_status, print_summary
from search_provider import find_website, find_updated_url
from crawler         import crawl_firm
from extractor       import extract_firm

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DELAY_BETWEEN_FIRMS = 1.0   # seconds — be polite to web servers
MAX_SEARCH_ATTEMPTS = 2     # how many times to try finding a missing website
MAX_CRAWL_ATTEMPTS  = 2     # how many times to retry a failed crawl

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(limit: int = None, slug: str = None, summary_only: bool = False):
    today = date.today().isoformat()

    print(f"\n{'='*50}")
    print(f"  PE/VC Research Pipeline")
    print(f"  Date: {today}")
    print(f"  Mode: {'SUMMARY ONLY' if summary_only else 'PROCESSING'}")
    if limit:
        print(f"  Limit: {limit} firms")
    if slug:
        print(f"  Target slug: {slug}")
    print(f"{'='*50}\n")

    # --- Load master file ---
    records = load_master()
    if not records:
        print("ERROR: No records loaded. Exiting.")
        return

    print_summary(records)

    if summary_only:
        return

    # --- Build work queue ---
    if slug:
        queue = [r for r in records if r.get("firm_slug") == slug]
        if not queue:
            print(f"ERROR: slug not found: {slug}")
            return
    else:
        queue = get_pending(records)

    if limit:
        queue = queue[:limit]

    total  = len(queue)
    done   = 0
    failed = 0

    print(f"Starting processing — {total} firms in queue\n")

    # --- Per-firm loop ---
    for i, record in enumerate(queue, 1):
        firm_name = record.get("firm_name", "Unknown")
        firm_slug = record.get("firm_slug", "unknown")

        print(f"[{i}/{total}] {firm_name}")

        # Mark in_progress immediately — crash recovery
        set_status(records, firm_slug, "in_progress")

        try:
            updated = process_firm(record, records)
        except Exception as e:
            print(f"  FATAL ERROR on {firm_name}: {e}")
            updated = _mark_fatal(record, str(e))

        # Write back immediately after every firm
        save_record(records, updated)

        # Track stats
        if updated.get("status") == "complete":
            done += 1
        else:
            failed += 1

        # Progress report every 10 firms
        if i % 10 == 0:
            print(f"\n  --- Progress: {i}/{total} processed | {done} complete | {failed} failed ---\n")

        # Polite delay between firms
        if i < total:
            time.sleep(DELAY_BETWEEN_FIRMS)

    # --- Final summary ---
    print(f"\n{'='*50}")
    print(f"  Run complete")
    print(f"  Processed: {total}")
    print(f"  Complete:  {done}")
    print(f"  Failed:    {failed}")
    print(f"{'='*50}")
    print_summary(records)


# ---------------------------------------------------------------------------
# Per-firm processing
# ---------------------------------------------------------------------------

def process_firm(record: dict, records: list[dict]) -> dict:
    """
    Full processing pipeline for a single firm.

    Steps:
      1. Resolve website (use existing or search for it)
      2. Crawl website
      3. If crawl fails → try to find updated URL and retry
      4. Extract structured data from crawl content
      5. Return updated record
    """
    firm_name = record.get("firm_name")
    website   = record.get("website")

    # --- Step 1: Resolve website ---
    if not website:
        print(f"  No website — searching...")
        website = _find_website_with_retry(firm_name)
        if website:
            record["website"] = website
            record["website_source"] = "search"
        else:
            print(f"  Could not find website for {firm_name}")
            return _mark_no_website(record)

    # --- Step 2: Crawl ---
    crawl_result = crawl_firm(record)

    # --- Step 3: Recovery if crawl failed ---
    if not crawl_result["success"]:
        reason = crawl_result.get("failure_reason", "unknown")

        # Try to find updated URL if site appears dead
        if reason in ["404_not_found", "connection_refused", "timeout"]:
            print(f"  Crawl failed ({reason}) — searching for updated URL...")
            new_url = find_updated_url(firm_name, website)

            if new_url and new_url != website:
                record["website"] = new_url
                record["website_source"] = "search_recovery"
                crawl_result = crawl_firm(record)  # retry with new URL

        # Still failed after recovery attempt
        if not crawl_result["success"]:
            return _mark_crawl_failed(record, crawl_result)

    # --- Step 4: Extract ---
    updated = extract_firm(record, crawl_result)
    return updated


# ---------------------------------------------------------------------------
# Website resolution with retry
# ---------------------------------------------------------------------------

def _find_website_with_retry(firm_name: str) -> str | None:
    for attempt in range(MAX_SEARCH_ATTEMPTS):
        result = find_website(firm_name)
        if result:
            return result
        if attempt < MAX_SEARCH_ATTEMPTS - 1:
            time.sleep(1.0)
    return None


# ---------------------------------------------------------------------------
# Failure state helpers
# ---------------------------------------------------------------------------

def _mark_no_website(record: dict) -> dict:
    updated = record.copy()
    updated["status"]            = "needs_review"
    updated["confidence"]        = "Low"
    updated["needs_review"]      = True
    updated["last_checked_date"] = date.today().isoformat()
    updated["notes"]             = (
        f"{record.get('notes', '')} | [pipeline] No website found after search attempts"
    ).strip(" |")
    return updated


def _mark_crawl_failed(record: dict, crawl_result: dict) -> dict:
    updated = record.copy()
    updated["status"]            = "needs_review"
    updated["confidence"]        = "Low"
    updated["needs_review"]      = True
    updated["last_checked_date"] = date.today().isoformat()
    updated["notes"]             = (
        f"{record.get('notes', '')} | [pipeline] Crawl failed: {crawl_result.get('failure_reason')}"
    ).strip(" |")
    return updated


def _mark_fatal(record: dict, error: str) -> dict:
    updated = record.copy()
    updated["status"]            = "needs_review"
    updated["confidence"]        = "Low"
    updated["needs_review"]      = True
    updated["last_checked_date"] = date.today().isoformat()
    updated["notes"]             = (
        f"{record.get('notes', '')} | [pipeline] FATAL: {error}"
    ).strip(" |")
    return updated


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PE/VC Research Pipeline")
    parser.add_argument("--limit",       type=int,  default=None,  help="Max firms to process")
    parser.add_argument("--slug",        type=str,  default=None,  help="Process one firm by slug")
    parser.add_argument("--summary",     action="store_true",      help="Print summary only, no processing")
    args = parser.parse_args()

    run(
        limit        = args.limit,
        slug         = args.slug,
        summary_only = args.summary,
    )