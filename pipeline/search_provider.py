"""
search_provider.py
------------------
Decision layer for website resolution and crawl failure recovery.
Acts as a swappable interface — swap the engine (Exa, Google, SerpAPI)
without touching any other pipeline file.

Current mode: STUB — returns mock data, no real API calls.
To activate real search: set EXA_API_KEY in environment and
flip SEARCH_ENABLED = True.

Called by crawler.py in two situations:
  1. Firm has no website → search for it
  2. Crawl fails (404, dead site) → search for updated URL
"""

import os
import time
import random

# ---------------------------------------------------------------------------
# Config — flip to True when Exa API key is ready
# ---------------------------------------------------------------------------

SEARCH_ENABLED = False
EXA_API_KEY    = os.environ.get("EXA_API_KEY", None)


# ---------------------------------------------------------------------------
# Public interface — this is the only function crawler.py calls
# ---------------------------------------------------------------------------

def search(query: str, context: str = "website") -> str | None:
    """
    Main entry point. Routes to real search or mock based on SEARCH_ENABLED.

    Args:
        query:   Search query string e.g. "Borgman Capital private equity website"
        context: "website"  — looking for a firm's main URL
                 "recovery" — crawl failed, looking for updated URL
                 "criteria" — looking for investment criteria page specifically

    Returns:
        URL string if found, None if not found
    """
    if SEARCH_ENABLED:
        return _real_search(query, context)
    else:
        return _mock_search(query, context)


def find_website(firm_name: str) -> str | None:
    """
    Convenience wrapper — find a firm's website by name.
    Called when master.json has no website for a firm.
    """
    query = f"{firm_name} private equity firm official website"
    result = search(query, context="website")

    if result:
        print(f"    [search] Found website for {firm_name}: {result}")
    else:
        print(f"    [search] No website found for {firm_name}")

    return result


def find_updated_url(firm_name: str, dead_url: str) -> str | None:
    """
    Recovery wrapper — called when a known URL returns 404 or times out.
    Tries to find if the firm moved to a new domain.
    """
    query = f"{firm_name} private equity website 2024 2025"
    result = search(query, context="recovery")

    if result and result != dead_url:
        print(f"    [search] Recovery URL for {firm_name}: {result}")
        return result

    print(f"    [search] No recovery URL found for {firm_name}")
    return None


# ---------------------------------------------------------------------------
# Mock search — returns fake URLs for testing pipeline logic
# ---------------------------------------------------------------------------

def _mock_search(query: str, context: str) -> str | None:
    """
    Simulates search results without hitting any API.
    Returns a plausible fake URL ~80% of the time to simulate real-world
    hit rates (some firms genuinely can't be found).
    """
    # Simulate network latency
    time.sleep(random.uniform(0.1, 0.3))

    # Extract firm name from query for a realistic fake URL
    # e.g. "Borgman Capital private equity..." → "borgman-capital"
    words = query.lower().split()
    stop  = {"private", "equity", "firm", "official", "website",
             "investment", "capital", "partners", "llc", "lp", "inc",
             "2024", "2025", "criteria", "strategy"}
    name_words = [w for w in words[:4] if w not in stop]
    slug  = "-".join(name_words[:2]) if name_words else "unknown-firm"

    # Simulate ~80% hit rate
    if random.random() < 0.8:
        return f"https://www.{slug}.com"
    else:
        return None


# ---------------------------------------------------------------------------
# Real search — Exa integration (activate when ready)
# ---------------------------------------------------------------------------

def _real_search(query: str, context: str) -> str | None:
    """
    Real Exa search. Only called when SEARCH_ENABLED = True.
    Returns the most likely URL from results.

    TODO: activate by:
      1. pip install exa-py
      2. set EXA_API_KEY environment variable
      3. set SEARCH_ENABLED = True above
    """
    try:
        from exa_py import Exa  # type: ignore

        if not EXA_API_KEY:
            print("  ERROR: EXA_API_KEY not set")
            return None

        client  = Exa(api_key=EXA_API_KEY)
        results = client.search(
            query,
            num_results      = 3,
            use_autoprompt   = True,
            type             = "auto",
        )

        if results and results.results:
            # Return the top result URL
            top = results.results[0]
            return top.url

        return None

    except ImportError:
        print("  ERROR: exa-py not installed. Run: pip install exa-py")
        return None
    except Exception as e:
        print(f"  ERROR: Exa search failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== search_provider.py test ===")
    print(f"Mode: {'REAL (Exa)' if SEARCH_ENABLED else 'MOCK'}\n")

    # Test 1 — find website for firm with no URL
    print("Test 1 — find_website()")
    url = find_website("Borgman Capital LLC")
    print(f"  Result: {url}\n")

    # Test 2 — recovery from dead URL
    print("Test 2 — find_updated_url()")
    url = find_updated_url("Some Dead Firm LLC", "https://www.old-dead-url.com")
    print(f"  Result: {url}\n")

    # Test 3 — simulate 5 searches, show hit rate
    print("Test 3 — hit rate simulation (5 searches)")
    hits = 0
    for i in range(5):
        result = search(f"Test Firm {i} private equity website")
        status = "HIT" if result else "MISS"
        print(f"  [{status}] {result}")
        if result:
            hits += 1
    print(f"\n  Hit rate: {hits}/5")