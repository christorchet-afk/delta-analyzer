"""
DELTA ANALYZER — Railway Edition
=================================
Script autonome de monitoring Polymarket + Dashboard web intégré
Accessible depuis iPhone via URL Railway publique

Dashboard : https://ton-app.railway.app
DB        : delta_analysis.db (local Railway)
"""

import os
import re
import sys
import time
import json
import math
import sqlite3
import threading
import requests
from datetime import datetime
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler

# ═══════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════

DB_PATH  = 'delta_analysis.db'
PORT     = int(os.environ.get('PORT', 8080))

ASSETS = ["btc", "eth", "sol", "xrp", "doge", "hype", "bnb"]
DUREES = [("5m", 300), ("15m", 900), ("4h", 14400)]

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_URL  = "https://clob.polymarket.com"

TIMING_SNAPSHOTS = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
DELTA_SEUILS     = [0.02, 0.03, 0.05, 0.07, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30]

# Mapping asset → paire Kraken (pas de restrictions géo)
KRAKEN_PAIRS = {
    "btc":  "XBTUSD",
    "eth":  "ETHUSD",
    "sol":  "SOLUSD",
    "xrp":  "XRPUSD",
    "doge": "DOGEUSD",
    "bnb":  "BNBUSD",
    "hype": "HYPEUSD",
}

sess = requests.Session()
sess.headers.update({'User-Agent': 'DeltaAnalyzer/2.0'})


# ═══════════════════════════════════════════════════════════════════════
# ÉTAT GLOBAL
# ═══════════════════════════════════════════════════════════════════════

PRIX_LIVE: dict = {}
_prix_lock = threading.Lock()

PRIX_T0: dict = {}
_t0_lock = threading.Lock()

SNAPSHOTS: dict = defaultdict(dict)
_snap_lock = threading.Lock()

_db_lock = threading.Lock()

# Cache HTML dashboard
_html_cache = ""
_html_ts    = 0


# ═══════════════════════════════════════════════════════════════════════
# BASE DE DONNÉES
# ═══════════════════════════════════════════════════════════════════════

