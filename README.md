# Legislative Alpha

A daily-updating tracker for Congressional bills, amendments, Senate stock trades (STOCK Act disclosures), and lobbying spend — organized by thematic investing sector.

**Live site:** enable GitHub Pages in repo Settings → Pages → set source to the `main` branch, root folder. Your URL will be `https://<your-username>.github.io/legislative-alpha/`.

## How it works

1. `.github/workflows/daily-update.yml` runs `scripts/fetch_data.py` once a day (and can be triggered manually from the Actions tab).
2. The script pulls three live sources and writes `data.json`:
   - **Bills + amendments** — [Congress.gov API](https://api.congress.gov) (needs a free API key)
   - **Senate stock trades** — scraped directly from [efdsearch.senate.gov](https://efdsearch.senate.gov) (no key; House disclosures are scanned PDFs and are not included)
   - **Lobbying filings** — [Senate LDA API](https://lda.gov/api/) (public, no key needed)
3. Everything is matched to a sector defined in `scripts/sectors.json` — edit that file to add/remove sectors, ETFs, keywords, or tracked companies.
4. `index.html` is a static page that reads `data.json` and renders it. No API keys are ever exposed to site visitors.

## One-time setup

1. Get a free Congress.gov API key: https://api.congress.gov/sign-up
2. In this repo: Settings → Secrets and variables → Actions → New repository secret → name it `CONGRESS_API_KEY` → paste the key.
3. Settings → Pages → Source: Deploy from branch → `main` / `(root)`.
4. Actions tab → "Daily data update" workflow → Run workflow (to generate the first `data.json` immediately instead of waiting for the schedule).

## Local testing

```
pip install -r scripts/requirements.txt
CONGRESS_API_KEY=your_key_here python scripts/fetch_data.py
python -m http.server 8080
```

Then open http://localhost:8080.

## Notes

- Sector → ETF → constituent-company lists in `sectors.json` are a hand-curated starting point, not derived from live ETF holdings data. Review periodically.
- The "Legislative Momentum" score is built from real, observable signals (bill stage, cosponsor count, recency) — it is not a predictive model and does not estimate historical passage rates.
- Not investment advice.
