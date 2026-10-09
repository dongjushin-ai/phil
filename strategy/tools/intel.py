#!/usr/bin/env python3
"""Market intel: one-call research data for a candidate market.

Operator-added 2026-10-09 (dongjushin-ai fork). Stdlib only. Every section
is best-effort: a failed source returns {"error": "..."} and never aborts the
call. Output is compact JSON so it can be pasted into reasoning cheaply.

Subcommands (all print JSON):
  stock TICKER        price, day/pre/after-market move, 60d OHLCV technicals
                      (MA5/20/60, RSI14, 20d realized vol, volume vs 20d avg,
                      gap, 52w range), valuation (P/E, fwd P/E, EPS, div
                      yield, market cap), analyst consensus + target price,
                      peer moves. US tickers (AAPL) or KRX codes (005930).
  earnings TICKER     next report date, quarterly EPS consensus + #ests,
                      last 4 surprises (beat rate, avg surprise %),
                      options-implied move from the nearest-expiry ATM
                      straddle.
  calendar DATE       Nasdaq earnings calendar for YYYY-MM-DD (consensus EPS,
                      #ests, last year's EPS, time of day).
  news QUERY          latest Google News headlines (source, age in hours).
                      --hours N (default 48), --lang en|ko, --n N.
  social TICKER       StockTwits: last ~30 messages, bullish/bearish counts,
                      message velocity (msgs/hour), watcher count.
  pmflow TOKEN_ID     Polymarket price path (1h/6h/24h/7d change) and recent
                      trade flow on that token (buy/sell size, big prints).
  fx                  USD vs KRW, EUR, JPY, CNY, GBP.

Tag convention (for strategy/tools/source_score.py): when a forecast or bet
relied on one of these, put "[src:stock,earnings,news]" (whichever applied)
in its --note / rationale. That is what lets the experiment measure which
data actually improves calibration.
"""
import argparse
import datetime as dt
import email.utils
import json
import math
import statistics
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
TIMEOUT = 15


def _get(url, raw=False):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = r.read().decode("utf-8", "replace")
    return body if raw else json.loads(body)


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:  # best-effort by design
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"}


def _num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _is_kr(t):
    return t.isdigit() and len(t) == 6


def _naver_code(ticker):
    """Resolve a US ticker to Naver's reuters code (AAPL.O, KO.N, ...)."""
    t = ticker.upper()
    for suf in (".O", ".N", ".K", ".A", ""):
        try:
            d = _get(f"https://api.stock.naver.com/stock/{t}{suf}/basic")
            if d.get("reutersCode"):
                return d["reutersCode"], d
        except Exception:
            continue
    raise ValueError(f"no Naver listing for {ticker}")


# ---------- technicals ----------

def _technicals(rows):
    """rows: oldest->newest dicts with o,h,l,c,v."""
    if len(rows) < 5:
        return {"error": "not enough history"}
    c = [r["c"] for r in rows]
    v = [r["v"] for r in rows if r["v"] is not None]
    out = {"last_close": c[-1], "days": len(rows)}

    def ma(n):
        return round(sum(c[-n:]) / n, 4) if len(c) >= n else None
    out.update({"ma5": ma(5), "ma20": ma(20), "ma60": ma(60)})
    rets = [math.log(c[i] / c[i - 1]) for i in range(1, len(c)) if c[i - 1]]
    if len(rets) >= 20:
        out["vol20_annual_pct"] = round(statistics.pstdev(rets[-20:]) * math.sqrt(252) * 100, 2)
        out["daily_sigma_pct"] = round(statistics.pstdev(rets[-20:]) * 100, 3)
    for n in (1, 5, 20):
        if len(c) > n:
            out[f"chg_{n}d_pct"] = round((c[-1] / c[-1 - n] - 1) * 100, 2)
    gains = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))][-14:]
    losses = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))][-14:]
    if len(gains) == 14:
        ag, al = sum(gains) / 14, sum(losses) / 14
        out["rsi14"] = round(100 - 100 / (1 + ag / al), 1) if al else 100.0
    if len(v) >= 21:
        avg = sum(v[-21:-1]) / 20
        out["vol_vs_20d_avg"] = round(v[-1] / avg, 2) if avg else None
        out["last_volume"] = v[-1]
        out["last_dollar_volume"] = round(v[-1] * c[-1])
    if len(rows) >= 2 and rows[-2]["c"]:
        out["last_gap_pct"] = round((rows[-1]["o"] / rows[-2]["c"] - 1) * 100, 2)
    out["range_hi"] = max(r["h"] for r in rows)
    out["range_lo"] = min(r["l"] for r in rows)
    out["recent"] = [{"d": r["d"], "c": r["c"], "v": r["v"]} for r in rows[-5:]]
    return out


