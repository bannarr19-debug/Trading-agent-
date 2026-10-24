#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC पेपर ट्रेडिंग एजेंट: वर्कर की तरह
  1) निगरानी : हर कैंडल पढ़कर डेटा जमा करता है (knowledge.json, days.csv)
  2) कमांड   : बॉस के आदेश commands.json में, कभी भी बदलो
  3) ट्रेडर  : कमांड के हिसाब से पेपर ट्रेड (असली पैसा/API key नहीं)
  4) रिपोर्ट : बताता है कौन सा सेशन चला, कौन सा नहीं

  python btc_agent.py download --days 1095 --csv btc_5m.csv
  python btc_agent.py backtest --csv btc_5m.csv --split 2025-10-25
  python btc_agent.py once      # एक बार चलकर निकल जाए (GitHub Actions)
  python btc_agent.py live      # अपने कंप्यूटर/VPS पर लगातार
  python btc_agent.py report    # अब तक की सीख की रिपोर्ट
"""
import argparse, csv, json, os, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

CFG = dict(
    symbol="BTCUSDT", interval="5m", api_base="https://fapi.binance.com",
    capital_inr=100000, usd_inr=88.0,
    # ── बॉस की कमांड (commands.json से बदलती हैं) ──
    max_trades_per_day=3,      # दिन में अधिकतम ट्रेड (सेटअप बने तभी)
    max_consec_losses=3,       # लगातार इतने लॉस के बाद ब्रेक
    pause_days=5,              # ब्रेक के दिन: सिर्फ़ मार्केट पढ़ना, ट्रेड नहीं
    sl_closes_day=True,        # SL हिट होते ही आज का दिन बंद
    risk_pct=1.5, max_risk_pct=2.0, min_rr=2.0, max_leverage=3.0,
    or_minutes=30, min_range_pct=0.2, max_range_pct=3.0,
    # ── तकनीकी ──
    fee_pct=0.05, slippage_pct=0.02,
    state_file="state.json", know_file="knowledge.json",
    days_file="days.csv", log_file="trades.csv", poll_seconds=30,
    source=os.environ.get("DATA_SOURCE", "auto"),     # auto / binance / coinbase
)
COMMANDS = ["max_trades_per_day", "max_consec_losses", "pause_days", "sl_closes_day",
            "risk_pct", "max_risk_pct", "min_rr", "max_leverage",
            "or_minutes", "min_range_pct", "max_range_pct"]


def load_commands(path="commands.json"):
    if os.path.exists(path):
        try:
            for k, v in json.load(open(path)).items():
                if k in COMMANDS:
                    CFG[k] = v
        except Exception as e:
            print("⚠️ commands.json पढ़ने में गड़बड़ी:", e)


def utc_day(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()


def fmt_t(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ───────────────────────── डेटा ─────────────────────────
def _binance(start_ms, limit=1500):
    q = urllib.parse.urlencode(dict(symbol=CFG["symbol"], interval=CFG["interval"],
                                    limit=limit, startTime=start_ms))
    with urllib.request.urlopen(f"{CFG['api_base']}/fapi/v1/klines?{q}", timeout=20) as r:
        raw = json.loads(r.read())
    return [dict(t=k[0], o=float(k[1]), h=float(k[2]), l=float(k[3]), c=float(k[4]),
                 v=float(k[5]), ct=k[6]) for k in raw]


def _coinbase(start_ms, limit=300):
    """Coinbase BTC-USD (5 मिनट)। अमेरिकी सर्वर (GitHub Actions) से भी खुलता है।"""
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_ms = min(start_ms + limit * 300000, int(time.time() * 1000))
    q = urllib.parse.urlencode(dict(granularity=300, start=iso(start_ms), end=iso(end_ms)))
    req = urllib.request.Request(f"https://api.exchange.coinbase.com/products/BTC-USD/candles?{q}",
                                 headers={"User-Agent": "paper-agent"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = json.loads(r.read())
    out = [dict(t=k[0] * 1000, o=float(k[3]), h=float(k[2]), l=float(k[1]), c=float(k[4]),
                v=float(k[5]), ct=k[0] * 1000 + 299999) for k in raw]
    return sorted(out, key=lambda x: x["t"])


def fetch_klines(start_ms, limit=1500):
    fns = {"binance": [_binance], "coinbase": [_coinbase]}.get(CFG["source"], [_binance, _coinbase])
    err = None
    for f in fns:
        try:
            return f(start_ms)
        except Exception as e:
            err = e
    raise err


def download(days, path):
    now = int(time.time() * 1000)
    start = now - days * 86400 * 1000
    rows = []
    while start < now:
        cs = fetch_klines(start)
        if not cs:
            break
        rows += cs
        start = cs[-1]["t"] + 1
        time.sleep(0.2)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "o", "h", "l", "c", "v"])
        for c in rows:
            w.writerow([c["t"], c["o"], c["h"], c["l"], c["c"], c["v"]])
    print(f"{len(rows)} कैंडल सेव हुए -> {path}")


def read_csv(path):
    with open(path) as f:
        return [dict(t=int(r["t"]), o=float(r["o"]), h=float(r["h"]), l=float(r["l"]),
                     c=float(r["c"]), v=float(r.get("v") or 0)) for r in csv.DictReader(f)]


# ───────────────────────── स्ट्रैटेजी (बदलने लायक) ─────────────────────────
def strategy_orb(s, c):
    """ओपनिंग रेंज ब्रेकआउट। रेंज के अंदर लौटने के बाद ही अगला ब्रेकआउट गिना जाता है।"""
    hi, lo = s["or_high"], s["or_low"]
    if hi is None or lo is None:
        return None
    if not (CFG["min_range_pct"] <= (hi - lo) / lo * 100 <= CFG["max_range_pct"]):
        return None
    if lo <= c["c"] <= hi:
        s["armed"] = True
        return None
    if not s["armed"]:
        return None
    s["armed"] = False
    return dict(side="LONG", sl=lo) if c["c"] > hi else dict(side="SHORT", sl=hi)


def ai_filter(sig, s, c):
    """दिमाग का हुक। अभी हमेशा True। बाद में यहाँ Claude API जोड़ी जा सकती है।"""
    return True


# ───────────────────────── रिस्क गार्ड ─────────────────────────
def can_trade(s, day):
    if s["pos"]:
        return False, "पोज़िशन पहले से खुली है"
    if s["pause_until"] and day <= s["pause_until"]:
        return False, f"लगातार लॉस की वजह से ब्रेक ({s['pause_until']} तक, सिर्फ़ मार्केट पढ़ाई)"
    if s["day_closed"]:
        return False, "आज SL हिट हो चुका, दिन बंद"
    if s["trades_today"] >= CFG["max_trades_per_day"]:
        return False, f"आज के {CFG['max_trades_per_day']} ट्रेड हो चुके"
    return True, ""


def check_setup(entry, sl, tp, qty, balance):
    if not sl:
        return False, "SL नहीं है"
    if abs(entry - sl) * qty > balance * CFG["max_risk_pct"] / 100 + 1e-9:
        return False, "रिस्क लिमिट से ज़्यादा"
    if abs(tp - entry) / abs(entry - sl) < CFG["min_rr"] - 1e-9:
        return False, "RR कम है"
    return True, ""


# ───────────────────────── स्टेट और ज्ञान ─────────────────────────
def new_state():
    b = CFG["capital_inr"] / CFG["usd_inr"]
    return dict(balance=b, start_balance=b, peak=b, max_dd=0.0, wins=0, losses=0,
                consec_losses=0, pause_until=None, day=None, trades_today=0,
                day_closed=False, day_pnl=0.0, pos=None, or_high=None, or_low=None,
                armed=False, last_t=0, last_close=None, block_noted=False,
                d_o=None, d_h=None, d_l=None, d_v=0.0)


def new_know():
    return dict(hours={}, trade_hours={}, sides={})


# ───────────────────────── एजेंट ─────────────────────────
class Agent:
    def __init__(self, persist=True, log_file=None, days_file=None, quiet=False):
        self.persist, self.quiet = persist, quiet
        self.log_file = log_file or CFG["log_file"]
        self.days_file = days_file or CFG["days_file"]
        self.s, self.k = new_state(), new_know()
        if persist:
            if os.path.exists(CFG["state_file"]):
                self.s.update(json.load(open(CFG["state_file"])))
            if os.path.exists(CFG["know_file"]):
                self.k = json.load(open(CFG["know_file"]))
        self._head(self.log_file, ["समय", "साइड", "एंट्री", "SL", "टारगेट", "एग्ज़िट", "वजह",
                                   "qty", "P/L_USDT", "P/L_INR", "बैलेंस_INR"])
        self._head(self.days_file, ["तारीख़", "ओपन", "हाई", "लो", "क्लोज़", "रेंज%", "वॉल्यूम",
                                    "ट्रेड", "P/L_USDT"])

    @staticmethod
    def _head(path, cols):
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(cols)

    def say(self, msg):
        if not self.quiet:
            print(msg)

    def save(self):
        if self.persist:
            json.dump(self.s, open(CFG["state_file"], "w"), indent=1)
            json.dump(self.k, open(CFG["know_file"], "w"))

    def inr(self, usdt):
        return usdt * CFG["usd_inr"]

    # --- निगरानी: हर कैंडल की गहरी पढ़ाई जमा करना ---
    def observe(self, c):
        s, o = self.s, c["o"]
        h = str((c["t"] // 3600000) % 24)
        d = self.k["hours"].setdefault(h, dict(n=0, rng=0.0, body=0.0, upw=0.0, loww=0.0, vol=0.0, up=0))
        d["n"] += 1
        d["rng"] += (c["h"] - c["l"]) / o * 100
        d["body"] += abs(c["c"] - o) / o * 100
        d["upw"] += (c["h"] - max(o, c["c"])) / o * 100     # ऊपर की पूँछ: ऊपर से बिकवाली
        d["loww"] += (min(o, c["c"]) - c["l"]) / o * 100    # नीचे की पूँछ: नीचे से खरीदारी
        d["vol"] += c.get("v", 0)
        d["up"] += 1 if c["c"] >= o else 0
        s["d_o"] = o if s["d_o"] is None else s["d_o"]
        s["d_h"] = c["h"] if s["d_h"] is None else max(s["d_h"], c["h"])
        s["d_l"] = c["l"] if s["d_l"] is None else min(s["d_l"], c["l"])
        s["d_v"] += c.get("v", 0)

    # --- हर कैंडल पर ---
    def on_candle(self, c):
        s = self.s
        day = utc_day(c["t"]).isoformat()
        if s["day"] != day:
            self.new_day(day, c)
        self.observe(c)
        if s["pos"]:
            self.manage(c)
        if (c["t"] // 60000) % 1440 < CFG["or_minutes"]:      # अभी रेंज बन रही है
            s["or_high"] = c["h"] if s["or_high"] is None else max(s["or_high"], c["h"])
            s["or_low"] = c["l"] if s["or_low"] is None else min(s["or_low"], c["l"])
            s["armed"] = True
        elif not s["pos"]:
            sig = strategy_orb(s, c)
            if sig:
                self.try_enter(sig, c, day)
        s["last_t"], s["last_close"] = c["t"], c["c"]

    def new_day(self, day, c):
        s = self.s
        if s["day"]:
            if s["pos"]:                                       # दिन के अंत में खुली पोज़िशन बंद
                self.close(s["last_close"], "EOD", c["t"])
            if s["d_o"]:
                with open(self.days_file, "a", newline="") as f:
                    csv.writer(f).writerow([s["day"], s["d_o"], s["d_h"], s["d_l"], s["last_close"],
                                            round((s["d_h"] - s["d_l"]) / s["d_o"] * 100, 3),
                                            round(s["d_v"], 2), s["trades_today"], round(s["day_pnl"], 2)])
            self.say(f"📅 {s['day']} समरी | दिन का P/L ₹{self.inr(s['day_pnl']):,.0f} | "
                     f"बैलेंस ₹{self.inr(s['balance']):,.0f}")
        s.update(day=day, trades_today=0, day_closed=False, day_pnl=0.0, or_high=None, or_low=None,
                 armed=False, block_noted=False, d_o=None, d_h=None, d_l=None, d_v=0.0)

    def try_enter(self, sig, c, day):
        s = self.s
        ok, why = can_trade(s, day)
        if not ok:
            if not s["block_noted"]:
                self.say(f"⛔ {fmt_t(c['t'])} सिग्नल मिला पर ब्लॉक: {why}")
                s["block_noted"] = True
            return
        slip = CFG["slippage_pct"] / 100
        long = sig["side"] == "LONG"
        entry = c["c"] * (1 + slip) if long else c["c"] * (1 - slip)
        sl = sig["sl"]
        risk_unit = abs(entry - sl)
        if risk_unit <= 0 or (long and sl >= entry) or (not long and sl <= entry):
            return
        tp = entry + CFG["min_rr"] * risk_unit if long else entry - CFG["min_rr"] * risk_unit
        qty = s["balance"] * CFG["risk_pct"] / 100 / risk_unit
        qty = min(qty, s["balance"] * CFG["max_leverage"] / entry)
        ok, why = check_setup(entry, sl, tp, qty, s["balance"])
        if not ok:
            self.say(f"⛔ सेटअप रद्द: {why}")
            return
        if not ai_filter(sig, s, c):
            self.say("⛔ AI फ़िल्टर ने सेटअप रद्द किया")
            return
        s["pos"] = dict(side=sig["side"], entry=entry, sl=sl, tp=tp, qty=qty, t=c["t"])
        s["trades_today"] += 1
        self.say(f"✅ EXECUTED (Paper) {fmt_t(c['t'])} {sig['side']} @ {entry:.1f} SL {sl:.1f} TP {tp:.1f}")

    def manage(self, c):
        p = self.s["pos"]
        if p["side"] == "LONG":
            if c["l"] <= p["sl"]:
                self.close(p["sl"], "SL", c["t"])        # दोनों छुएँ तो SL पहले माना जाता है
            elif c["h"] >= p["tp"]:
                self.close(p["tp"], "TP", c["t"])
        else:
            if c["h"] >= p["sl"]:
                self.close(p["sl"], "SL", c["t"])
            elif c["l"] <= p["tp"]:
                self.close(p["tp"], "TP", c["t"])

    def close(self, price, reason, t):
        s, p = self.s, self.s["pos"]
        d = 1 if p["side"] == "LONG" else -1
        fees = (p["entry"] + price) * p["qty"] * CFG["fee_pct"] / 100
        pnl = (price - p["entry"]) * p["qty"] * d - fees
        s["balance"] += pnl
        s["day_pnl"] += pnl
        s["peak"] = max(s["peak"], s["balance"])
        s["max_dd"] = max(s["max_dd"], (s["peak"] - s["balance"]) / s["peak"] * 100)
        for bucket, key in ((self.k["trade_hours"], str((p["t"] // 3600000) % 24)), (self.k["sides"], p["side"])):
            x = bucket.setdefault(key, dict(n=0, w=0, pnl=0.0))
            x["n"] += 1
            x["w"] += 1 if pnl > 0 else 0
            x["pnl"] += pnl
        if pnl < 0:
            s["losses"] += 1
            s["consec_losses"] += 1
            if reason == "SL" and CFG["sl_closes_day"]:
                s["day_closed"] = True
            if s["consec_losses"] >= CFG["max_consec_losses"]:
                until = utc_day(t) + timedelta(days=CFG["pause_days"])
                s["pause_until"] = until.isoformat()
                s["consec_losses"] = 0
                self.say(f"🛑 लगातार {CFG['max_consec_losses']} लॉस। {until} तक सिर्फ़ मार्केट पढ़ाई, ट्रेड नहीं")
        else:
            s["wins"] += 1
            s["consec_losses"] = 0
        with open(self.log_file, "a", newline="") as f:
            csv.writer(f).writerow([fmt_t(t), p["side"], round(p["entry"], 1), round(p["sl"], 1),
                                    round(p["tp"], 1), round(price, 1), reason, round(p["qty"], 5),
                                    round(pnl, 2), round(self.inr(pnl)), round(self.inr(s["balance"]))])
        self.say(f"📕 {fmt_t(t)} बंद ({reason}) P/L ₹{self.inr(pnl):,.0f}")
        s["pos"] = None

    def summary(self, name=""):
        s = self.s
        n = s["wins"] + s["losses"]
        wr = s["wins"] / n * 100 if n else 0
        ret = (s["balance"] / s["start_balance"] - 1) * 100
        return (f"── {name} ── ट्रेड {n} | जीत {s['wins']} हार {s['losses']} | विन रेट {wr:.1f}% | "
                f"बैलेंस ₹{self.inr(s['balance']):,.0f} | रिटर्न {ret:.1f}% | मैक्स ड्रॉडाउन {s['max_dd']:.1f}%")


# ───────────────────────── रिपोर्ट (साल भर बाद बॉस को जवाब) ─────────────────────────
SESSIONS = [("एशिया 00-08 UTC", range(0, 8)), ("यूरोप 08-16 UTC", range(8, 16)),
            ("अमेरिका 16-24 UTC", range(16, 24))]


def report(k):
    lines, best = ["📊 सेशन रिपोर्ट"], None
    for name, hrs in SESSIONS:
        n = rng = up = t = w = 0
        pnl = 0.0
        for h in hrs:
            d, x = k["hours"].get(str(h)), k["trade_hours"].get(str(h))
            if d:
                n, rng, up = n + d["n"], rng + d["rng"], up + d["up"]
            if x:
                t, w, pnl = t + x["n"], w + x["w"], pnl + x["pnl"]
        lines.append(f"{name}: कैंडल {n} | औसत रेंज {rng / n if n else 0:.3f}% | "
                     f"तेज़ी की कैंडल {up / n * 100 if n else 0:.0f}% | ट्रेड {t} | "
                     f"विन {w / t * 100 if t else 0:.0f}% | P/L ₹{pnl * CFG['usd_inr']:,.0f}")
        if t >= 20 and (best is None or pnl > best[1]):      # कम ट्रेड वाले नतीजे पर भरोसा नहीं
            best = (name, pnl)
    for side, x in k["sides"].items():
        lines.append(f"{side}: ट्रेड {x['n']} | विन {x['w'] / x['n'] * 100:.0f}% | P/L ₹{x['pnl'] * CFG['usd_inr']:,.0f}")
    lines.append(f"🏆 अब तक सबसे ज़्यादा कमाई वाला सेशन: {best[0]}" if best
                 else "ℹ️ किसी सेशन में अभी 20 ट्रेड नहीं हुए, इसलिए नतीजा कहना जल्दबाज़ी होगी")
    return "\n".join(lines)


# ───────────────────────── मोड ─────────────────────────
def backtest(path, split):
    candles = read_csv(path)
    day = lambda c: utc_day(c["t"]).isoformat()
    parts = ([("ट्रेन (सीखने वाला)", [c for c in candles if day(c) < split]),
              ("टेस्ट (अनदेखा डेटा)", [c for c in candles if day(c) >= split])]
             if split else [("पूरा डेटा", candles)])
    for i, (name, cs) in enumerate(parts):
        if not cs:
            continue
        lf, df = f"backtest_{i}.csv", f"backtest_days_{i}.csv"
        for fn in (lf, df):
            if os.path.exists(fn):
                os.remove(fn)
        a = Agent(persist=False, log_file=lf, days_file=df, quiet=True)
        for c in cs:
            a.on_candle(c)
        print(a.summary(name))
        print(report(a.k) + "\n")
    print("ट्रेड की लिस्ट: backtest_0.csv / backtest_1.csv")


def once():
    """एक बार चलकर बाक़ी कैंडल निपटाओ, स्टेट सेव करो और बाहर निकलो (GitHub Actions)।"""
    a = Agent()
    for _ in range(20):
        now = int(time.time() * 1000)
        start = a.s["last_t"] + 1 if a.s["last_t"] else now - now % 86400000
        new = [c for c in fetch_klines(start) if c["ct"] < now and c["t"] > a.s["last_t"]]
        if not new:
            break
        for c in new:
            a.on_candle(c)
    a.save()
    print(a.summary("अभी तक"))


def live():
    a = Agent()
    print("🟢 पेपर एजेंट चालू। रोकने के लिए Ctrl+C")
    while True:
        try:
            load_commands()
            now = int(time.time() * 1000)
            start = a.s["last_t"] + 1 if a.s["last_t"] else now - now % 86400000
            for c in fetch_klines(start):
                if c["ct"] < now and c["t"] > a.s["last_t"]:
                    a.on_candle(c)
            a.save()
        except Exception as e:
            print("⚠️ गड़बड़ी (दोबारा कोशिश होगी):", e)
        time.sleep(CFG["poll_seconds"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["download", "backtest", "live", "once", "report"])
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--csv", default="btc_5m.csv")
    ap.add_argument("--split", default=None, help="YYYY-MM-DD: इससे पहले ट्रेन, बाद में टेस्ट")
    args = ap.parse_args()
    load_commands()
    if args.mode == "download":
        download(args.days, args.csv)
    elif args.mode == "backtest":
        backtest(args.csv, args.split)
    elif args.mode == "once":
        once()
    elif args.mode == "report":
        print(report(json.load(open(CFG["know_file"]))))
    else:
        live()
