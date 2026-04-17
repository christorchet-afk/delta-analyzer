"""
DELTA ANALYZER — Railway Edition v3
=====================================
Monitoring Polymarket crypto Up/Down + Dashboard web
Prix via Kraken REST (pas de restrictions géo)
"""

import os, time, json, math, sqlite3, threading, requests
from datetime import datetime
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler

# ═══════════════ CONFIG ═══════════════

PORT   = int(os.environ.get('PORT', 8080))
DB     = 'delta.db'

ASSETS = ["btc", "eth", "sol", "xrp", "doge", "hype", "bnb"]
DUREES = [("5m", 300), ("15m", 900), ("4h", 14400)]

GAMMA  = "https://gamma-api.polymarket.com"
CLOB   = "https://clob.polymarket.com"

TIMINGS = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
SEUILS  = [0.02, 0.03, 0.05, 0.07, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30]

KRAKEN = {
    "btc":"XBTUSD","eth":"ETHUSD","sol":"SOLUSD",
    "xrp":"XRPUSD","doge":"DOGEUSD","bnb":"BNBUSD","hype":"HYPEUSD"
}

sess = requests.Session()
sess.headers.update({'User-Agent': 'DeltaAnalyzer/3.0'})

# ═══════════════ ÉTAT GLOBAL ═══════════════

PRIX   = {}   # {asset: price}
T0     = {}   # {market_key: price_t0}
SNAPS  = defaultdict(dict)
_pl    = threading.Lock()
_tl    = threading.Lock()
_dl    = threading.Lock()
HTML   = "<html><body style='background:#080b0f;color:#00d4ff;font-family:monospace;padding:40px'><h2>⚡ Démarrage...</h2></body></html>"

# ═══════════════ BASE DE DONNÉES ═══════════════