def _us_history(code, days=90):
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=days)
    d = _get("https://api.stock.naver.com/chart/foreign/item/"
             f"{code}/day?startDateTime={start:%Y%m%d}0000&endDateTime={end:%Y%m%d}2359")
    return [{"d": r["localDate"], "o": r["openPrice"], "h": r["highPrice"],
             "l": r["lowPrice"], "c": r["closePrice"],
             "v": r.get("accumulatedTradingVolume")} for r in d]


def _kr_history(code, days=90):
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=days)
    d = _get("https://api.stock.naver.com/chart/domestic/item/"
             f"{code}/day?startDateTime={start:%Y%m%d}0000&endDateTime={end:%Y%m%d}2359")
    return [{"d": r["localDate"], "o": r["openPrice"], "h": r["highPrice"],
             "l": r["lowPrice"], "c": r["closePrice"],
             "v": r.get("accumulatedTradingVolume")} for r in d]


# ---------- stock ----------

def _quote_us(code, basic):
    q = {"name": basic.get("stockNameEng") or basic.get("stockName"),
         "exchange": basic.get("stockExchangeName"),
         "close": _num(basic.get("closePrice")),
         "chg_pct": _num(basic.get("fluctuationsRatio")),
         "as_of": basic.get("localTradedAt"),
         "market_status": basic.get("marketStatus"),
         "industry": (basic.get("industryCodeType") or {}).get("industryGroupKor")}
    om = basic.get("overMarketPriceInfo") or {}
    if om.get("overPrice"):
        q["extended"] = {"session": om.get("tradingSessionType"),
                         "price": _num(om.get("overPrice")),
                         "chg_pct": _num(om.get("fluctuationsRatio")),
                         "as_of": om.get("localTradedAt")}
    return q


def _valuation_us(ticker):
    d = _get(f"https://api.nasdaq.com/api/quote/{ticker}/summary?assetclass=stocks")
    sd = (d.get("data") or {}).get("summaryData") or {}
    keep = {"MarketCap", "PERatio", "ForwardPE1Yr", "EarningsPerShare",
            "AnnualizedDividend", "Yield", "ExDividendDate", "DividendPaymentDate",
            "FiftTwoWeekHighLow", "AverageVolume", "ShareVolume", "Beta",
            "TodayHighLow", "PreviousClose", "Sector", "Industry"}
    keep.add("OneYrTarget")
    out = {k: (v or {}).get("value") for k, v in sd.items() if k in keep}
    price = _num(out.get("PreviousClose"))
    su = _safe(_get, f"https://api.nasdaq.com/api/company/{ticker}/earnings-surprise")
    rows = []
    if isinstance(su, dict):
        rows = ((su.get("data") or {}).get("earningsSurpriseTable") or {}).get("rows") or []
    eps = [_num(r.get("eps")) for r in rows[:4]]
    if len(eps) == 4 and None not in eps:
        out["eps_ttm"] = round(sum(eps), 3)
        if price and sum(eps) > 0:
            out["pe_ttm"] = round(price / sum(eps), 2)
    fc = _safe(_get, f"https://api.nasdaq.com/api/analyst/{ticker}/earnings-forecast")
    yr = []
    if isinstance(fc, dict):
        yr = ((fc.get("data") or {}).get("yearlyForecast") or {}).get("rows") or []
    if yr and price:
        f = _num(yr[0].get("consensusEPSForecast"))
        if f and f > 0:
            out["eps_fwd_fy"] = f
            out["pe_fwd_fy"] = round(price / f, 2)
            out["fwd_fy"] = yr[0].get("fiscalEnd")
    return out