def init_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS cycles (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_creation     INTEGER,
        asset           TEXT,
        duree           TEXT,
        market_key      TEXT UNIQUE,
        slug            TEXT,
        condition_id    TEXT,
        start_ts        INTEGER,
        end_ts          INTEGER,
        prix_t0_binance REAL,
        resultat        TEXT,
        ts_resolution   INTEGER
    );
    CREATE TABLE IF NOT EXISTS snapshots (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        market_key      TEXT,
        asset           TEXT,
        duree           TEXT,
        timing_s        INTEGER,
        ts_snap         INTEGER,
        prix_binance    REAL,
        prix_t0         REAL,
        delta_pct       REAL,
        direction_calc  TEXT,
        prix_clob_up    REAL,
        prix_clob_down  REAL,
        liquidite_up    REAL,
        liquidite_down  REAL,
        resultat        TEXT,
        correct         INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_snap_asset  ON snapshots(asset);
    CREATE INDEX IF NOT EXISTS idx_snap_timing ON snapshots(timing_s);
    CREATE INDEX IF NOT EXISTS idx_snap_market ON snapshots(market_key);
    CREATE INDEX IF NOT EXISTS idx_cycles_mkey ON cycles(market_key);
    """)
    db.commit()
    print(f"  [DB] OK → {DB_PATH}")
    return db


# ═══════════════════════════════════════════════════════════════════════
# PRIX KRAKEN — REST polling (pas de restriction géo)
# ═══════════════════════════════════════════════════════════════════════

def fetch_kraken_prices():
    """Récupère tous les prix via Kraken REST API — toutes les 3s."""
    while True:
        try:
            pairs = ",".join(KRAKEN_PAIRS.values())
            r = sess.get(
                f"https://api.kraken.com/0/public/Ticker?pair={pairs}",
                timeout=8
            )
            if r.status_code == 200:
                data = r.json().get('result', {})
                for asset, pair in KRAKEN_PAIRS.items():
                    # Kraken peut renommer les paires (ex: XBTUSD → XXBTZUSD)
                    for key, val in data.items():
                        if pair.replace('USD','') in key or pair in key:
                            price = float(val['c'][0])  # 'c' = last trade price
                            if price > 0:
                                with _prix_lock:
                                    PRIX_LIVE[asset] = price
                            break
                with _prix_lock:
                    nb = len(PRIX_LIVE)
                if nb > 0:
                    print(f"  [KRAKEN] {nb}/{len(ASSETS)} prix OK — BTC={PRIX_LIVE.get('btc','?'):.0f}$" if 'btc' in PRIX_LIVE else f"  [KRAKEN] {nb} prix OK")
        except Exception as e:
            print(f"  [KRAKEN] Erreur: {e}")
        time.sleep(3)

def start_ws():
    """Lance le polling Kraken en arrière-plan."""
    t = threading.Thread(target=fetch_kraken_prices, daemon=True)
    t.start()
    print("  [KRAKEN] Polling démarré ✅")


# ═══════════════════════════════════════════════════════════════════════
# POLYMARKET
# ═══════════════════════════════════════════════════════════════════════

def get_market_info(asset: str, duree_label: str) -> dict | None:
    try:
        params = {"tag_slug": f"crypto-{asset}", "active": "true",
                  "closed": "false", "limit": 50}
        r = sess.get(f"{GAMMA_API}/markets", params=params, timeout=10)
        if r.status_code != 200:
            return None
        markets = r.json()
        if isinstance(markets, dict):
            markets = markets.get('markets', [])

        now = int(time.time())
        duree_s = {"5m": 300, "15m": 900, "4h": 14400}.get(duree_label, 300)
        candidates = []

        for m in markets:
            slug = m.get('slug', '').lower()
            q    = m.get('question', '').lower()
            if not (asset.lower() in slug or asset.lower() in q):
                continue
            if not (duree_label in slug or duree_label in q):
                continue
            end_ts = m.get('endDateIso') or m.get('end_date_iso')
            if end_ts:
                try:
                    if isinstance(end_ts, str):
                        from datetime import datetime
                        dt = datetime.fromisoformat(end_ts.replace('Z', '+00:00'))
                        end_ts = int(dt.timestamp())
                    if abs(end_ts - now) > duree_s * 3:
                        continue
                    candidates.append((abs(end_ts - now - duree_s), m, end_ts))
                except Exception:
                    pass

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0])
        _, best, end_ts = candidates[0]
        tokens = best.get('tokens', best.get('outcomes', []))
        token_up = token_down = tid_up = tid_down = None

        for t in tokens:
            name = (t.get('outcome') or t.get('name') or '').upper()
            tid  = t.get('token_id') or t.get('id') or ''
            if 'UP' in name or 'HIGHER' in name or 'YES' in name:
                token_up, tid_up = float(t.get('price', 0.5)), tid
            elif 'DOWN' in name or 'LOWER' in name or 'NO' in name:
                token_down, tid_down = float(t.get('price', 0.5)), tid

        return {'slug': best.get('slug', ''),
                'condition_id': best.get('condition_id', ''),
                'end_ts': end_ts,
                'prix_up': token_up or 0.5, 'prix_down': token_down or 0.5,
                'tid_up': tid_up, 'tid_down': tid_down}
    except Exception:
        return None


def get_clob_prices(tid_up, tid_down):
    try:
        prix_up = prix_down = liq_up = liq_down = None
        for tid, side in [(tid_up, 'up'), (tid_down, 'down')]:
            if not tid:
                continue
            r = sess.get(f"{CLOB_URL}/book", params={"token_id": tid}, timeout=5)
            if r.status_code != 200:
                continue
            book = r.json()
            asks = book.get('asks', [])
            if asks:
                best_ask = float(asks[0].get('price', 0))
                liq = sum(float(a.get('size', 0)) for a in asks[:3])
                if side == 'up':
                    prix_up, liq_up = best_ask, liq
                else:
                    prix_down, liq_down = best_ask, liq
        return prix_up, prix_down, liq_up, liq_down
    except Exception:
        return None, None, None, None


def get_resolution(condition_id):
    try:
        r = sess.get(
            f"https://data-api.polymarket.com/markets/{condition_id}",
            timeout=10)
        if r.status_code != 200:
            return None
        for o in r.json().get('outcomes', []):
            if float(o.get('price', 0)) > 0.99:
                name = (o.get('outcome') or o.get('name') or '').upper()
                if 'UP' in name or 'HIGHER' in name or 'YES' in name:
                    return 'UP'
                elif 'DOWN' in name or 'LOWER' in name or 'NO' in name:
                    return 'DOWN'
        return None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════
# SCANNER
# ═══════════════════════════════════════════════════════════════════════

def scanner(db):
    now = int(time.time())
    for asset in ASSETS:
        for duree_label, period_s in DUREES:
            try:
                info = get_market_info(asset, duree_label)
                if not info:
                    continue

                end_ts     = info['end_ts']
                start_ts   = end_ts - period_s
                secs_left  = end_ts - now
                market_key = f"{asset}-{duree_label}-{start_ts}"

                with _db_lock:
                    existing = db.execute(
                        "SELECT id, prix_t0_binance FROM cycles WHERE market_key=?",
                        (market_key,)).fetchone()

                    if not existing:
                        with _prix_lock:
                            p_t0 = PRIX_LIVE.get(asset)
                        db.execute("""
                            INSERT OR IGNORE INTO cycles
                            (ts_creation,asset,duree,market_key,slug,
                             condition_id,start_ts,end_ts,prix_t0_binance)
                            VALUES (?,?,?,?,?,?,?,?,?)
                        """, (now, asset, duree_label, market_key,
                              info['slug'], info['condition_id'],
                              start_ts, end_ts, p_t0))
                        db.commit()
                        if p_t0:
                            with _t0_lock:
                                PRIX_T0[market_key] = p_t0
                        print(f"  [NEW] {asset.upper()} {duree_label} T-{secs_left}s")
                    else:
                        with _t0_lock:
                            if market_key not in PRIX_T0 and existing[1]:
                                PRIX_T0[market_key] = existing[1]

                if secs_left < 0 or secs_left > 12:
                    continue

                with _prix_lock:
                    prix_now = PRIX_LIVE.get(asset)
                with _t0_lock:
                    prix_t0 = PRIX_T0.get(market_key)

                if not prix_now or not prix_t0:
                    continue

                delta_pct      = (prix_now - prix_t0) / prix_t0 * 100
                direction_calc = 'UP' if delta_pct > 0 else 'DOWN'
                prix_up, prix_down, liq_up, liq_down = get_clob_prices(
                    info.get('tid_up'), info.get('tid_down'))

                for timing in TIMING_SNAPSHOTS:
                    if secs_left > timing or secs_left < timing - 1:
                        continue
                    with _db_lock:
                        if db.execute(
                            "SELECT id FROM snapshots WHERE market_key=? AND timing_s=?",
                                (market_key, timing)).fetchone():
                            continue
                        db.execute("""
                            INSERT INTO snapshots
                            (market_key,asset,duree,timing_s,ts_snap,
                             prix_binance,prix_t0,delta_pct,direction_calc,
                             prix_clob_up,prix_clob_down,liquidite_up,liquidite_down)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """, (market_key, asset, duree_label, timing, now,
                              prix_now, prix_t0, delta_pct, direction_calc,
                              prix_up, prix_down, liq_up, liq_down))
                        db.commit()
                    print(f"  [SNAP] {asset.upper()} {duree_label} "
                          f"T-{timing}s delta={delta_pct:+.3f}% {direction_calc}")

            except Exception as e:
                print(f"  [ERR] {asset} {duree_label}: {e}")


def resolution_checker(db):
    now = int(time.time())
    with _db_lock:
        cycles = db.execute("""
            SELECT market_key, condition_id, end_ts
            FROM cycles WHERE resultat IS NULL
            AND end_ts < ? AND end_ts > ?
        """, (now, now - 3600)).fetchall()

    for market_key, condition_id, end_ts in cycles:
        if now < end_ts + 10:
            continue
        resultat = get_resolution(condition_id)
        if not resultat:
            continue
        with _db_lock:
            db.execute(
                "UPDATE cycles SET resultat=?, ts_resolution=? WHERE market_key=?",
                (resultat, now, market_key))
            db.execute("""
                UPDATE snapshots SET resultat=?,
                correct=CASE WHEN direction_calc=? THEN 1 ELSE 0 END
                WHERE market_key=?
            """, (resultat, resultat, market_key))
            db.commit()
        print(f"  [RES] {market_key} → {resultat} ✅")


# ═══════════════════════════════════════════════════════════════════════
# ANALYSE
# ═══════════════════════════════════════════════════════════════════════

def analyser(db) -> dict:
    results = {}

    # WR par timing
    wr_timing = {}
    for timing in TIMING_SNAPSHOTS:
        row = db.execute("""
            SELECT COUNT(*), SUM(correct),
            AVG(CASE WHEN correct=1 THEN
                CASE direction_calc WHEN 'UP' THEN (1.0/prix_clob_up)-1
                ELSE (1.0/prix_clob_down)-1 END ELSE -1.0 END)
            FROM snapshots WHERE timing_s=? AND resultat IS NOT NULL
        """, (timing,)).fetchone()
        total, wins, ev = row
        if total and total >= 5:
            wr_timing[timing] = {
                'total': total, 'wins': wins or 0,
                'wr': round((wins or 0) / total * 100, 1),
                'ev': round((ev or 0) * 100, 2)
            }
    results['wr_timing'] = wr_timing

    # Delta optimal par asset
    delta_optimal = {}
    for asset in ASSETS:
        best = None
        best_score = 0
        for seuil in DELTA_SEUILS:
            row = db.execute("""
                SELECT COUNT(*), SUM(correct) FROM snapshots
                WHERE asset=? AND timing_s=5
                AND ABS(delta_pct)>=? AND resultat IS NOT NULL
            """, (asset, seuil)).fetchone()
            total, wins = row
            if total and total >= 5:
                wr = (wins or 0) / total * 100
                score = wr * math.log(max(total, 1))
                if score > best_score:
                    best_score = score
                    best = {'seuil': seuil, 'wr': round(wr, 1), 'total': total}
        delta_optimal[asset] = best
    results['delta_optimal'] = delta_optimal

    # WR par delta × timing × asset
    wr_delta = {}
    for asset in ASSETS:
        wr_delta[asset] = {}
        for timing in [8, 7, 6, 5, 4, 3]:
            wr_delta[asset][timing] = []
            for seuil in DELTA_SEUILS:
                row = db.execute("""
                    SELECT COUNT(*), SUM(correct) FROM snapshots
                    WHERE asset=? AND timing_s=? AND ABS(delta_pct)>=?
                    AND resultat IS NOT NULL
                """, (asset, timing, seuil)).fetchone()
                total, wins = row
                if total and total >= 3:
                    wr_delta[asset][timing].append({
                        'seuil': seuil, 'total': total,
                        'wr': round((wins or 0) / total * 100, 1)
                    })
    results['wr_delta'] = wr_delta

    # Rentabilité par prix CLOB
    rent_prix = {}
    prix_buckets = [(0.75,0.80),(0.80,0.85),(0.85,0.88),
                    (0.88,0.91),(0.91,0.93),(0.93,0.95),(0.95,0.97),(0.97,0.99)]
    for timing in [8, 7, 6, 5, 4, 3]:
        rent_prix[timing] = []
        for pmin, pmax in prix_buckets:
            row = db.execute("""
                SELECT COUNT(*), SUM(correct),
                AVG(CASE direction_calc WHEN 'UP' THEN prix_clob_up
                    ELSE prix_clob_down END)
                FROM snapshots WHERE timing_s=?
                AND ((direction_calc='UP' AND prix_clob_up BETWEEN ? AND ?)
                  OR (direction_calc='DOWN' AND prix_clob_down BETWEEN ? AND ?))
                AND resultat IS NOT NULL
            """, (timing, pmin, pmax, pmin, pmax)).fetchone()
            total, wins, prix_moy = row
            if total and total >= 3 and prix_moy:
                wr = (wins or 0) / total * 100
                rdt = (1 / prix_moy - 1) * 100
                ev  = (wr/100 * rdt) - ((1-wr/100) * 100)
                rent_prix[timing].append({
                    'pmin': pmin, 'pmax': pmax, 'total': total,
                    'wr': round(wr, 1), 'prix_moy': round(prix_moy, 3),
                    'rdt': round(rdt, 1), 'ev': round(ev, 2)
                })
    results['rent_prix'] = rent_prix

    # WR par durée
    wr_duree = {}
    for duree_label, _ in DUREES:
        row = db.execute("""
            SELECT COUNT(*), SUM(correct), COUNT(DISTINCT market_key)
            FROM snapshots WHERE duree=? AND timing_s=5 AND resultat IS NOT NULL
        """, (duree_label,)).fetchone()
        total, wins, nb = row
        if total and total >= 3:
            wr_duree[duree_label] = {
                'total': total, 'wins': wins or 0,
                'wr': round((wins or 0) / total * 100, 1), 'nb_cycles': nb
            }
    results['wr_duree'] = wr_duree

    # Stats générales
    stats = db.execute("""
        SELECT COUNT(DISTINCT market_key), COUNT(*),
        SUM(CASE WHEN resultat IS NOT NULL THEN 1 ELSE 0 END),
        MIN(ts_snap), MAX(ts_snap)
        FROM snapshots
    """).fetchone()
    results['stats'] = {
        'nb_cycles': stats[0] or 0,
        'nb_snapshots': stats[1] or 0,
        'resolus': stats[2] or 0,
        'debut': datetime.fromtimestamp(stats[3]).strftime('%d/%m %H:%M') if stats[3] else 'N/A',
        'fin': datetime.fromtimestamp(stats[4]).strftime('%d/%m %H:%M') if stats[4] else 'N/A',
    }

    # Prix live
    with _prix_lock:
        results['prix_live'] = dict(PRIX_LIVE)

    return results


# ═══════════════════════════════════════════════════════════════════════
# DASHBOARD HTML
# ═══════════════════════════════════════════════════════════════════════

def wr_color(wr):
    if wr >= 93: return '#00ff88'
    if wr >= 88: return '#88ff44'
    if wr >= 82: return '#ffcc00'
    if wr >= 75: return '#ff8800'
    return '#ff4444'

def ev_color(ev):
    if ev > 3:  return '#00ff88'
    if ev > 0:  return '#88ff44'
    if ev > -5: return '#ffcc00'
    return '#ff4444'


def generer_html(results: dict) -> str:
    stats       = results.get('stats', {})
    wr_timing   = results.get('wr_timing', {})
    rent_prix   = results.get('rent_prix', {})
    delta_opt   = results.get('delta_optimal', {})
    wr_duree    = results.get('wr_duree', {})
    wr_delta    = results.get('wr_delta', {})
    prix_live   = results.get('prix_live', {})
    now_str     = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
    resolus     = stats.get('resolus', 0)

    # Prix live bar
    live_bar = ""
    for asset in ASSETS:
        p = prix_live.get(asset)
        live_bar += f'<div class="live-item"><span class="live-sym">{asset.upper()}</span><span class="live-price">${p:,.2f}</span></div>' if p else f'<div class="live-item"><span class="live-sym">{asset.upper()}</span><span class="live-price dim">—</span></div>'

    # WR timing table
    html_timing = ""
    for t in sorted(wr_timing.keys(), reverse=True):
        d = wr_timing[t]
        c1 = wr_color(d['wr'])
        c2 = ev_color(d['ev'])
        html_timing += f'<tr><td class="mono">T-{t}s</td><td style="color:{c1};font-weight:700">{d["wr"]}%</td><td style="color:{c2}">{d["ev"]:+.1f}%</td><td class="dim">{d["wins"]}/{d["total"]}</td></tr>'

    # Delta optimal
    html_delta = ""
    for asset in ASSETS:
        d = delta_opt.get(asset)
        if d:
            c = wr_color(d['wr'])
            html_delta += f'<tr><td class="asset-tag">{asset.upper()}</td><td class="mono">{d["seuil"]}%</td><td style="color:{c};font-weight:700">{d["wr"]}%</td><td class="dim">{d["total"]}</td></tr>'
        else:
            html_delta += f'<tr><td class="asset-tag">{asset.upper()}</td><td class="dim" colspan="3">En collecte...</td></tr>'

    # Durée
    html_duree = ""
    for dl, _ in DUREES:
        d = wr_duree.get(dl)
        if d:
            c = wr_color(d['wr'])
            html_duree += f'<tr><td class="mono">{dl}</td><td style="color:{c};font-weight:700">{d["wr"]}%</td><td class="dim">{d["nb_cycles"]} cycles</td></tr>'

    # Rentabilité
    html_rent = ""
    for timing in [7, 5, 3]:
        data = rent_prix.get(timing, [])
        if not data:
            continue
        html_rent += f'<div class="section-label">T-{timing}s</div><table><tr><th>Prix CLOB</th><th>WR</th><th>Rdt/win</th><th>EV</th><th>N</th></tr>'
        for d in data:
            c1 = wr_color(d['wr'])
            c2 = ev_color(d['ev'])
            html_rent += f'<tr><td class="mono">{d["pmin"]:.2f}–{d["pmax"]:.2f}</td><td style="color:{c1};font-weight:700">{d["wr"]}%</td><td class="mono">{d["rdt"]:+.1f}%</td><td style="color:{c2}">{d["ev"]:+.2f}%</td><td class="dim">{d["total"]}</td></tr>'
        html_rent += '</table>'

    # Delta détail par asset
    html_delta_detail = ""
    for asset in ASSETS:
        asset_data = wr_delta.get(asset, {})
        has = any(asset_data.get(t) for t in [7, 5, 3])
        if not has:
            continue
        html_delta_detail += f'<div class="section-label">{asset.upper()}</div>'
        html_delta_detail += '<table><tr><th>Delta min</th>'
        for t in [8, 7, 6, 5, 4, 3]:
            html_delta_detail += f'<th>T-{t}s</th>'
        html_delta_detail += '</tr>'
        for seuil in DELTA_SEUILS:
            row = f'<td class="mono">{seuil}%</td>'
            has_row = False
            for t in [8, 7, 6, 5, 4, 3]:
                found = next((d for d in asset_data.get(t, []) if d['seuil'] == seuil), None)
                if found:
                    c = wr_color(found['wr'])
                    row += f'<td style="color:{c}">{found["wr"]}%<span class="dim"> ({found["total"]})</span></td>'
                    has_row = True
                else:
                    row += '<td class="dim">—</td>'
            if has_row:
                html_delta_detail += f'<tr>{row}</tr>'
        html_delta_detail += '</table>'

    notice = f'<div class="notice">⏳ Collecte en cours — <strong>{resolus}</strong> cycles résolus sur ~50 nécessaires pour des analyses fiables.</div>' if resolus < 20 else ''

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>⚡ Delta Analyzer</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@400;700;800&display=swap');
:root{{--bg:#080b0f;--bg2:#0f1318;--bg3:#161b22;--border:#1c2230;--text:#b8c8d8;--dim:#3a4a5a;--accent:#00d4ff;--green:#00ff88;--yellow:#ffd000;--red:#ff4455}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:'Syne',sans-serif;padding:16px;min-height:100vh}}
.header{{border-bottom:1px solid var(--border);padding-bottom:14px;margin-bottom:20px;display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:10px}}
.title{{font-size:24px;font-weight:800;color:var(--accent)}}
.sub{{font-size:12px;color:var(--dim);margin-top:3px}}
.meta{{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--dim);text-align:right}}
.live-bar{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px}}
.live-item{{background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;display:flex;flex-direction:column;min-width:80px}}
.live-sym{{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:1px}}
.live-price{{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;color:var(--accent);margin-top:2px}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:20px}}
.stat{{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:14px}}
.stat-label{{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:1px}}
.stat-val{{font-size:22px;font-weight:800;color:var(--accent);font-family:'JetBrains Mono',monospace;margin-top:4px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}}
@media(max-width:600px){{.grid{{grid-template-columns:1fr}}}}
.card{{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:16px}}
.card-title{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:var(--accent);margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid var(--border)}}
.full{{grid-column:1/-1}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{text-align:left;padding:5px 8px;font-size:10px;text-transform:uppercase;letter-spacing:.8px;color:var(--dim);border-bottom:1px solid var(--border)}}
td{{padding:6px 8px;border-bottom:1px solid rgba(28,34,48,.5)}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:var(--bg3)}}
.mono{{font-family:'JetBrains Mono',monospace}}
.dim{{color:var(--dim);font-size:11px}}
.asset-tag{{font-weight:700;color:var(--accent)}}
.section-label{{font-size:11px;font-weight:700;color:var(--yellow);text-transform:uppercase;letter-spacing:1px;margin:12px 0 6px}}
.notice{{background:rgba(0,212,255,.05);border:1px solid rgba(0,212,255,.2);border-radius:8px;padding:12px 16px;font-size:12px;color:var(--dim);margin-bottom:16px}}
.notice strong{{color:var(--accent)}}
.empty{{color:var(--dim);font-size:12px;padding:16px 0}}
</style>
</head>
<body>
<div class="header">
  <div>
    <div class="title">⚡ DELTA ANALYZER</div>
    <div class="sub">Polymarket Crypto Up/Down — Monitoring temps réel</div>
  </div>
  <div class="meta">Refresh auto 60s<br><strong style="color:var(--text)">{now_str}</strong></div>
</div>

<div class="live-bar">{live_bar}</div>

<div class="stats">
  <div class="stat"><div class="stat-label">Cycles</div><div class="stat-val">{stats.get('nb_cycles',0)}</div></div>
  <div class="stat"><div class="stat-label">Snapshots</div><div class="stat-val">{stats.get('nb_snapshots',0)}</div></div>
  <div class="stat"><div class="stat-label">Résolus</div><div class="stat-val">{resolus}</div></div>
  <div class="stat"><div class="stat-label">Depuis</div><div class="stat-val" style="font-size:14px">{stats.get('debut','—')}</div></div>
</div>

{notice}

<div class="grid">
  <div class="card">
    <div class="card-title">📊 WR par timing</div>
    {'<div class="empty">En collecte...</div>' if not html_timing else f'<table><tr><th>Timing</th><th>Win Rate</th><th>EV</th><th>Trades</th></tr>{html_timing}</table>'}
  </div>
  <div class="card">
    <div class="card-title">🎯 Delta optimal / asset (T-5s)</div>
    <table><tr><th>Asset</th><th>Delta min</th><th>WR</th><th>N</th></tr>{html_delta}</table>
  </div>
</div>

<div class="card full" style="margin-bottom:14px">
  <div class="card-title">💰 Rentabilité par prix CLOB</div>
  {'<div class="empty">En collecte...</div>' if not html_rent else html_rent}
</div>

<div class="grid">
  <div class="card">
    <div class="card-title">⏱️ WR par durée (T-5s)</div>
    {'<div class="empty">En collecte...</div>' if not html_duree else f'<table><tr><th>Durée</th><th>WR</th><th>Cycles</th></tr>{html_duree}</table>'}
  </div>
  <div class="card">
    <div class="card-title">🎨 Légende</div>
    <table>
      <tr><td style="color:#00ff88;font-weight:700">≥ 93%</td><td>Excellent ✅</td></tr>
      <tr><td style="color:#88ff44;font-weight:700">88–93%</td><td>Bon ✅</td></tr>
      <tr><td style="color:#ffcc00;font-weight:700">82–88%</td><td>Limite ⚠️</td></tr>
      <tr><td style="color:#ff8800;font-weight:700">75–82%</td><td>Insuffisant ❌</td></tr>
      <tr><td style="color:#ff4455;font-weight:700">&lt; 75%</td><td>Éviter ❌</td></tr>
    </table>
    <div style="margin-top:12px;font-size:11px;color:var(--dim)">EV = espérance de valeur par trade<br>EV positif = stratégie profitable</div>
  </div>
</div>

<div class="card full">
  <div class="card-title">🔬 WR delta × timing × asset</div>
  {'<div class="empty">En collecte — besoin de ~50 trades par asset</div>' if not html_delta_detail else html_delta_detail}
</div>
</body></html>"""


# ═══════════════════════════════════════════════════════════════════════
# SERVEUR WEB
# ═══════════════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _html_cache, _html_ts
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(_html_cache.encode('utf-8'))

    def log_message(self, *args):
        pass  # Silencer les logs HTTP


def start_server():
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f"  [WEB] Dashboard → http://0.0.0.0:{PORT}")
    server.serve_forever()


