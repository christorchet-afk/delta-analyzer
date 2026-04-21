#!/usr/bin/env python3
"""
DELTA ANALYZER v12 — Liquidite CLOB + nettoyage spread
=================================================================================
Changements v12 :
  - Suppression du spread (inutile dans la strategie)
  - Capture liquidite CLOB via REST /book a T-10s, T-5s, T-3s uniquement
    → liq_ask_vol  : volume total $ disponible en asks entre 0.90 et 0.999
    → liq_ask_best : volume $ au meilleur ask
    → liq_ask_lvls : nombre de niveaux de prix disponibles
  - ~126 appels REST /book par cycle 5min — tres en dessous des rate limits
  - Dashboard : affichage liquidite moyenne par asset/duree
"""

import os, time, math, json, sqlite3, threading, requests, websocket
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

# ══════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════

DB_PATH  = os.environ.get('DB_PATH', '/root/delta_analysis.db')
PORT     = int(os.environ.get('PORT', 8080))
HOST     = '0.0.0.0'

ASSETS   = ["btc", "eth", "sol", "xrp", "doge", "hype", "bnb"]
DUREES   = [("5m", 300), ("15m", 900), ("4h", 14400)]

GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_URL    = "https://clob.polymarket.com"
RTDS_URL    = "wss://ws-live-data.polymarket.com"
MARKET_WS   = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
DATA_API    = "https://data-api.polymarket.com"

TIMINGS  = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
SEUILS   = [0.02, 0.03, 0.05, 0.07, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30]
FEE_PCT  = 0.005

# Chainlink supporte tous les assets via RTDS
CHAINLINK_OK = {
    "btc":  "btc/usd",
    "eth":  "eth/usd",
    "sol":  "sol/usd",
    "xrp":  "xrp/usd",
    "doge": "doge/usd",
    "hype": "hype/usd",
    "bnb":  "bnb/usd",
}

sess = requests.Session()
sess.headers.update({'User-Agent': 'DeltaAnalyzer/12.0'})

# Cache Gamma API
_gamma_cache: dict = {}
GAMMA_CACHE_TTL = 55
_cl_seen: set = set()

# ══════════════════════════════════════════════════════
# ETAT GLOBAL
# ══════════════════════════════════════════════════════

PRIX_CL: dict   = {}   # {asset: float}  — Chainlink uniquement
PRIX_T0: dict   = {}   # {market_key: float}  prix CL au debut du cycle
PRIX_CLOB: dict = {}   # {token_id: float}  last_trade_price / best_ask temps reel

# Historique prix CL pour momentum court terme
# {asset: [(ts, price), ...]}  — garde les 60 dernières secondes
PRIX_CL_HIST: dict = {}
_lock_hist = threading.Lock()

# Marches actifs
MARCHES: dict = {}

_last_fenetre_call = 0.0

_lock_prix   = threading.Lock()
_lock_clob   = threading.Lock()
_lock_marche = threading.Lock()
_lock_t0     = threading.Lock()
_lock_db     = threading.Lock()

_HTML = "<html><body style='background:#080b0f;color:#00d4ff;font-family:monospace;padding:40px'><h2>⚡ Delta Analyzer v12</h2><p>Demarrage...</p></body></html>"

# ══════════════════════════════════════════════════════
# BASE DE DONNEES
# ══════════════════════════════════════════════════════

