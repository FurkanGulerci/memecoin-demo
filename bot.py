#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MEMECOIN DEMO BOTU (sahte para, Solana)

Bu bot GERCEK PARA KULLANMAZ, hicbir cuzdana baglanmaz. Sadece fiyatlari izler
ve "olsaydi ne olurdu" hesabini yapar. Her calistiginda:
  1) acik demo pozisyonlari kontrol eder (kar al / stop / sure / rug)
  2) yeni demo alimlar yapar (Strateji A ve B)
  3) her alim/satimda Telegram'a mesaj atar
  4) Turkiye saatiyle gece 00:00'da gunluk rapor gonderir
  5) 7 gun sonra final raporu ve CSV dosyalarini gonderir
Durum state.json dosyasinda, islemler trades.csv ve positions.csv dosyalarinda tutulur.
"""
import os
import sys
import csv
import json
import time
from datetime import datetime, timedelta, timezone

import requests

# =====================================================================
#  AYARLAR (degistirmek istersen sadece bu bolumu duzenle)
# =====================================================================
START_BALANCE = 100.0      # her strateji icin demo baslangic bakiyesi ($)
TRADE_SIZE = 10.0          # islem basina demo tutar ($)
MAX_OPEN = 5               # bir stratejide ayni anda en fazla acik pozisyon
EXPERIMENT_DAYS = 7        # deney suresi (gun)

PCT_FEE = 0.01             # platform ucreti (%1)
FIXED_FEE = 0.10           # islem basina sabit maliyet ($): ag ucreti, oncelik ucreti, bot koruma bahsisi
RUG_LIQ_DROP = 0.5         # likidite girisin %50'sinin altina inerse "RUG" say
MAX_RUGCHECK_PER_RUN = 6   # bir calismada en fazla kac token guvenlik taramasina girsin

STRATEGIES = {
    "A": {
        "name": "A - Hızlı serseri",
        "source": "new",            # yeni havuz listesi
        "min_age": 0, "max_age": 10,      # token yasi (dakika): en fazla 10 dk
        "min_liq": 2000,                  # minimum likidite ($)  <-- A cok az islem yaparsa bunu 500'e dusur
        "min_vol24": 0,
        "need_h1_up": False,
        "use_filter": False,              # guvenlik filtresi KAPALI (sadece etiketler)
        "tp_mult": 2.0, "tp_fraction": 0.5,   # 2x'te %50 sat
        "stop_mult": 0.60,                # -%40 stop
        "max_hold_h": 3,                  # 3 saat sure siniri
    },
    "B": {
        "name": "B - Güvenli hızlı kâr",
        "source": "established",    # trend / hacimli havuz listesi
        "min_age": 120, "max_age": 1440,  # 2 saat - 24 saat arasi
        "min_liq": 20000,
        "min_vol24": 50000,
        "need_h1_up": True,               # son 1 saatte yukselis
        "use_filter": True,               # guvenlik filtresi ACIK
        "tp_mult": 1.25, "tp_fraction": 1.0,  # +%25'te hepsini sat
        "stop_mult": 0.85,                # -%15 stop
        "max_hold_h": 2,                  # 2 saat sure siniri
    },
}
# =====================================================================

TRT = timezone(timedelta(hours=3))   # Turkiye saati
GT = "https://api.geckoterminal.com/api/v2"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens"
HEADERS = {"Accept": "application/json;version=20230302", "User-Agent": "memecoin-demo-bot"}
QUOTES = {"SOL", "WSOL", "USDC", "USDT"}

STATE_FILE = "state.json"
TRADES_CSV = "trades.csv"
POSITIONS_CSV = "positions.csv"
TRADE_FIELDS = ["zaman_trt", "strateji", "olay", "token", "mint", "havuz", "fiyat_usd",
                "token_adedi", "tutar_usd", "ucret_sabit", "ucret_yuzde", "kayma", "not"]
POS_FIELDS = ["strateji", "token", "mint", "havuz", "acilis_trt", "kapanis_trt", "sure_dk",
              "maliyet", "gelir", "kar_zarar", "toplam_maliyet", "cikis_nedeni", "risk_etiketi",
              "en_yuksek_carpan", "kismi_satis"]

REASON_TXT = {
    "TP": "Hedef (kâr al)",
    "STOP": "Stop (zarar kes)",
    "SURE": "Süre doldu",
    "RUG": "RUG (likidite çöktü / havuz yok)",
    "SATILAMAZ": "SATILAMAZ şüphesi",
}


# ---------------------------------------------------------------------
#  Yardimcilar
# ---------------------------------------------------------------------
def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def trt_str(dt):
    return dt.astimezone(TRT).strftime("%Y-%m-%d %H:%M")


def trt_day(dt):
    return dt.astimezone(TRT).date().isoformat()


def log(*a):
    print(*a, flush=True)


def fmt_price(p):
    if p is None:
        return "?"
    return f"{p:.4f}" if p >= 0.01 else f"{p:.3e}"


def usd(x, sign=False):
    return f"{x:+.2f} $" if sign else f"{x:.2f} $"


def short(mint):
    return mint[:4] + "..." + mint[-4:] if mint and len(mint) > 10 else (mint or "?")


# ---------------------------------------------------------------------
#  Ag istekleri
# ---------------------------------------------------------------------
_last_call = {}


def http_get(url, params=None, tries=3, min_gap=2.3):
    """(durum_kodu, json) dondurur. Ag hatasinda (None, None)."""
    host = url.split("/")[2]
    for i in range(tries):
        wait = min_gap - (time.time() - _last_call.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.time()
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
            if r.status_code == 429:
                log("429 (cok fazla istek), bekleniyor:", host)
                time.sleep(8 * (i + 1))
                continue
            if r.status_code == 404:
                return 404, None
            if r.status_code >= 400:
                log("HTTP", r.status_code, host)
                time.sleep(2)
                continue
            return r.status_code, r.json()
        except Exception as e:
            log("Ag hatasi:", type(e).__name__, host)
            time.sleep(2)
    return None, None


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_pool(item):
    """GeckoTerminal havuz kaydini sade bir sozluge cevirir."""
    try:
        a = item.get("attributes") or {}
        rel = item.get("relationships") or {}
        name = a.get("name") or ""
        base, _, quote = name.partition("/")
        base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
        mint = base_id.split("_", 1)[1] if "_" in base_id else base_id
        pool = a.get("address") or (item.get("id") or "").split("_", 1)[-1]
        created = a.get("pool_created_at")
        pc = a.get("price_change_percentage") or {}
        vol = a.get("volume_usd") or {}
        tx1 = (a.get("transactions") or {}).get("h1") or {}
        return {
            "pool": pool,
            "mint": mint,
            "name": base.strip() or "?",
            "quote": quote.strip(),
            "created": parse_iso(created) if created else None,
            "liq": _f(a.get("reserve_in_usd")),
            "price": _f(a.get("base_token_price_usd")),
            "h1": _f(pc.get("h1")),
            "vol24": _f(vol.get("h24")),
            "buys1": tx1.get("buys"),
            "sells1": tx1.get("sells"),
        }
    except Exception as e:
        log("Havuz ayristirma hatasi:", type(e).__name__)
        return None


def dedupe(pools):
    seen, out = set(), []
    for p in pools:
        if p and p["pool"] and p["pool"] not in seen:
            seen.add(p["pool"])
            out.append(p)
    return out


def fetch_new_pools(pages=3):
    out = []
    for page in range(1, pages + 1):
        st, j = http_get(f"{GT}/networks/solana/new_pools", {"page": page})
        if not j:
            break
        out += [parse_pool(x) for x in j.get("data", [])]
    return dedupe(out)


def fetch_established_pools():
    out = []
    calls = [
        ("trending_pools", {"duration": "1h"}),
        ("trending_pools", {"duration": "6h"}),
        ("trending_pools", {"duration": "24h"}),
        ("pools", {"order": "h24_volume_usd_desc", "page": 1}),
        ("pools", {"order": "h24_volume_usd_desc", "page": 2}),
    ]
    for path, params in calls:
        st, j = http_get(f"{GT}/networks/solana/{path}", params)
        if j:
            out += [parse_pool(x) for x in j.get("data", [])]
    return dedupe(out)


def fetch_pools(addrs):
    """Açık pozisyonlarin havuzlarini toplu ceker. (bulunanlar, kesin_kaybolanlar)"""
    found, gone = {}, set()
    addrs = list(dict.fromkeys(addrs))
    for i in range(0, len(addrs), 30):
        chunk = addrs[i:i + 30]
        st, j = http_get(f"{GT}/networks/solana/pools/multi/{','.join(chunk)}")
        if j:
            for it in j.get("data", []):
                p = parse_pool(it)
                if p and p["pool"]:
                    found[p["pool"]] = p
    for a in addrs:
        if a in found:
            continue
        st, j = http_get(f"{GT}/networks/solana/pools/{a}")
        if st == 404:
            gone.add(a)
        elif j and j.get("data"):
            p = parse_pool(j["data"])
            if p:
                found[a] = p
    return found, gone


def rugcheck(mint):
    """RugCheck ozeti. Basarisizsa None."""
    st, j = http_get(f"{RUGCHECK}/{mint}/report/summary", min_gap=1.2)
    if not j:
        return None
    risks = j.get("risks") or []
    return {
        "danger": [r.get("name", "?") for r in risks if r.get("level") == "danger"],
        "warn": [r.get("name", "?") for r in risks if r.get("level") == "warn"],
        "score": j.get("score_normalised"),
    }


# ---------------------------------------------------------------------
#  Telegram (token'i asla loglamayiz)
# ---------------------------------------------------------------------
def tg_send(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("[Telegram ayari yok] " + text)
        return False
    ok = True
    for i in range(0, len(text), 3900):
        chunk = text[i:i + 3900]
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              data={"chat_id": chat, "text": chunk,
                                    "disable_web_page_preview": "true"}, timeout=20)
            if r.status_code != 200:
                log("Telegram mesaj hatasi, kod:", r.status_code)
                ok = False
        except Exception as e:
            log("Telegram mesaj hatasi:", type(e).__name__)
            ok = False
        time.sleep(0.5)
    return ok


def tg_doc(path, caption=""):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat or not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as fh:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendDocument",
                              data={"chat_id": chat, "caption": caption},
                              files={"document": fh}, timeout=60)
        if r.status_code != 200:
            log("Telegram dosya hatasi, kod:", r.status_code)
        return r.status_code == 200
    except Exception as e:
        log("Telegram dosya hatasi:", type(e).__name__)
        return False


# ---------------------------------------------------------------------
#  Durum ve CSV
# ---------------------------------------------------------------------
def new_account():
    return {"cash": START_BALANCE, "positions": [], "closed": [], "seen": [],
            "rejected": [], "funnel": {}}


def init_state(now):
    start_day = datetime.fromisoformat(trt_day(now)).date()
    return {
        "version": 1,
        "start": iso(now),
        "end": iso(now + timedelta(days=EXPERIMENT_DAYS)),
        "reported_through": (start_day - timedelta(days=1)).isoformat(),
        "final_sent": False,
        "accounts": {"A": new_account(), "B": new_account()},
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def ensure_csv(path, fields):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh).writerow(fields)


def append_csv(path, fields, row):
    ensure_csv(path, fields)
    with open(path, "a", newline="", encoding="utf-8-sig") as fh:
        csv.DictWriter(fh, fieldnames=fields).writerow(row)


def log_trade(sk, event, pos, price, tokens, usd_amt, cost, note, now):
    append_csv(TRADES_CSV, TRADE_FIELDS, {
        "zaman_trt": trt_str(now), "strateji": sk, "olay": event, "token": pos["name"],
        "mint": pos["mint"], "havuz": pos["pool"], "fiyat_usd": f"{price:.10g}",
        "token_adedi": f"{tokens:.6g}", "tutar_usd": f"{usd_amt:.4f}",
        "ucret_sabit": f"{cost.get('fee_fixed', 0):.4f}",
        "ucret_yuzde": f"{cost.get('fee_pct', 0):.4f}",
        "kayma": f"{cost.get('slip', 0):.4f}", "not": note})


# ---------------------------------------------------------------------
#  Islem simulasyonu (sabit carpim havuzu varsayimi)
# ---------------------------------------------------------------------
def sim_buy(spend, price, liq):
    """spend $ ile alim: sabit maliyet + %ucret + likiditeye bagli kayma."""
    eff = max(spend - FIXED_FEE, 0) * (1 - PCT_FEE)
    q = max((liq or 0) / 2, 1e-9)
    exec_price = price * (1 + eff / q)
    tokens = eff / exec_price
    fee_pct = max(spend - FIXED_FEE, 0) * PCT_FEE
    slip = eff - tokens * price
    return tokens, {"fee_fixed": FIXED_FEE, "fee_pct": fee_pct, "slip": slip}


def sim_sell(tokens, price, liq):
    v = tokens * price
    q = max((liq or 0) / 2, 1e-9)
    gross = v / (1 + v / q)
    slip = v - gross
    fee_pct = gross * PCT_FEE
    net = max(gross - fee_pct - FIXED_FEE, 0.0)
    return net, {"fee_fixed": FIXED_FEE, "fee_pct": fee_pct, "slip": slip}


# ---------------------------------------------------------------------
#  Alim
# ---------------------------------------------------------------------
def open_position(sk, acct, p, risk_label, risk_info, now):
    tokens, c = sim_buy(TRADE_SIZE, p["price"], p["liq"])
    total_cost = c["fee_fixed"] + c["fee_pct"] + c["slip"]
    acct["cash"] -= TRADE_SIZE
    acct["seen"].append(p["mint"])
    pos = {
        "mint": p["mint"], "pool": p["pool"], "name": p["name"],
        "opened": iso(now), "entry_price": p["price"], "entry_liq": p["liq"] or 0.0,
        "tokens": tokens, "cost": TRADE_SIZE, "proceeds": 0.0, "fees": total_cost,
        "partial": False, "risk": risk_label, "miss": 0,
        "last_price": p["price"], "max_m": 1.0,
    }
    acct["positions"].append(pos)
    log_trade(sk, "ALIM", pos, p["price"], tokens, TRADE_SIZE, c, f"risk={risk_label}", now)

    extra = ""
    if risk_info and risk_info.get("danger"):
        extra = "\nRiskler: " + ", ".join(risk_info["danger"][:4])
    msg = (f"🟢 [{sk}] DEMO ALIM\n"
           f"Token: {pos['name']} ({short(p['mint'])})\n"
           f"Fiyat: {fmt_price(p['price'])} $ | Likidite: {p['liq']:.0f} $\n"
           f"Tutar: {usd(TRADE_SIZE)} (maliyet: {usd(total_cost)})\n"
           f"Güvenlik: {risk_label}{extra}\n"
           f"Açık pozisyon: {len(acct['positions'])}/{MAX_OPEN}\n"
           f"https://dexscreener.com/solana/{p['pool']}")
    tg_send(msg)


# ---------------------------------------------------------------------
#  Satis / kapanis
# ---------------------------------------------------------------------
def sell_position(sk, acct, pos, fraction, price, liq, reason, now, zero=False):
    n = pos["tokens"] * fraction
    if zero:
        net, c = 0.0, {"fee_fixed": 0.0, "fee_pct": 0.0, "slip": 0.0}
        lost_value = n * (price or 0)
        fee_add = 0.0
    else:
        net, c = sim_sell(n, price, liq if liq is not None else pos["entry_liq"])
        fee_add = c["fee_fixed"] + c["fee_pct"] + c["slip"]
    pos["tokens"] -= n
    pos["proceeds"] += net
    pos["fees"] += fee_add
    acct["cash"] += net
    closing = fraction >= 0.999 or pos["tokens"] <= 1e-12
    if reason == "TP" and not closing:
        pos["partial"] = True

    event = {"TP": "SAT_TP", "STOP": "SAT_STOP", "SURE": "SAT_SURE",
             "RUG": "RUG", "SATILAMAZ": "SATILAMAZ"}[reason]
    m = (price / pos["entry_price"]) if price and pos["entry_price"] else 0
    log_trade(sk, event, pos, price or 0, n, net, c, f"carpan={m:.2f}", now)

    if closing:
        pnl = pos["proceeds"] - pos["cost"]
        hold = (now - parse_iso(pos["opened"])).total_seconds() / 60
        rec = {
            "strategy": sk, "token": pos["name"], "mint": pos["mint"], "pool": pos["pool"],
            "opened": pos["opened"], "closed": iso(now), "closed_day": trt_day(now),
            "hold_min": round(hold, 1), "cost": pos["cost"], "proceeds": round(pos["proceeds"], 4),
            "pnl": round(pnl, 4), "fees": round(pos["fees"], 4), "reason": reason,
            "risk": pos["risk"], "max_m": round(pos["max_m"], 3), "partial": pos["partial"],
        }
        acct["closed"].append(rec)
        acct["positions"] = [x for x in acct["positions"] if x is not pos]
        append_csv(POSITIONS_CSV, POS_FIELDS, {
            "strateji": sk, "token": rec["token"], "mint": rec["mint"], "havuz": rec["pool"],
            "acilis_trt": trt_str(parse_iso(rec["opened"])), "kapanis_trt": trt_str(now),
            "sure_dk": rec["hold_min"], "maliyet": rec["cost"], "gelir": rec["proceeds"],
            "kar_zarar": rec["pnl"], "toplam_maliyet": rec["fees"],
            "cikis_nedeni": reason, "risk_etiketi": rec["risk"],
            "en_yuksek_carpan": rec["max_m"], "kismi_satis": "evet" if rec["partial"] else "hayir"})

    # Telegram mesaji
    head = {"TP": "💰", "STOP": "🔻", "SURE": "⏱️", "RUG": "☠️", "SATILAMAZ": "🚫"}[reason]
    lines = [f"{head} [{sk}] DEMO SATIŞ - {REASON_TXT[reason]}",
             f"Token: {pos['name']} ({short(pos['mint'])})",
             f"Fiyat çarpanı: {m:.2f}x"]
    if zero:
        lines.append(f"Kalan pozisyon sıfıra yazıldı (kayıp değer ~{usd(lost_value)})")
    else:
        lines.append(f"Satılan: %{fraction * 100:.0f} | Net gelir: {usd(net)} (maliyet: {usd(fee_add)})")
    if closing:
        pnl = pos["proceeds"] - pos["cost"]
        lines.append(f"POZİSYON KAPANDI | Toplam gelir: {usd(pos['proceeds'])} | K/Z: {usd(pnl, True)}")
        if reason in ("RUG", "SATILAMAZ"):
            lines.append("⚠️ Bu işlem rug/satılamaz olay olarak raporlanacak. "
                         f"Alımdaki güvenlik etiketi: {pos['risk']}")
    else:
        lines.append(f"Kalan: %{100 - fraction * 100:.0f} pozisyon hâlâ açık")
    lines.append(f"Bakiye (nakit): {usd(acct['cash'])}")
    tg_send("\n".join(lines))


def looks_unsellable(p):
    b, s = p.get("buys1"), p.get("sells1")
    return b is not None and s is not None and s == 0 and b >= 10


def track_positions(state, now):
    addrs = [pos["pool"] for acct in state["accounts"].values() for pos in acct["positions"]]
    if not addrs:
        return
    found, gone = fetch_pools(addrs)
    for sk, cfg in STRATEGIES.items():
        acct = state["accounts"][sk]
        for pos in list(acct["positions"]):
            p = found.get(pos["pool"])
            if p is None:
                if pos["pool"] in gone:
                    pos["miss"] += 1
                    if pos["miss"] >= 2:
                        sell_position(sk, acct, pos, 1.0, pos["last_price"], 0, "RUG", now, zero=True)
                continue
            pos["miss"] = 0
            if not p["price"] or p["price"] <= 0:
                continue
            pos["last_price"] = p["price"]
            m = p["price"] / pos["entry_price"]
            pos["max_m"] = max(pos["max_m"], m)

            # 1) RUG: likidite girisin yarisinin altina indi
            if p["liq"] is not None and p["liq"] < RUG_LIQ_DROP * pos["entry_liq"]:
                sell_position(sk, acct, pos, 1.0, p["price"], p["liq"], "RUG", now, zero=True)
                continue

            # 2) cikis kurallari
            age_h = (now - parse_iso(pos["opened"])).total_seconds() / 3600
            trig = None
            if m <= cfg["stop_mult"]:
                trig = ("STOP", 1.0)
            elif m >= cfg["tp_mult"] and not pos["partial"]:
                trig = ("TP", cfg["tp_fraction"])
            elif age_h >= cfg["max_hold_h"]:
                trig = ("SURE", 1.0)
            if not trig:
                continue

            # 3) satmak isterken: son 1 saatte hic satis yok, alim var -> satilamaz suphesi
            if looks_unsellable(p):
                sell_position(sk, acct, pos, 1.0, p["price"], p["liq"], "SATILAMAZ", now, zero=True)
            else:
                sell_position(sk, acct, pos, trig[1], p["price"], p["liq"], trig[0], now)


# ---------------------------------------------------------------------
#  Giris (yeni demo alimlar)
# ---------------------------------------------------------------------
def screen(cfg, p, now, acct, f):
    f["tarama"] += 1
    if (not p["mint"] or p["mint"] in acct["seen"] or p["mint"] in acct["rejected"]
            or not p["price"] or p["price"] <= 0 or p["liq"] is None or not p["created"]
            or p["quote"].upper() not in QUOTES):
        return False
    age = (now - p["created"]).total_seconds() / 60
    if not (cfg["min_age"] <= age <= cfg["max_age"]):
        return False
    f["yas"] += 1
    if p["liq"] < cfg["min_liq"]:
        return False
    f["likidite"] += 1
    if cfg["min_vol24"] > 0 and (p["vol24"] is None or p["vol24"] < cfg["min_vol24"]):
        return False
    f["hacim"] += 1
    if cfg["need_h1_up"] and not (p["h1"] is not None and p["h1"] > 0):
        return False
    f["yukselis"] += 1
    return True


def do_entries(state, now):
    day = trt_day(now)
    cache = {}
    for sk, cfg in STRATEGIES.items():
        acct = state["accounts"][sk]
        free = MAX_OPEN - len(acct["positions"])
        if free <= 0 or acct["cash"] < TRADE_SIZE - 1e-9:
            continue
        if cfg["source"] not in cache:
            cache[cfg["source"]] = (fetch_new_pools() if cfg["source"] == "new"
                                    else fetch_established_pools())
        pools = cache[cfg["source"]]
        f = acct["funnel"].setdefault(day, {"tarama": 0, "yas": 0, "likidite": 0, "hacim": 0,
                                            "yukselis": 0, "guvenlik_red": 0, "giris": 0})
        cands = [p for p in pools if screen(cfg, p, now, acct, f)]
        cands.sort(key=lambda p: p["liq"], reverse=True)
        rc_calls = 0
        for p in cands:
            if free <= 0 or acct["cash"] < TRADE_SIZE - 1e-9:
                break
            if rc_calls >= MAX_RUGCHECK_PER_RUN:
                break
            rc = rugcheck(p["mint"])
            rc_calls += 1
            if cfg["use_filter"]:
                if rc is None:
                    continue                      # tarama basarisiz: bu sefer atla
                if rc["danger"]:
                    acct["rejected"].append(p["mint"])
                    f["guvenlik_red"] += 1
                    continue
                label = "temiz"
            else:
                label = "bilinmiyor" if rc is None else ("riskli" if rc["danger"] else "temiz")
            open_position(sk, acct, p, label, rc, now)
            f["giris"] += 1
            free -= 1


# ---------------------------------------------------------------------
#  Raporlar
# ---------------------------------------------------------------------
def equity(acct):
    return acct["cash"] + sum(pos["tokens"] * pos["last_price"] for pos in acct["positions"])


def stats(acct, days=None):
    cl = [c for c in acct["closed"] if days is None or c["closed_day"] in days]
    n = len(cl)
    wins = sum(1 for c in cl if c["pnl"] > 0)
    rugs = [c for c in cl if c["reason"] in ("RUG", "SATILAMAZ")]
    return {
        "cl": cl, "n": n, "wins": wins, "losses": n - wins,
        "rate": (wins / n * 100) if n else 0.0,
        "pnl": sum(c["pnl"] for c in cl), "fees": sum(c["fees"] for c in cl),
        "rugs": rugs, "rug_loss": sum(c["cost"] - c["proceeds"] for c in rugs),
        "best": max(cl, key=lambda c: c["pnl"]) if cl else None,
        "worst": min(cl, key=lambda c: c["pnl"]) if cl else None,
    }


def strategy_block(sk, acct, days, label):
    cfg = STRATEGIES[sk]
    s = stats(acct, days)
    L = [f"【{cfg['name']}】 ({label})"]
    L.append(f"Kapanan işlem: {s['n']} (kazanan {s['wins']}, kaybeden {s['losses']}, isabet %{s['rate']:.0f})")
    L.append(f"Net K/Z: {usd(s['pnl'], True)} | Toplam maliyet (ücret+kayma): {usd(s['fees'])}")
    if s["best"]:
        L.append(f"En iyi: {s['best']['token']} {usd(s['best']['pnl'], True)} | "
                 f"En kötü: {s['worst']['token']} {usd(s['worst']['pnl'], True)}")
    L.append(f"RUG / satılamaz olaylar: {len(s['rugs'])} işlem, {usd(-s['rug_loss'], True) if s['rugs'] else '0.00 $'}")
    for c in s["rugs"][:8]:
        tag = "RUG" if c["reason"] == "RUG" else "SATILAMAZ şüphesi"
        L.append(f"   • {c['token']} ({short(c['mint'])}): {tag}, {usd(c['pnl'], True)}, "
                 f"alımdaki güvenlik etiketi: {c['risk']}")
    if len(s["rugs"]) > 8:
        L.append(f"   ... ve {len(s['rugs']) - 8} tane daha (CSV'de)")
    return "\n".join(L), s


def funnel_text(acct, days):
    tot = {"tarama": 0, "yas": 0, "likidite": 0, "hacim": 0, "yukselis": 0, "guvenlik_red": 0, "giris": 0}
    for d, f in acct["funnel"].items():
        if days is None or d in days:
            for k in tot:
                tot[k] += f.get(k, 0)
    return (f"Tarama (tekrarlar dahil): {tot['tarama']} → yaşa uyan {tot['yas']} → likiditeye uyan "
            f"{tot['likidite']} → hacim/yükseliş uyan {tot['yukselis']} → güvenlik reddi {tot['guvenlik_red']} "
            f"→ GİRİLEN {tot['giris']}")


def risk_table(acct):
    """A icin: guvenlik etiketine gore sonuc dagilimi (tum zamanlar)."""
    rows = {}
    for c in acct["closed"]:
        r = rows.setdefault(c["risk"], {"n": 0, "bad": 0, "pnl": 0.0})
        r["n"] += 1
        r["bad"] += 1 if c["reason"] in ("RUG", "SATILAMAZ") else 0
        r["pnl"] += c["pnl"]
    if not rows:
        return ""
    out = ["Güvenlik etiketine göre (tüm zamanlar):"]
    for k in ("riskli", "temiz", "bilinmiyor"):
        if k in rows:
            r = rows[k]
            out.append(f"   • {k}: {r['n']} işlem, {r['bad']} tanesi rug/satılamaz, net {usd(r['pnl'], True)}")
    return "\n".join(out)


def open_text(sk, acct):
    return (f"Açık pozisyon: {len(acct['positions'])} | Nakit: {usd(acct['cash'])} | "
            f"Toplam değer: {usd(equity(acct))} (başlangıç {usd(START_BALANCE)}, "
            f"toplam K/Z {usd(equity(acct) - START_BALANCE, True)})")


def build_report(state, days, title):
    A, B = state["accounts"]["A"], state["accounts"]["B"]
    parts = [title, ""]
    lbl = "bu dönem" if days else "tüm deney"
    blkA, sA = strategy_block("A", A, days, lbl)
    blkB, sB = strategy_block("B", B, days, lbl)
    parts += [blkA, funnel_text(A, days), "", blkB, funnel_text(B, days), ""]
    parts += ["── TOPLAM (deney başından beri) ──",
              f"A: {open_text('A', A)}", f"B: {open_text('B', B)}", ""]
    rt = risk_table(A)
    if rt:
        parts += ["A - " + rt, ""]
    sa, sb = stats(A), stats(B)
    parts += ["── KARŞILAŞTIRMA (tüm zamanlar) ──",
              f"A: {sa['n']} işlem, isabet %{sa['rate']:.0f}, net {usd(sa['pnl'], True)}, maliyet {usd(sa['fees'])}",
              f"B: {sb['n']} işlem, isabet %{sb['rate']:.0f}, net {usd(sb['pnl'], True)}, maliyet {usd(sb['fees'])}"]
    return "\n".join(parts)


DISCLAIMER = ("\n\nNot: Demo sonuçlar gerçekten iyimser olabilir (botlar, gerçek kayma ve satış engelleri "
              "tam simüle edilemez). Rug sayımı muhafazakâr: likidite yarıya inerse pozisyon sıfıra yazılır.")


def maybe_daily_report(state, now):
    today = datetime.fromisoformat(trt_day(now)).date()
    yesterday = today - timedelta(days=1)
    rep = datetime.fromisoformat(state["reported_through"]).date()
    if yesterday <= rep:
        return
    days = set()
    d = rep + timedelta(days=1)
    while d <= yesterday:
        days.add(d.isoformat())
        d += timedelta(days=1)
    title = f"📊 GÜNLÜK RAPOR - {yesterday.isoformat()}" if len(days) == 1 else \
        f"📊 RAPOR ({min(days)} → {max(days)})"
    tg_send(build_report(state, days, title) + DISCLAIMER)
    state["reported_through"] = yesterday.isoformat()


def maybe_final(state, now):
    if state["final_sent"] or now < parse_iso(state["end"]):
        return
    if any(acct["positions"] for acct in state["accounts"].values()):
        return
    tg_send(build_report(state, None, "🏁 FİNAL RAPOR - 7 günlük deney bitti") + DISCLAIMER)
    tg_doc(TRADES_CSV, "Tüm demo işlemler (alım/satış)")
    tg_doc(POSITIONS_CSV, "Kapanan pozisyonlar özeti")
    tg_send("Deney tamamlandı. GitHub'da Actions sekmesinden bu otomasyonu (workflow) kapatabilirsin.")
    state["final_sent"] = True


# ---------------------------------------------------------------------
#  Ana akis
# ---------------------------------------------------------------------
def startup_message(state):
    A, B = STRATEGIES["A"], STRATEGIES["B"]
    return (
        "🤖 Demo memecoin botu başladı (SAHTE PARA, Solana)\n"
        f"Süre: {EXPERIMENT_DAYS} gün | Her strateji: {usd(START_BALANCE)} bakiye, işlem başı {usd(TRADE_SIZE)}, "
        f"en fazla {MAX_OPEN} açık pozisyon\n\n"
        f"A (hızlı serseri): 10 dakikadan genç token, likidite ≥ {A['min_liq']} $, güvenlik filtresi YOK. "
        f"2x'te %50 sat, -%40 stop, {A['max_hold_h']} saat limit\n"
        f"B (güvenli hızlı kâr): 2-24 saatlik token, likidite ≥ {B['min_liq']} $, hacim ≥ {B['min_vol24']} $, "
        f"son 1 saat yükseliş, güvenlik filtresi VAR. +%25'te sat, -%15 stop, {B['max_hold_h']} saat limit\n\n"
        "Maliyet modeli: %1 ücret + işlem başı 0.10 $ + likiditeye bağlı kayma.\n"
        "Rapor: her gece 00:00 (TR saati). Her alım/satımda mesaj gelir."
    )


def main():
    now = utcnow()
    ensure_csv(TRADES_CSV, TRADE_FIELDS)
    ensure_csv(POSITIONS_CSV, POS_FIELDS)
    state = load_state()
    if state is None:
        state = init_state(now)
        tg_send(startup_message(state))
    if state.get("final_sent"):
        log("Deney bitti, yapilacak is yok.")
        save_state(state)
        return

    ending = now >= parse_iso(state["end"])
    steps = [("takip", lambda: track_positions(state, now))]
    if not ending:
        steps.append(("giris", lambda: do_entries(state, now)))
    steps.append(("rapor", lambda: maybe_daily_report(state, now)))
    steps.append(("final", lambda: maybe_final(state, now)))
    for name, fn in steps:
        try:
            fn()
        except Exception as e:
            log(f"HATA ({name}):", type(e).__name__, str(e)[:200])

    if os.environ.get("REPORT_NOW", "").lower() == "true":
        today = {trt_day(now)}
        tg_send(build_report(state, today, "🧪 TEST RAPORU (bugüne kadar)") + DISCLAIMER)

    save_state(state)
    A, B = state["accounts"]["A"], state["accounts"]["B"]
    log(f"Bitti. A: acik={len(A['positions'])} kapali={len(A['closed'])} | "
        f"B: acik={len(B['positions'])} kapali={len(B['closed'])}")


if __name__ == "__main__":
    main()