def _analysts_us(ticker, code):
    out = {}
    tp = _safe(_get, f"https://api.nasdaq.com/api/analyst/{ticker}/targetprice")
    if isinstance(tp, dict) and tp.get("data"):
        co = tp["data"].get("consensusOverview") or {}
        out["nasdaq_target"] = co
    rt = _safe(_get, f"https://api.nasdaq.com/api/analyst/{ticker}/ratings")
    if isinstance(rt, dict) and rt.get("data"):
        out["mean_rating"] = rt["data"].get("meanRatingType")
        out["recent_up_downgrades"] = (rt["data"].get("upgradesDowngrades") or [])[:5]
    integ = _safe(_get, f"https://api.stock.naver.com/stock/{code}/integration")
    if isinstance(integ, dict) and "consensusInfo" in integ:
        ci = integ.get("consensusInfo") or {}
        out["naver_consensus"] = {k: ci.get(k) for k in
                                  ("createDate", "recommMean", "priceTargetMean",
                                   "priceTargetHigh", "priceTargetLow")}
        peers = ((integ.get("industryCompareInfo") or {}).get("globalStocks") or [])[:6]
        out["peers"] = [{"name": p.get("stockNameEng") or p.get("stockName"),
                         "code": p.get("reutersCode"),
                         "chg_pct": _num(p.get("fluctuationsRatio"))} for p in peers]
    return out


def _stock_kr(code):
    b = _get(f"https://m.stock.naver.com/api/stock/{code}/basic")
    q = {"name": b.get("stockName"), "close": _num(b.get("closePrice")),
         "chg_pct": _num(b.get("fluctuationsRatio")),
         "as_of": b.get("localTradedAt"), "market_status": b.get("marketStatus")}
    integ = _safe(_get, f"https://m.stock.naver.com/api/stock/{code}/integration")
    val = {}
    if isinstance(integ, dict):
        for x in integ.get("totalInfos") or []:
            val[x.get("code") or x.get("key")] = x.get("value")
        ci = integ.get("consensusInfo") or {}
        if ci:
            val["consensus"] = ci
        val["peers"] = [{"name": p.get("stockName"), "code": p.get("itemCode"),
                         "chg_pct": _num(p.get("fluctuationsRatio"))}
                        for p in (integ.get("industryCompareInfo") or [])[:6]]
    hist = _safe(_kr_history, code)
    return {"ticker": code, "quote": q, "valuation": val,
            "technicals": _technicals(hist) if isinstance(hist, list) else hist}


def cmd_stock(t):
    if _is_kr(t):
        return _stock_kr(t)
    t = t.upper()
    code, basic = _naver_code(t)
    hist = _safe(_us_history, code)
    return {"ticker": t, "naver_code": code,
            "quote": _quote_us(code, basic),
            "valuation": _safe(_valuation_us, t),
            "technicals": _technicals(hist) if isinstance(hist, list) else hist,
            "analysts": _safe(_analysts_us, t, code)}


# ---------- earnings ----------

def _implied_move(ticker, n_exp=3):
    """ATM straddle / spot for the nearest n_exp expiries. For an earnings
    question, read the first expiry that lands AFTER the report date."""
    d = _get(f"https://api.nasdaq.com/api/quote/{ticker}/option-chain"
             "?assetclass=stocks&limit=400&fromdate=all&todate=undefined&excode=oprac"
             "&callput=callput&money=at&type=all")
    data = d.get("data") or {}
    rows = (data.get("table") or {}).get("rows") or []
    last = _num((data.get("lastTrade") or "").split("$")[-1].split(" ")[0])
    if not last:
        return {"error": "no spot"}
    groups, exp = {}, None
    for r in rows:
        if r.get("expirygroup"):
            exp = r["expirygroup"]
            continue
        k = _num(r.get("strike"))
        q = [_num(r.get(x)) for x in ("c_Bid", "c_Ask", "p_Bid", "p_Ask")]
        if exp is None or k is None or None in q:
            continue
        st = (q[0] + q[1]) / 2 + (q[2] + q[3]) / 2
        cur = groups.get(exp)
        if cur is None or abs(k - last) < abs(cur[0] - last):
            groups[exp] = (k, st)
    out = [{"expiry": e, "strike": k, "straddle": round(st, 3),
            "implied_move_pct": round(st / last * 100, 2)}
           for e, (k, st) in list(groups.items())[:n_exp]]
    return {"spot": last, "by_expiry": out} if out else {"error": "no ATM straddle found"}


