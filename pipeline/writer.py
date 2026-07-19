"""
writer.py
---------
Handles all reads and writes to master.json.
Never batches — writes after every firm so crashes don't lose progress.

Usage (internal — called by pipeline.py):
    from writer import load_master, save_record, set_status
"""

import json
import os
from pathlib import Path
from datetime import date

# ---------------------------------------------------------------------------
# Paths — relative to project root (ICS Code Base/)
# ---------------------------------------------------------------------------

MASTER_PATH = Path(__file__).parent.parent / "data" / "master.json"


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_master() -> list[dict]:
    """Load all records from master.json. Returns empty list if file missing."""
    if not MASTER_PATH.exists():
        print(f"ERROR: master.json not found at {MASTER_PATH}")
        return []

    with open(MASTER_PATH, encoding="utf-8") as f:
        records = json.load(f)

    print(f"✓ Loaded {len(records)} records from master.json")
    return records


# ---------------------------------------------------------------------------
# Get pending firms
# ---------------------------------------------------------------------------

def get_pending(records: list[dict]) -> list[dict]:
    """Return all records with status = pending or stale, pending first."""
    pending = [r for r in records if r.get("status") == "pending"]
    stale   = [r for r in records if r.get("status") == "stale"]
    queue   = pending + stale
    print(f"  Queued: {len(pending)} pending + {len(stale)} stale = {len(queue)} total")
    return queue


# ---------------------------------------------------------------------------
# Save single record back to master.json
# Called after EVERY firm — crash recovery depends on this
# ---------------------------------------------------------------------------

def save_record(records: list[dict], updated: dict) -> None:
    """
    Find the record matching updated firm_slug and overwrite it.
    Then write the entire master.json back to disk immediately.
    Never overwrites a 'complete' record unless status is explicitly set.
    """
    slug = updated.get("firm_slug")
    if not slug:
        print(f"  ERROR: Cannot save record with no firm_slug")
        return

    for i, record in enumerate(records):
        if record["firm_slug"] == slug:
            # Safety check — never silently overwrite a complete record
            if record.get("status") == "complete" and updated.get("status") != "complete":
                print(f"  WARN: Skipping overwrite of complete record: {slug}")
                return
            records[i] = updated
            break
    else:
        print(f"  WARN: slug not found in master, appending: {slug}")
        records.append(updated)

    _write_master(records)
    print(f"  ✓ Saved: {updated.get('firm_name')} [{updated.get('status')}]")


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

def set_status(records: list[dict], slug: str, status: str) -> None:
    """Quick status update without touching other fields."""
    for record in records:
        if record["firm_slug"] == slug:
            record["status"] = status
            _write_master(records)
            return
    print(f"  WARN: set_status — slug not found: {slug}")


# ---------------------------------------------------------------------------
# Internal write
# ---------------------------------------------------------------------------

def _write_master(records: list[dict]) -> None:
    """Write full records list to master.json atomically."""
    # Write to temp file first, then rename — prevents corruption on crash
    tmp_path = MASTER_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, default=str)
    os.replace(tmp_path, MASTER_PATH)


# ---------------------------------------------------------------------------
# Summary stats
# ---------------------------------------------------------------------------

def print_summary(records: list[dict]) -> None:
    total    = len(records)
    complete = sum(1 for r in records if r.get("status") == "complete")
    pending  = sum(1 for r in records if r.get("status") == "pending")
    stale    = sum(1 for r in records if r.get("status") == "stale")
    review   = sum(1 for r in records if r.get("status") == "needs_review")
    progress = sum(1 for r in records if r.get("status") == "in_progress")

    print(f"\n--- Master file summary ---")
    print(f"  Total:        {total}")
    print(f"  Complete:     {complete}")
    print(f"  Pending:      {pending}")
    print(f"  Stale:        {stale}")
    print(f"  Needs review: {review}")
    print(f"  In progress:  {progress}  ← interrupted last session")
    print(f"---------------------------\n")


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    records = load_master()
    if records:
        print_summary(records)
        queue = get_pending(records)
        if queue:
            print(f"\nFirst pending firm: {queue[0]['firm_name']}")
            print(f"  Website: {queue[0].get('website')}")
            print(f"  Status:  {queue[0].get('status')}")