"""Refresh data/market.json with NSE closing prices and basic fundamentals.

Runs in GitHub Actions after market close. Prices and fundamentals come from
Yahoo Finance (NSE tickers end in .NS). Any value that can't be fetched keeps
its previous value, so the site never breaks on a bad day.
"""
import json
import math
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf

PATH = Path(__file__).resolve().parent.parent / "data" / "market.json"
INDEX_TICKERS = {"NIFTY": "^NSEI", "SENSEX": "^BSESN", "BANKNIFTY": "^NSEBANK", "FINNIFTY": "NIFTY_FIN_SERVICE.NS", "MIDCPNIFTY": "NIFTY_MID_SELECT.NS", "INDIAVIX": "^INDIAVIX"}
IST = timezone(timedelta(hours=5, minutes=30))


def clean(x, scale=1.0, nd=2):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x * scale, nd)


def weekly_closes(ticker):
    hist = yf.Ticker(ticker).history(period="1y", interval="1wk", auto_adjust=False)
    closes = [clean(v) for v in hist["Close"].tolist()]
    return [c for c in closes if c is not None]


def last_two_closes(ticker):
    hist = yf.Ticker(ticker).history(period="10d", interval="1d", auto_adjust=False)
    closes = [clean(v) for v in hist["Close"].tolist()]
    closes = [c for c in closes if c is not None]
    if len(closes) < 2:
        return None, None
    return closes[-1], closes[-2]


def refresh_quote(item, ticker):
    p, pc = last_two_closes(ticker)
    if p is None:
        raise ValueError("no recent closes")
    item["p"], item["pc"] = p, pc
    w = weekly_closes(ticker)
    if len(w) >= 20:
        w[-1] = p
        item["w"] = w[-52:]


def refresh_fundamentals(item, ticker):
    info = yf.Ticker(ticker).info or {}
    pairs = {
        "roe": clean(info.get("returnOnEquity"), 100, 1),
        "npm": clean(info.get("profitMargins"), 100, 1),
        "eg": clean(info.get("earningsGrowth"), 100, 1),
        "pe": clean(info.get("trailingPE"), 1, 1),
    }
    if not item.get("fin"):
        de = clean(info.get("debtToEquity"), 0.01, 2)  # Yahoo reports D/E in %
        pairs["de"] = de
    for k, v in pairs.items():
        if v is not None:
            item[k] = v
    name = info.get("longName") or info.get("shortName")
    if name:
        item["n"] = name


def main():
    data = json.loads(PATH.read_text())
    ok, failed = 0, []
    for idx in data["indices"]:
        try:
            refresh_quote(idx, INDEX_TICKERS.get(idx["s"], idx["s"]))
            ok += 1
        except Exception as e:  # keep previous values
            failed.append(f"{idx['s']}: {e}")
    for st in data["stocks"]:
        ticker = st["s"] + ".NS"
        try:
            refresh_quote(st, ticker)
            ok += 1
        except Exception as e:
            failed.append(f"{st['s']}: {e}")
            continue
        try:
            refresh_fundamentals(st, ticker)
        except Exception as e:
            failed.append(f"{st['s']} fundamentals: {e}")

    for et in data.get("etfs", []):
        try:
            refresh_quote(et, et["s"] + ".NS")
            ok += 1
        except Exception as e:
            failed.append(f"{et['s']}: {e}")
    for us in data.get("us", []):
        try:
            refresh_quote(us, us["s"])
            ok += 1
        except Exception as e:
            failed.append(f"{us['s']}: {e}")
    try:
        fx, _ = last_two_closes("INR=X")
        if fx:
            data["fx"] = fx
    except Exception as e:
        failed.append(f"USDINR: {e}")

    if ok == 0:
        print("Nothing refreshed; leaving file unchanged.", file=sys.stderr)
        sys.exit(1)

    data["source"] = "live"
    now = datetime.now(IST)
    data["asOf"] = now.strftime("%Y-%m-%d")
    data["asOfTime"] = now.strftime("%H:%M")
    data["note"] = "NSE prices and fundamentals via Yahoo Finance. Refreshed every 30 minutes in market hours and after close."
    PATH.write_text(json.dumps(data, separators=(",", ":")))
    print(f"Refreshed {ok} items. Failures: {len(failed)}")
    for f in failed:
        print("  -", f)


if __name__ == "__main__":
    main()
