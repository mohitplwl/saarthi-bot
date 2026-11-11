#!/usr/bin/env python3
"""
Saarthi Bot: a personal Nifty trade assistant.

What it does on every trading day, with no input from you:
  1. Logs in to Angel One SmartAPI (free) and pulls live Nifty, India VIX and option prices.
  2. Runs seven checks after each 15-minute candle between 9:45 and 14:00 IST.
  3. When every check passes, sends a Telegram message with Yes / No buttons.
  4. Yes -> paper mode: logs the trade at live prices and manages the exit for you.
            live mode : shows the two orders to place by hand, then tracks the position.
  5. Watches the open position every 5 minutes and tells you when to get out.

It never places an order. SEBI rules require a registered static IP for API orders,
and this script runs fine without one because it only reads market data.

Secrets come from environment variables (GitHub Actions secrets):
  ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PIN, ANGEL_TOTP_SECRET, TG_TOKEN, TG_CHAT_ID, STATE_KEY
"""
import base64, datetime as dt, hashlib, json, os, subprocess, sys, time

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NIFTY, VIX = "99926000", "99926017"          # SmartAPI index tokens
ROOT = "https://apiconnect.angelone.in"
MASTER_URLS = ["https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
               "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"]
STATE_FILE = "state.enc"

DEFAULTS = {
    "settings": {"capital": 100000, "risk_pct": 2.0, "mode": "paper", "brokerage": 20, "stt": 0.15, "txn": 0.0355},
    "events": [],        # dates (YYYY-MM-DD) of major events; no new trade on the day or the day before
    "paused": False,
    "pos": None,
    "journal": [],
    "today": {"date": "", "phase": "scan", "watch_sent": False, "offer": None},
    "tg_offset": 0,
}

# ----------------------------------------------------------------------------- helpers