def init_db():
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
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
        prix_t0_cl   REAL,
        resultat     TEXT,
        ts_res       INTEGER
    );
    CREATE TABLE IF NOT EXISTS snaps (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        market_key   TEXT,
        asset        TEXT,
        duree        TEXT,
        timing       INTEGER,
        ts           INTEGER,
        prix_cl      REAL,
        prix_cl_t0   REAL,
        delta_cl     REAL,
        momentum_cl  REAL,
        direction_cl TEXT,
        clob_up      REAL,
        clob_dn      REAL,
        liq_ask_vol  REAL,
        liq_ask_best REAL,
        liq_ask_lvls INTEGER,
        resultat     TEXT,
        correct_cl   INTEGER,
        pnl_simule   REAL,
        prix_entree  REAL
    );
    CREATE INDEX IF NOT EXISTS i1 ON snaps(asset);
    CREATE INDEX IF NOT EXISTS i2 ON snaps(timing);
    CREATE INDEX IF NOT EXISTS i3 ON snaps(market_key);
    CREATE INDEX IF NOT EXISTS i4 ON snaps(asset, duree, timing);
    CREATE INDEX IF NOT EXISTS i5 ON snaps(asset, timing);
    CREATE INDEX IF NOT EXISTS i6 ON cycles(market_key);
    """)
    db.commit()
    print(f"[DB] OK → {DB_PATH}")
    return db

# ══════════════════════════════════════════════════════
# RTDS — PRIX CHAINLINK UNIQUEMENT
# ══════════════════════════════════════════════════════

def rtds_on_open(ws):
    print("[RTDS] Connecte ✅")
    ws.send(json.dumps({"action":"subscribe","subscriptions":[{
        "topic":"crypto_prices_chainlink","type":"*","filters":""
    }]}))
    def ping():
        while True:
            try: ws.send("PING")
            except: break
            time.sleep(5)
    threading.Thread(target=ping, daemon=True).start()
    print("[RTDS] Abonne Chainlink (7 assets)")

def rtds_on_message(ws, msg):
    if not msg or not msg.strip().startswith('{'):
        return
    try:
        d = json.loads(msg)
        topic   = d.get('topic','')
        payload = d.get('payload',{})
        if not payload or topic != 'crypto_prices_chainlink':
            return
        sym   = payload.get('symbol','').lower()
        praw  = payload.get('value') or payload.get('full_accuracy_value', 0)
        price = float(praw) if praw else 0.0
        if not sym or price <= 0 or price > 1_000_000:
            return
        for asset, sym_cl in CHAINLINK_OK.items():
            if sym_cl == sym:
                with _lock_prix:
                    PRIX_CL[asset] = price
                # Historique pour momentum court terme
                with _lock_hist:
                    if asset not in PRIX_CL_HIST:
                        PRIX_CL_HIST[asset] = []
                    now_h = time.time()
                    PRIX_CL_HIST[asset].append((now_h, price))
                    # Garder seulement les 60 dernières secondes
                    PRIX_CL_HIST[asset] = [(t, p) for t, p in PRIX_CL_HIST[asset] if now_h - t <= 60]
                if sym not in _cl_seen:
                    _cl_seen.add(sym)
                    print(f"[CL] {sym}={price:.4f}")
                break
    except Exception as e:
        print(f"[RTDS] Err: {e}")

def rtds_on_error(ws, e):
    print(f"[RTDS] Erreur: {e}")

def rtds_on_close(ws, *a):
    print("[RTDS] Ferme — reconnexion 5s...")
    time.sleep(5)
    start_rtds()

def start_rtds():
    ws = websocket.WebSocketApp(RTDS_URL,
        on_open=rtds_on_open, on_message=rtds_on_message,
        on_error=rtds_on_error, on_close=rtds_on_close)
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ══════════════════════════════════════════════════════
# MARKET WEBSOCKET — DETECTION MARCHES + PRIX CLOB
# ══════════════════════════════════════════════════════

_watched_tokens: set = set()
_mws = None

def get_market_info_gamma(slug: str) -> dict | None:
    now_c = time.time()
    if slug in _gamma_cache:
        ts_c, result = _gamma_cache[slug]
        if now_c - ts_c < GAMMA_CACHE_TTL:
            return result
    try:
        r = sess.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=6)
        if r.status_code == 429:
            print(f"[GAMMA] 429 rate limit")
            time.sleep(2)
            return _gamma_cache.get(slug, (0, None))[1]
        text = r.text.strip()
        if r.status_code != 200 or not text or text in ('[]','null',''):
            _gamma_cache[slug] = (now_c, None)
            return None
        import json as _j
        data = _j.loads(text)
        if not data or not isinstance(data, list):
            _gamma_cache[slug] = (now_c, None)
            return None
        mkt = data[0]
        if not isinstance(mkt, dict):
            _gamma_cache[slug] = (now_c, None)
            return None
        cid = mkt.get('conditionId','')
        if not cid:
            _gamma_cache[slug] = (now_c, None)
            return None
        end_raw = mkt.get('endDateIso') or mkt.get('endDate','')
        end_ts  = 0
        if end_raw:
            from datetime import datetime as _dt
            end_ts = int(_dt.fromisoformat(end_raw.replace('Z','+00:00')).timestamp())
        idu = idd = None
        try:
            rc = sess.get(f"{CLOB_URL}/markets/{cid}", timeout=5)
            if rc.status_code == 200:
                tokens = rc.json().get('tokens', [])
                if len(tokens) >= 2:
                    t0, t1 = tokens[0], tokens[1]
                    o0 = t0.get('outcome','').lower()
                    up, dn = (t0,t1) if ('up' in o0 or 'yes' in o0) else (t1,t0)
                    idu = up.get('token_id','')
                    idd = dn.get('token_id','')
        except Exception as e:
            print(f"[CLOB] Erreur enrichissement {cid[:12]}: {e}")
        result = {'condition_id': cid, 'idu': idu, 'idd': idd, 'end_ts': end_ts}
        _gamma_cache[slug] = (now_c, result)
        return result
    except Exception as e:
        print(f"[GAMMA] Erreur {slug}: {e}")
        return None


def get_marches_par_slug(now: int) -> list:
    result = []
    for asset in ASSETS:
        for dl, ds in DUREES:
            start_ts = (now // ds) * ds
            end_ts   = start_ts + ds
            secs     = end_ts - now
            if secs <= 0:
                continue
            slug = f"{asset}-updown-{dl}-{start_ts}"
            try:
                r = sess.get(f"{GAMMA_API}/markets",
                    params={"slug": slug}, timeout=6)
                if r.status_code == 429:
                    time.sleep(1)
                    continue
                if r.status_code != 200:
                    continue
                text = r.text.strip()
                if not text or text in ('[]','null',''):
                    continue
                import json as _j
                data = _j.loads(text)
                if not data or not isinstance(data, list):
                    continue
                mkt = data[0]
                if not isinstance(mkt, dict):
                    continue
                cid = mkt.get('conditionId','')
                if not cid:
                    continue
                clob_raw = mkt.get('clobTokenIds','')
                idu = idd = None
                if clob_raw:
                    try:
                        ids = _j.loads(clob_raw) if isinstance(clob_raw,str) else clob_raw
                        if len(ids) >= 2:
                            idu, idd = ids[0], ids[1]
                    except Exception:
                        pass
                if not idu:
                    tokens = mkt.get('tokens',[])
                    if len(tokens) >= 2:
                        idu = tokens[0].get('token_id','')
                        idd = tokens[1].get('token_id','')
                if not idu or not idd:
                    idu2, idd2 = enrich_tokens(cid)
                    if idu2: idu = idu2
                    if idd2: idd = idd2
                result.append({
                    'slug': slug, 'condition_id': cid,
                    'asset': asset, 'duree': dl, 'period_s': ds,
                    'end_ts': end_ts, 'start_ts': start_ts,
                    'idu': idu, 'idd': idd,
                })
                tok_ok = '✅' if (idu and idd) else '❌'
                print(f"[GAMMA] {asset.upper()} {dl} T-{secs}s {tok_ok}")
            except Exception:
                pass
            time.sleep(0.05)
    print(f"[GAMMA] Slug: {len(result)}/{len(ASSETS)*len(DUREES)} marches trouves")
    return result


def subscribe_market_ws(token_ids: list):
    global _mws
    if not token_ids or not _mws:
        return
    new = [t for t in token_ids if t and t not in _watched_tokens]
    if not new:
        return
    try:
        _mws.send(json.dumps({
            "assets_ids": new,
            "type": "market",
            "custom_feature_enabled": True
        }))
        _watched_tokens.update(new)
    except Exception as e:
        print(f"[MWS] Erreur souscription: {e}")

def detect_asset_duree(slug: str, question: str = '') -> tuple:
    slug_l = slug.lower()
    asset  = None
    for a in ASSETS:
        if slug_l.startswith(a+'-') or a in slug_l[:10]:
            asset = a; break
    duree = None
    for dl, ds in DUREES:
        if f'-{dl}-' in slug_l or f'updown-{dl}' in slug_l:
            duree = (dl, ds); break
    return asset, duree

def register_market(slug: str, condition_id: str, assets_ids: list,
                    outcomes: list, end_ts: int, db, now: int):
    asset, dur = detect_asset_duree(slug)
    if not asset or not dur:
        return
    dl, ds = dur
    start_ts = end_ts - ds
    mkey     = f"{asset}-{dl}-{start_ts}"
    secs     = end_ts - now
    if secs < 0 or secs > ds + 120:
        return

    idu = idd = None
    if assets_ids and len(assets_ids) >= 2:
        for i, out in enumerate(outcomes or []):
            if i >= len(assets_ids): break
            o = str(out).upper()
            if any(x in o for x in ['UP','HIGHER','YES']): idu = assets_ids[i]
            elif any(x in o for x in ['DOWN','LOWER','NO']): idd = assets_ids[i]
        if not idu: idu = assets_ids[0]
        if not idd and len(assets_ids) > 1: idd = assets_ids[1]

    with _lock_prix:
        cl_now = PRIX_CL.get(asset)

    minfo = {
        'slug': slug, 'condition_id': condition_id,
        'asset': asset, 'duree': dl, 'period_s': ds,
        'start_ts': start_ts, 'end_ts': end_ts,
        'idu': idu, 'idd': idd,
    }
    with _lock_marche:
        MARCHES[mkey] = minfo
    with _lock_t0:
        if mkey not in PRIX_T0:
            PRIX_T0[mkey] = cl_now

    with _lock_db:
        existing = db.execute("SELECT id FROM cycles WHERE market_key=?", (mkey,)).fetchone()
        if not existing:
            db.execute("""INSERT OR IGNORE INTO cycles
                (ts,asset,duree,market_key,slug,condition_id,start_ts,end_ts,prix_t0_cl)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (now,asset,dl,mkey,slug,condition_id,start_ts,end_ts,cl_now))
            db.commit()

    subscribe_market_ws([idu, idd])
    print(f"[NEW] {asset.upper()} {dl} T-{secs}s | {slug}")