def init_db():
    db = sqlite3.connect(DB, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS cycles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, asset TEXT, duree TEXT,
        market_key TEXT UNIQUE, slug TEXT,
        condition_id TEXT, start_ts INTEGER,
        end_ts INTEGER, prix_t0 REAL,
        resultat TEXT, ts_res INTEGER
    );
    CREATE TABLE IF NOT EXISTS snaps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market_key TEXT, asset TEXT, duree TEXT,
        timing INTEGER, ts INTEGER,
        prix_now REAL, prix_t0 REAL, delta REAL,
        direction TEXT, clob_up REAL, clob_dn REAL,
        liq_up REAL, liq_dn REAL,
        resultat TEXT, correct INTEGER
    );
    CREATE INDEX IF NOT EXISTS i1 ON snaps(asset);
    CREATE INDEX IF NOT EXISTS i2 ON snaps(timing);
    CREATE INDEX IF NOT EXISTS i3 ON snaps(market_key);
    CREATE INDEX IF NOT EXISTS i4 ON cycles(market_key);
    """)
    db.commit()
    print(f"[DB] OK → {DB}")
    return db

# ═══════════════ KRAKEN PRIX ═══════════════

def kraken_loop():
    while True:
        try:
            pairs = ",".join(KRAKEN.values())
            r = sess.get(f"https://api.kraken.com/0/public/Ticker?pair={pairs}", timeout=8)
            if r.status_code == 200:
                data = r.json().get('result', {})
                for asset, pair in KRAKEN.items():
                    base = pair.replace('USD','')
                    for key, val in data.items():
                        if base in key:
                            p = float(val['c'][0])
                            if p > 0:
                                with _pl:
                                    PRIX[asset] = p
                            break
                with _pl:
                    nb = len(PRIX)
                btc = PRIX.get('btc', 0)
                print(f"[KRAKEN] {nb}/{len(ASSETS)} assets | BTC={btc:.0f}$")
        except Exception as e:
            print(f"[KRAKEN] Erreur: {e}")
        time.sleep(5)

# ═══════════════ POLYMARKET ═══════════════

def get_market(asset, duree_label):
    try:
        r = sess.get(f"{GAMMA}/markets",
            params={"tag_slug": f"crypto-{asset}", "active":"true", "closed":"false", "limit":50},
            timeout=10)
        if r.status_code != 200: return None
        markets = r.json()
        if isinstance(markets, dict): markets = markets.get('markets', [])
        now = int(time.time())
        ds  = {"5m":300,"15m":900,"4h":14400}.get(duree_label, 300)
        best = None
        best_diff = 999999
        for m in markets:
            slug = m.get('slug','').lower()
            q    = m.get('question','').lower()
            if not (asset in slug or asset in q): continue
            if not (duree_label in slug or duree_label in q): continue
            ets = m.get('endDateIso') or m.get('end_date_iso')
            if not ets: continue
            try:
                if isinstance(ets, str):
                    dt = datetime.fromisoformat(ets.replace('Z','+00:00'))
                    ets = int(dt.timestamp())
                diff = abs(ets - now - ds)
                if diff < best_diff and abs(ets - now) < ds * 3:
                    best_diff = diff
                    best = (m, ets)
            except: pass
        if not best: return None
        m, ets = best
        tokens = m.get('tokens', m.get('outcomes', []))
        tu = td = idu = idd = None
        for t in tokens:
            name = (t.get('outcome') or t.get('name') or '').upper()
            tid  = t.get('token_id') or t.get('id') or ''
            p    = float(t.get('price', 0.5))
            if any(x in name for x in ['UP','HIGHER','YES']):
                tu, idu = p, tid
            elif any(x in name for x in ['DOWN','LOWER','NO']):
                td, idd = p, tid
        return {'slug': m.get('slug',''), 'condition_id': m.get('condition_id',''),
                'end_ts': ets, 'pu': tu or 0.5, 'pd': td or 0.5,
                'idu': idu, 'idd': idd}
    except: return None

def get_clob(idu, idd):
    pu = pd = lu = ld = None
    for tid, side in [(idu,'u'),(idd,'d')]:
        if not tid: continue
        try:
            r = sess.get(f"{CLOB}/book", params={"token_id":tid}, timeout=5)
            if r.status_code != 200: continue
            asks = r.json().get('asks',[])
            if asks:
                p = float(asks[0].get('price',0))
                l = sum(float(a.get('size',0)) for a in asks[:3])
                if side=='u': pu,lu = p,l
                else: pd,ld = p,l
        except: pass
    return pu, pd, lu, ld

def get_result(cid):
    try:
        r = sess.get(f"https://data-api.polymarket.com/markets/{cid}", timeout=10)
        if r.status_code != 200: return None
        for o in r.json().get('outcomes',[]):
            if float(o.get('price',0)) > 0.99:
                name = (o.get('outcome') or o.get('name') or '').upper()
                if any(x in name for x in ['UP','HIGHER','YES']): return 'UP'
                if any(x in name for x in ['DOWN','LOWER','NO']): return 'DOWN'
    except: pass
    return None

# ═══════════════ SCANNER ═══════════════

def scanner(db):
    now = int(time.time())
    for asset in ASSETS:
        for dl, ds in DUREES:
            try:
                info = get_market(asset, dl)
                if not info: continue
                ets  = info['end_ts']
                sts  = ets - ds
                secs = ets - now
                mkey = f"{asset}-{dl}-{sts}"
                with _dl:
                    ex = db.execute("SELECT id,prix_t0 FROM cycles WHERE market_key=?", (mkey,)).fetchone()
                    if not ex:
                        with _pl: p0 = PRIX.get(asset)
                        db.execute("INSERT OR IGNORE INTO cycles (ts,asset,duree,market_key,slug,condition_id,start_ts,end_ts,prix_t0) VALUES (?,?,?,?,?,?,?,?,?)",
                            (now,asset,dl,mkey,info['slug'],info['condition_id'],sts,ets,p0))
                        db.commit()
                        if p0:
                            with _tl: T0[mkey] = p0
                        print(f"[NEW] {asset.upper()} {dl} T-{secs}s p0={p0}")
                    else:
                        with _tl:
                            if mkey not in T0 and ex[1]:
                                T0[mkey] = ex[1]
                if secs < 0 or secs > 12: continue
                with _pl: pn = PRIX.get(asset)
                with _tl: p0 = T0.get(mkey)
                if not pn or not p0: continue
                delta = (pn - p0) / p0 * 100
                direc = 'UP' if delta > 0 else 'DOWN'
                pu, pd, lu, ld = get_clob(info.get('idu'), info.get('idd'))
                for t in TIMINGS:
                    if secs > t or secs < t-1: continue
                    with _dl:
                        if db.execute("SELECT id FROM snaps WHERE market_key=? AND timing=?", (mkey,t)).fetchone(): continue
                        db.execute("INSERT INTO snaps (market_key,asset,duree,timing,ts,prix_now,prix_t0,delta,direction,clob_up,clob_dn,liq_up,liq_dn) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (mkey,asset,dl,t,now,pn,p0,delta,direc,pu,pd,lu,ld))
                        db.commit()
                    print(f"[SNAP] {asset.upper()} {dl} T-{t}s delta={delta:+.3f}% {direc}")
            except Exception as e:
                print(f"[ERR] {asset} {dl}: {e}")

def resolver(db):
    now = int(time.time())
    with _dl:
        rows = db.execute("SELECT market_key,condition_id,end_ts FROM cycles WHERE resultat IS NULL AND end_ts<?AND end_ts>?", (now,now-3600)).fetchall()
    for mkey, cid, ets in rows:
        if now < ets+10: continue
        res = get_result(cid)
        if not res: continue
        with _dl:
            db.execute("UPDATE cycles SET resultat=?,ts_res=? WHERE market_key=?", (res,now,mkey))
            db.execute("UPDATE snaps SET resultat=?,correct=CASE WHEN direction=? THEN 1 ELSE 0 END WHERE market_key=?", (res,res,mkey))
            db.commit()
        print(f"[RES] {mkey} → {res} ✅")

# ═══════════════ ANALYSE ═══════════════

def analyse(db):
    R = {}
    # WR par timing
    wt = {}
    for t in TIMINGS:
        row = db.execute("SELECT COUNT(*),SUM(correct),AVG(CASE WHEN correct=1 THEN CASE direction WHEN 'UP' THEN (1.0/clob_up)-1 ELSE (1.0/clob_dn)-1 END ELSE -1.0 END) FROM snaps WHERE timing=? AND resultat IS NOT NULL",(t,)).fetchone()
        n,w,ev = row
        if n and n>=5:
            wt[t]={'n':n,'w':w or 0,'wr':round((w or 0)/n*100,1),'ev':round((ev or 0)*100,2)}
    R['wt'] = wt

    # Delta optimal par asset
    dopt = {}
    for a in ASSETS:
        best=None; bs=0
        for s in SEUILS:
            row = db.execute("SELECT COUNT(*),SUM(correct) FROM snaps WHERE asset=? AND timing=5 AND ABS(delta)>=? AND resultat IS NOT NULL",(a,s)).fetchone()
            n,w = row
            if n and n>=5:
                wr=(w or 0)/n*100
                sc=wr*math.log(max(n,1))
                if sc>bs: bs=sc; best={'s':s,'wr':round(wr,1),'n':n}
        dopt[a]=best
    R['dopt'] = dopt

    # WR delta x timing x asset
    wdta = {}
    for a in ASSETS:
        wdta[a]={}
        for t in [8,7,6,5,4,3]:
            wdta[a][t]=[]
            for s in SEUILS:
                row = db.execute("SELECT COUNT(*),SUM(correct) FROM snaps WHERE asset=? AND timing=? AND ABS(delta)>=? AND resultat IS NOT NULL",(a,t,s)).fetchone()
                n,w=row
                if n and n>=3:
                    wdta[a][t].append({'s':s,'n':n,'wr':round((w or 0)/n*100,1)})
    R['wdta'] = wdta

    # Rentabilité par prix
    rp={}
    buckets=[(0.75,0.80),(0.80,0.85),(0.85,0.88),(0.88,0.91),(0.91,0.93),(0.93,0.95),(0.95,0.97),(0.97,0.99)]
    for t in [8,7,6,5,4,3]:
        rp[t]=[]
        for pmin,pmax in buckets:
            row = db.execute("SELECT COUNT(*),SUM(correct),AVG(CASE direction WHEN 'UP' THEN clob_up ELSE clob_dn END) FROM snaps WHERE timing=? AND ((direction='UP' AND clob_up BETWEEN ? AND ?) OR (direction='DOWN' AND clob_dn BETWEEN ? AND ?)) AND resultat IS NOT NULL",(t,pmin,pmax,pmin,pmax)).fetchone()
            n,w,pm=row
            if n and n>=3 and pm:
                wr=(w or 0)/n*100
                rdt=(1/pm-1)*100
                ev=(wr/100*rdt)-((1-wr/100)*100)
                rp[t].append({'pmin':pmin,'pmax':pmax,'n':n,'wr':round(wr,1),'pm':round(pm,3),'rdt':round(rdt,1),'ev':round(ev,2)})
    R['rp'] = rp

    # WR par durée
    wd={}
    for dl,_ in DUREES:
        row = db.execute("SELECT COUNT(*),SUM(correct),COUNT(DISTINCT market_key) FROM snaps WHERE duree=? AND timing=5 AND resultat IS NOT NULL",(dl,)).fetchone()
        n,w,nb=row
        if n and n>=3:
            wd[dl]={'n':n,'wr':round((w or 0)/n*100,1),'nb':nb}
    R['wd'] = wd

    # Stats
    s = db.execute("SELECT COUNT(DISTINCT market_key),COUNT(*),SUM(CASE WHEN resultat IS NOT NULL THEN 1 ELSE 0 END),MIN(ts),MAX(ts) FROM snaps").fetchone()
    R['stats']={'cyc':s[0] or 0,'snaps':s[1] or 0,'res':s[2] or 0,
        'debut':datetime.fromtimestamp(s[3]).strftime('%d/%m %H:%M') if s[3] else 'N/A',
        'fin':datetime.fromtimestamp(s[4]).strftime('%d/%m %H:%M') if s[4] else 'N/A'}
    with _pl: R['prix'] = dict(PRIX)
    return R

# ═══════════════ HTML DASHBOARD ═══════════════

def wc(w): 
    if w>=93: return '#00ff88'
    if w>=88: return '#88ff44'
    if w>=82: return '#ffcc00'
    if w>=75: return '#ff8800'
    return '#ff4444'

def ec(e):
    if e>3: return '#00ff88'
    if e>0: return '#88ff44'
    if e>-5: return '#ffcc00'
    return '#ff4444'

def make_html(R):
    st   = R.get('stats',{})
    prix = R.get('prix',{})
    wt   = R.get('wt',{})
    dopt = R.get('dopt',{})
    wdta = R.get('wdta',{})
    rp   = R.get('rp',{})
    wd   = R.get('wd',{})
    res  = st.get('res',0)
    now  = datetime.now().strftime('%d/%m/%Y %H:%M:%S')

    live = ""
    for a in ASSETS:
        p = prix.get(a)
        v = f"${p:,.2f}" if p else "—"
        live += f'<div class="li"><span class="ls">{a.upper()}</span><span class="lp">{v}</span></div>'

    hwt = ""
    for t in sorted(wt.keys(),reverse=True):
        d=wt[t]; c1=wc(d['wr']); c2=ec(d['ev'])
        hwt += f'<tr><td class="m">T-{t}s</td><td style="color:{c1};font-weight:700">{d["wr"]}%</td><td style="color:{c2}">{d["ev"]:+.1f}%</td><td class="di">{d["w"]}/{d["n"]}</td></tr>'

    hdo = ""
    for a in ASSETS:
        d=dopt.get(a)
        if d: c=wc(d['wr']); hdo+=f'<tr><td class="at">{a.upper()}</td><td class="m">{d["s"]}%</td><td style="color:{c};font-weight:700">{d["wr"]}%</td><td class="di">{d["n"]}</td></tr>'
        else: hdo+=f'<tr><td class="at">{a.upper()}</td><td class="di" colspan="3">En collecte...</td></tr>'

    hwd = ""
    for dl,_ in DUREES:
        d=wd.get(dl)
        if d: c=wc(d['wr']); hwd+=f'<tr><td class="m">{dl}</td><td style="color:{c};font-weight:700">{d["wr"]}%</td><td class="di">{d["nb"]} cycles</td></tr>'

    hrp = ""
    for t in [7,5,3]:
        data=rp.get(t,[])
        if not data: continue
        hrp+=f'<div class="sl">T-{t}s</div><table><tr><th>Prix</th><th>WR</th><th>Rdt</th><th>EV</th><th>N</th></tr>'
        for d in data:
            c1=wc(d['wr']); c2=ec(d['ev'])
            hrp+=f'<tr><td class="m">{d["pmin"]:.2f}–{d["pmax"]:.2f}</td><td style="color:{c1};font-weight:700">{d["wr"]}%</td><td class="m">{d["rdt"]:+.1f}%</td><td style="color:{c2}">{d["ev"]:+.2f}%</td><td class="di">{d["n"]}</td></tr>'
        hrp+='</table>'

    hdd = ""
    for a in ASSETS:
        ad=wdta.get(a,{})
        has=any(ad.get(t) for t in [7,5,3])
        if not has: continue
        hdd+=f'<div class="sl">{a.upper()}</div><table><tr><th>Delta</th>'
        for t in [8,7,6,5,4,3]: hdd+=f'<th>T-{t}s</th>'
        hdd+='</tr>'
        for s in SEUILS:
            row=f'<td class="m">{s}%</td>'; hr=False
            for t in [8,7,6,5,4,3]:
                f2=next((d for d in ad.get(t,[]) if d['s']==s),None)
                if f2: c=wc(f2['wr']); row+=f'<td style="color:{c}">{f2["wr"]}%<span class="di"> ({f2["n"]})</span></td>'; hr=True
                else: row+='<td class="di">—</td>'
            if hr: hdd+=f'<tr>{row}</tr>'
        hdd+='</table>'

    ntc = f'<div class="notice">⏳ Collecte en cours — <strong>{res}</strong> cycles résolus sur ~50 nécessaires.</div>' if res<20 else ''

    return f"""<!DOCTYPE html><html lang="fr"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>⚡ Delta Analyzer</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@400;700;800&display=swap');