# ═══════════════════════════════════════════════════════════════════════
# BOUCLE PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════

def main():
    global _html_cache, _html_ts

    print("=" * 55)
    print("   ⚡  DELTA ANALYZER — Railway Edition")
    print("=" * 55)
    print(f"   Assets  : {', '.join(a.upper() for a in ASSETS)}")
    print(f"   Marchés : {', '.join(d[0] for d in DUREES)}")
    print(f"   Port    : {PORT}")
    print("=" * 55)

    db = init_db()

    # Message initial dashboard
    _html_cache = "<html><body style='background:#080b0f;color:#00d4ff;font-family:monospace;padding:40px'><h2>⚡ Delta Analyzer démarrage...</h2><p>Connexion Binance en cours — rafraîchis dans 10s</p></body></html>"

    # Démarrer serveur web
    threading.Thread(target=start_server, daemon=True).start()

    # Démarrer WebSocket Binance
    print("\n  [WS] Connexion Binance...")
    start_ws()
    time.sleep(4)

    with _prix_lock:
        nb = len(PRIX_LIVE)
    print(f"  [WS] {nb}/{len(ASSETS)} assets connectés\n")

    last_scan  = 0.0
    last_res   = 0.0
    last_html  = 0.0

    while True:
        now = time.time()

        if now - last_scan >= 0.5:
            scanner(db)
            last_scan = now

        if now - last_res >= 30:
            resolution_checker(db)
            last_res = now

        if now - last_html >= 60:
            try:
                results = analyser(db)
                _html_cache = generer_html(results)
                _html_ts = now
            except Exception as e:
                print(f"  [HTML] Erreur: {e}")
            last_html = now

        time.sleep(0.1)


if __name__ == '__main__':
    main()