def mws_on_open(ws):
    global _mws
    _mws = ws
    print("[MWS] Market WebSocket connecte ✅")
    ws.send(json.dumps({
        "assets_ids": [],
        "type": "market",
        "custom_feature_enabled": True
    }))

def mws_on_message(ws, msg, db_ref):
    if not msg or not msg.strip().startswith('{'):
        return
    try:
        d     = json.loads(msg)
        etype = d.get('event_type','')
        now   = int(time.time())

        if etype == 'new_market':
            slug      = d.get('slug','')
            cid       = d.get('market','')
            assets_ids = d.get('assets_ids',[])
            outcomes  = d.get('outcomes',[])
            if slug:
                asset, dur = detect_asset_duree(slug)
                if asset and dur:
                    dl, ds = dur
                    ginfo = get_market_info_gamma(slug)
                    if ginfo and ginfo.get('end_ts',0) > now:
                        end_ts = ginfo['end_ts']
                        if ginfo.get('idu'): assets_ids = [ginfo['idu'], ginfo.get('idd','')]
                        if ginfo.get('condition_id'): cid = ginfo['condition_id']
                        register_market(slug, cid, assets_ids, outcomes, end_ts, db_ref, now)

        elif etype == 'last_trade_price':
            asset_id = d.get('asset_id','')
            price    = float(d.get('price', 0))
            if asset_id and price > 0:
                with _lock_clob:
                    PRIX_CLOB[asset_id] = price

        elif etype == 'best_bid_ask':
            asset_id = d.get('asset_id','')
            best_ask = float(d.get('best_ask', 0) or 0)
            if asset_id and best_ask > 0:
                with _lock_clob:
                    PRIX_CLOB[asset_id] = best_ask

        elif etype == 'market_resolved':
            slug    = d.get('slug','')
            win_out = d.get('winning_outcome','')
            win_id  = d.get('winning_asset_id','')
            if slug:
                resolve_by_slug(slug, win_out, win_id, db_ref, now)

    except Exception as e:
        print(f"[MWS] Err: {e}")

def resolve_by_slug(slug: str, winning_outcome: str, winning_asset_id: str, db, now: int):
    res = None
    wo  = winning_outcome.upper()
    if any(x in wo for x in ['UP','HIGHER','YES']): res = 'UP'
    elif any(x in wo for x in ['DOWN','LOWER','NO']): res = 'DOWN'
    if not res:
        return
    with _lock_marche:
        mkey = next((k for k,v in MARCHES.items() if v.get('slug') == slug), None)
    if not mkey:
        with _lock_db:
            row = db.execute("SELECT market_key FROM cycles WHERE slug=? AND resultat IS NULL",
                           (slug,)).fetchone()
            if row: mkey = row[0]
    if not mkey:
        return
    with _lock_db:
        db.execute("UPDATE cycles SET resultat=?, ts_res=? WHERE market_key=?", (res, now, mkey))
        db.execute("""UPDATE snaps SET resultat=?,
            correct_cl=CASE WHEN direction_cl=? THEN 1 ELSE 0 END,
            pnl_simule=CASE
                WHEN direction_cl=? AND prix_entree>0 THEN ROUND((1.0/prix_entree - 1.0 - ?)*100, 2)
                WHEN direction_cl!=? AND direction_cl IS NOT NULL THEN -100.0
                ELSE NULL END
            WHERE market_key=?""",
            (res, res, res, FEE_PCT, res, mkey))
        db.commit()
    print(f"[RES] {mkey} → {res} ✅")

