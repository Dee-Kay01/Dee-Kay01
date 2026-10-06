# Kosh

A clickable prototype of a simple, transparent investing app for Indian investors.

**Live site:** `https://<your-username>.github.io/<repo-name>/` once GitHub Pages is on.

## What's in it

- **Four tabs, each thing in one place:** Home (your money, holdings, watchlist), Explore (search, Buffett-style stocks, movers, funds, IPOs), Insights, Activity.
- **Insights:** how your stocks did against NIFTY 50, what to act on, quality stocks that fill gaps in your portfolio, and an ideal mix from three questions.
- **Buffett-style checks** on every stock: return on equity ≥ 15%, debt ≤ 0.5× equity (skipped for lenders), profit margin ≥ 10%, profit growth ≥ 10%, P/E ≤ 25. This is an educational screen, not investment advice.
- **Fewer steps:** buying is one sheet with slide-to-confirm and Undo; a fund and its SIP are one sheet.

## Data

`data/market.json` holds index levels, prices, a year of weekly closes and basic fundamentals for 26 large NSE stocks.

- The committed file is a **sample snapshot**.
- `.github/workflows/refresh-data.yml` runs `scripts/fetch_market.py` every weekday at 4:45 PM IST. It pulls NSE closing data and fundamentals from Yahoo Finance (`.NS` tickers) and commits the updated file. Any value it can't fetch keeps its previous value.
- Mutual funds and IPOs are sample data.

To run the refresh straight away: **Actions → Refresh market data → Run workflow**.

## Publish on GitHub Pages

1. Create a public repository and upload everything in this folder, keeping the folder structure. The `.github` folder and the `.nojekyll` file start with a dot and may be hidden on your computer.
2. Go to **Settings → Pages**. Under *Build and deployment*, choose **Deploy from a branch**, then select `main` and `/ (root)`.
3. Go to **Settings → Actions → General → Workflow permissions** and choose **Read and write permissions**, so the daily refresh can commit.
4. Open the site link that Pages shows after a minute or two.

## Run locally

Opening `index.html` directly works, using the built-in snapshot. To load `data/market.json`, serve the folder instead:

```bash
python3 -m http.server 8000
# then open http://localhost:8000
```

To refresh data locally:

```bash
pip install -r scripts/requirements.txt
python scripts/fetch_market.py
```
