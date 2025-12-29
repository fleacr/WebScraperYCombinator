# Y Combinator Startup Scraper (Python + Playwright)

A small, modular web scraper that collects startup/company records from Y Combinator and enriches them with hiring signals derived from Dice.com.

Purpose
- Designed for startup research, recruiting, talent sourcing, and market analysis.

What it does
- Sources company records from Y Combinator listings (YC is the source of truth for company discovery).
- Enriches those records with a **Hiring Signal** derived from external, verifiable sources (currently: Dice.com).
- Persists results as semicolon-delimited CSV (`Companies.csv`) and a human-friendly HTML report (`Companies.html`).

Key design points
- **Deterministic, data-driven Hiring Signal** (High / Low / Unsure) — we never infer hiring intent from YC text or tags.
- **Dice.com is used as hiring evidence only**; YC data is the canonical source of company records.
- **Modular enrichment**: the hiring-enrichment step is separated from YC sourcing so additional sources (LinkedIn, company career pages) can be added later.
- Signals are persistent and **do not downgrade a High** entry unless evidence changes.

Hiring Signal logic
- **High** → the company is found on Dice during the enrichment step (clear hiring evidence).
- **Low** → Dice was queried successfully and the company was not found.
- **Unsure** → Dice data was unavailable, inconclusive, or the company is newly listed on YC (default when insufficient evidence).

Outputs & schema
- `Companies.csv` (semicolon-delimited) — main fields (top columns):
  - `No.` — monotonically decreasing numbering so newest rows are at the top
  - `name` — Company name (from YC)
  - `Company Website` — canonical web/LinkedIn URL discovered for the company
  - `Date Added` — date the company was first added to the dataset (example: `Sunday 28th, Dec`)
  - `Hiring Signal` — `High`, `Low`, or `Unsure`

- `Companies.html` — an HTML report with the same columns and a snapshot timestamp.

Notes on `YC Batch` and legacy fields
- If available on a company's YC profile, a `YC Batch` field may be present (the scraper does not rely on it for hiring inference).
- Older or deprecated fields (e.g., the former `Tech Hiring Platforms`) are cleaned out during processing.

How to run
- Manual (local):
  - Install dependencies (Playwright, pandas) and Playwright browsers.
  - Run: `python scraper.py`
  - Environment variables available:
    - `HEADLESS` — if set (1/true) runs browser headless; unset defaults to headless in CI, headed locally.
    - `MAX_COMPANIES` — limit number of YC companies scraped (useful for quick tests).

- Automated (GitHub Actions):
  - A weekly workflow is included at `.github/workflows/scrape-weekly.yml` that runs the scraper on weekdays (07:30 AM GMT-6 by default) and uploads `Companies.html` as a workflow artifact.

Implementation & technologies
- Python 3 (async)
- Playwright (Chromium) for JavaScript-rendered pages
- AsyncIO for concurrency
- pandas for CSV handling and CSV/HTML generation
- GitHub Actions for scheduled automation

Extensibility
- The code is organized to keep sourcing (YC) and enrichment (Dice, future sources) separate. Adding additional hiring sources or a database backend (SQLite/Postgres) is straightforward.

License & contribution
- Suitable for public, non-commercial data collection respecting site terms of service. Contributions and PRs welcome.

---

Concise, technical, and ready for expansion — see `scraper.py` for implementation details and the GitHub Actions workflow for scheduling.