def start_mws(db):
    def on_open(ws): mws_on_open(ws)
    def on_message(ws, msg): mws_on_message(ws, msg, db)
    def on_error(ws, e): print(f"[MWS] Erreur: {e}")
    def on_close(ws, *a):
        print("[MWS] Ferme — reconnexion 5s...")
        time.sleep(5)
        start_mws(db)
    ws = websocket.WebSocketApp(MARKET_WS,
        on_open=on_open, on_message=on_message,
        on_error=on_error, on_close=on_close)
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ══════════════════════════════════════════════════════
# SCANNER — SNAPSHOTS T-10s → T-1s
# ══════════════════════════════════════════════════════

def scanner(db):
    now = int(time.time())

    global _last_fenetre_call
    with _lock_marche:
        nb_actifs = len(MARCHES)
    if nb_actifs < len(ASSETS) * len(DUREES) // 2 and time.time() - _last_fenetre_call > 60:
        _last_fenetre_call = time.time()
        for minfo in get_marches_par_slug(int(time.time())):
            slug = minfo['slug']
            cid  = minfo['condition_id']
            end_ts_m = minfo['end_ts']
            with _lock_marche:
                mkey_test = f"{minfo['asset']}-{minfo['duree']}-{minfo['start_ts']}"
                if mkey_test in MARCHES:
                    continue
            idu = minfo.get('idu') or ''
            idd = minfo.get('idd') or ''
            register_market(slug, cid, [idu, idd],
                           ['Up','Down'], end_ts_m, db, now)

    with _lock_marche:
        marches_copy = dict(MARCHES)

    for mkey, minfo in marches_copy.items():
        end_ts = minfo.get('end_ts', 0)
        secs   = end_ts - now
        if secs < 0 or secs > max(TIMINGS) + 1:
            continue

        asset = minfo['asset']
        dl    = minfo['duree']
        idu   = minfo.get('idu','')
        idd   = minfo.get('idd','')

        # Enrichir tokens si vides
        if not idu or not idd:
            cid_m = minfo.get('condition_id','')
            if cid_m:
                idu2, idd2 = enrich_tokens(cid_m)
                if idu2: idu = idu2; minfo['idu'] = idu2
                if idd2: idd = idd2; minfo['idd'] = idd2

        with _lock_prix:
            cl_now = PRIX_CL.get(asset)

        with _lock_t0:
            cl_t0 = PRIX_T0.get(mkey)

        delta_cl = ((cl_now - cl_t0) / cl_t0 * 100) if cl_now and cl_t0 else None
        dir_cl   = ('UP' if delta_cl > 0 else 'DOWN') if delta_cl is not None else None

        # Momentum court terme : variation CL sur les ~10 dernieres secondes
        # Compare prix actuel au prix le plus ancien disponible dans la fenetre 10-15s
        momentum_cl = None
        with _lock_hist:
            hist = PRIX_CL_HIST.get(asset, [])
            if hist and cl_now:
                now_h = time.time()
                # Chercher un prix entre 10s et 15s en arriere
                ref_prices = [(t, p) for t, p in hist if 8 <= now_h - t <= 15]
                if ref_prices:
                    # Prendre le plus ancien dans cette fenetre
                    ref_ts, ref_p = min(ref_prices, key=lambda x: x[0])
                    if ref_p > 0:
                        momentum_cl = round((cl_now - ref_p) / ref_p * 100, 5)

        # Prix CLOB — WebSocket d'abord, fallback REST token par token
        with _lock_clob:
            pu = PRIX_CLOB.get(idu) if idu else None
            pd = PRIX_CLOB.get(idd) if idd else None

        # Fallback REST si l'un ou l'autre manque
        if not pu or not pd:
            pu2, pd2 = get_clob_rest(
                idu if not pu else None,
                idd if not pd else None
            )
            if not pu and pu2: pu = pu2
            if not pd and pd2: pd = pd2

        p_ent = (pu if dir_cl == 'UP' else pd) if dir_cl and (pu or pd) else None

        for t in TIMINGS:
            if secs > t or secs < t - 1:
                continue
            with _lock_db:
                if db.execute("SELECT id FROM snaps WHERE market_key=? AND timing=?",
                              (mkey, t)).fetchone():
                    continue

            # Liquidite CLOB uniquement a T-10s, T-5s, T-3s
            liq_vol = liq_best = liq_lvls = None
            if t in (10, 5, 3):
                tid_dir = idu if dir_cl == 'UP' else idd
                if tid_dir:
                    liq_vol, liq_best, liq_lvls = get_book_liquidite(tid_dir)

            with _lock_db:
                db.execute("""INSERT INTO snaps
                    (market_key,asset,duree,timing,ts,
                     prix_cl,prix_cl_t0,delta_cl,momentum_cl,
                     direction_cl,
                     clob_up,clob_dn,
                     liq_ask_vol,liq_ask_best,liq_ask_lvls,
                     prix_entree)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (mkey,asset,dl,t,now,
                     cl_now,cl_t0,delta_cl,momentum_cl,
                     dir_cl,
                     pu,pd,
                     liq_vol,liq_best,liq_lvls,
                     p_ent))
                db.commit()
            liq_str = f'LIQ={liq_vol:.1f}$/{liq_lvls}niv' if liq_vol else ''
            print(f"[SNAP] {asset.upper()} {dl} T-{t}s | "
                  f"CL={f'{delta_cl:+.3f}' if delta_cl else 'N/A'}% "
                  f"MOM={f'{momentum_cl:+.4f}' if momentum_cl is not None else 'N/A'}% "
                  f"dir={dir_cl or 'N/A'} "
                  f"CLOB={f'{pu:.4f}' if pu else f'dn:{pd:.4f}' if pd else 'N/A'} "
                  f"{liq_str}")

def get_clob_rest(idu: str | None, idd: str | None) -> tuple:
    """
    Fallback REST pour prix CLOB.
    Appele uniquement les tokens manquants (idu=None ou idd=None).
    Essaie /midpoints d'abord (1 appel), puis /price token par token.
    """
    pu = pd = None
    tids = [t for t in [idu, idd] if t]
    if not tids:
        return pu, pd
    try:
        r = sess.get(f"{CLOB_URL}/midpoints",
            params={"token_id": tids}, timeout=3)
        if r.status_code == 200:
            mids = r.json()
            if isinstance(mids, dict):
                if idu and idu in mids:
                    pu = float(mids[idu].get('mid', 0) or 0) or None
                if idd and idd in mids:
                    pd = float(mids[idd].get('mid', 0) or 0) or None
            if pu or pd:
                return pu, pd
    except: pass
    # Fallback /price token par token
    for tid, side in [(idu,'u'),(idd,'d')]:
        if not tid: continue
        try:
            r = sess.get(f"{CLOB_URL}/price",
                params={"token_id": tid, "side": "buy"}, timeout=3)
            if r.status_code == 200:
                p = float(r.json().get('price', 0) or 0)
                if p > 0:
                    if side == 'u': pu = p
                    else: pd = p
        except: pass
    return pu, pd

def get_book_liquidite(token_id: str) -> tuple:
    """
    Recupere la liquidite disponible en asks via REST /book.
    Retourne (vol_total_$, vol_meilleur_ask_$, nb_niveaux)
    Filtre uniquement les asks entre 0.90 et 0.999 (zone pertinente).
    Timeout court (2s) pour ne pas bloquer le scanner.
    """
    try:
        r = sess.get(f"{CLOB_URL}/book",
            params={"token_id": token_id}, timeout=2)
        if r.status_code != 200:
            return None, None, None
        book = r.json()
        asks = book.get('asks', [])
        # Filtrer la zone pertinente 0.90-0.999
        asks_ok = [(float(a['price']), float(a['size']))
                   for a in asks
                   if 0.90 <= float(a.get('price', 0)) <= 0.999]
        if not asks_ok:
            return None, None, None
        # Trier par prix croissant
        asks_ok.sort(key=lambda x: x[0])
        vol_total = round(sum(p * s for p, s in asks_ok), 2)
        vol_best  = round(asks_ok[0][0] * asks_ok[0][1], 2)
        nb_lvls   = len(asks_ok)
        return vol_total, vol_best, nb_lvls
    except Exception:
        return None, None, None
    try:
        r = sess.get(f"{CLOB_URL}/clob-markets/{cid}", timeout=5)
        if r.status_code == 200:
            tokens = r.json().get('t', [])
            if len(tokens) >= 2:
                idu = idd = None
                for t in tokens:
                    o = (t.get('o') or '').upper()
                    if any(x in o for x in ['UP','HIGHER','YES']): idu = t.get('t','')
                    elif any(x in o for x in ['DOWN','LOWER','NO']): idd = t.get('t','')
                if not idu and len(tokens) >= 1: idu = tokens[0].get('t','')
                if not idd and len(tokens) >= 2: idd = tokens[1].get('t','')
                if idu and idd: return idu, idd
    except: pass
    try:
        r = sess.get(f"{CLOB_URL}/markets/{cid}", timeout=5)
        if r.status_code != 200: return None, None
        tokens = r.json().get('tokens', [])
        if len(tokens) < 2: return None, None
        t0, t1 = tokens[0], tokens[1]
        o0 = t0.get('outcome','').lower()
        up, dn = (t0,t1) if ('up' in o0 or 'yes' in o0) else (t1,t0)
        return up.get('token_id',''), dn.get('token_id','')
    except: return None, None

# ══════════════════════════════════════════════════════
# RESOLUTION PERIODIQUE (fallback WS)
# ══════════════════════════════════════════════════════

def resolver(db):
    now = int(time.time())
    with _lock_db:
        rows = db.execute("""SELECT market_key, condition_id, end_ts
            FROM cycles WHERE resultat IS NULL AND end_ts < ? AND end_ts > ?""",
            (now, now - 7200)).fetchall()
    for mkey, cid, ets in rows:
        if now < ets + 10: continue
        try:
            r = sess.get(f"{CLOB_URL}/markets/{cid}", timeout=8)
            if r.status_code != 200: continue
            tokens = r.json().get('tokens', [])
            res = None
            for t in tokens:
                if float(t.get('price', 0)) >= 0.99:
                    o = (t.get('outcome') or '').upper()
                    if any(x in o for x in ['UP','HIGHER','YES']): res = 'UP'
                    elif any(x in o for x in ['DOWN','LOWER','NO']): res = 'DOWN'
                    break
            if res:
                with _lock_db:
                    db.execute("UPDATE cycles SET resultat=?, ts_res=? WHERE market_key=?",
                              (res, now, mkey))
                    db.execute("""UPDATE snaps SET resultat=?,
                        correct_cl=CASE WHEN direction_cl=? THEN 1 ELSE 0 END,
                        pnl_simule=CASE
                            WHEN direction_cl=? AND prix_entree>0
                            THEN ROUND((1.0/prix_entree - 1.0 - ?)*100, 2)
                            WHEN direction_cl!=? AND direction_cl IS NOT NULL THEN -100.0
                            ELSE NULL END
                        WHERE market_key=?""",
                        (res,res,res,FEE_PCT,res,mkey))
                    db.commit()
                print(f"[RES] {mkey} → {res} ✅")
        except Exception as e:
            print(f"[RES] Erreur {mkey}: {e}")

# ══════════════════════════════════════════════════════
# NETTOYAGE
# ══════════════════════════════════════════════════════

def cleanup(db):
    now = int(time.time())
    with _lock_marche:
        expired = [k for k,v in MARCHES.items() if v.get('end_ts',0) < now - 120]
        for k in expired:
            del MARCHES[k]

# ══════════════════════════════════════════════════════
# ANALYSE STATISTIQUE v11
# ══════════════════════════════════════════════════════

# Tranches delta (valeur absolue, %)
DELTA_TRANCHES = [
    ('0.05-0.10', 0.05, 0.10),
    ('0.10-0.15', 0.10, 0.15),
    ('≥0.15',     0.15, 999.0),
]

# Zones CLOB — prix du cote gagnant
CLOB_ZONES = [
    ('0.70-0.75', 0.70, 0.75),
    ('0.75-0.80', 0.75, 0.80),
    ('0.80-0.85', 0.80, 0.85),
    ('0.85-0.90', 0.85, 0.90),
    ('0.90-0.95', 0.90, 0.95),
    ('0.95-0.96', 0.95, 0.96),
    ('0.96-0.97', 0.96, 0.97),
    ('0.98-0.99', 0.98, 0.99),
]

N_MIN = 5  # N minimum pour afficher une cellule

def analyser(db) -> dict:
    R = {}

    # ── Analyse principale : asset x duree x delta x timing x zone CLOB ──
    # Structure : analyse[asset][duree][delta_label][timing][zone_label] = {wr, pnl, n}
    analyse = {}
    for asset in ASSETS:
        analyse[asset] = {}
        for dl, _ in DUREES:
            analyse[asset][dl] = {}
            for dlabel, dmin, dmax in DELTA_TRANCHES:
                analyse[asset][dl][dlabel] = {}
                for t in TIMINGS:
                    analyse[asset][dl][dlabel][t] = {}
                    for zlabel, zmin, zmax in CLOB_ZONES:
                        row = db.execute("""
                            SELECT COUNT(*), SUM(correct_cl), AVG(pnl_simule)
                            FROM snaps
                            WHERE resultat IS NOT NULL
                              AND asset = ?
                              AND duree = ?
                              AND timing = ?
                              AND ABS(delta_cl) >= ? AND ABS(delta_cl) < ?
                              AND (
                                (direction_cl='UP'   AND clob_up >= ? AND clob_up < ?)
                             OR (direction_cl='DOWN' AND clob_dn >= ? AND clob_dn < ?)
                              )
                        """, (asset, dl, t, dmin, dmax, zmin, zmax, zmin, zmax)).fetchone()
                        n, w, pnl = row
                        if n and n >= N_MIN:
                            analyse[asset][dl][dlabel][t][zlabel] = {
                                'n':   n,
                                'wr':  round((w or 0) / n * 100, 1),
                                'pnl': round(pnl or 0, 1),
                            }
    R['analyse'] = analyse

    # ── Liquidite moyenne par asset/duree (T-10s, T-5s, T-3s) ──
    liquidite = {}
    for asset in ASSETS:
        liquidite[asset] = {}
        for dl, _ in DUREES:
            rows = db.execute("""
                SELECT timing,
                  AVG(liq_ask_vol)  as vol_moy,
                  AVG(liq_ask_best) as best_moy,
                  AVG(liq_ask_lvls) as lvls_moy,
                  COUNT(*) as n
                FROM snaps
                WHERE asset=? AND duree=?
                  AND timing IN (10,5,3)
                  AND liq_ask_vol IS NOT NULL
                  AND resultat IS NOT NULL
                GROUP BY timing ORDER BY timing DESC
            """, (asset, dl)).fetchall()
            if rows:
                liquidite[asset][dl] = {
                    r[0]: {
                        'vol':  round(r[1] or 0, 1),
                        'best': round(r[2] or 0, 1),
                        'lvls': round(r[3] or 0, 1),
                        'n':    r[4],
                    } for r in rows
                }
    R['liquidite'] = liquidite

    # Stats generales
    s = db.execute("""SELECT COUNT(DISTINCT market_key), COUNT(*),
        SUM(CASE WHEN resultat IS NOT NULL THEN 1 ELSE 0 END),
        MIN(ts), MAX(ts)
        FROM snaps""").fetchone()
    cyc_res = db.execute("SELECT COUNT(*) FROM cycles WHERE resultat IS NOT NULL").fetchone()[0]
    R['stats'] = {
        'cyc': s[0] or 0, 'snaps': s[1] or 0, 'res': s[2] or 0,
        'cyc_res': cyc_res or 0,
        'debut': datetime.fromtimestamp(s[3]).strftime('%d/%m %H:%M') if s[3] else 'N/A',
        'fin':   datetime.fromtimestamp(s[4]).strftime('%d/%m %H:%M') if s[4] else 'N/A',
    }
    with _lock_prix:
        R['prix'] = dict(PRIX_CL)
    with _lock_marche:
        R['nb_marches'] = len(MARCHES)
    return R

# ══════════════════════════════════════════════════════
# DASHBOARD HTML v11
# ══════════════════════════════════════════════════════

def wc(w):
    if w >= 95: return '#00ff88'
    if w >= 90: return '#44dd77'
    if w >= 85: return '#aadd00'
    if w >= 80: return '#ffcc00'
    if w >= 75: return '#ff8800'
    return '#ff4444'

def pc(p):
    if p is None: return '#3a4a5a'
    if p > 10:  return '#00ff88'
    if p > 0:   return '#aadd00'
    if p > -20: return '#ff8800'
    return '#ff4444'

def make_html(R: dict) -> str:
    st        = R.get('stats', {})
    prix_cl   = R.get('prix', {})
    analyse   = R.get('analyse', {})
    liquidite = R.get('liquidite', {})
    res       = st.get('res', 0)
    now       = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
    nm        = R.get('nb_marches', 0)

    live = ''.join(f'''<div class="li">
        <span class="ls">{a.upper()}</span>
        <span class="lp">{"$"+f"{prix_cl.get(a):,.2f}" if prix_cl.get(a) else "—"}</span>
        <span class="ls2">Chainlink</span>
    </div>''' for a in ASSETS)

    notice = f'<div class="notice">⏳ <strong>{st.get("cyc_res",0)}</strong> cycles resolus — minimum 50 recommande par duree</div>' if st.get('cyc_res',0) < 50 else ''

    zone_labels = [z[0] for z in CLOB_ZONES]

    def cell(data):
        if not data:
            return '<td class="emp">—</td>'
        w   = data['wr']
        n   = data['n']
        pnl = data['pnl']
        pc_color = pc(pnl)
        pnl_str  = f'{pnl:+.0f}%' if pnl is not None else '?'
        return (f'<td style="background:{wc(w)}14;border-left:2px solid {wc(w)}40" '
                f'title="N={n} | PNL={pnl_str}">'
                f'<span style="color:{wc(w)};font-weight:700">{w}%</span>'
                f'<br><span style="color:{pc_color};font-size:9px">{pnl_str}</span>'
                f'<br><span class="nn">{n}</span>'
                f'</td>')

    # Sections par asset
    sections = ''
    for asset in ASSETS:
        prix_str = f"${prix_cl.get(asset):,.2f}" if prix_cl.get(asset) else '—'

        # Liquidite par duree
        liq_info = ''
        for dl, _ in DUREES:
            ldata = liquidite.get(asset, {}).get(dl, {})
            if ldata:
                # Prendre T-5s comme reference
                ref = ldata.get(5) or ldata.get(10) or ldata.get(3)
                if ref and ref['vol'] > 0:
                    vol = ref['vol']
                    lc = '#00ff88' if vol >= 100 else '#ffcc00' if vol >= 20 else '#ff4444'
                    liq_info += f'<span class="liq-badge" style="border-color:{lc};color:{lc}">{dl} ~{vol:.0f}$</span>'

        asset_html = ''
        for dl, _ in DUREES:
            duree_html = ''
            for dlabel, dmin, dmax in DELTA_TRANCHES:
                tdata = analyse.get(asset, {}).get(dl, {}).get(dlabel, {})

                thead = ('<tr><th>Timing</th>'
                         + ''.join(f'<th>{z}</th>' for z in zone_labels)
                         + '</tr>')
                rows = ''
                has_data = False
                for t in sorted(TIMINGS, reverse=True):
                    zdata    = tdata.get(t, {})
                    row_cells = ''.join(cell(zdata.get(z)) for z in zone_labels)
                    if any(zdata.get(z) for z in zone_labels):
                        has_data = True
                    rows += f'<tr><td class="tm">T-{t}s</td>{row_cells}</tr>'

                table = (f'<div class="tscroll"><table>{thead}{rows}</table></div>'
                         if has_data else '<div class="emp-blk">En collecte...</div>')
                duree_html += f'''
                <div class="dtab">
                  <div class="dlabel">Δ {dlabel}%</div>
                  {table}
                </div>'''

            asset_html += f'''
            <div class="duree-block">
              <div class="duree-hdr">{dl}</div>
              {duree_html}
            </div>'''

        sections += f'''
        <div class="asset-card">
          <div class="asset-hdr">
            <span class="asset-name">{asset.upper()}</span>
            <span class="asset-prix">{prix_str}</span>
            <span class="liq-wrap">{liq_info}</span>
          </div>
          {asset_html}
        </div>'''

    CSS = """
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@400;700;800&display=swap');
:root{--bg:#080b0f;--bg2:#0f1318;--bg3:#161b22;--bd:#1c2230;--tx:#b8c8d8;--di:#3a4a5a;--ac:#00d4ff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:'Syne',sans-serif;padding:16px}
.hdr{border-bottom:1px solid var(--bd);padding-bottom:12px;margin-bottom:18px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px}
.ttl{font-size:22px;font-weight:800;color:var(--ac)}
.sub{font-size:11px;color:var(--di);margin-top:3px}
.meta{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--di);text-align:right}
.live{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px}
.li{background:var(--bg2);border:1px solid var(--bd);border-radius:6px;padding:8px 12px;min-width:90px}
.ls{font-size:10px;color:var(--di);text-transform:uppercase;letter-spacing:1px;display:block}
.ls2{font-size:9px;color:var(--di);display:block;margin-top:1px}
.lp{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;color:var(--ac);margin-top:2px;display:block}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px;margin-bottom:18px}
.kp{background:var(--bg2);border:1px solid var(--bd);border-radius:8px;padding:12px}
.kl{font-size:10px;color:var(--di);text-transform:uppercase;letter-spacing:1px}
.kv{font-size:20px;font-weight:800;color:var(--ac);font-family:'JetBrains Mono',monospace;margin-top:4px}
.notice{background:rgba(0,212,255,.05);border:1px solid rgba(0,212,255,.2);border-radius:8px;padding:12px;font-size:12px;color:var(--di);margin-bottom:14px}
.notice strong{color:var(--ac)}
.tag{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;background:rgba(0,255,136,.1);color:#00ff88;border:1px solid rgba(0,255,136,.2);margin-left:4px}
.asset-card{background:var(--bg2);border:1px solid var(--bd);border-radius:12px;padding:16px;margin-bottom:16px}
.asset-hdr{display:flex;align-items:center;gap:12px;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid var(--bd);flex-wrap:wrap}
.asset-name{font-size:20px;font-weight:800;color:var(--ac);min-width:60px}
.asset-prix{font-family:'JetBrains Mono',monospace;font-size:13px;color:var(--tx)}
.liq-wrap{display:flex;gap:6px;flex-wrap:wrap;margin-left:auto}
.liq-badge{font-family:'JetBrains Mono',monospace;font-size:9px;padding:2px 6px;border-radius:3px;border:1px solid;opacity:.9}
.duree-block{margin-bottom:18px}
.duree-hdr{font-size:13px;font-weight:800;color:var(--tx);text-transform:uppercase;letter-spacing:2px;padding:6px 10px;background:var(--bg3);border-radius:6px;display:inline-block;margin-bottom:10px;border-left:3px solid var(--ac)}
.dtab{margin-bottom:12px}
.dlabel{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:var(--di);margin-bottom:5px;padding:2px 7px;background:rgba(0,212,255,.06);border-radius:3px;display:inline-block}
.tscroll{overflow-x:auto}
table{border-collapse:collapse;font-size:11px;min-width:600px}
th{text-align:center;padding:5px 6px;font-size:9px;text-transform:uppercase;letter-spacing:.6px;color:var(--di);border-bottom:1px solid var(--bd);white-space:nowrap}
th:first-child{text-align:left;min-width:55px}
td{padding:5px 6px;border-bottom:1px solid rgba(28,34,48,.3);text-align:center;font-family:'JetBrains Mono',monospace;font-size:11px;line-height:1.3;vertical-align:middle}
td:first-child{text-align:left}
td.tm{color:var(--tx);font-weight:700;font-size:11px}
td.emp{color:var(--di);font-size:10px}
tr:hover td{background:rgba(255,255,255,.02)}
.nn{font-size:8px;opacity:.4;font-weight:400;display:block}
.emp-blk{color:var(--di);font-size:11px;padding:6px 0;font-style:italic}
"""

    return f"""<!DOCTYPE html>
<html lang="fr"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>⚡ Delta Analyzer v12</title>
<style>{CSS}</style>
</head><body>
<div class="hdr">
  <div><div class="ttl">⚡ DELTA ANALYZER <span class="tag">v12</span></div>
  <div class="sub">Polymarket • Chainlink RTDS • WR+PNL+Liquidite par asset / duree / delta / timing / zone CLOB</div></div>
  <div class="meta">Refresh 60s<br><strong style="color:var(--tx)">{now}</strong></div>
</div>
<div class="live">{live}</div>
<div class="kpis">
  <div class="kp"><div class="kl">Marches actifs</div><div class="kv">{nm}</div></div>
  <div class="kp"><div class="kl">Cycles</div><div class="kv">{st.get('cyc',0)}</div></div>
  <div class="kp"><div class="kl">Cycles resolus</div><div class="kv">{st.get('cyc_res',0)}</div></div>
  <div class="kp"><div class="kl">Snapshots</div><div class="kv">{st.get('snaps',0)}</div></div>
  <div class="kp"><div class="kl">Depuis</div><div class="kv" style="font-size:11px">{st.get('debut','—')}</div></div>
</div>
{notice}
{sections}
</body></html>"""

# ══════════════════════════════════════════════════════
# SERVEUR WEB
# ══════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(_HTML.encode('utf-8'))
    def log_message(self,*a): pass

# ══════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════

def main():
    global _HTML
    print("="*60)
    print("   ⚡  DELTA ANALYZER v12 — Liquidite CLOB")
    print("="*60)
    print(f"   Assets    : {', '.join(a.upper() for a in ASSETS)}")
    print(f"   Durees    : {', '.join(d[0] for d in DUREES)}")
    print(f"   Signal    : Chainlink + momentum + liquidite CLOB (T-10/5/3s)")
    print(f"   Analyse   : asset x duree x delta x timing x zone CLOB")
    print(f"   Dashboard : http://localhost:{PORT}")
    print(f"   DB        : {DB_PATH}")
    print("="*60)

    db = init_db()

    threading.Thread(target=lambda: HTTPServer((HOST,PORT),Handler).serve_forever(),
                     daemon=True).start()
    print(f"[WEB] Dashboard → http://localhost:{PORT}")

    print("[RTDS] Connexion RTDS Polymarket — Chainlink uniquement...")
    start_rtds()
    time.sleep(6)
    with _lock_prix:
        print(f"[RTDS] CL: {len(PRIX_CL)}/7")
        if PRIX_CL: print("[RTDS] CL prix: " + " ".join(f"{a}={v:.2f}" for a,v in PRIX_CL.items()))

    print("[MWS] Connexion Market WebSocket...")
    start_mws(db)
    time.sleep(3)

    print("[INIT] Chargement marches courants...")
    now = int(time.time())
    marches_init = get_marches_par_slug(now)
    for minfo in marches_init:
        slug     = minfo['slug']
        cid      = minfo['condition_id']
        end_ts_i = minfo['end_ts']
        idu      = minfo.get('idu') or ''
        idd      = minfo.get('idd') or ''
        register_market(slug, cid, [idu, idd],
                       ['Up','Down'], end_ts_i, db, now)

    with _lock_marche:
        print(f"[INIT] {len(MARCHES)} marches charges")

    ts = tl = tc = th = 0.0
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Boucle demarre\n")

    while True:
        now = time.time()
        if now - ts >= 0.5:
            scanner(db); ts = now
        if now - tl >= 30:
            resolver(db); tl = now
        if now - tc >= 60:
            cleanup(db); tc = now
        if now - th >= 60:
            try: _HTML = make_html(analyser(db))
            except Exception as e: print(f"[HTML] {e}")
            th = now
        time.sleep(0.1)

if __name__ == '__main__':
    main()