def cmd_earnings(t):
    t = t.upper()
    out = {"ticker": t}
    info = _safe(_get, f"https://api.nasdaq.com/api/quote/{t}/info?assetclass=stocks")
    if isinstance(info, dict) and info.get("data"):
        notes = (info["data"].get("notifications") or [])
        out["notifications"] = [e.get("eventName") or e.get("headline")
                                for n in notes for e in (n.get("eventTypes") or [])][:5]
    fc = _safe(_get, f"https://api.nasdaq.com/api/analyst/{t}/earnings-forecast")
    if isinstance(fc, dict) and fc.get("data"):
        q = ((fc["data"].get("quarterlyForecast") or {}).get("rows") or [])[:2]
        y = ((fc["data"].get("yearlyForecast") or {}).get("rows") or [])[:2]
        out["eps_consensus_quarterly"] = q
        out["eps_consensus_yearly"] = y
    else:
        out["eps_consensus_quarterly"] = fc
    su = _safe(_get, f"https://api.nasdaq.com/api/company/{t}/earnings-surprise")
    if isinstance(su, dict) and su.get("data"):
        rows = ((su["data"].get("earningsSurpriseTable") or {}).get("rows") or [])
        out["last_surprises"] = rows
        pcts = [_num(r.get("percentageSurprise")) for r in rows]
        pcts = [p for p in pcts if p is not None]
        if pcts:
            out["beat_rate"] = round(sum(p > 0 for p in pcts) / len(pcts), 2)
            out["avg_surprise_pct"] = round(sum(pcts) / len(pcts), 2)
    else:
        out["last_surprises"] = su
    out["options_implied_move"] = _safe(_implied_move, t)
    return out


def cmd_calendar(date):
    d = _get(f"https://api.nasdaq.com/api/calendar/earnings?date={date}")
    rows = ((d.get("data") or {}).get("rows") or [])
    return {"date": date, "n": len(rows), "rows": [
        {k: r.get(k) for k in ("symbol", "name", "time", "marketCap", "epsForecast",
                               "noOfEsts", "lastYearEPS", "fiscalQuarterEnding")}
        for r in rows]}


# ---------- news / social ----------

