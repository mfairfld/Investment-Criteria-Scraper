# Investment Criteria Scraper (ICS)

Pipeline that enriches a database of PE/VC firms with structured investment criteria (deal sizing, sector focus, activity signals) pulled from firm websites — seeded from CapIQ exports, crawled and extracted automatically.

## How it works

1. **Preprocess** — `preprocess_capiq.py` converts a raw CapIQ buyer export (TSV/CSV) into `data/master.json`, one record per firm. It does a regex first-pass on descriptions to seed revenue/EBITDA/check-size ranges, sector tier 1, deal types, and activity tier where possible.
2. **Pipeline** — `pipeline.py` loops over firms with `status = pending` or `stale`, processing one at a time and writing back to `master.json` after every firm (crash-safe, no batching).
3. **Search** — `search_provider.py` resolves a firm's website if missing, or finds an updated URL if a crawl fails (404/timeout). Currently stubbed with mock results; swap in a real provider (e.g. Exa) by setting `EXA_API_KEY` and flipping `SEARCH_ENABLED = True`.
4. **Crawl** — `crawler.py` uses Crawl4AI's `AdaptiveCrawler` to pull whatever it can from the homepage, then parses internal links for priority pages (criteria, strategy, portfolio) and fetches them directly with browser-header requests to work around 403s. Saves crawl state per firm and exports raw pages to `kb/{slug}.jsonl`.
5. **Extract** — `extractor.py` cleans and combines crawled pages, then calls the Claude API (`claude-haiku-4-5`) to pull structured fields into the master schema. Never overwrites confirmed values with nulls; flags conflicts against CapIQ-seeded data as `needs_review`.
6. **Export** — `export_to_xlsx.py` renders `master.json` as a formatted, color-coded Excel workbook with a summary sheet.

## Setup

```bash
pip install crawl4ai requests markdownify anthropic python-dotenv openpyxl
```

Create a `.env` file in the project root:

```
ANTHROPIC_API_KEY=your-key-here
EXA_API_KEY=your-key-here          # optional, only if real search is enabled
```

## Usage

```bash
# 1. Convert a CapIQ export into master.json
python pipeline/preprocess_capiq.py --input data/capiq_export.tsv --output data/master.json

# 2. Run the pipeline
python pipeline/pipeline.py                            # process all pending/stale firms
python pipeline/pipeline.py --limit 5                  # test on first 5
python pipeline/pipeline.py --slug borgman-capital-llc  # process one firm
python pipeline/pipeline.py --summary                   # status counts only, no processing

# 3. Export to Excel
python pipeline/export_to_xlsx.py
python pipeline/export_to_xlsx.py --status complete     # only completed firms
```

## Record schema (excerpt)

Each firm in `master.json` includes: identity (`firm_name`, `website`, `firm_type`, HQ location), deal sizing (`revenue_min/max`, `ebitda_min/max`, `enterprise_value_min/max`, `check_size_min/max`, `deal_types`), sector coverage (`sector_tier1/2/3`, `sector_keywords`, `sector_exclusions`), activity signals (`activity_tier`, `aum_usd_millions`, `last_known_deal_date`), and data quality fields (`confidence`, `needs_review`, `source_urls`, `last_checked_date`, `status`).

Status values: `pending → in_progress → complete`, or `needs_review` on failure. Records become `stale` after 90 days and re-enter the queue on the next run.

## Project structure

```
pipeline/
  preprocess_capiq.py   # CapIQ export -> master.json
  pipeline.py           # main orchestration loop
  search_provider.py    # website resolution (mock or Exa)
  crawler.py            # Crawl4AI + direct-fetch crawling
  extractor.py          # Claude-based structured extraction
  export_to_xlsx.py     # master.json -> formatted .xlsx
data/
  master.json           # the master record file (gitignored)
crawl_states/            # per-firm crawl state (gitignored)
kb/                       # per-firm raw crawled pages, .jsonl (gitignored)
```

## Notes

- `.env`, `crawl_states/`, `kb/`, and `data/master.json` are gitignored. `data/*.xlsx` and `data/*.csv` are currently **not** ignored — double-check before committing if those contain firm data you don't want public.
- `search_provider.py` and `crawler.py` both ship with mock modes for testing pipeline logic without hitting real APIs or websites.
