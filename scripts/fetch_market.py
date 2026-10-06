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

import bisect

import requests
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


def refresh_long(item, ticker):
    """3- and 5-year yearly returns from monthly closes."""
    hist = yf.Ticker(ticker).history(period="6y", interval="1mo", auto_adjust=True)
    closes = [c for c in (clean(v) for v in hist["Close"].tolist()) if c]
    if len(closes) < 37:
        return
    last = closes[-1]
    item["r3"] = round(((last / closes[-37]) ** (1 / 3) - 1) * 100, 1)
    if len(closes) >= 61:
        item["r5"] = round(((last / closes[-61]) ** (1 / 5) - 1) * 100, 1)


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
    mc = clean(info.get("marketCap"), 1e-7, 0)  # rupees -> crore
    if mc:
        item["mc"] = mc


def mf_refresh(data, failed):
    funds = data.get("mf", [])
    if not funds:
        return 0
    rows = None
    ok = 0
    ua = {"User-Agent": "Mozilla/5.0 (kosh-data-bot)"}

    def good(name, q, ex=(), fof=False):
        name = name.lower()
        bad = ("idcw", "dividend", "bonus", "segregated") + (() if fof else (" etf", "fund of fund"))
        return not any(x in name for x in ex) and all(x in name for x in q) and "direct" in name and "growth" in name and not any(x in name for x in bad)

    for f in funds:
        code = None
        try:
            res = requests.get("https://api.mfapi.in/mf/search", params={"q": f.get("sq") or " ".join(f["q"])}, headers=ua, timeout=30).json()
            f["cands"] = [r.get("schemeName", "") for r in res[:6]]
            for r in res:
                if good(r.get("schemeName", ""), f["q"], f.get("x", ()), f.get("cat") == "Gold"):
                    code, f["scheme"] = str(r["schemeCode"]), r["schemeName"]
                    break
        except Exception as e:
            failed.append(f"MF search {f['k']}: {e}")
        if not code:
            try:
                if rows is None:
                    txt = requests.get("https://www.amfiindia.com/spages/NAVAll.txt", headers=ua, timeout=60).text
                    rows = [r.split(";") for r in txt.splitlines() if r.count(";") >= 5]
                for r in rows:
                    if good(r[3], f["q"], f.get("x", ()), f.get("cat") == "Gold"):
                        code, f["scheme"] = r[0].strip(), r[3].strip()
                        break
            except Exception as e:
                failed.append(f"MF amfi {f['k']}: {e}")
                rows = []
        if not code and f.get("code"):
            try:
                meta = requests.get(f"https://api.mfapi.in/mf/{f['code']}", headers=ua, timeout=60).json().get("meta", {})
                nm = meta.get("scheme_name", "")
                if good(nm, f["q"], f.get("x", ()), f.get("cat") == "Gold") or (all(x in nm.lower() for x in f["q"]) and "direct" in nm.lower() and not any(x in nm.lower() for x in f.get("x", ()))):
                    code, f["scheme"] = str(f["code"]), nm
                else:
                    failed.append(f"MF {f['k']}: code {f['code']} is '{nm}'")
            except Exception as e:
                failed.append(f"MF code {f['k']}: {e}")
        if not code:
            failed.append(f"MF {f['k']}: no AMFI match")
            continue
        f["sc"] = str(code)
        try:
            hist = requests.get(f"https://api.mfapi.in/mf/{code}", headers=ua, timeout=60).json()["data"]
            pts = sorted((datetime.strptime(h["date"], "%d-%m-%Y"), float(h["nav"])) for h in hist if float(h["nav"]) > 0)
            dates = [p[0] for p in pts]
            last_d, last = pts[-1]

            def nav_at(days):
                i = bisect.bisect_right(dates, last_d - timedelta(days=days)) - 1
                return pts[i][1] if i >= 0 else None

            def cagr(years):
                v = nav_at(round(365.25 * years))
                if not v or dates[0] > last_d - timedelta(days=round(365.25 * years) - 7):
                    return None
                return round(((last / v) ** (1 / years) - 1) * 100, 2) if years > 1 else round((last / v - 1) * 100, 2)

            f["nav"], f["navDate"] = round(last, 4), last_d.strftime("%Y-%m-%d")
            f["r"] = {"1Y": cagr(1), "3Y": cagr(3), "5Y": cagr(5)}
            w = [nav_at(7 * i) for i in range(156, -1, -1)]
            f["w"] = [round(x, 4) for x in w if x]
            start = last_d - timedelta(days=1096)
            peak, dd = 0, 0
            for dte, v in pts:
                if dte < start:
                    continue
                peak = max(peak, v)
                dd = min(dd, v / peak - 1)
            f["dd"] = round(dd * 100, 1)
            m = [nav_at(30 * i) for i in range(36, -1, -1)]
            rets = [m[i] / m[i - 1] - 1 for i in range(1, len(m)) if m[i] and m[i - 1]]
            if len(rets) > 6:
                mu = sum(rets) / len(rets)
                f["vol"] = round((sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5 * (12 ** 0.5) * 100, 1)
            ok += 1
        except Exception as e:
            failed.append(f"MF {f['k']}: {e}")
    return ok


# ---------- whole fund universe from AMFI ----------
AMFI_NOW = "https://www.amfiindia.com/spages/NAVAll.txt"
AMFI_HIST = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?frmdt={a}&todt={b}"
CATMAP = [("Large & Mid Cap", "Large & mid"), ("Large Cap", "Large cap"), ("Mid Cap", "Mid cap"), ("Small Cap", "Small cap"),
          ("Flexi Cap", "Flexi cap"), ("Multi Cap", "Multi cap"), ("ELSS", "Tax saver"), ("Value", "Value"), ("Contra", "Value"),
          ("Dividend Yield", "Value"), ("Focused", "Focused"), ("Sectoral", "Sector"), ("Index Funds", "Index"),
          ("Hybrid Scheme", "Hybrid"), ("Solution Oriented", "Hybrid"), ("Debt Scheme", "Debt"), ("FoF Overseas", "Global"), ("FoF Domestic", "FoF")]
BADNAME = ("idcw", "dividend", "bonus", "payout", "reinvest", "segregat", "fixed term", "fmp", "interval", "series ", "unclaimed", "institutional", "retail plan")


def amfi_get(url, ua):
    r = requests.get(url, headers=ua, timeout=90)
    r.raise_for_status()
    return r.text


def parse_hist(txt):
    """code -> latest NAV in a history report."""
    out, ix_nav = {}, None
    for line in txt.splitlines():
        parts = [x.strip() for x in line.split(";")]
        if len(parts) < 5:
            continue
        if parts[0].lower().startswith("scheme code"):
            low = [x.lower() for x in parts]
            ix_nav = low.index("net asset value") if "net asset value" in low else 4
            continue
        try:
            nav = float(parts[ix_nav if ix_nav is not None else 4])
        except (ValueError, IndexError):
            continue
        if nav > 0:
            out[parts[0]] = nav
    return out


def mf_universe(data, failed):
    today = datetime.now(IST)
    if data.get("mfuAsOf") == today.strftime("%Y-%m-%d") and data.get("mfu"):
        return
    ua = {"User-Agent": "Mozilla/5.0 (kosh prototype)"}
    txt = amfi_get(AMFI_NOW, ua)
    rows, cat, sub, amc = [], None, "", ""
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        if ";" not in line:
            if line.startswith("Open Ended Schemes"):
                inner = line[line.find("(") + 1:line.rfind(")")] if "(" in line else line
                cat = next((c for key, c in CATMAP if key in inner), None)
                sub = inner.split(" - ", 1)[1] if " - " in inner else inner
                sub = sub.replace(" Fund", "").replace("Index Funds", "Index").strip()
            elif line.startswith(("Close Ended", "Interval Fund")):
                cat = None
            else:
                amc = line.replace(" Mutual Fund", "").strip()
            continue
        if not cat:
            continue
        p = line.split(";")
        if len(p) < 6:
            continue
        code, name, nav = p[0].strip(), p[3].strip(), p[4].strip()
        low = name.lower()
        if "direct" not in low or "growth" not in low or any(b in low for b in BADNAME):
            continue
        if " etf" in low and "fund of fund" not in low and "fof" not in low:
            continue
        c = cat
        if c == "FoF":
            c = "Gold" if ("gold" in low or "silver" in low) else "Hybrid"
        if c == "Index" and ("gold" in low or "silver" in low):
            c = "Gold"
        try:
            nav = round(float(nav), 4)
        except ValueError:
            continue
        rows.append([code, name, amc, c, sub, nav])
    if len(rows) < 200:
        failed.append(f"MFU: only {len(rows)} schemes parsed")
        return
    past = {}
    for yrs in (1, 3, 5):
        end = today - timedelta(days=round(365.25 * yrs))
        a, b = (end - timedelta(days=5)).strftime("%d-%b-%Y"), end.strftime("%d-%b-%Y")
        try:
            past[yrs] = parse_hist(amfi_get(AMFI_HIST.format(a=a, b=b), ua))
        except Exception as e:
            failed.append(f"MFU hist {yrs}y: {e}")
            past[yrs] = {}
    out = []
    for code, name, amc, c, sub, nav in rows:
        rr = []
        for yrs in (1, 3, 5):
            old = past[yrs].get(code)
            rr.append(round(((nav / old) ** (1 / yrs) - 1) * 100, 2) if old else None)
        out.append([code, name, amc, c, sub, nav] + rr)
    data["mfu"] = out
    data["mfuAsOf"] = today.strftime("%Y-%m-%d")
    failed.append(f"MFU ok: {len(out)} schemes, with 1Y {sum(1 for r in out if r[6] is not None)}, 3Y {sum(1 for r in out if r[7] is not None)}, 5Y {sum(1 for r in out if r[8] is not None)}")


# ---------- batch prices ----------
def batch_quotes(items, tickers, failed):
    """Fill p, pc and weekly w for many tickers with two downloads. Returns names that failed."""
    left = []
    try:
        dy = yf.download(tickers, period="10d", interval="1d", auto_adjust=False, group_by="ticker", threads=True, progress=False)
        wk = yf.download(tickers, period="1y", interval="1wk", auto_adjust=False, group_by="ticker", threads=True, progress=False)
    except Exception as e:
        failed.append(f"batch: {e}")
        return list(zip(items, tickers))
    for it, t in zip(items, tickers):
        try:
            c = [x for x in (clean(v) for v in dy[t]["Close"].tolist()) if x is not None]
            w = [x for x in (clean(v) for v in wk[t]["Close"].tolist()) if x is not None]
            if len(c) < 2:
                raise ValueError("no closes")
            it["p"], it["pc"] = c[-1], c[-2]
            if len(w) >= 20:
                w[-1] = c[-1]
                it["w"] = w[-52:]
            elif "w" not in it:
                raise ValueError("short history")
        except Exception:
            left.append((it, t))
    return left


def main():
    data = json.loads(PATH.read_text())
    ok, failed = 0, []
    today = datetime.now(IST).strftime("%Y-%m-%d")
    long_due = data.get("longAsOf") != today
    for idx in data["indices"]:
        try:
            refresh_quote(idx, INDEX_TICKERS.get(idx["s"], idx["s"]))
            ok += 1
        except Exception as e:  # keep previous values
            failed.append(f"{idx['s']}: {e}")
    stocks = data["stocks"]
    left = batch_quotes(stocks, [st["s"] + ".NS" for st in stocks], failed)
    ok += len(stocks) - len(left)
    for st, t in left:
        try:
            refresh_quote(st, t)
            ok += 1
        except Exception as e:
            failed.append(f"{st['s']}: {e}")
            st["bad"] = True
    for st in stocks:
        if st.get("p") and st.get("w"):
            st.pop("bad", None)

    def slow(st):
        t = st["s"] + ".NS"
        errs = []
        if long_due or "pe" not in st and "roe" not in st:
            try:
                refresh_fundamentals(st, t)
            except Exception as e:
                errs.append(f"{st['s']} fundamentals: {e}")
        if long_due or "r3" not in st:
            try:
                refresh_long(st, t)
            except Exception as e:
                errs.append(f"{st['s']} long: {e}")
        return errs

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex:
        for errs in ex.map(slow, [st for st in stocks if not st.get("bad")]):
            failed.extend(errs)

    if long_due:
        for idx in data["indices"]:
            if idx["s"] in ("NIFTY", "SENSEX", "BANKNIFTY"):
                try:
                    refresh_long(idx, INDEX_TICKERS[idx["s"]])
                except Exception as e:
                    failed.append(f"{idx['s']} long: {e}")
        data["longAsOf"] = today

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
    for cm in data.get("cmd", []):
        try:
            refresh_quote(cm, cm["y"])
            ok += 1
        except Exception as e:
            failed.append(f"{cm['s']}: {e}")
    try:
        fx, _ = last_two_closes("INR=X")
        if fx:
            data["fx"] = fx
    except Exception as e:
        failed.append(f"USDINR: {e}")

    try:
        ok += mf_refresh(data, failed)
    except Exception as e:
        failed.append(f"MF: {e}")
    try:
        mf_universe(data, failed)
    except Exception as e:
        failed.append(f"MFU: {e}")

    if ok == 0:
        print("Nothing refreshed; leaving file unchanged.", file=sys.stderr)
        sys.exit(1)

    data["log"] = failed[:120]
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
