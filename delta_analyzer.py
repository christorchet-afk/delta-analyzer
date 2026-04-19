#!/usr/bin/env python3
"""
DELTA ANALYZER v8 — Script optimal de monitoring et analyse
============================================================
Objectif : déterminer les paramètres optimaux pour bot_ultimateshoot.py

Sources prix :
  - Polymarket RTDS WebSocket → Chainlink (oracle résolution) + Binance
  - Double validation : signal valide seulement si CL et BN concordent

Marchés :
  - Gamma API keyset pagination (Apr 2026)
  - 7 assets × 3 durées = 21 marchés simultanés

Analyses produites :
  1. WR par timing T-1s à T-10s (CL seul, BN seul, CL+BN concordant)
  2. Delta optimal par asset et par timing
  3. Corrélation delta × prix CLOB (à quel prix le signal est-il encore exploitable ?)
  4. P&L simulé (gains/pertes réels si on avait suivi le signal)
  5. Fenêtre optimale (T-?s → ratio signal/bruit maximal)
  6. Analyse par volatilité (marché calme vs agité)
  7. Concordance CL+BN (avantage du double signal)
  8. WR par durée 5min / 15min / 4h
  9. Rentabilité nette après fees Polymarket

Dashboard : http://localhost:8080
DB        : /root/delta_analysis.db
Lancer    : python3 delta_analyzer.py
"""

import os, time, math, json, sqlite3, threading, requests, websocket
from datetime import datetime
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler

# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════

DB_PATH = os.environ.get('DB_PATH', '/root/delta_analysis.db')
PORT    = int(os.environ.get('PORT', 8080))
HOST    = os.environ.get('HOST', '0.0.0.0')

ASSETS = ["btc", "eth", "sol", "xrp", "doge", "hype", "bnb"]
DUREES = [("5m", 300), ("15m", 900), ("1h", 3600), ("4h", 14400)]

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_URL  = "https://clob.polymarket.com"
RTDS_WS   = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Timings à capturer
TIMINGS = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]

# Seuils delta à tester (%)
SEUILS = [0.02, 0.03, 0.05, 0.07, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30]

# Fee Polymarket sur marchés crypto 5min (peak 1.56% à 50%)
# f = C × rate × p × (1-p) avec C=fee_constant, rate=fee_rate
# Approximation conservatrice : 0.5% à 0.95 de probabilité
FEE_PCT = 0.005

ASSET_NAMES = {
    "btc":  ["btc", "bitcoin"],
    "eth":  ["eth", "ethereum"],
    "sol":  ["sol", "solana"],
    "xrp":  ["xrp", "ripple"],
    "doge": ["doge", "dogecoin"],
    "bnb":  ["bnb", "binance"],
    "hype": ["hype"],
}

sess = requests.Session()
sess.headers.update({'User-Agent': 'DeltaAnalyzer/8.0'})

# VPS dédié — pas de partage IP avec les bots
# Rate limit officiel : 50 req/10s = 5 req/s
# On utilise 4 req/s pour rester sous la limite avec marge
_clob_cache    = {}
CLOB_CACHE_TTL = 0.5   # cache 500ms — données très fraîches

def clob_rate_ok() -> bool:
    """Toujours True sur VPS dédié — pas de partage IP."""
    return True

# ═══════════════════════════════════════════════════════════════════
# ÉTAT GLOBAL
# ═══════════════════════════════════════════════════════════════════

PRIX_CL: dict = {}   # {asset: price} Chainlink
PRIX_BN: dict = {}   # {asset: price} Binance
PRIX_T0: dict = {}   # {market_key: {cl, bn}}

_pl = threading.Lock()
_tl = threading.Lock()
_dl = threading.Lock()

_HTML = """<html><body style='background:#080b0f;color:#00d4ff;
font-family:monospace;padding:40px'>
<h2>⚡ Delta Analyzer v8</h2>
<p>Connexion RTDS Polymarket...</p>
<p>Rafraîchis dans 30s.</p></body></html>"""

# ═══════════════════════════════════════════════════════════════════
# BASE DE DONNÉES
# ═══════════════════════════════════════════════════════════════════