:root{{--bg:#080b0f;--bg2:#0f1318;--bg3:#161b22;--bd:#1c2230;--tx:#b8c8d8;--di:#3a4a5a;--ac:#00d4ff;--gn:#00ff88;--yw:#ffd000;--rd:#ff4455}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--tx);font-family:'Syne',sans-serif;padding:16px;min-height:100vh}}
.hdr{{border-bottom:1px solid var(--bd);padding-bottom:12px;margin-bottom:18px;display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:8px}}
.ttl{{font-size:22px;font-weight:800;color:var(--ac)}}
.sub{{font-size:11px;color:var(--di);margin-top:3px}}
.meta{{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--di);text-align:right}}
.live{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px}}
.li{{background:var(--bg2);border:1px solid var(--bd);border-radius:6px;padding:8px 12px;min-width:75px}}
.ls{{font-size:10px;color:var(--di);text-transform:uppercase;letter-spacing:1px;display:block}}
.lp{{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;color:var(--ac);margin-top:2px;display:block}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:18px}}
.sc{{background:var(--bg2);border:1px solid var(--bd);border-radius:8px;padding:12px}}
.sl2{{font-size:10px;color:var(--di);text-transform:uppercase;letter-spacing:1px}}
.sv{{font-size:20px;font-weight:800;color:var(--ac);font-family:'JetBrains Mono',monospace;margin-top:4px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}}
@media(max-width:550px){{.grid{{grid-template-columns:1fr}}}}
.card{{background:var(--bg2);border:1px solid var(--bd);border-radius:10px;padding:14px}}
.ct{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:var(--ac);margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--bd)}}
.full{{grid-column:1/-1}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{text-align:left;padding:5px 7px;font-size:10px;text-transform:uppercase;letter-spacing:.8px;color:var(--di);border-bottom:1px solid var(--bd)}}
td{{padding:6px 7px;border-bottom:1px solid rgba(28,34,48,.5)}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:var(--bg3)}}
.m{{font-family:'JetBrains Mono',monospace}}
.di{{color:var(--di);font-size:11px}}
.at{{font-weight:700;color:var(--ac)}}
.sl{{font-size:11px;font-weight:700;color:var(--yw);text-transform:uppercase;letter-spacing:1px;margin:12px 0 6px}}
.notice{{background:rgba(0,212,255,.05);border:1px solid rgba(0,212,255,.2);border-radius:8px;padding:12px 14px;font-size:12px;color:var(--di);margin-bottom:14px}}
.notice strong{{color:var(--ac)}}
.em{{color:var(--di);font-size:12px;padding:14px 0}}
</style></head><body>
<div class="hdr">
  <div><div class="ttl">⚡ DELTA ANALYZER</div><div class="sub">Polymarket Crypto Up/Down — Monitoring temps réel</div></div>
  <div class="meta">Refresh 60s<br><strong style="color:var(--tx)">{now}</strong></div>