def cmd_news(query, hours=48, lang="en", n=15):
    if lang == "ko":
        params = {"q": f"{query} when:{max(1, hours // 24)}d", "hl": "ko", "gl": "KR", "ceid": "KR:ko"}
    else:
        params = {"q": f"{query} when:{max(1, hours // 24)}d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    raw = _get("https://news.google.com/rss/search?" + urllib.parse.urlencode(params), raw=True)
    root = ET.fromstring(raw)
    now = dt.datetime.now(dt.timezone.utc)
    items = []
    for it in root.iter("item"):
        pub = it.findtext("pubDate")
        try:
            ts = email.utils.parsedate_to_datetime(pub)
            age = round((now - ts).total_seconds() / 3600, 1)
        except Exception:
            age = None
        if age is not None and age > hours:
            continue
        src = it.find("source")
        items.append({"age_h": age, "title": it.findtext("title"),
                      "source": src.text if src is not None else None,
                      "link": it.findtext("link")})
    items.sort(key=lambda x: (x["age_h"] is None, x["age_h"]))
    return {"query": query, "lang": lang, "hours": hours, "count": len(items),
            "items": items[:n]}


def cmd_social(t):
    d = _get(f"https://api.stocktwits.com/api/2/streams/symbol/{t.upper()}.json")
    msgs = d.get("messages") or []
    bull = sum(1 for m in msgs if ((m.get("entities") or {}).get("sentiment") or {}).get("basic") == "Bullish")
    bear = sum(1 for m in msgs if ((m.get("entities") or {}).get("sentiment") or {}).get("basic") == "Bearish")
    ts = []
    for m in msgs:
        try:
            ts.append(dt.datetime.strptime(m["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
        except Exception:
            pass
    span_h = (max(ts) - min(ts)).total_seconds() / 3600 if len(ts) > 1 else None
    sym = d.get("symbol") or {}
    return {"ticker": t.upper(), "watchers": sym.get("watchlist_count"),
            "n_msgs": len(msgs), "bullish": bull, "bearish": bear,
            "bull_share_tagged": round(bull / (bull + bear), 2) if bull + bear else None,
            "msgs_per_hour": round(len(msgs) / span_h, 1) if span_h else None,
            "sample": [(m.get("body") or "")[:140] for m in msgs[:5]]}


# ---------- polymarket flow ----------

def cmd_pmflow(token):
    out = {"token_id": token}
    h = _safe(_get, "https://clob.polymarket.com/prices-history?"
              + urllib.parse.urlencode({"market": token, "interval": "1w", "fidelity": 60}))
    pts = (h or {}).get("history") if isinstance(h, dict) else None
    if pts:
        now = pts[-1]["t"]
        last = pts[-1]["p"]
        out["last"] = last

        def at(sec):
            cand = [p for p in pts if p["t"] <= now - sec]
            return cand[-1]["p"] if cand else pts[0]["p"]
        for lab, sec in (("1h", 3600), ("6h", 21600), ("24h", 86400), ("7d", 604800)):
            out[f"chg_{lab}"] = round(last - at(sec), 4)
        out["hi_7d"] = max(p["p"] for p in pts)
        out["lo_7d"] = min(p["p"] for p in pts)
    else:
        out["history"] = h
    m = _safe(_get, f"https://clob.polymarket.com/markets-by-token/{token}")
    cid = m.get("condition_id") if isinstance(m, dict) else None
    if cid:
        tr = _safe(_get, "https://data-api.polymarket.com/trades?"
                   + urllib.parse.urlencode({"market": cid, "limit": 500}))
    else:
        tr = {"error": "condition id lookup failed"}
    if isinstance(tr, list):
        tr = [x for x in tr if str(x.get("asset")) == str(token)]
        buy = sum(_num(x.get("size")) or 0 for x in tr if x.get("side") == "BUY")
        sell = sum(_num(x.get("size")) or 0 for x in tr if x.get("side") == "SELL")
        notional = [(_num(x.get("size")) or 0) * (_num(x.get("price")) or 0) for x in tr]
        big = sorted(zip(notional, tr), key=lambda z: -z[0])[:5]
        out["trades"] = {"n": len(tr), "buy_size": round(buy, 1), "sell_size": round(sell, 1),
                         "buy_share": round(buy / (buy + sell), 2) if buy + sell else None,
                         "notional_usd": round(sum(notional), 1),
                         "biggest": [{"usd": round(u, 1), "side": x.get("side"),
                                      "price": x.get("price"),
                                      "ts": x.get("timestamp")} for u, x in big]}
    else:
        out["trades"] = tr
    return out


def cmd_fx():
    d = _get("https://open.er-api.com/v6/latest/USD")
    r = d.get("rates") or {}
    return {"base": "USD", "as_of": d.get("time_last_update_utc"),
            "rates": {k: r.get(k) for k in ("KRW", "EUR", "JPY", "CNY", "GBP")}}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name in ("stock", "earnings", "social"):
        sp.add_parser(name).add_argument("ticker")
    sp.add_parser("calendar").add_argument("date")
    n = sp.add_parser("news")
    n.add_argument("query")
    n.add_argument("--hours", type=int, default=48)
    n.add_argument("--lang", default="en", choices=["en", "ko"])
    n.add_argument("--n", type=int, default=15)
    sp.add_parser("pmflow").add_argument("token_id")
    sp.add_parser("fx")
    a = ap.parse_args()
    fn = {
        "stock": lambda: cmd_stock(a.ticker),
        "earnings": lambda: cmd_earnings(a.ticker),
        "social": lambda: cmd_social(a.ticker),
        "calendar": lambda: cmd_calendar(a.date),
        "news": lambda: cmd_news(a.query, a.hours, a.lang, a.n),
        "pmflow": lambda: cmd_pmflow(a.token_id),
        "fx": cmd_fx,
    }[a.cmd]
    print(json.dumps(_safe(fn), ensure_ascii=False, separators=(",", ":"), default=str))


if __name__ == "__main__":
    main()