def inr(n, signed=False):
    neg = n < 0
    s = str(abs(int(round(n))))
    if len(s) > 3:
        head, tail, parts = s[:-3], s[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        if head: parts.insert(0, head)
        s = ",".join(parts + [tail])
    sign = "−" if neg else ("+" if signed and n > 0 else "")
    return f"{sign}₹{s}"

def pt(x, d=2):
    return f"{x:,.{d}f}".rstrip("0").rstrip(".") if d else f"{x:,.0f}"

def d_(s): return dt.date.fromisoformat(s)
def nice(s): return d_(s).strftime("%a %d %b")
def prev_weekday(d):
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5: d -= dt.timedelta(days=1)
    return d
def next_weekday(d):
    d += dt.timedelta(days=1)
    while d.weekday() >= 5: d += dt.timedelta(days=1)
    return d

def costs(buy_px, sell_px, exit_long, exit_short, lot, s):
    """Round-trip charges for a two-leg debit spread: four orders."""
    brokerage = 4 * s["brokerage"]
    turnover = (buy_px + sell_px + exit_long + exit_short) * lot
    txn, sebi = turnover * s["txn"] / 100, turnover * 0.000001
    stt = (sell_px + exit_long) * lot * s["stt"] / 100       # charged on sells
    stamp = (buy_px + exit_short) * lot * 0.00003            # charged on buys
    return brokerage + txn + sebi + stt + stamp + 0.18 * (brokerage + txn + sebi)

def stats(trades):
    n = len(trades); eq = peak = dd = 0.0
    for t in trades:
        eq += t["net"]; peak = max(peak, eq); dd = max(dd, peak - eq)
    wins = [t for t in trades if t["net"] > 0]
    return {"n": n, "total": eq, "avg": eq / n if n else 0.0, "win": 100 * len(wins) / n if n else 0.0, "dd": dd}

# ----------------------------------------------------------------------------- the rules

def limit_gate(journal, mode, capital, today):
    tr = [t for t in journal if t["mode"] == mode]
    if any(t["entry_date"] == today for t in tr):
        return "fail", "You’ve already taken today’s one trade."
    td = d_(today); ws = (td - dt.timedelta(days=td.weekday())).isoformat(); mo = today[:7]
    wk = sum(t["net"] for t in tr if t["exit_date"] >= ws)
    mn = sum(t["net"] for t in tr if t["exit_date"][:7] == mo)
    if mn <= -0.10 * capital: return "fail", f"Down {inr(-mn)} this month, past the 10% limit. No new trades until next month."
    if wk <= -0.05 * capital: return "fail", f"Down {inr(-wk)} this week, past the 5% limit. No new trades until Monday."
    last3 = tr[-3:]
    if len(last3) == 3 and all(t["net"] < 0 for t in last3) and td <= next_weekday(d_(last3[-1]["exit_date"])):
        return "fail", "Three losses in a row. Sitting out one trading day."
    return "pass", f"Week {inr(wk, True)}, month {inr(mn, True)}. Inside limits."

def evaluate(x, state, today, mins):
    """x: prev_close, sma20, sma50, vix, vix_chg, open, or_high, or_low, now. Returns verdict dict."""
    s, g = state["settings"], []
    add = lambda name, st, text: g.append((name, st, text))
    # 1 clock
    if mins < 585: add("Clock", "wait", "Entries open at 9:45.")
    elif mins > 840: add("Clock", "fail", "No valid breakout before the 14:00 cut-off.")
    else: add("Clock", "pass", "Inside the 9:45–14:00 window.")
    # 2 limits
    add("Loss limits", *limit_gate(state["journal"], s["mode"], s["capital"], today))
    # 3 events
    ev = [e for e in state["events"] if e in (today, next_weekday(d_(today)).isoformat())]
    add("Event risk", *(("fail", f"Major event marked for {nice(ev[0])}.") if ev else ("pass", "No event marked.")))
    # 4 volatility
    if x["vix_chg"] >= 10: add("Volatility", "fail", f"VIX is up {x['vix_chg']:.1f}% today. Fear is spiking.")
    elif x["vix"] > 22: add("Volatility", "fail", f"VIX at {x['vix']:.1f} is above 22.")
    else: add("Volatility", "pass", f"VIX {x['vix']:.1f}, {x['vix_chg']:+.1f}% today.")
    # 5 opening
    gap = (x["open"] - x["prev_close"]) / x["prev_close"] * 100
    orw = (x["or_high"] - x["or_low"]) / x["open"] * 100
    if abs(gap) > 0.7: add("Opening", "fail", f"Nifty gapped {gap:+.2f}%. The first move after a big gap is unreliable.")
    elif orw > 1.2: add("Opening", "fail", f"The first 30 minutes swung {orw:.2f}%. Too wild.")
    else: add("Opening", "pass", f"Gap {gap:+.2f}%, opening range {x['or_high'] - x['or_low']:.0f} points.")
    # 6 trend
    direction = None
    if x["now"] > x["sma20"] > x["sma50"]: direction = "bull"; add("Trend", "pass", "Up-trend: price above 20-day average, above 50-day.")
    elif x["now"] < x["sma20"] < x["sma50"]: direction = "bear"; add("Trend", "pass", "Down-trend: price below 20-day average, below 50-day.")
    else: add("Trend", "fail", "Mixed trend: price and the two averages don’t line up.")
    # 7 breakout
    if direction:
        chase = 0.0035 * x["now"]; hi, lo, now = x["or_high"], x["or_low"], x["now"]
        if direction == "bull":
            if now > hi: add("Breakout", *(("fail", f"Already {now - hi:.0f} points past {pt(hi)}. Not chasing.") if now - hi > chase
                                           else ("pass", f"Closed above the opening high of {pt(hi)}.")))
            elif now < lo: add("Breakout", "fail", f"Broke down through {pt(lo)}, against the up-trend.")
            else: add("Breakout", "wait", f"Waiting for a 15-minute close above {pt(hi)}.")
        else:
            if now < lo: add("Breakout", *(("fail", f"Already {lo - now:.0f} points past {pt(lo)}. Not chasing.") if lo - now > chase
                                           else ("pass", f"Closed below the opening low of {pt(lo)}.")))
            elif now > hi: add("Breakout", "fail", f"Broke up through {pt(hi)}, against the down-trend.")
            else: add("Breakout", "wait", f"Waiting for a 15-minute close below {pt(lo)}.")
    fails = [t for _, st, t in g if st == "fail"]; waits = [t for n, st, t in g if st == "wait"]
    if fails: return {"k": "stop", "reason": fails[0], "gates": g, "dir": direction}
    if waits: return {"k": "wait", "reason": waits[-1], "gates": g, "dir": direction}
    return {"k": "go", "reason": "All seven checks pass.", "gates": g, "dir": direction}

def spread_numbers(buy_px, sell_px, width, lot, s):
    """Sizing and sanity checks for one candidate spread. Returns dict with 'ok' and 'why'."""
    debit = buy_px - sell_px
    r = {"debit": debit, "ok": False, "why": ""}
    if debit <= 0: r["why"] = "prices don’t make sense"; return r
    cost = costs(buy_px, sell_px, buy_px, sell_px, lot, s)
    r.update(cost=cost, max_loss=debit * lot + cost, max_profit=(width - debit) * lot - cost,
             budget=s["capital"] * s["risk_pct"] / 100, stop_val=0.5 * debit, target_val=debit + 0.7 * (width - debit))
    r["rr"] = r["max_profit"] / r["max_loss"]
    if r["max_loss"] > r["budget"]: r["why"] = f"one lot can lose {inr(r['max_loss'])}, above the {inr(r['budget'])} limit"
    elif debit > 0.6 * width: r["why"] = "costs more than 60% of its width"
    elif r["rr"] < 0.8: r["why"] = "reward is under 0.8 of the risk after charges"
    else: r["ok"] = True
    return r

def leg_liquid(q):
    bid, ask = q.get("bid") or 0, q.get("ask") or 0
    return bid > 0 and ask > bid * 0.999 and (ask - bid) <= max(1.5, 0.03 * (ask + bid) / 2)

def pick_spread(direction, spot, strikes, quote_fn, lot, s, step=50):
    """Try a few strike pairs near the money and return the first that fits. quote_fn(strike)->{bid,ask,ltp}."""
    sign = 1 if direction == "bull" else -1
    atm = round(spot / step) * step
    tried = []
    for shift, width in ((1, 50), (0, 50), (1, 100), (2, 50)):
        bk, sk = atm + sign * shift * step, atm + sign * (shift * step + width)
        if bk not in strikes or sk not in strikes: continue
        bq, sq = quote_fn(bk), quote_fn(sk)
        if not (bq and sq and leg_liquid(bq) and leg_liquid(sq)):
            tried.append(f"{bk:.0f}/{sk:.0f}: bid–ask gap too wide"); continue
        n = spread_numbers(bq["ask"], sq["bid"], width, lot, s)
        if n["ok"]:
            n.update(buy_k=bk, sell_k=sk, width=width, buy_px=bq["ask"], sell_px=sq["bid"]); return n, tried
        tried.append(f"{bk:.0f}/{sk:.0f}: {n['why']}")
    return None, tried

def monitor(pos, today, mins, spot_close, value):
    """Decide hold or exit. spot_close is the latest 15-minute close (or None between candles)."""
    if today >= pos["expiry"] or today > pos["time_stop"] or (today == pos["time_stop"] and mins >= 900):
        return "exit", "Time stop", "Past the time stop. Never carry this into expiry."
    if spot_close is not None and ((pos["dir"] == "bull" and spot_close < pos["inval"]) or (pos["dir"] == "bear" and spot_close > pos["inval"])):
        return "exit", "Idea invalidated", f"Nifty closed a 15-minute candle {'below' if pos['dir'] == 'bull' else 'above'} {pt(pos['inval'])}."
    if value is not None:
        if value <= pos["stop_val"]: return "exit", "Value stop", f"Spread is worth {pt(value)}, at or below the stop of {pt(pos['stop_val'])}."
        if value >= pos["target_val"]: return "exit", "Target hit", f"Spread is worth {pt(value)}, at or above the target of {pt(pos['target_val'])}."
    return "hold", "", ""

# ----------------------------------------------------------------------------- Angel One (data only)

class Angel:
    def __init__(self, api_key, client, pin, totp_secret):
        import requests
        self.rq, self.api_key, self.client, self.pin, self.totp_secret = requests.Session(), api_key, client, pin, totp_secret
        self.jwt, self._chain = None, None

    def _headers(self):
        h = {"Content-Type": "application/json", "Accept": "application/json", "X-UserType": "USER", "X-SourceID": "WEB",
             "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1", "X-MACAddress": "00:00:00:00:00:00",
             "X-PrivateKey": self.api_key}
        if self.jwt: h["Authorization"] = "Bearer " + self.jwt
        return h

    def login(self):
        import pyotp
        self.jwt = None
        for attempt in (1, 2):
            r = self.rq.post(ROOT + "/rest/auth/angelbroking/user/v1/loginByPassword", headers=self._headers(), timeout=20,
                             json={"clientcode": self.client, "password": self.pin, "totp": pyotp.TOTP(self.totp_secret).now()}).json()
            if r.get("status") and r.get("data"): break
            if attempt == 2: raise RuntimeError("Angel One login failed: " + str(r.get("message"))[:80])
            time.sleep(3)
        self.jwt = r["data"]["jwtToken"].replace("Bearer ", "")

    def _post(self, path, body, retry=True):
        if not self.jwt: self.login()
        resp = self.rq.post(ROOT + path, headers=self._headers(), json=body, timeout=20)
        try: r = resp.json()
        except Exception: r = {"status": False, "message": f"HTTP {resp.status_code}"}
        if not r.get("status"):
            if retry:
                time.sleep(1.5); self.login(); return self._post(path, body, retry=False)
            raise RuntimeError("Angel One: " + str(r.get("message") or r.get("errorcode"))[:80])
        return r.get("data")

    def candles(self, token, interval, start, end, exchange="NSE"):
        data = self._post("/rest/secure/angelbroking/historical/v1/getCandleData",
                          {"exchange": exchange, "symboltoken": token, "interval": interval,
                           "fromdate": start.strftime("%Y-%m-%d %H:%M"), "todate": end.strftime("%Y-%m-%d %H:%M")})
        return [{"t": c[0], "o": c[1], "h": c[2], "l": c[3], "c": c[4]} for c in (data or [])]

    def quotes(self, nse=(), nfo=()):
        ex = {}
        if nse: ex["NSE"] = list(nse)
        if nfo: ex["NFO"] = list(nfo)
        time.sleep(1.1)                                         # the quote API allows about one call a second
        data = self._post("/rest/secure/angelbroking/market/v1/quote", {"mode": "FULL", "exchangeTokens": ex}) or {}
        out = {}
        for q in data.get("fetched", []):
            depth = q.get("depth") or {}
            bid = (depth.get("buy") or [{}])[0].get("price") or 0
            ask = (depth.get("sell") or [{}])[0].get("price") or 0
            out[str(q["symbolToken"])] = {"ltp": q.get("ltp"), "open": q.get("open"), "close": q.get("close"), "bid": bid, "ask": ask}
        return out

    def chain(self):
        """Nifty index options from the public instrument file: {expiry_iso: {(strike, 'CE'|'PE'): (token, symbol, lot)}}"""
        if self._chain: return self._chain
        rows = None
        for url in MASTER_URLS:
            try: rows = self.rq.get(url, timeout=90).json(); break
            except Exception: continue
        if rows is None: raise RuntimeError("Could not download the instrument list")
        ch = {}
        for r in rows:
            if r.get("name") != "NIFTY" or r.get("exch_seg") != "NFO" or r.get("instrumenttype") != "OPTIDX": continue
            try: exp = dt.datetime.strptime(r["expiry"].title(), "%d%b%Y").date().isoformat()
            except Exception: continue
            ch.setdefault(exp, {})[(float(r["strike"]) / 100, r["symbol"][-2:])] = (str(r["token"]), r["symbol"], int(float(r["lotsize"])))
        if not ch: raise RuntimeError("No Nifty options found in the instrument list")
        self._chain = ch
        return ch

# ----------------------------------------------------------------------------- Telegram

class Telegram:
    def __init__(self, token, chat_id):
        import requests
        self.rq, self.base, self.chat = requests.Session(), f"https://api.telegram.org/bot{token}/", str(chat_id)

    def _call(self, method, http_timeout=30, **kw):
        try: return self.rq.post(self.base + method, json=kw, timeout=http_timeout).json()
        except Exception: return {"ok": False}

    def send(self, text, buttons=None):
        kw = {"chat_id": self.chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons: kw["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}
        r = self._call("sendMessage", **kw)
        return (r.get("result") or {}).get("message_id")

    def clear_buttons(self, message_id):
        if message_id: self._call("editMessageReplyMarkup", chat_id=self.chat, message_id=message_id, reply_markup={"inline_keyboard": []})

    def poll(self, offset, wait):
        """Long-poll. Returns (new_offset, [('cb', data, message_id) | ('cmd', text, None)]) from your chat only."""
        r = self._call("getUpdates", http_timeout=wait + 10, offset=offset, timeout=wait, allowed_updates=["message", "callback_query"])
        out = []
        for u in r.get("result", []) if r.get("ok") else []:
            offset = max(offset, u["update_id"] + 1)
            cq, msg = u.get("callback_query"), u.get("message")
            if cq and str(cq.get("message", {}).get("chat", {}).get("id")) == self.chat:
                self._call("answerCallbackQuery", callback_query_id=cq["id"])
                out.append(("cb", cq.get("data", ""), cq["message"].get("message_id")))
            elif msg and str(msg.get("chat", {}).get("id")) == self.chat and msg.get("text"):
                out.append(("cmd", msg["text"].strip(), None))
        return offset, out

# ----------------------------------------------------------------------------- state (encrypted at rest)

class Store:
    def __init__(self, path, key, git=False):
        from cryptography.fernet import Fernet
        self.path, self.git = path, git
        self.f = Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))

    def load(self):
        state = json.loads(json.dumps(DEFAULTS))
        if os.path.exists(self.path):
            raw = json.loads(self.f.decrypt(open(self.path, "rb").read()))   # wrong STATE_KEY fails loudly, by design
            for k, v in raw.items():
                if isinstance(v, dict) and isinstance(state.get(k), dict): state[k].update(v)
                else: state[k] = v
        return state

    def save(self, state):
        open(self.path, "wb").write(self.f.encrypt(json.dumps(state).encode()))
        if not self.git: return
        run = lambda *a: subprocess.run(["git", *a], capture_output=True)
        run("add", self.path)
        if run("commit", "-m", "update state").returncode == 0 and run("push").returncode != 0:
            run("pull", "--rebase", "-X", "theirs"); run("push")

# ----------------------------------------------------------------------------- the bot

HELP = ("<b>Commands</b>\n/status – position, results, settings\n/mode paper | live\n/capital 100000\n/risk 2\n"
        "/event 2026-12-05 – mark or unmark a major event day\n/pause and /resume – stop or restart new trade alerts\n"
        "/fill 157.5 135 – correct the entry prices of the open trade\n/setnet -850 – correct the last result from your contract note")

class Bot:
    def __init__(self, angel, tg, store, now_fn=lambda: dt.datetime.now(IST)):
        self.a, self.tg, self.store, self.now = angel, tg, store, now_fn
        self.s = store.load()
        self.day = {}                      # prev_close, sma20, sma50, or_high, or_low (fetched once a day)
        self.scan_slot = self.pos_slot = self.inval_slot = None
        self.last_error = 0
        self.done = False

    # -- small utilities
    def save(self): self.store.save(self.s)
    def clock(self):
        n = self.now(); return n, n.date().isoformat(), n.hour * 60 + n.minute, n.hour * 3600 + n.minute * 60 + n.second
    def trades(self): return [t for t in self.s["journal"] if t["mode"] == self.s["settings"]["mode"]]
    def summary(self):
        st = stats(self.trades()); m = self.s["settings"]["mode"]
        return f"{m.capitalize()} record: {st['n']} trade{'s' if st['n'] != 1 else ''}, net {inr(st['total'], True)}, average {inr(st['avg'], True)}, won {st['win']:.0f}%, deepest drawdown {inr(st['dd'])}."

    def roll_day(self, today):
        if self.s["today"]["date"] != today:
            self.s["today"] = {"date": today, "phase": "scan", "watch_sent": False, "offer": None}
            self.save()

    # -- market data
    def load_day(self, now, today):
        daily = self.a.candles(NIFTY, "ONE_DAY", now - dt.timedelta(days=120), now)
        closes = [c["c"] for c in daily if c["t"][:10] < today]
        if len(closes) < 50: raise RuntimeError("Not enough daily history for the averages")
        start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        intra = [c for c in self.a.candles(NIFTY, "FIFTEEN_MINUTE", start, now) if c["t"][:10] == today and c["t"][11:16] in ("09:15", "09:30")]
        if len(intra) < 2: return False                         # no candles today: market holiday
        self.day = {"prev_close": closes[-1], "sma20": sum(closes[-20:]) / 20, "sma50": sum(closes[-50:]) / 50,
                    "or_high": max(c["h"] for c in intra), "or_low": min(c["l"] for c in intra)}
        return True

    def snapshot(self):
        q = self.a.quotes(nse=[NIFTY, VIX]); n, v = q[NIFTY], q[VIX]
        return dict(self.day, now=n["ltp"], open=n["open"], vix=v["ltp"], vix_chg=(v["ltp"] - v["close"]) / v["close"] * 100)

    def leg_quotes(self, pos):
        q = self.a.quotes(nse=[NIFTY], nfo=[pos["buy_token"], pos["sell_token"]])
        return q[NIFTY]["ltp"], q[pos["buy_token"]], q[pos["sell_token"]]

    # -- scanning for an entry
    def scan(self, now, today, mins):
        t = self.s["today"]
        if self.s["paused"]:
            t["phase"] = "done"; self.save(); return
        if not self.day and not self.load_day(now, today):
            t["phase"] = "done"; self.save(); print("no candles today; treating as a holiday"); return
        x = self.snapshot(); v = evaluate(x, self.s, today, mins)
        if v["k"] == "stop":
            self.tg.send(f"🔴 <b>No trade today</b>\n{v['reason']}")
            t["phase"] = "done"; self.save()
        elif v["k"] == "wait":
            if not t["watch_sent"]:
                self.tg.send(f"🟡 <b>Watching</b>\n{'Up' if v['dir'] == 'bull' else 'Down'}-trend day. {v['reason']} I’ll message you if it happens before 14:00.")
                t["watch_sent"] = True; self.save()
        else:
            self.offer(v, x, today)

    def offer(self, v, x, today):
        s, t = self.s["settings"], self.s["today"]
        chain = self.a.chain()
        exps = sorted(e for e in chain if (d_(e) - d_(today)).days >= 3)
        if not exps: raise RuntimeError("No suitable expiry found")
        exp, typ = exps[0], ("CE" if v["dir"] == "bull" else "PE")
        legs = {k: val for (k, ty), val in chain[exp].items() if ty == typ}
        near = sorted(k for k in legs if abs(k - x["now"]) <= 400)
        qs = self.a.quotes(nfo=[legs[k][0] for k in near])
        lot = legs[near[0]][2]
        pick, tried = pick_spread(v["dir"], x["now"], set(near), lambda k: qs.get(legs[k][0]), lot, s)
        if not pick:
            self.tg.send("🔴 <b>No trade today</b>\nThe checks passed but no spread fits your limits:\n" + "\n".join("• " + w for w in tried))
            t["phase"] = "done"; self.save(); return
        o = dict(pick, id=f"{today}-{int(time.time()) % 100000}", dir=v["dir"], type=typ, expiry=exp, lot=lot,
                 buy_token=legs[pick["buy_k"]][0], sell_token=legs[pick["sell_k"]][0],
                 buy_sym=legs[pick["buy_k"]][1], sell_sym=legs[pick["sell_k"]][1],
                 inval=x["or_low"] if v["dir"] == "bull" else x["or_high"], entry_spot=x["now"],
                 time_stop=prev_weekday(d_(exp)).isoformat(), expires=time.time() + 300)
        why = "; ".join(text for name, _, text in v["gates"] if name in ("Trend", "Breakout", "Volatility"))
        msg = (f"🟢 <b>Trade ready: {'bull call' if o['dir'] == 'bull' else 'bear put'} spread</b>\n"
               f"Buy  NIFTY {nice(exp)} {pt(o['buy_k'], 0)} {typ} at {pt(o['buy_px'])}\n"
               f"Sell NIFTY {nice(exp)} {pt(o['sell_k'], 0)} {typ} at {pt(o['sell_px'])}\n"
               f"1 lot of {lot}, Nifty at {pt(x['now'])}\n\n"
               f"Net cost {pt(o['debit'])}. Most you can lose {inr(o['max_loss'])} (limit {inr(o['budget'])}).\n"
               f"Most you can make {inr(o['max_profit'])}. Reward to risk {o['rr']:.2f}. Charges {inr(o['cost'])}.\n\n"
               f"<b>Exit plan</b>\nStop: spread worth {pt(o['stop_val'])} or less\n"
               f"Wrong if: 15-min close {'below' if o['dir'] == 'bull' else 'above'} {pt(o['inval'])}\n"
               f"Target: spread worth {pt(o['target_val'])} or more\nOut by: 15:00 on {nice(o['time_stop'])}\n\n"
               f"Why: {why}\n{s['mode'].upper()} mode. This offer lapses in 5 minutes.")
        o["msg"] = self.tg.send(msg, [("Yes, take it", "yes:" + o["id"]), ("No, skip", "no:" + o["id"])])
        t["offer"], t["phase"] = o, "offered"; self.save()

    def open_position(self, o, buy_px, sell_px, today):
        n = spread_numbers(buy_px, sell_px, o["width"], o["lot"], self.s["settings"])
        keep = ("id", "dir", "type", "expiry", "lot", "buy_k", "sell_k", "width", "buy_token", "sell_token", "buy_sym", "sell_sym", "inval", "entry_spot", "time_stop")
        self.s["pos"] = dict({k: o[k] for k in keep}, mode=self.s["settings"]["mode"], buy_px=buy_px, sell_px=sell_px, debit=n["debit"],
                             stop_val=n["stop_val"], target_val=n["target_val"], risk=n["max_loss"], entry_date=today, exit_alert=None)
        self.s["today"].update(phase="done", offer=None); self.save()

    def on_yes(self, o, today):
        s, t = self.s["settings"], self.s["today"]
        self.tg.clear_buttons(o.get("msg"))
        q = self.a.quotes(nfo=[o["buy_token"], o["sell_token"]]); bq, sq = q[o["buy_token"]], q[o["sell_token"]]
        n = spread_numbers(bq["ask"], sq["bid"], o["width"], o["lot"], s) if leg_liquid(bq) and leg_liquid(sq) else {"ok": False, "why": "bid–ask gap widened", "debit": 0}
        if not n["ok"] or n["debit"] > o["debit"] * 1.10 + 0.5:
            self.tg.send(f"⚪ Skipped. Prices moved since the alert ({n['why'] or 'net cost is now ' + pt(n['debit'])}). No chasing; done for today.")
            t.update(phase="done", offer=None); self.save(); return
        if s["mode"] == "paper":
            self.open_position(o, bq["ask"], sq["bid"], today)
            self.tg.send(f"✅ Paper trade logged at a net cost of {pt(n['debit'])}. I’ll watch it and close it by the plan, then tell you the result.")
        else:
            o.update(buy_px=bq["ask"], sell_px=sq["bid"], expires=time.time() + 600)
            o["msg"] = self.tg.send(
                f"<b>Place these two orders now</b> (limit, product NRML / carry forward):\n"
                f"1. BUY 1 lot {o['buy_sym']} at {pt(bq['ask'])}\n2. Then SELL 1 lot {o['sell_sym']} at {pt(sq['bid'])}\n"
                f"If the second leg doesn’t fill in a minute or two, close the first. Never hold one leg alone.",
                [("Both filled", "filled:" + o["id"]), ("Couldn’t fill", "nofill:" + o["id"])])
            t["phase"] = "confirm"; self.save()

    # -- managing the open position
    def close_position(self, reason, long_px, short_px, today, followed=True):
        p, s = self.s["pos"], self.s["settings"]
        cost = costs(p["buy_px"], p["sell_px"], long_px, short_px, p["lot"], s)
        value = long_px - short_px; net = (value - p["debit"]) * p["lot"] - cost
        self.s["journal"].append({"id": p["id"], "mode": p["mode"], "dir": p["dir"], "buy_k": p["buy_k"], "sell_k": p["sell_k"], "expiry": p["expiry"],
                                  "lot": p["lot"], "entry_date": p["entry_date"], "exit_date": today, "debit": round(p["debit"], 2), "exit_value": round(value, 2),
                                  "costs": round(cost), "net": round(net), "risk": round(p["risk"]), "reason": reason, "followed": followed})
        self.s["pos"] = None; self.save()
        self.tg.send(f"{'🟢' if net > 0 else '🔴'} <b>Closed: {reason}</b>\nIn at {pt(p['debit'])}, out at {pt(value)}. Result after charges: <b>{inr(net, True)}</b>\n{self.summary()}")

    def check_position(self, today, mins, at_candle_close):
        p = self.s["pos"]
        spot, lq, sq = self.leg_quotes(p)
        long_px = lq["bid"] or lq["ltp"]; short_px = sq["ask"] or sq["ltp"]       # what you would actually get on exit
        action, reason, text = monitor(p, today, mins, spot if at_candle_close else None, long_px - short_px)
        if action == "hold": return
        if p["mode"] == "paper":
            self.close_position(reason, long_px, short_px, today)
        else:
            al = p.get("exit_alert")
            if not al or time.time() - al["sent"] > 600:
                msg = self.tg.send(f"🚨 <b>Exit now: {reason}</b>\n{text}\n1. BUY back {p['sell_sym']} first\n2. Then SELL {p['buy_sym']}\nSpread is worth about {pt(long_px - short_px)}.",
                                   [("Closed both legs", "closed:" + p["id"])])
                p["exit_alert"] = {"reason": reason, "sent": time.time(), "msg": msg}; self.save()

    # -- Telegram input
    def handle(self, kind, data, msg_id, today):
        s, t = self.s["settings"], self.s["today"]
        if kind == "cb":
            act, _, ref = data.partition(":"); o = t.get("offer")
            if act in ("yes", "no") and t["phase"] == "offered" and o and o["id"] == ref:
                if act == "no":
                    self.tg.clear_buttons(msg_id); self.tg.send("⚪ Skipped. Nothing more today.")
                    t.update(phase="done", offer=None); self.save()
                else: self.on_yes(o, today)
            elif act in ("filled", "nofill") and t["phase"] == "confirm" and o and o["id"] == ref:
                self.tg.clear_buttons(msg_id)
                if act == "filled":
                    self.open_position(o, o["buy_px"], o["sell_px"], today)
                    self.tg.send("✅ Live trade recorded. If your fill prices differ, send /fill buy-price sell-price. I’ll alert you when it’s time to exit.")
                else:
                    self.tg.send("⚪ Noted, no trade. Make sure no single leg is left open.")
                    t.update(phase="done", offer=None); self.save()
            elif act == "closed" and self.s["pos"] and self.s["pos"]["id"] == ref:
                self.tg.clear_buttons(msg_id); p = self.s["pos"]
                _, lq, sq = self.leg_quotes(p)
                self.close_position((p.get("exit_alert") or {}).get("reason", "Manual exit"), lq["bid"] or lq["ltp"], sq["ask"] or sq["ltp"], today)
                self.tg.send("Result uses current quotes. Send /setnet amount to replace it with the figure from your contract note.")
            elif act in ("yes", "no", "filled", "nofill", "closed"):
                self.tg.clear_buttons(msg_id); self.tg.send("That alert is no longer active.")
            return
        parts = data.split(); cmd, args = parts[0].lower().split("@")[0], parts[1:]
        try:
            if cmd in ("/start", "/help"): self.tg.send(HELP)
            elif cmd == "/status":
                p = self.s["pos"]
                pos = (f"Open {p['mode']} trade: {'bull call' if p['dir'] == 'bull' else 'bear put'} {pt(p['buy_k'], 0)}/{pt(p['sell_k'], 0)} {p['type']}, {nice(p['expiry'])} expiry, "
                       f"in at {pt(p['debit'])}. Stop {pt(p['stop_val'])}, target {pt(p['target_val'])}, out by 15:00 {nice(p['time_stop'])}.") if p else "No open trade."
                self.tg.send(f"{pos}\n{self.summary()}\nMode {s['mode']}, capital {inr(s['capital'])}, risk {s['risk_pct']}% a trade ({inr(s['capital'] * s['risk_pct'] / 100)}). "
                             f"Alerts {'paused' if self.s['paused'] else 'on'}. Events: {', '.join(self.s['events']) or 'none'}.")
            elif cmd == "/mode" and args and args[0] in ("paper", "live"):
                n = len([x for x in self.s["journal"] if x["mode"] == "paper"])
                if self.s["pos"]: self.tg.send("Close the open trade before switching mode.")
                elif args[0] == "live" and n < 20 and args[1:] != ["confirm"]:
                    self.tg.send(f"You have {n} of 20 paper trades. Going live now risks real money on untested rules. Send /mode live confirm to switch anyway.")
                else: s["mode"] = args[0]; self.save(); self.tg.send(f"Mode is now {args[0]}.")
            elif cmd == "/capital" and args: s["capital"] = max(10000.0, float(args[0])); self.save(); self.tg.send(f"Capital set to {inr(s['capital'])}.")
            elif cmd == "/risk" and args: s["risk_pct"] = min(2.5, max(0.5, float(args[0]))); self.save(); self.tg.send(f"Risk per trade set to {s['risk_pct']}%.")
            elif cmd == "/event" and args:
                e = d_(args[0]).isoformat(); ev = self.s["events"]
                ev.remove(e) if e in ev else ev.append(e); ev.sort(); self.save(); self.tg.send(f"Event days: {', '.join(ev) or 'none'}.")
            elif cmd == "/pause": self.s["paused"] = True; self.save(); self.tg.send("New trade alerts paused. An open trade is still managed.")
            elif cmd == "/resume": self.s["paused"] = False; self.save(); self.tg.send("New trade alerts are on again from the next check.")
            elif cmd == "/fill" and len(args) == 2 and self.s["pos"]:
                p = self.s["pos"]; b, sl = float(args[0]), float(args[1]); n = spread_numbers(b, sl, p["width"], p["lot"], s)
                if n["debit"] <= 0: self.tg.send("Those prices don’t make sense: the buy price must be higher than the sell price.")
                else:
                    p.update(buy_px=b, sell_px=sl, debit=n["debit"], stop_val=n["stop_val"], target_val=n["target_val"], risk=n["max_loss"]); self.save()
                    self.tg.send(f"Entry corrected to a net cost of {pt(n['debit'])}. Stop {pt(n['stop_val'])}, target {pt(n['target_val'])}.")
            elif cmd == "/setnet" and args and self.s["journal"]:
                self.s["journal"][-1]["net"] = round(float(args[0].replace("−", "-"))); self.save(); self.tg.send("Last result corrected.\n" + self.summary())
            else: self.tg.send("I didn’t understand that.\n" + HELP)
        except (ValueError, IndexError):
            self.tg.send("I couldn’t read that value.\n" + HELP)

    # -- one turn of the loop
    def step(self, wait=20):
        self.s["tg_offset"], updates = self.tg.poll(self.s["tg_offset"], wait)
        now, today, mins, secs = self.clock()
        self.roll_day(today)
        t = self.s["today"]
        try:
            for kind, data, msg_id in updates: self.handle(kind, data, msg_id, today)
            if self.s["pos"]:
                slot5, slot15 = (secs - 45) // 300, (secs - 45) // 900
                if mins >= 9 * 60 + 20 and slot5 != self.pos_slot:
                    at_close = slot15 != self.inval_slot and mins >= 9 * 60 + 30 and (secs - 45) % 900 < 240
                    self.pos_slot, self.inval_slot = slot5, slot15
                    self.check_position(today, mins, at_close)
            elif t["phase"] == "scan" and secs >= 9 * 3600 + 45 * 60 + 45:
                slot = (secs - 45) // 900
                if slot != self.scan_slot:
                    self.scan_slot = slot; self.scan(now, today, mins)
            if t["phase"] in ("offered", "confirm") and t.get("offer") and time.time() > t["offer"]["expires"]:
                self.tg.clear_buttons(t["offer"].get("msg"))
                self.tg.send("⚪ No answer in time, so the offer lapsed. Nothing more today." if t["phase"] == "offered"
                             else "⚪ No confirmation received, so I’ve recorded no trade. Check that no single leg is open, or send /status.")
                t.update(phase="done", offer=None); self.save()
        except Exception as e:                      # keep running; tell the owner at most once every 15 minutes
            print("error:", type(e).__name__)
            if time.time() - self.last_error > 900:
                self.last_error = time.time(); self.tg.send(f"⚠️ Data problem: {str(e)[:120]}. I’ll keep retrying.")
        pos = self.s["pos"]
        if mins >= 15 * 60 + 5 and not (pos and pos["mode"] == "live" and pos.get("exit_alert") and mins < 15 * 60 + 25): self.done = True
        elif not pos and t["phase"] == "done": self.done = True

def main():
    env = {k: os.environ.get(k, "").strip() for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_TOTP_SECRET", "TG_TOKEN", "TG_CHAT_ID", "STATE_KEY")}
    missing = [k for k, v in env.items() if not v]
    if missing: sys.exit("Missing secrets: " + ", ".join(missing))
    angel = Angel(env["ANGEL_API_KEY"], env["ANGEL_CLIENT_ID"], env["ANGEL_PIN"], env["ANGEL_TOTP_SECRET"])
    tg = Telegram(env["TG_TOKEN"], env["TG_CHAT_ID"])
    store = Store(STATE_FILE, env["STATE_KEY"], git=bool(os.environ.get("GITHUB_ACTIONS")))
    if "--check" in sys.argv:                       # connection test: run it once after setup
        bot = Bot(angel, tg, store)
        try:
            q = angel.quotes(nse=[NIFTY, VIX]); n_exp = len(angel.chain())
            msg = tg.send(f"✅ <b>Saarthi is connected</b>\nNifty {pt(q[NIFTY]['ltp'])}, India VIX {pt(q[VIX]['ltp'])}, {n_exp} option expiries found.\n{bot.summary()}\nTap a button to test replies.",
                          [("Yes", "test:yes"), ("No", "test:no")])
        except Exception as e:
            tg.send(f"⚠️ Telegram works, but Angel One failed: {str(e)[:150]}"); sys.exit(1)
        if not msg: sys.exit("Telegram message failed: check TG_TOKEN and TG_CHAT_ID")
        end = time.time() + 120
        while time.time() < end:
            bot.s["tg_offset"], ups = tg.poll(bot.s["tg_offset"], 15)
            if any(k == "cb" and d.startswith("test:") for k, d, _ in ups):
                tg.clear_buttons(msg); tg.send("Buttons work. You’re set. I’ll message you on trading days.\n" + HELP); break
        store.save(bot.s); return
    bot = Bot(angel, tg, store)
    now = bot.now()
    if now.weekday() >= 5: return
    print("started", now.strftime("%H:%M"))
    while not bot.done: bot.step()
    bot.save(); print("finished")

if __name__ == "__main__":
    main()