</div>
<div class="live">{live}</div>
<div class="stats">
  <div class="sc"><div class="sl2">Cycles</div><div class="sv">{st.get('cyc',0)}</div></div>
  <div class="sc"><div class="sl2">Snapshots</div><div class="sv">{st.get('snaps',0)}</div></div>
  <div class="sc"><div class="sl2">Résolus</div><div class="sv">{res}</div></div>
  <div class="sc"><div class="sl2">Depuis</div><div class="sv" style="font-size:13px">{st.get('debut','—')}</div></div>
</div>
{ntc}
<div class="grid">
  <div class="card"><div class="ct">📊 WR par timing</div>{'<div class="em">En collecte...</div>' if not hwt else f'<table><tr><th>Timing</th><th>WR</th><th>EV</th><th>Trades</th></tr>{hwt}</table>'}</div>
  <div class="card"><div class="ct">🎯 Delta optimal / asset (T-5s)</div><table><tr><th>Asset</th><th>Delta</th><th>WR</th><th>N</th></tr>{hdo}</table></div>
</div>
<div class="card full" style="margin-bottom:12px"><div class="ct">💰 Rentabilité par prix CLOB</div>{'<div class="em">En collecte...</div>' if not hrp else hrp}</div>
<div class="grid">
  <div class="card"><div class="ct">⏱️ WR par durée (T-5s)</div>{'<div class="em">En collecte...</div>' if not hwd else f'<table><tr><th>Durée</th><th>WR</th><th>Cycles</th></tr>{hwd}</table>'}</div>
  <div class="card"><div class="ct">🎨 Légende WR</div><table>
    <tr><td style="color:#00ff88;font-weight:700">≥93%</td><td>Excellent ✅</td></tr>
    <tr><td style="color:#88ff44;font-weight:700">88–93%</td><td>Bon ✅</td></tr>
    <tr><td style="color:#ffcc00;font-weight:700">82–88%</td><td>Limite ⚠️</td></tr>
    <tr><td style="color:#ff4455;font-weight:700">&lt;82%</td><td>Éviter ❌</td></tr>
  </table></div>