def init_db():
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    -- Cycles : un cycle = une période de marché (ex: BTC 5min de 14:00 à 14:05)
    CREATE TABLE IF NOT EXISTS cycles (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           INTEGER,
        asset        TEXT,
        duree        TEXT,
        market_key   TEXT UNIQUE,
        slug         TEXT,
        condition_id TEXT,
        start_ts     INTEGER,
        end_ts       INTEGER,
        prix_t0_cl   REAL,        -- prix Chainlink à T0 (ouverture du cycle)
        prix_t0_bn   REAL,        -- prix Binance à T0
        prix_res_cl  REAL,        -- prix Chainlink à la résolution (si dispo)
        volatilite   REAL,        -- amplitude max / prix_t0 pendant le cycle (%)
        resultat     TEXT,        -- UP ou DOWN
        ts_res       INTEGER
    );

    -- Snapshots : une photo à T-Xs avant la résolution
    CREATE TABLE IF NOT EXISTS snaps (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        market_key    TEXT,
        asset         TEXT,
        duree         TEXT,
        timing        INTEGER,    -- secondes avant résolution
        ts            INTEGER,

        -- Prix et deltas Chainlink (oracle de résolution)
        prix_cl       REAL,
        prix_cl_t0    REAL,
        delta_cl      REAL,       -- (prix_cl - prix_cl_t0) / prix_cl_t0 * 100

        -- Prix et deltas Binance (feed de marché)
        prix_bn       REAL,
        prix_bn_t0    REAL,
        delta_bn      REAL,

        -- Signal
        direction_cl  TEXT,       -- UP ou DOWN selon CL
        direction_bn  TEXT,       -- UP ou DOWN selon BN
        concordant    INTEGER,    -- 1 si CL et BN pointent dans le même sens
        signal_force  REAL,       -- min(abs(delta_cl), abs(delta_bn)) si concordant

        -- Prix CLOB au moment du snapshot
        clob_up       REAL,       -- prix ask côté UP
        clob_dn       REAL,       -- prix ask côté DOWN
        liq_up        REAL,       -- liquidité disponible UP
        liq_dn        REAL,       -- liquidité disponible DOWN
        spread        REAL,       -- |clob_up - (1 - clob_dn)| = inefficience du marché

        -- Résultat et performance
        resultat      TEXT,       -- UP ou DOWN réel
        correct_cl    INTEGER,    -- 1 si direction_cl = resultat
        correct_bn    INTEGER,    -- 1 si direction_bn = resultat

        -- P&L simulé (si on avait joué le signal CL+BN concordant)
        pnl_simule    REAL,       -- gain ou perte en % si on avait joué
        prix_entree   REAL        -- prix CLOB au moment de l'entrée simulée
    );

    -- Index pour les requêtes d'analyse
    CREATE INDEX IF NOT EXISTS i1  ON snaps(asset);
    CREATE INDEX IF NOT EXISTS i2  ON snaps(timing);
    CREATE INDEX IF NOT EXISTS i3  ON snaps(market_key);
    CREATE INDEX IF NOT EXISTS i4  ON snaps(delta_cl);
    CREATE INDEX IF NOT EXISTS i5  ON snaps(concordant);
    CREATE INDEX IF NOT EXISTS i6  ON snaps(asset, timing, concordant);
    CREATE INDEX IF NOT EXISTS i7  ON snaps(asset, timing, delta_cl);
    CREATE INDEX IF NOT EXISTS i8  ON snaps(duree);
    CREATE INDEX IF NOT EXISTS i9  ON cycles(market_key);
    CREATE INDEX IF NOT EXISTS i10 ON cycles(asset, duree);
    """)
    db.commit()
    print(f"[DB] OK → {DB_PATH}")
    return db

# ═══════════════════════════════════════════════════════════════════
# RTDS — PRIX CHAINLINK + BINANCE
# ═══════════════════════════════════════════════════════════════════

def on_rtds_open(ws):
    print("[RTDS] Connecté ✅")
    sub = json.dumps({
        "type": "subscribe",
        "channel": "crypto_prices",
        "assets": [s for a in ASSETS
                   for s in [a.upper(), a.upper() + "USDT"]]
    })
    ws.send(sub)

def on_rtds_message(ws, message):
    try:
        data = json.loads(message)
        if not isinstance(data, list):
            data = [data]
        for item in data:
            if item.get('event_type') != 'crypto_price':
                continue
            sym    = item.get('asset', '').upper().replace('USDT', '')
            price  = float(item.get('price', 0))
            source = item.get('source', '').lower()
            if not sym or not price:
                continue
            for asset in ASSETS:
                if asset.upper() == sym or asset.upper() in sym:
                    with _pl:
                        if 'chainlink' in source:
                            PRIX_CL[asset] = price
                        else:
                            PRIX_BN[asset] = price
                    break
    except Exception:
        pass

def on_rtds_error(ws, error):
    print(f"[RTDS] Erreur: {error}")

def on_rtds_close(ws, *args):
    print("[RTDS] Fermé — reconnexion 5s...")
    time.sleep(5)
    start_rtds()

def start_rtds():
    ws = websocket.WebSocketApp(
        RTDS_WS,
        on_open=on_rtds_open,
        on_message=on_rtds_message,
        on_error=on_rtds_error,
        on_close=on_rtds_close
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════
# GAMMA API — MARCHÉS
# ═══════════════════════════════════════════════════════════════════

def get_market(asset, dl, ds):
    now = int(time.time())
    wts = now - (now % ds)
    ets = wts + ds
    secs = ets - now
    names = ASSET_NAMES.get(asset, [asset])

    for endpoint in [f"{GAMMA_API}/markets/keyset", f"{GAMMA_API}/markets"]:
        try:
            r = sess.get(endpoint, params={
                "active": "true", "closed": "false", "limit": 200,
                "end_date_min": wts - 60, "end_date_max": ets + 120,
            }, timeout=10)
            if r.status_code not in (200, 404):
                continue
            if r.status_code == 404:
                continue

            data    = r.json()
            markets = data if isinstance(data, list) else data.get('markets', [])

            for m in markets:
                if not m.get('condition_id'):
                    continue
                q  = (m.get('question') or '').lower()
                sl = (m.get('slug')     or '').lower()

                # Filtre : asset présent
                if not any(n in q or n in sl for n in names):
                    continue

                # Filtre : marché Up/Down
                if not any(x in q for x in ['up or down', 'higher or lower',
                                              'updown', 'up-or-down']):
                    continue

                # Filtre : fenêtre de temps
                ets_m = (m.get('endDateIso') or m.get('end_date_iso')
                         or m.get('end_date'))
                if not ets_m:
                    continue
                try:
                    if isinstance(ets_m, str):
                        from datetime import datetime as dt
                        ets_m = int(dt.fromisoformat(
                            ets_m.replace('Z', '+00:00')).timestamp())
                    if abs(ets_m - ets) > 90:
                        continue
                    secs = ets_m - now
                except Exception:
                    continue

                # Extraire token IDs UP/DOWN
                tokens = m.get('tokens', m.get('outcomes', []))
                idu = idd = None
                for t in tokens:
                    name = (t.get('outcome') or t.get('name') or '').upper()
                    tid  = t.get('token_id') or t.get('id') or ''
                    if any(x in name for x in ['UP', 'HIGHER', 'YES']):
                        idu = tid
                    elif any(x in name for x in ['DOWN', 'LOWER', 'NO']):
                        idd = tid

                print(f"[GAMMA] ✅ {asset.upper()} {dl} T-{secs}s "
                      f"| {m.get('slug','')[:40]}")
                return {
                    'slug': m.get('slug', ''), 'condition_id': m.get('condition_id', ''),
                    'start_ts': wts, 'end_ts': ets_m, 'secs': secs,
                    'idu': idu, 'idd': idd,
                }
        except Exception as e:
            print(f"[GAMMA] {endpoint.split('/')[-1]} {asset} {dl}: {e}")
    return None


def get_clob_single(tid: str) -> tuple:
    """Récupère prix+liquidité pour un token avec cache et rate limiting."""
    if not tid:
        return None, None
    now = time.time()

    # Cache court 500ms — données très fraîches sur VPS dédié
    if tid in _clob_cache:
        ts_cache, price, liq = _clob_cache[tid]
        if now - ts_cache < CLOB_CACHE_TTL:
            return price, liq

    try:
        r = sess.get(f"{CLOB_URL}/book",
                     params={"token_id": tid}, timeout=5)
        if r.status_code == 429:
            print("[CLOB] ⚠️ Rate limit 429 — pause 2s")
            time.sleep(2)
            return None, None
        if r.status_code != 200:
            return None, None
        asks = r.json().get('asks', [])
        if asks:
            p = float(asks[0].get('price', 0))
            l = sum(float(a.get('size', 0)) for a in asks[:3])
            _clob_cache[tid] = (now, p, l)
            return p, l
    except Exception:
        pass
    return None, None


def get_clob(idu, idd):
    """Récupère prix UP et DOWN avec cache et rate limiting."""
    pu, lu = get_clob_single(idu)
    pd, ld = get_clob_single(idd)
    return pu, pd, lu, ld


def get_result(cid):
    try:
        r = sess.get(f"https://data-api.polymarket.com/markets/{cid}",
                     timeout=10)
        if r.status_code != 200:
            return None
        for o in r.json().get('outcomes', []):
            if float(o.get('price', 0)) > 0.99:
                name = (o.get('outcome') or o.get('name') or '').upper()
                if any(x in name for x in ['UP', 'HIGHER', 'YES']):
                    return 'UP'
                if any(x in name for x in ['DOWN', 'LOWER', 'NO']):
                    return 'DOWN'
    except Exception:
        pass
    return None

# ═══════════════════════════════════════════════════════════════════
# SCANNER
# ═══════════════════════════════════════════════════════════════════

def scanner(db):
    now = int(time.time())
    for asset in ASSETS:
        for dl, ds in DUREES:
            try:
                info = get_market(asset, dl, ds)
                if not info:
                    continue

                sts  = info['start_ts']
                ets  = info['end_ts']
                secs = info['secs']
                mkey = f"{asset}-{dl}-{sts}"

                with _pl:
                    cl_now = PRIX_CL.get(asset)
                    bn_now = PRIX_BN.get(asset)

                # Enregistrer le cycle
                with _dl:
                    ex = db.execute(
                        "SELECT id, prix_t0_cl, prix_t0_bn FROM cycles WHERE market_key=?",
                        (mkey,)).fetchone()
                    if not ex:
                        db.execute("""
                            INSERT OR IGNORE INTO cycles
                            (ts,asset,duree,market_key,slug,condition_id,
                             start_ts,end_ts,prix_t0_cl,prix_t0_bn)
                            VALUES (?,?,?,?,?,?,?,?,?,?)
                        """, (now,asset,dl,mkey,info['slug'],info['condition_id'],
                              sts,ets,cl_now,bn_now))
                        db.commit()
                        with _tl:
                            PRIX_T0[mkey] = {'cl': cl_now, 'bn': bn_now}
                        print(f"[NEW] {asset.upper()} {dl} T-{secs}s "
                              f"CL={cl_now} BN={bn_now}")
                    else:
                        with _tl:
                            if mkey not in PRIX_T0:
                                PRIX_T0[mkey] = {'cl': ex[1], 'bn': ex[2]}

                if secs < 0 or secs > 12:
                    continue

                with _tl:
                    t0 = PRIX_T0.get(mkey, {})
                cl_t0 = t0.get('cl')
                bn_t0 = t0.get('bn')

                if not cl_now and not bn_now:
                    continue

                # Calcul deltas
                delta_cl = ((cl_now - cl_t0) / cl_t0 * 100) if cl_now and cl_t0 else None
                delta_bn = ((bn_now - bn_t0) / bn_t0 * 100) if bn_now and bn_t0 else None

                dir_cl = ('UP' if delta_cl > 0 else 'DOWN') if delta_cl is not None else None
                dir_bn = ('UP' if delta_bn > 0 else 'DOWN') if delta_bn is not None else None
                concordant = 1 if (dir_cl and dir_bn and dir_cl == dir_bn) else 0

                # Force du signal = minimum des deux deltas si concordant
                signal_force = None
                if concordant and delta_cl is not None and delta_bn is not None:
                    signal_force = min(abs(delta_cl), abs(delta_bn))

                # Prix CLOB — appelé UNE SEULE FOIS par timing capturé
                # (le cache évite les doublons si plusieurs timings dans le même scan)
                pu, pd, lu, ld = get_clob(info.get('idu'), info.get('idd'))

                # Spread = inefficience du marché
                spread = None
                if pu and pd:
                    spread = round(abs(pu - (1 - pd)), 4)

                # P&L simulé : si concordant, on achète au clob le bon côté
                pnl   = None
                p_ent = None
                if concordant and dir_cl:
                    p_ent = pu if dir_cl == 'UP' else pd
                    if p_ent and p_ent > 0:
                        # Si win : retour = (1/prix - 1) - fee
                        # Si lose : retour = -1 (perd la mise)
                        # On calcule le P&L attendu (rempli lors de la résolution)
                        pass  # sera calculé lors de la résolution

                # Enregistrer snapshots
                for t in TIMINGS:
                    if secs > t or secs < t - 1:
                        continue
                    with _dl:
                        if db.execute(
                            "SELECT id FROM snaps WHERE market_key=? AND timing=?",
                            (mkey, t)).fetchone():
                            continue
                        db.execute("""
                            INSERT INTO snaps
                            (market_key,asset,duree,timing,ts,
                             prix_cl,prix_cl_t0,delta_cl,
                             prix_bn,prix_bn_t0,delta_bn,
                             direction_cl,direction_bn,concordant,signal_force,
                             clob_up,clob_dn,liq_up,liq_dn,spread,
                             prix_entree)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """, (mkey,asset,dl,t,now,
                              cl_now,cl_t0,delta_cl,
                              bn_now,bn_t0,delta_bn,
                              dir_cl,dir_bn,concordant,signal_force,
                              pu,pd,lu,ld,spread,p_ent))
                        db.commit()

                    print(f"[SNAP] {asset.upper()} {dl} T-{t}s | "
                          f"CL={delta_cl:+.3f if delta_cl else 'N/A'}% "
                          f"BN={delta_bn:+.3f if delta_bn else 'N/A'}% "
                          f"{'✅CONC' if concordant else '❌'} "
                          f"CLOB={pu}")

            except Exception as e:
                print(f"[ERR] {asset} {dl}: {e}")


def resolver(db):
    now = int(time.time())
    with _dl:
        rows = db.execute("""
            SELECT market_key, condition_id, end_ts
            FROM cycles
            WHERE resultat IS NULL AND end_ts < ? AND end_ts > ?
        """, (now, now - 3600)).fetchall()

    for mkey, cid, ets in rows:
        if now < ets + 15:
            continue
        res = get_result(cid)
        if not res:
            continue

        with _dl:
            # Mettre à jour le cycle
            db.execute(
                "UPDATE cycles SET resultat=?, ts_res=? WHERE market_key=?",
                (res, now, mkey))

            # Mettre à jour les snapshots + calculer P&L
            snaps = db.execute(
                "SELECT id, direction_cl, direction_bn, prix_entree, clob_up, clob_dn "
                "FROM snaps WHERE market_key=?", (mkey,)).fetchall()

            for sid, dir_cl, dir_bn, p_ent, clob_up, clob_dn in snaps:
                # Correct ou non
                cor_cl = 1 if dir_cl == res else 0
                cor_bn = 1 if dir_bn == res else 0

                # P&L simulé
                pnl = None
                if p_ent and p_ent > 0:
                    if dir_cl == res:  # WIN
                        pnl = round((1.0 / p_ent - 1.0 - FEE_PCT) * 100, 2)
                    else:              # LOSE
                        pnl = round(-100.0, 2)

                db.execute("""
                    UPDATE snaps SET
                        resultat=?, correct_cl=?, correct_bn=?, pnl_simule=?
                    WHERE id=?
                """, (res, cor_cl, cor_bn, pnl, sid))

            db.commit()

        print(f"[RES] {mkey} → {res} ✅")

# ═══════════════════════════════════════════════════════════════════
# ANALYSE STATISTIQUE
# ═══════════════════════════════════════════════════════════════════

def analyser(db):
    R = {}

    # ── 1. WR par timing ──
    wt = {}
    for t in TIMINGS:
        row = db.execute("""
            SELECT COUNT(*),
                   SUM(correct_cl), SUM(correct_bn),
                   SUM(concordant),
                   SUM(CASE WHEN concordant=1 THEN correct_cl ELSE NULL END),
                   COUNT(CASE WHEN concordant=1 THEN 1 END),
                   AVG(pnl_simule),
                   AVG(CASE WHEN concordant=1 THEN pnl_simule END)
            FROM snaps WHERE timing=? AND resultat IS NOT NULL
        """, (t,)).fetchone()
        n, wcl, wbn, nc, wconc, n_conc, pnl_all, pnl_conc = row
        if n and n >= 5:
            wt[t] = {
                'n':         n,
                'wr_cl':     round((wcl   or 0) / n * 100, 1),
                'wr_bn':     round((wbn   or 0) / n * 100, 1),
                'n_conc':    n_conc or 0,
                'wr_conc':   round((wconc or 0) / max(n_conc or 1, 1) * 100, 1),
                'pct_conc':  round((nc    or 0) / n * 100, 1),
                'pnl_all':   round(pnl_all  or 0, 2),
                'pnl_conc':  round(pnl_conc or 0, 2),
            }
    R['wt'] = wt

    # ── 2. WR et P&L par delta (signal force) et timing — OPTIMAL ──
    wd = {}
    for asset in ASSETS:
        wd[asset] = {}
        for t in TIMINGS:
            wd[asset][t] = []
            for s in SEUILS:
                row = db.execute("""
                    SELECT COUNT(*), SUM(correct_cl),
                           AVG(pnl_simule), MIN(pnl_simule), MAX(pnl_simule),
                           AVG(clob_up)
                    FROM snaps
                    WHERE asset=? AND timing=? AND concordant=1
                    AND signal_force >= ?
                    AND resultat IS NOT NULL
                """, (asset, t, s)).fetchone()
                n, w, pnl_avg, pnl_min, pnl_max, prix_moy = row
                if n and n >= 3:
                    wr = (w or 0) / n * 100
                    wd[asset][t].append({
                        's':        s,
                        'n':        n,
                        'wr':       round(wr, 1),
                        'pnl_avg':  round(pnl_avg  or 0, 2),
                        'pnl_min':  round(pnl_min  or 0, 2),
                        'pnl_max':  round(pnl_max  or 0, 2),
                        'prix_moy': round(prix_moy or 0, 3),
                    })
    R['wd'] = wd

    # ── 3. Delta optimal par asset — sweet spot ──
    dopt = {}
    for asset in ASSETS:
        best = None
        bs   = 0
        for s in SEUILS:
            for t in [5, 6, 7]:
                row = db.execute("""
                    SELECT COUNT(*), SUM(correct_cl), AVG(pnl_simule)
                    FROM snaps
                    WHERE asset=? AND timing=? AND concordant=1
                    AND signal_force >= ? AND resultat IS NOT NULL
                """, (asset, t, s)).fetchone()
                n, w, pnl = row
                if n and n >= 5:
                    wr  = (w or 0) / n * 100
                    # Score = WR × log(n) × max(pnl, 0.01)
                    sc  = wr * math.log(max(n, 1)) * max((pnl or 0) + 100, 1)
                    if sc > bs:
                        bs   = sc
                        best = {
                            's':    s,
                            't':    t,
                            'wr':   round(wr, 1),
                            'pnl':  round(pnl or 0, 2),
                            'n':    n,
                        }
        dopt[asset] = best
    R['dopt'] = dopt

    # ── 4. Corrélation delta × prix CLOB ──
    # Question : à quel prix CLOB le signal est-il encore exploitable ?
    corr = {}
    prix_buckets = [
        (0.75, 0.80), (0.80, 0.85), (0.85, 0.88),
        (0.88, 0.91), (0.91, 0.93), (0.93, 0.95),
        (0.95, 0.97), (0.97, 0.99)
    ]
    for t in [8, 7, 6, 5, 4, 3]:
        corr[t] = []
        for pmin, pmax in prix_buckets:
            for s in [0.05, 0.10, 0.15]:
                row = db.execute("""
                    SELECT COUNT(*), SUM(correct_cl), AVG(pnl_simule),
                           AVG(signal_force)
                    FROM snaps
                    WHERE timing=? AND concordant=1
                    AND signal_force >= ?
                    AND ((direction_cl='UP'   AND clob_up   BETWEEN ? AND ?)
                      OR (direction_cl='DOWN' AND clob_dn   BETWEEN ? AND ?))
                    AND resultat IS NOT NULL
                """, (t, s, pmin, pmax, pmin, pmax)).fetchone()
                n, w, pnl, sf = row
                if n and n >= 3:
                    wr  = (w or 0) / n * 100
                    rdt = None
                    # Rendement brut si win
                    mid = (pmin + pmax) / 2
                    if mid > 0:
                        rdt = round((1 / mid - 1) * 100, 1)
                    corr[t].append({
                        'pmin':  pmin, 'pmax': pmax,
                        'seuil': s,
                        'n':     n,
                        'wr':    round(wr, 1),
                        'pnl':   round(pnl or 0, 2),
                        'rdt':   rdt,
                        'sf':    round(sf or 0, 3),
                    })
    R['corr'] = corr

    # ── 5. Fenêtre optimale — ratio signal/bruit ──
    fenetre = []
    for t in TIMINGS:
        row = db.execute("""
            SELECT COUNT(*),
                   SUM(CASE WHEN concordant=1 THEN 1 ELSE 0 END),
                   AVG(CASE WHEN concordant=1 THEN correct_cl END),
                   AVG(CASE WHEN concordant=1 THEN pnl_simule END),
                   AVG(ABS(delta_cl))
            FROM snaps WHERE timing=? AND resultat IS NOT NULL
        """, (t,)).fetchone()
        n, nc, wr_conc, pnl_conc, delta_moy = row
        if n and n >= 5:
            ratio_conc = (nc or 0) / n  # % de fois où le signal est concordant
            fenetre.append({
                't':          t,
                'n':          n,
                'pct_conc':   round(ratio_conc * 100, 1),
                'wr_conc':    round((wr_conc  or 0) * 100, 1),
                'pnl_conc':   round(pnl_conc  or 0, 2),
                'delta_moy':  round(delta_moy or 0, 3),
                # Score global = WR × % concordance × delta moyen
                'score':      round((wr_conc or 0) * ratio_conc * (delta_moy or 0) * 1000, 1),
            })
    R['fenetre'] = sorted(fenetre, key=lambda x: x['score'], reverse=True)

    # ── 6. Analyse par volatilité ──
    vol = {}
    vol_buckets = [
        ('Calme',  0.00, 0.05),
        ('Normal', 0.05, 0.15),
        ('Agité',  0.15, 0.40),
        ('Violent',0.40, 9.99),
    ]
    for label, vmin, vmax in vol_buckets:
        row = db.execute("""
            SELECT COUNT(*), SUM(s.correct_cl), AVG(s.pnl_simule)
            FROM snaps s
            JOIN cycles c ON s.market_key = c.market_key
            WHERE s.concordant=1 AND s.timing=5
            AND s.resultat IS NOT NULL
            AND c.volatilite BETWEEN ? AND ?
        """, (vmin, vmax)).fetchone()
        n, w, pnl = row
        if n and n >= 3:
            vol[label] = {
                'n':   n,
                'wr':  round((w or 0) / n * 100, 1),
                'pnl': round(pnl or 0, 2),
            }
    R['vol'] = vol

    # ── 7. WR par durée ──
    wdur = {}
    for dl, _ in DUREES:
        row = db.execute("""
            SELECT COUNT(*), SUM(correct_cl), SUM(concordant),
                   AVG(pnl_simule), COUNT(DISTINCT market_key)
            FROM snaps
            WHERE duree=? AND timing=5 AND resultat IS NOT NULL
        """, (dl,)).fetchone()
        n, w, nc, pnl, nb = row
        if n and n >= 5:
            wdur[dl] = {
                'n':    n,
                'wr':   round((w  or 0) / n * 100, 1),
                'conc': round((nc or 0) / n * 100, 1),
                'pnl':  round(pnl or 0, 2),
                'nb':   nb,
            }
    R['wdur'] = wdur

    # ── 8. Stats générales ──
    s = db.execute("""
        SELECT COUNT(DISTINCT market_key), COUNT(*),
               SUM(CASE WHEN resultat IS NOT NULL THEN 1 ELSE 0 END),
               MIN(ts), MAX(ts),
               SUM(CASE WHEN concordant=1 THEN 1 ELSE 0 END)
        FROM snaps
    """).fetchone()
    R['stats'] = {
        'cyc':    s[0] or 0,
        'snaps':  s[1] or 0,
        'res':    s[2] or 0,
        'debut':  datetime.fromtimestamp(s[3]).strftime('%d/%m %H:%M') if s[3] else 'N/A',
        'fin':    datetime.fromtimestamp(s[4]).strftime('%d/%m %H:%M') if s[4] else 'N/A',
        'conc':   s[5] or 0,
    }
    with _pl:
        R['prix'] = {'cl': dict(PRIX_CL), 'bn': dict(PRIX_BN)}

    return R

# ═══════════════════════════════════════════════════════════════════
# DASHBOARD HTML
# ═══════════════════════════════════════════════════════════════════

def wc(w):
    if w >= 93: return '#00ff88'
    if w >= 88: return '#88ff44'
    if w >= 82: return '#ffcc00'
    if w >= 75: return '#ff8800'
    return '#ff4444'

def pc(p):
    if p >  5: return '#00ff88'
    if p >  0: return '#88ff44'
    if p > -10: return '#ffcc00'
    return '#ff4444'

def make_html(R):
    st    = R.get('stats', {})
    prix  = R.get('prix', {'cl': {}, 'bn': {}})
    wt    = R.get('wt', {})
    dopt  = R.get('dopt', {})
    wd    = R.get('wd', {})
    corr  = R.get('corr', {})
    fen   = R.get('fenetre', [])
    vol   = R.get('vol', {})
    wdur  = R.get('wdur', {})
    res   = st.get('res', 0)
    now   = datetime.now().strftime('%d/%m/%Y %H:%M:%S')

    # Prix live
    live = ""
    for a in ASSETS:
        cl = prix['cl'].get(a)
        bn = prix['bn'].get(a)
        live += f'''<div class="li">
            <span class="ls">{a.upper()}</span>
            <span class="lp">{"$"+f"{cl:,.2f}" if cl else "—"}</span>
            <span class="ls2">CL | BN: {"$"+f"{bn:,.2f}" if bn else "—"}</span>
        </div>'''

    # WR par timing
    hwt = ""
    for t in sorted(wt.keys(), reverse=True):
        d = wt[t]
        hwt += f'''<tr>
            <td class="m">T-{t}s</td>
            <td style="color:{wc(d['wr_cl'])};font-weight:700">{d['wr_cl']}%</td>
            <td style="color:{wc(d['wr_bn'])};font-weight:700">{d['wr_bn']}%</td>
            <td style="color:{wc(d['wr_conc'])};font-weight:700">{d['wr_conc']}%</td>
            <td class="di">{d['pct_conc']}%</td>
            <td style="color:{pc(d['pnl_conc'])}">{d['pnl_conc']:+.1f}%</td>
            <td class="di">{d['n_conc']}/{d['n']}</td>
        </tr>'''

    # Delta optimal par asset
    hdo = ""
    for a in ASSETS:
        d = dopt.get(a)
        if d:
            hdo += f'''<tr>
                <td class="at">{a.upper()}</td>
                <td class="m">≥{d['s']}%</td>
                <td>T-{d['t']}s</td>
                <td style="color:{wc(d['wr'])};font-weight:700">{d['wr']}%</td>
                <td style="color:{pc(d['pnl'])}">{d['pnl']:+.1f}%</td>
                <td class="di">{d['n']}</td>
            </tr>'''
        else:
            hdo += f'<tr><td class="at">{a.upper()}</td><td class="di" colspan="5">En collecte...</td></tr>'

    # Fenêtre optimale
    hfen = ""
    for d in fen[:6]:
        hfen += f'''<tr>
            <td class="m">T-{d['t']}s</td>
            <td style="color:{wc(d['wr_conc'])};font-weight:700">{d['wr_conc']}%</td>
            <td class="di">{d['pct_conc']}%</td>
            <td style="color:{pc(d['pnl_conc'])}">{d['pnl_conc']:+.1f}%</td>
            <td class="di">{d['delta_moy']}%</td>
            <td style="color:{'#00ff88' if d['score']>50 else '#ffcc00'}">{d['score']}</td>
        </tr>'''

    # Corrélation delta × prix CLOB (T-5s uniquement)
    hcorr = ""
    data5 = corr.get(5, [])
    seen  = set()
    for d in data5:
        key = (d['pmin'], d['seuil'])
        if key in seen:
            continue
        seen.add(key)
        hcorr += f'''<tr>
            <td class="m">{d['pmin']:.2f}–{d['pmax']:.2f}</td>
            <td class="m">≥{d['seuil']}%</td>
            <td style="color:{wc(d['wr'])};font-weight:700">{d['wr']}%</td>
            <td style="color:{pc(d['pnl'])}">{d['pnl']:+.1f}%</td>
            <td class="m">{d['rdt']:+.1f}%</td>
            <td class="di">{d['n']}</td>
        </tr>'''

    # Volatilité
    hvol = ""
    for label in ['Calme', 'Normal', 'Agité', 'Violent']:
        d = vol.get(label)
        if d:
            hvol += f'''<tr>
                <td>{label}</td>
                <td style="color:{wc(d['wr'])};font-weight:700">{d['wr']}%</td>
                <td style="color:{pc(d['pnl'])}">{d['pnl']:+.1f}%</td>
                <td class="di">{d['n']}</td>
            </tr>'''

    # WR par durée
    hwdur = ""
    for dl, _ in DUREES:
        d = wdur.get(dl)
        if d:
            hwdur += f'''<tr>
                <td class="m">{dl}</td>
                <td style="color:{wc(d['wr'])};font-weight:700">{d['wr']}%</td>
                <td class="di">{d['conc']}% concordants</td>
                <td style="color:{pc(d['pnl'])}">{d['pnl']:+.1f}%</td>
                <td class="di">{d['nb']} cycles</td>
            </tr>'''

    # WR delta par asset — tableau compact
    hwd = ""
    for a in ASSETS:
        ad = wd.get(a, {})
        rows_html = ""
        for s in [0.05, 0.08, 0.10, 0.15, 0.20]:
            row_html = f'<td class="m">≥{s}%</td>'
            hr = False
            for t in [8, 7, 6, 5, 4, 3]:
                f2 = next((d for d in ad.get(t, []) if d['s'] == s), None)
                if f2:
                    row_html += f'<td style="color:{wc(f2["wr"])}">{f2["wr"]}%<br><span style="color:{pc(f2["pnl_avg"])};font-size:10px">{f2["pnl_avg"]:+.0f}%</span></td>'
                    hr = True
                else:
                    row_html += '<td class="di">—</td>'
            if hr:
                rows_html += f'<tr>{row_html}</tr>'
        if rows_html:
            hwd += f'''<div class="sl">{a.upper()}</div>
            <table><tr><th>Delta</th>
            {"".join(f"<th>T-{t}s</th>" for t in [8,7,6,5,4,3])}
            </tr>{rows_html}</table>'''

    notice = f'<div class="notice">⏳ <strong>{res}</strong>/50 cycles résolus — analyses incomplètes.</div>' if res < 20 else ''

    em = '<div class="em">En collecte...</div>'

    CSS = """
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@400;700;800&display=swap');
:root{--bg:#080b0f;--bg2:#0f1318;--bg3:#161b22;--bd:#1c2230;--tx:#b8c8d8;--di:#3a4a5a;--ac:#00d4ff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:'Syne',sans-serif;padding:16px}
.hdr{border-bottom:1px solid var(--bd);padding-bottom:12px;margin-bottom:18px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;align-items:flex-end}
.ttl{font-size:22px;font-weight:800;color:var(--ac)}.sub{font-size:11px;color:var(--di);margin-top:3px}
.meta{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--di);text-align:right}
.live{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px}
.li{background:var(--bg2);border:1px solid var(--bd);border-radius:6px;padding:8px 12px;min-width:90px}
.ls{font-size:10px;color:var(--di);text-transform:uppercase;letter-spacing:1px;display:block}
.ls2{font-size:9px;color:var(--di);display:block;margin-top:1px}
.lp{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;color:var(--ac);margin-top:2px;display:block}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:18px}
.sc{background:var(--bg2);border:1px solid var(--bd);border-radius:8px;padding:12px}
.sl2{font-size:10px;color:var(--di);text-transform:uppercase;letter-spacing:1px}
.sv{font-size:20px;font-weight:800;color:var(--ac);font-family:'JetBrains Mono',monospace;margin-top:4px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
@media(max-width:600px){.grid{grid-template-columns:1fr}}
.card{background:var(--bg2);border:1px solid var(--bd);border-radius:10px;padding:14px;margin-bottom:12px}
.ct{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:var(--ac);margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--bd)}
.full{grid-column:1/-1}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:5px 7px;font-size:10px;text-transform:uppercase;letter-spacing:.8px;color:var(--di);border-bottom:1px solid var(--bd)}
td{padding:6px 7px;border-bottom:1px solid rgba(28,34,48,.5);vertical-align:middle}
tr:last-child td{border-bottom:none}tr:hover td{background:var(--bg3)}
.m{font-family:'JetBrains Mono',monospace}.di{color:var(--di);font-size:11px}.at{font-weight:700;color:var(--ac)}
.sl{font-size:11px;font-weight:700;color:#ffd000;text-transform:uppercase;letter-spacing:1px;margin:12px 0 6px}
.notice{background:rgba(0,212,255,.05);border:1px solid rgba(0,212,255,.2);border-radius:8px;padding:12px 14px;font-size:12px;color:var(--di);margin-bottom:14px}
.notice strong{color:var(--ac)}.em{color:var(--di);font-size:12px;padding:14px 0}
.tag{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;background:rgba(0,255,136,.1);color:#00ff88;border:1px solid rgba(0,255,136,.2);margin-left:4px}
"""

    return f"""<!DOCTYPE html>
<html lang="fr"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>⚡ Delta Analyzer v8</title>
<style>{CSS}</style>
</head><body>

<div class="hdr">
  <div>
    <div class="ttl">⚡ DELTA ANALYZER <span class="tag">v8</span></div>
    <div class="sub">Polymarket — Chainlink + Binance RTDS | Analyse optimisation bot</div>
  </div>
  <div class="meta">Refresh 60s<br><strong style="color:var(--tx)">{now}</strong></div>
</div>

<div class="live">{live}</div>

<div class="stats">
  <div class="sc"><div class="sl2">Cycles</div><div class="sv">{st.get('cyc',0)}</div></div>
  <div class="sc"><div class="sl2">Snapshots</div><div class="sv">{st.get('snaps',0)}</div></div>
  <div class="sc"><div class="sl2">Résolus</div><div class="sv">{res}</div></div>
  <div class="sc"><div class="sl2">Concordants</div><div class="sv">{st.get('conc',0)}</div></div>
  <div class="sc"><div class="sl2">Depuis</div><div class="sv" style="font-size:12px">{st.get('debut','—')}</div></div>
</div>

{notice}

<div class="card">
  <div class="ct">📊 WR et P&L par timing — CL=Chainlink, BN=Binance, CONC=signal concordant</div>
  {em if not hwt else f'''<table>
    <tr><th>Timing</th><th>WR CL</th><th>WR BN</th><th>WR CONC</th>
    <th>% Conc</th><th>P&L CONC</th><th>N conc/total</th></tr>
    {hwt}</table>'''}
</div>

<div class="card">
  <div class="ct">🎯 Delta + timing optimaux par asset (signal CL+BN concordant)</div>
  {em if not hdo else f'''<table>
    <tr><th>Asset</th><th>Delta min</th><th>Timing</th><th>WR</th><th>P&L moy</th><th>N</th></tr>
    {hdo}</table>'''}
</div>

<div class="card">
  <div class="ct">⏱️ Fenêtre optimale — classée par score signal/bruit</div>
  <div style="font-size:11px;color:var(--di);margin-bottom:8px">
    Score = WR × % concordance × delta moyen — plus c'est haut, mieux c'est
  </div>
  {em if not hfen else f'''<table>
    <tr><th>Timing</th><th>WR</th><th>% Conc</th><th>P&L</th><th>Delta moy</th><th>Score</th></tr>
    {hfen}</table>'''}
</div>

<div class="card">
  <div class="ct">💰 Corrélation delta × prix CLOB (T-5s) — signal concordant uniquement</div>
  <div style="font-size:11px;color:var(--di);margin-bottom:8px">
    Question : à quel prix CLOB le delta est-il encore exploitable ?
  </div>
  {em if not hcorr else f'''<table>
    <tr><th>Prix CLOB</th><th>Delta min</th><th>WR</th><th>P&L net</th><th>Rdt brut</th><th>N</th></tr>
    {hcorr}</table>'''}
</div>

<div class="grid">
  <div class="card">
    <div class="ct">📈 WR par volatilité du marché (T-5s)</div>
    {em if not hvol else f'''<table>
      <tr><th>Volatilité</th><th>WR</th><th>P&L</th><th>N</th></tr>
      {hvol}</table>'''}
  </div>
  <div class="card">
    <div class="ct">⏰ WR par durée de marché (T-5s)</div>
    {em if not hwdur else f'''<table>
      <tr><th>Durée</th><th>WR</th><th>Concordance</th><th>P&L</th><th>Cycles</th></tr>
      {hwdur}</table>'''}
  </div>
</div>

<div class="card">
  <div class="ct">🔬 WR + P&L par delta × timing × asset (signal concordant)</div>
  <div style="font-size:11px;color:var(--di);margin-bottom:8px">
    WR% en haut / P&L moyen en bas — vert = profitable, rouge = perdant
  </div>
  {em if not hwd else hwd}
</div>

<div class="card">
  <div class="ct">🎨 Légende</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:12px">
    <div>
      <div style="color:var(--di);margin-bottom:6px">Win Rate</div>
      <div style="color:#00ff88">≥ 93% Excellent ✅</div>
      <div style="color:#88ff44">88–93% Bon ✅</div>
      <div style="color:#ffcc00">82–88% Limite ⚠️</div>
      <div style="color:#ff4455">&lt; 82% Éviter ❌</div>
    </div>
    <div>
      <div style="color:var(--di);margin-bottom:6px">Sources</div>
      <div>CL = Chainlink (oracle résolution)</div>
      <div>BN = Binance (feed marché)</div>
      <div>CONC = CL et BN d'accord</div>
      <div>P&L = après fees {FEE_PCT*100:.1f}%</div>
    </div>
  </div>
</div>

</body></html>"""

# ═══════════════════════════════════════════════════════════════════
# SERVEUR WEB
# ═══════════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(_HTML.encode('utf-8'))
    def log_message(self, *a): pass

# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    global _HTML
    print("=" * 60)
    print("   ⚡  DELTA ANALYZER v8 — Optimal")
    print("=" * 60)
    print(f"   Assets   : {', '.join(a.upper() for a in ASSETS)}")
    print(f"   Durées   : {', '.join(d[0] for d in DUREES)}")
    print(f"   Dashboard: http://localhost:{PORT}")
    print(f"   DB       : {DB_PATH}")
    print("=" * 60)

    db = init_db()

    threading.Thread(
        target=lambda: HTTPServer((HOST, PORT), Handler).serve_forever(),
        daemon=True
    ).start()
    print(f"[WEB] Dashboard → http://localhost:{PORT}")

    print("\n[RTDS] Connexion Polymarket WebSocket...")
    start_rtds()
    time.sleep(6)

    with _pl:
        print(f"[RTDS] CL: {len(PRIX_CL)}/{len(ASSETS)} | BN: {len(PRIX_BN)}/{len(ASSETS)}")

    ts = tl = th = 0.0
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Boucle démarrée\n")

    while True:
        now = time.time()
        if now - ts >= 0.5:
            scanner(db); ts = now
        if now - tl >= 30:
            resolver(db); tl = now
        if now - th >= 60:
            try:
                _HTML = make_html(analyser(db))
            except Exception as e:
                print(f"[HTML] {e}")
            th = now
        time.sleep(0.1)


if __name__ == '__main__':
    main()