</div>
<div class="card full"><div class="ct">🔬 WR delta × timing × asset</div>{'<div class="em">En collecte — besoin de ~50 trades par asset</div>' if not hdd else hdd}</div>
</body></html>"""

# ═══════════════ SERVEUR WEB ═══════════════

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(HTML.encode('utf-8'))
    def log_message(self,*a): pass

def web_loop():
    HTTPServer(('0.0.0.0', PORT), Handler).serve_forever()

# ═══════════════ MAIN ═══════════════

def main():
    global HTML
    print("="*50)
    print("  ⚡ DELTA ANALYZER v3 — Railway")
    print(f"  Assets : {', '.join(a.upper() for a in ASSETS)}")
    print(f"  Port   : {PORT}")
    print("="*50)

    db = init_db()
    threading.Thread(target=web_loop, daemon=True).start()
    print(f"[WEB] Dashboard → port {PORT}")

    threading.Thread(target=kraken_loop, daemon=True).start()
    print("[KRAKEN] Démarré — attente prix...")
    time.sleep(8)

    ts=tl=th = 0.0
    while True:
        now = time.time()
        if now-ts >= 0.5:
            scanner(db); ts=now
        if now-tl >= 30:
            resolver(db); tl=now
        if now-th >= 60:
            try: HTML = make_html(analyse(db))
            except Exception as e: print(f"[HTML] {e}")
            th=now
        time.sleep(0.1)

if __name__=='__main__':
    main()
