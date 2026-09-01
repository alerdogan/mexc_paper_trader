from typing import Optional
import asyncio, json, sqlite3, subprocess, hmac, hashlib, random, time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

BASE=Path(__file__).parent; DB=BASE/'trader.db'; app=FastAPI(title='MEXC Futures Paper Trader'); templates=Jinja2Templates(directory=str(BASE/'templates'))
DEFAULTS={"symbols":["BTC_USDT"],"top_volume_count":30,"universe_refresh_minutes":15,"paper_balance":4000.0,"risk_per_trade_usd":20.0,"max_alt_notional_usd":5000.0,"max_btc_notional_usd":10000.0,"max_total_open_risk_usd":100.0,"daily_loss_limit_usd":300.0,"leverage":2,"signal_threshold":80,"scan_seconds":30,"stop_atr_mult":1.5,"tp1_r":1.0,"tp1_pct":30.0,"tp2_r":2.0,"tp2_pct":30.0,"runner_pct":40.0,"move_be_at_r":1.0,"min_free_balance_pct":20.0,"paper_fee_rate":0.0008}
settings=DEFAULTS.copy(); state={"running":False,"panic":False,"last_scan":None,"market":{},"live_prices":{},"last_price_update":None,"scanning":False,"error":None,"feed":"MEXC FUTURES","public_api":None,"api_saved":False,"private_api":None,"paper_test_threshold":None,"universe":[],"universe_updated":None,"scan_duration_sec":None,"rate_limit_wait":None}
TASK_NAMES=('scanner','position_engine','ghost_analyzer')
SUPERVISOR_BACKOFF=(1,2,5,10,30)
background_tasks={}

def _new_task_status():
 return {'alive':False,'running':False,'last_success':None,'last_error':None,'restart_count':0,'consecutive_failures':0,'restart_delay_seconds':None}

task_status={name:_new_task_status() for name in TASK_NAMES}

def _task_success(name):
 info=task_status[name]
 info['last_success']=datetime.now().isoformat(timespec='seconds')
 info['consecutive_failures']=0
 info['restart_delay_seconds']=None

def _task_error(name,error):
 info=task_status[name]
 info['last_error']={'type':type(error).__name__,'at':datetime.now().isoformat(timespec='seconds')}

def db():
 c=sqlite3.connect(DB,timeout=30)
 c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=30000')
 return c
def init_db():
 c=db()
 c.execute('PRAGMA journal_mode=WAL')
 c.execute('''CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT,side TEXT,status TEXT,entry REAL,stop REAL,initial_stop REAL,
  qty REAL,remaining_qty REAL,risk_usd REAL,score REAL,opened_at TEXT,
  closed_at TEXT,pnl REAL DEFAULT 0,tp1_done INTEGER DEFAULT 0,
  tp2_done INTEGER DEFAULT 0,mode TEXT DEFAULT 'PAPER'
 )''')
 cols=[x[1] for x in c.execute("PRAGMA table_info(positions)").fetchall()]
 if 'close_price' not in cols:
  c.execute('ALTER TABLE positions ADD COLUMN close_price REAL')
 if 'fee_paid' not in cols:
  c.execute('ALTER TABLE positions ADD COLUMN fee_paid REAL DEFAULT 0')
 if 'mae_r' not in cols:
  c.execute('ALTER TABLE positions ADD COLUMN mae_r REAL DEFAULT 0')
 if 'mfe_r' not in cols:
  c.execute('ALTER TABLE positions ADD COLUMN mfe_r REAL DEFAULT 0')
 if 'tracking_started_at' not in cols:
  c.execute('ALTER TABLE positions ADD COLUMN tracking_started_at TEXT')
 if 'close_reason' not in cols:
  c.execute('ALTER TABLE positions ADD COLUMN close_reason TEXT')
 if 'ghost_max_r' not in cols:
  c.execute('ALTER TABLE positions ADD COLUMN ghost_max_r REAL')
 if 'ghost_hit_1r' not in cols:
  c.execute('ALTER TABLE positions ADD COLUMN ghost_hit_1r INTEGER')
 if 'ghost_hit_2r' not in cols:
  c.execute('ALTER TABLE positions ADD COLUMN ghost_hit_2r INTEGER')
 if 'ghost_complete' not in cols:
  c.execute('ALTER TABLE positions ADD COLUMN ghost_complete INTEGER DEFAULT 0')
 if 'stop_analysis_source' not in cols:
  c.execute('ALTER TABLE positions ADD COLUMN stop_analysis_source TEXT')
 if 'stop_analysis_at' not in cols:
  c.execute('ALTER TABLE positions ADD COLUMN stop_analysis_at TEXT')
 # Existing open positions can only be tracked accurately from this upgrade forward.
 now_iso=datetime.now().isoformat(timespec='seconds')
 c.execute("UPDATE positions SET tracking_started_at=? WHERE status='OPEN' AND tracking_started_at IS NULL",(now_iso,))
 c.execute('''CREATE TABLE IF NOT EXISTS logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT,level TEXT,message TEXT
 )''')
 c.execute('''CREATE TABLE IF NOT EXISTS config(
  id INTEGER PRIMARY KEY,data TEXT
 )''')
 r=c.execute('SELECT data FROM config WHERE id=1').fetchone()
 if r:
  try:
   settings.update(json.loads(r['data']))
  except:
   pass
 else:
  c.execute('INSERT INTO config VALUES(1,?)',(json.dumps(settings),))
 settings['symbols']=[s.replace('USDT','_USDT') if '_' not in s else s for s in settings.get('symbols',DEFAULTS['symbols'])]
 c.execute('UPDATE config SET data=? WHERE id=1',(json.dumps(settings),))
 c.commit()
 c.close()
 state['api_saved']=bool(keychain_get('api_key'))

def log(msg,level='INFO'):
 for attempt in range(4):
  c=None
  try:
   c=db()
   c.execute('INSERT INTO logs(ts,level,message) VALUES(?,?,?)',(datetime.now().isoformat(timespec='seconds'),level,msg))
   c.commit()
   c.close()
   return
  except sqlite3.OperationalError:
   if c:
    try: c.close()
    except: pass
   if attempt<3:
    time.sleep(0.15*(attempt+1))
 # Logging must never block or undo a trading/database action.

def keychain_get(account):
 try: return subprocess.check_output(['security','find-generic-password','-s','MEXC Paper Trader','-a',account,'-w'],stderr=subprocess.DEVNULL,text=True).strip()
 except: return ''
def keychain_set(account,value):
 subprocess.run(['security','delete-generic-password','-s','MEXC Paper Trader','-a',account],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 if value: subprocess.run(['security','add-generic-password','-U','-s','MEXC Paper Trader','-a',account,'-w',value],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
def ema(vals,n):
 if not vals:return 0
 k=2/(n+1); e=vals[0]
 for v in vals[1:]:e=v*k+e*(1-k)
 return e
def rsi(vals,n=14):
 if len(vals)<n+1:return 50
 ds=[vals[i]-vals[i-1] for i in range(len(vals)-n,len(vals))]; g=sum(max(x,0) for x in ds)/n; l=sum(max(-x,0) for x in ds)/n
 return 100 if l==0 else 100-(100/(1+g/l))
def atr(rows,n=14):
 tr=[]
 for i in range(1,len(rows)):
  h,l,pc=rows[i][2],rows[i][3],rows[i-1][4]; tr.append(max(h-l,abs(h-pc),abs(l-pc)))
 return sum(tr[-n:])/min(n,len(tr)) if tr else 0
TF={'15m':'Min15','1h':'Min60','4h':'Hour4'}
# Shared MEXC request gate: scanner, live prices and API tests use one throttle.
mexc_request_lock=asyncio.Lock()
mexc_last_request_at=0.0
MEXC_MIN_REQUEST_INTERVAL=0.16
MEXC_RETRY_DELAYS=(1.0,2.0,4.0,8.0)
MEXC_TRANSIENT_RETRY_DELAYS=(0.5,1.0,2.0)
MEXC_RETRY_JITTER_MAX=0.15
SQLITE_LOCK_RETRY_DELAYS=(0.05,0.1,0.2)

def _is_transient_sqlite_lock(error):
 msg=str(error).lower()
 return any(x in msg for x in ('database is locked','database table is locked','database schema is locked'))

def _sqlite_write_with_retry(operation):
 for attempt in range(len(SQLITE_LOCK_RETRY_DELAYS)+1):
  c=None
  try:
   c=db()
   result=operation(c)
   c.commit()
   return result
  except sqlite3.OperationalError as e:
   if c:
    try: c.rollback()
    except: pass
   if not _is_transient_sqlite_lock(e) or attempt>=len(SQLITE_LOCK_RETRY_DELAYS):
    raise
   time.sleep(SQLITE_LOCK_RETRY_DELAYS[attempt])
  finally:
   if c:
    try: c.close()
    except: pass

async def mexc_get(client,url,**kwargs):
 global mexc_last_request_at
 last_response=None
 rate_limit_attempt=0
 transient_attempt=0
 while True:
  try:
   async with mexc_request_lock:
    now=time.monotonic()
    wait=MEXC_MIN_REQUEST_INTERVAL-(now-mexc_last_request_at)
    if wait>0:
     await asyncio.sleep(wait)
    response=await client.get(url,**kwargs)
    mexc_last_request_at=time.monotonic()
  except (httpx.ConnectTimeout,httpx.ConnectError) as e:
   if transient_attempt>=len(MEXC_TRANSIENT_RETRY_DELAYS):
    state['rate_limit_wait']=None
    raise
   delay=MEXC_TRANSIENT_RETRY_DELAYS[transient_attempt]+random.uniform(0,MEXC_RETRY_JITTER_MAX)
   transient_attempt+=1
   state['rate_limit_wait']=delay
   log(f'MEXC geçici bağlantı hatası ({type(e).__name__}) · {delay:.2f} sn sonra tekrar','WARN')
   await asyncio.sleep(delay)
   continue
  last_response=response

  rate_limited=response.status_code==429
  if not rate_limited:
   try:
    payload=response.json()
    if isinstance(payload,dict):
     code=payload.get('code')
     msg=str(payload.get('message') or payload.get('msg') or '')
     rate_limited=str(code)=='510' or 'too frequent' in msg.lower()
   except:
    pass

  if not rate_limited:
   if 500<=response.status_code<=599 and transient_attempt<len(MEXC_TRANSIENT_RETRY_DELAYS):
    delay=MEXC_TRANSIENT_RETRY_DELAYS[transient_attempt]+random.uniform(0,MEXC_RETRY_JITTER_MAX)
    transient_attempt+=1
    state['rate_limit_wait']=delay
    log(f'MEXC HTTP {response.status_code} · {delay:.2f} sn sonra tekrar','WARN')
    await asyncio.sleep(delay)
    continue
   state['rate_limit_wait']=None
   return response

  if rate_limit_attempt>=len(MEXC_RETRY_DELAYS):
   state['rate_limit_wait']=None
   return response

  delay=MEXC_RETRY_DELAYS[rate_limit_attempt]+random.uniform(0,MEXC_RETRY_JITTER_MAX)
  rate_limit_attempt+=1
  state['rate_limit_wait']=delay
  log(f'MEXC rate limit · {delay:.2f} sn bekleyip tekrar deneniyor','WARN')
  await asyncio.sleep(delay)

 state['rate_limit_wait']=None
 return last_response

async def klines(client,symbol,tf,limit=120):
 end=int(time.time()); seconds={'15m':900,'1h':3600,'4h':14400}[tf]; start=end-seconds*(limit+5)
 url=f'https://api.mexc.com/api/v1/contract/kline/{symbol}'
 r=await mexc_get(client,url,params={'interval':TF[tf],'start':start,'end':end},timeout=15)
 body=r.text
 if r.status_code!=200: raise RuntimeError(f'HTTP {r.status_code} | {body[:250]}')
 try: j=r.json()
 except: raise RuntimeError(f'Geçersiz JSON: {body[:250]}')
 if not j.get('success',False): raise RuntimeError(f"MEXC code={j.get('code')} | {j.get('message') or j.get('msg') or body[:200]}")
 d=j.get('data') or {}; keys=('time','open','high','low','close','vol')
 if not all(k in d for k in keys): raise RuntimeError('MEXC Futures mum cevabı beklenen formatta değil')
 rows=[[float(x) for x in vals] for vals in zip(d['time'],d['open'],d['high'],d['low'],d['close'],d['vol'])]
 rows.sort(key=lambda x:x[0]); return rows[-limit:]
def metrics(rows):
 closes=[x[4] for x in rows]; vols=[x[5] for x in rows]; e20,e50=ema(closes[-80:],20),ema(closes[-100:],50); recent=rows[-30:]; p=closes[-1]; hi=max(x[2] for x in rows[-60:]); lo=min(x[3] for x in rows[-60:]); fibs=[hi-(hi-lo)*x for x in (.382,.5,.618)]
 return {'price':p,'ema20':e20,'ema50':e50,'rsi':rsi(closes),'atr':atr(rows),'support':min(x[3] for x in recent),'resistance':max(x[2] for x in recent),'volume_ratio':(sum(vols[-5:])/5)/(sum(vols[-25:-5])/20 or 1),'trend':1 if p>e20>e50 else (-1 if p<e20<e50 else 0),'fib_near':min(abs(p-f) for f in fibs)/(p or 1)<.003}
def scores(m):
 L=S=0; details=[]
 trend_names={'4h':'4 saat trend','1h':'1 saat trend','15m':'15 dk trend'}
 for tf,w in [('4h',20),('1h',15),('15m',10)]:
  lp=w if m[tf]['trend']==1 else 0; sp=w if m[tf]['trend']==-1 else 0
  L+=lp; S+=sp; details.append({'name':trend_names[tf],'long':lp,'short':sp,'note':'Yukarı' if m[tf]['trend']==1 else ('Aşağı' if m[tf]['trend']==-1 else 'Yatay/kararsız')})
 rr=m['15m']['rsi']; lp=15 if 52<=rr<=70 else 0; sp=15 if 30<=rr<=48 else 0; L+=lp; S+=sp; details.append({'name':'RSI','long':lp,'short':sp,'note':f'{rr:.1f}'})
 vr=m['15m']['volume_ratio']; lp=sp=0
 if vr>=1.25:
  if m['15m']['trend']>=0:lp=15; L+=15
  if m['15m']['trend']<=0:sp=15; S+=15
 details.append({'name':'Hacim teyidi','long':lp,'short':sp,'note':f'{vr:.2f}x'})
 p=m['15m']['price']; a=m['15m']['atr'] or p*.005; lp=15 if m['15m']['resistance']-p>=2*a else 0; sp=15 if p-m['15m']['support']>=2*a else 0; L+=lp; S+=sp
 details.append({'name':'Destek / direnç alanı','long':lp,'short':sp,'note':f'D {m["15m"]["support"]:.6g} · R {m["15m"]["resistance"]:.6g}'})
 lp=sp=0
 if m['15m']['fib_near']:
  if m['1h']['trend']==1:lp=10; L+=10
  if m['1h']['trend']==-1:sp=10; S+=10
 details.append({'name':'Fibonacci yakınlığı','long':lp,'short':sp,'note':'Yakın' if m['15m']['fib_near'] else 'Yakın değil'})
 return min(L,100),min(S,100),details
def open_risk():
 c=db(); x=c.execute("SELECT COALESCE(SUM(risk_usd),0) x FROM positions WHERE status='OPEN'").fetchone()['x']; c.close(); return float(x)
def period_stats():
 now=datetime.now(); d=now.replace(hour=0,minute=0,second=0,microsecond=0); starts={'daily':d,'weekly':d-timedelta(days=d.weekday()),'monthly':d.replace(day=1),'yearly':d.replace(month=1,day=1)}; out={}; c=db()
 for key,start in starts.items():
  vals=[float(r['pnl'] or 0) for r in c.execute("SELECT pnl FROM positions WHERE status='CLOSED' AND closed_at>=?",(start.isoformat(timespec='seconds'),)).fetchall()]; wins=sum(v>0 for v in vals); losses=sum(v<0 for v in vals); out[key]={'pnl':sum(vals),'trades':len(vals),'wins':wins,'losses':losses,'win_rate':wins/len(vals)*100 if vals else 0}
 c.close(); return out
def all_time_closed_pnl():
 c=db()
 r=c.execute("SELECT COALESCE(SUM(pnl),0) AS pnl FROM positions WHERE status='CLOSED'").fetchone()
 c.close()
 return float(r['pnl'] or 0)

def all_time_realized_pnl():
 c=db()
 r=c.execute("SELECT COALESCE(SUM(pnl),0) AS pnl FROM positions").fetchone()
 c.close()
 return float(r['pnl'] or 0)

def current_paper_balance():
 # Cash wallet: starting balance + every realized P/L, including partial TP sales
 # on positions that are still open. Unrealized P/L is intentionally excluded.
 return float(settings['paper_balance'])+all_time_realized_pnl()

def used_open_margin():
 leverage=max(float(settings.get('leverage',1) or 1),1.0)
 c=db()
 rows=c.execute("SELECT entry,remaining_qty FROM positions WHERE status='OPEN'").fetchall()
 c.close()
 return sum(float(r['entry'] or 0)*float(r['remaining_qty'] or 0)/leverage for r in rows)

def available_paper_margin():
 balance=current_paper_balance()
 used=used_open_margin()
 reserve=max(balance,0.0)*max(float(settings.get('min_free_balance_pct',20.0)),0.0)/100.0
 return max(0.0,balance-used-reserve)

def has_open(sym):
 c=db(); r=c.execute("SELECT 1 FROM positions WHERE status='OPEN' AND symbol=?",(sym,)).fetchone(); c.close(); return bool(r)
def paper_open(sym,side,m,sc):
 if has_open(sym):
  return
 if period_stats()['daily']['pnl']<=-settings['daily_loss_limit_usd']:
  return
 balance=current_paper_balance()
 if balance<=0:
  log(f'{sym} {side} açılmadı | PAPER bakiye tükendi: ${balance:.2f}','RISK')
  return
 risk=float(settings['risk_per_trade_usd'])
 if open_risk()+risk>settings['max_total_open_risk_usd']:
  return
 p=m['15m']['price']
 dist=max(m['15m']['atr']*settings['stop_atr_mult'],p*.002)
 stop=p-dist if side=='LONG' else p+dist
 qty=risk/dist
 cap=settings['max_btc_notional_usd'] if sym=='BTC_USDT' else settings['max_alt_notional_usd']
 qty=min(qty,cap/p)
 actual=qty*dist
 leverage=max(float(settings.get('leverage',1) or 1),1.0)
 required_margin=(p*qty)/leverage
 available_margin=available_paper_margin()
 if required_margin>available_margin:
  log(f'{sym} {side} açılmadı | margin yetersiz: gereken ${required_margin:.2f}, kullanılabilir ${available_margin:.2f}','RISK')
  return
 fee_rate=max(float(settings.get('paper_fee_rate',0.0008) or 0),0.0)
 entry_fee=p*qty*fee_rate
 opened=datetime.now().isoformat(timespec='seconds')
 def write(c):
  c.execute('''INSERT INTO positions(symbol,side,status,entry,stop,initial_stop,qty,remaining_qty,risk_usd,score,opened_at,pnl,fee_paid,mae_r,mfe_r,tracking_started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(sym,side,'OPEN',p,stop,stop,qty,qty,actual,sc,opened,-entry_fee,entry_fee,0.0,0.0,opened))
 _sqlite_write_with_retry(write)
 log(f'{sym} {side} PAPER açıldı | risk ${actual:.2f} | margin ${required_margin:.2f} | giriş fee ${entry_fee:.4f} | skor {sc}')
def _manage_once(c):
 events=[]
 rows=c.execute("SELECT * FROM positions WHERE status='OPEN'").fetchall()
 for pos in rows:
  md=state.get('market',{}).get(pos['symbol']) or {}
  p=float((state.get('live_prices') or {}).get(pos['symbol']) or md.get('price') or 0)
  if p<=0:
   continue
  sign=1 if pos['side']=='LONG' else -1
  R=abs(pos['entry']-pos['initial_stop'])
  prog=(p-pos['entry'])*sign/R if R else 0
  pnl=float(pos['pnl'] or 0)
  fee_paid=float(pos['fee_paid'] or 0)
  # MAE = worst adverse excursion in R; MFE = best favorable excursion in R.
  # For positions that existed before Faz 1.8.1 this is partial, starting at upgrade time.
  mae_r=max(float(pos['mae_r'] or 0),max(0.0,-prog))
  mfe_r=max(float(pos['mfe_r'] or 0),max(0.0,prog))
  c.execute("UPDATE positions SET mae_r=?,mfe_r=? WHERE id=? AND status='OPEN'",(mae_r,mfe_r,pos['id']))
  fee_rate=max(float(settings.get('paper_fee_rate',0.0008) or 0),0.0)
  rem=pos['remaining_qty']
  stop=pos['stop']
  if (pos['side']=='LONG' and p<=stop) or (pos['side']=='SHORT' and p>=stop):
   pnl+=(p-pos['entry'])*sign*rem
   exit_fee=p*rem*fee_rate; pnl-=exit_fee; fee_paid+=exit_fee
   c.execute(
    "UPDATE positions SET status='CLOSED',remaining_qty=0,closed_at=?,pnl=?,close_price=?,fee_paid=?,close_reason='STOP',mae_r=?,mfe_r=? WHERE id=? AND status='OPEN'",
    (datetime.now().isoformat(timespec='seconds'),pnl,p,fee_paid,mae_r,mfe_r,pos['id'])
   )
   events.append((f"{pos['symbol']} kapandı | PnL ${pnl:.2f}",'TRADE'))
   continue
  if prog>=settings['tp1_r'] and not pos['tp1_done']:
   q=pos['qty']*settings['tp1_pct']/100
   pnl+=(p-pos['entry'])*sign*q
   exit_fee=p*q*fee_rate; pnl-=exit_fee; fee_paid+=exit_fee
   rem-=q
   c.execute(
    'UPDATE positions SET tp1_done=1,remaining_qty=?,pnl=?,stop=?,fee_paid=? WHERE id=? AND status=\'OPEN\'',
    (rem,pnl,pos['entry'],fee_paid,pos['id'])
   )
   events.append((f"{pos['symbol']} TP1",'TRADE'))
  if prog>=settings['tp2_r'] and not pos['tp2_done']:
   q=min(pos['qty']*settings['tp2_pct']/100,rem)
   pnl+=(p-pos['entry'])*sign*q
   exit_fee=p*q*fee_rate; pnl-=exit_fee; fee_paid+=exit_fee
   rem-=q
   c.execute(
    'UPDATE positions SET tp2_done=1,remaining_qty=?,pnl=?,stop=?,fee_paid=? WHERE id=? AND status=\'OPEN\'',
    (rem,pnl,pos['entry']+sign*R,fee_paid,pos['id'])
   )
   events.append((f"{pos['symbol']} TP2",'TRADE'))
 return events

def manage():
 events=_sqlite_write_with_retry(_manage_once)
 for message,level in events:
  log(message,level)

async def spot_crypto_whitelist(client):
 # Crypto-only guard: a Futures underlying must also exist as a MEXC Spot USDT crypto pair.
 # This dynamically excludes MEXC Stock Futures, ETFs/index-linked and similar synthetic products.
 r=await mexc_get(client,'https://api.mexc.com/api/v3/exchangeInfo',timeout=15)
 if r.status_code!=200:
  raise RuntimeError(f'Spot whitelist HTTP {r.status_code} | {r.text[:250]}')
 j=r.json()
 allowed=set()
 for x in j.get('symbols') or []:
  sym=str(x.get('symbol') or '').upper()
  quote=str(x.get('quoteAsset') or '').upper()
  base=str(x.get('baseAsset') or '').upper()
  status=str(x.get('status') or '').upper()
  if quote=='USDT' and base and sym:
   # MEXC status values have changed over time; only reject explicit disabled states.
   if status not in {'DISABLED','OFFLINE','SUSPEND','SUSPENDED'}:
    allowed.add(base)
 return allowed

def is_real_crypto_future(symbol, spot_bases):
 if not symbol.endswith('_USDT'): return False
 base=symbol[:-5].upper()
 # Explicit synthetic naming guard used by some equity/index contracts.
 if 'STOCK' in base or base.endswith(('ETF','INDEX')): return False
 if base in spot_bases: return True
 # Futures sometimes use multiplier prefixes while Spot uses the underlying token name.
 for prefix in ('1000','10000','1000000'):
  if base.startswith(prefix) and base[len(prefix):] in spot_bases:
   return True
 return False

async def top_volume_universe(client,force=False):
 # Cache the expensive Spot whitelist + Futures ranking for the configured refresh window.
 refresh_min=max(1,int(settings.get('universe_refresh_minutes',15)))
 if not force and state.get('universe') and state.get('universe_updated'):
  try:
   age=(datetime.now()-datetime.fromisoformat(state['universe_updated'])).total_seconds()
   if age < refresh_min*60:
    return [x['symbol'] for x in state['universe']]
  except:
   pass

 spot_bases=await spot_crypto_whitelist(client)
 r=await mexc_get(client,'https://api.mexc.com/api/v1/contract/ticker',timeout=15)
 body=r.text
 if r.status_code!=200:
  raise RuntimeError(f'Ticker HTTP {r.status_code} | {body[:250]}')
 j=r.json()
 if not j.get('success',False):
  raise RuntimeError(f"Ticker MEXC code={j.get('code')} | {j.get('message') or body[:200]}")
 items=j.get('data') or []
 ranked=[]
 filtered_non_crypto=0
 for x in items:
  sym=str(x.get('symbol') or '').upper()
  if not is_real_crypto_future(sym,spot_bases):
   filtered_non_crypto+=1
   continue
  val=x.get('amount24') or x.get('amount') or x.get('turnover24') or x.get('volume24') or 0
  try:
   vol=float(val or 0)
  except:
   vol=0
  if vol<=0:
   try:
    vol=float(x.get('volume24') or 0)*float(x.get('lastPrice') or 0)
   except:
    vol=0
  if vol>0:
   ranked.append((vol,sym))
 ranked.sort(reverse=True)
 n=max(5,min(50,int(settings.get('top_volume_count',30))))
 selected=ranked[:n]
 if not any(s=='BTC_USDT' for _,s in selected):
  btc=next(((v,s) for v,s in ranked if s=='BTC_USDT'),None)
  if btc:
   selected=[btc]+selected
 syms=[s for _,s in selected]
 state['universe']=[{'rank':i+1,'symbol':s,'volume24_usd':v} for i,(v,s) in enumerate(selected)]
 state['universe_updated']=datetime.now().isoformat(timespec='seconds')
 state['crypto_filter']={'spot_crypto_bases':len(spot_bases),'filtered_non_crypto':filtered_non_crypto,'mode':'CRYPTO_ONLY'}
 return syms

def position_live_view(pos):
 d=dict(pos)
 md=state.get('market',{}).get(d['symbol']) or {}
 price=float((state.get('live_prices') or {}).get(d['symbol']) or md.get('price') or d['entry'])
 sign=1 if d['side']=='LONG' else -1
 rem=float(d.get('remaining_qty') or 0)
 realized=float(d.get('pnl') or 0)
 unrealized=(price-float(d['entry']))*sign*rem
 risk=float(d.get('risk_usd') or 0)
 initial_r=max(abs(float(d['entry'])-float(d.get('initial_stop') or d['stop'])),1e-12)
 r_now=((price-float(d['entry']))*sign/initial_r)
 tp1_price=float(d['entry'])+sign*initial_r*float(settings['tp1_r'])
 tp2_price=float(d['entry'])+sign*initial_r*float(settings['tp2_r'])
 leverage=max(float(settings.get('leverage',1) or 1),1.0)
 position_notional=price*rem
 initial_notional=float(d['entry'])*float(d.get('qty') or 0)
 d.update({
  'current_price':price,
  'realized_pnl':realized,
  'unrealized_pnl':unrealized,
  'total_live_pnl':realized+unrealized,
  'pnl_pct':((realized+unrealized)/(float(d['entry'])*float(d.get('qty') or 0) or 1))*100,
  'r_now':r_now,
  'tp1_price':tp1_price,
  'tp2_price':tp2_price,
  'position_notional_usd':position_notional,
  'initial_notional_usd':initial_notional,
  'margin_usd':position_notional/leverage,
  'initial_margin_usd':initial_notional/leverage,
  'leverage_used':leverage,
  'stop_pct':(abs(float(d['entry'])-float(d['initial_stop']))/float(d['entry'])*100) if float(d['entry']) else 0,
 })
 return d

def history_rows(period='all'):
 now=datetime.now()
 params=[]
 where="status='CLOSED'"
 if period=='today':
  start=now.replace(hour=0,minute=0,second=0,microsecond=0)
 elif period=='week':
  d=now.replace(hour=0,minute=0,second=0,microsecond=0)
  start=d-timedelta(days=d.weekday())
 elif period=='month':
  start=now.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
 else:
  start=None
 if start:
  where += ' AND closed_at>=?'
  params.append(start.isoformat(timespec='seconds'))
 c=db()
 rows=[dict(x) for x in c.execute(
  f"SELECT * FROM positions WHERE {where} ORDER BY id DESC LIMIT 500",params
 ).fetchall()]
 c.close()
 for d in rows:
  if d.get('close_price') is None:
   d['close_price']=d.get('entry')
 return rows

async def historical_15m(client,symbol,start_dt,end_dt):
 start=int(start_dt.timestamp())
 end=int(end_dt.timestamp())
 if end<=start:
  return []
 url=f'https://api.mexc.com/api/v1/contract/kline/{symbol}'
 r=await mexc_get(client,url,params={'interval':'Min15','start':start,'end':end},timeout=15)
 body=r.text
 if r.status_code!=200:
  raise RuntimeError(f'History HTTP {r.status_code} | {body[:180]}')
 j=r.json()
 if not j.get('success',False):
  raise RuntimeError(f"History MEXC code={j.get('code')} | {j.get('message') or j.get('msg') or body[:160]}")
 d=j.get('data') or {}
 keys=('time','open','high','low','close','vol')
 if not all(k in d for k in keys):
  return []
 rows=[[float(x) for x in vals] for vals in zip(d['time'],d['open'],d['high'],d['low'],d['close'],d['vol'])]
 rows.sort(key=lambda x:x[0])
 return rows

def _parse_iso(v):
 try:
  return datetime.fromisoformat(v) if v else None
 except:
  return None

def _is_probable_old_stop(pos):
 if pos.get('close_reason')=='STOP':
  return True
 if pos.get('close_reason')=='MANUAL':
  return False
 cp=pos.get('close_price'); st=pos.get('stop'); side=pos.get('side')
 if cp is None or st is None:
  return False
 cp=float(cp); st=float(st)
 tol=max(abs(st)*0.003,1e-12)
 return abs(cp-st)<=tol or (side=='LONG' and cp<=st+tol) or (side=='SHORT' and cp>=st-tol)

async def analyze_closed_stop_position(client,pos,force=False):
 if not _is_probable_old_stop(pos):
  return False
 opened=_parse_iso(pos.get('opened_at')); closed=_parse_iso(pos.get('closed_at'))
 if not opened or not closed:
  return False
 entry=float(pos.get('entry') or 0)
 initial_stop=float(pos.get('initial_stop') or pos.get('stop') or 0)
 R=abs(entry-initial_stop)
 if entry<=0 or R<=0:
  return False

 now=datetime.now()
 ghost_end=min(closed+timedelta(hours=2),now)
 # Keep request reasonably bounded for old/very long positions.
 open_start=max(opened,closed-timedelta(days=2))
 rows_open=await historical_15m(client,pos['symbol'],open_start,closed+timedelta(minutes=15))
 rows_ghost=await historical_15m(client,pos['symbol'],closed,ghost_end+timedelta(minutes=15)) if ghost_end>closed else []

 sign=1 if pos['side']=='LONG' else -1
 mae=0.0; mfe=0.0
 for r in rows_open:
  high,low=r[2],r[3]
  fav=((high-entry)/R) if sign==1 else ((entry-low)/R)
  adv=((entry-low)/R) if sign==1 else ((high-entry)/R)
  mfe=max(mfe,fav,0.0); mae=max(mae,adv,0.0)

 ghost=0.0
 for r in rows_ghost:
  high,low=r[2],r[3]
  fav=((high-entry)/R) if sign==1 else ((entry-low)/R)
  ghost=max(ghost,fav,0.0)

 complete=1 if now>=closed+timedelta(hours=2) else 0
 source='LIVE+15M' if pos.get('stop_analysis_source')=='LIVE' else 'HISTORICAL_15M'
 c=db()
 c.execute("""UPDATE positions SET mae_r=?,mfe_r=?,ghost_max_r=?,ghost_hit_1r=?,ghost_hit_2r=?,
             ghost_complete=?,stop_analysis_source=?,stop_analysis_at=?,
             close_reason=COALESCE(close_reason,'STOP_INFERRED')
             WHERE id=?""",
          (mae,mfe,ghost,1 if ghost>=1 else 0,1 if ghost>=2 else 0,complete,source,
           datetime.now().isoformat(timespec='seconds'),pos['id']))
 c.commit(); c.close()
 return True

async def run_stop_backfill(limit=100,only_pending=False):
 c=db()
 q="SELECT * FROM positions WHERE status='CLOSED' ORDER BY id DESC LIMIT ?"
 rows=[dict(x) for x in c.execute(q,(limit,)).fetchall()]
 c.close()
 done=0; errors=0
 async with httpx.AsyncClient(headers={'User-Agent':'MEXC-Futures-Paper-Trader/2.0'}) as client:
  for pos in rows:
   if only_pending and pos.get('ghost_complete') and pos.get('stop_analysis_at'):
    continue
   if not _is_probable_old_stop(pos):
    continue
   try:
    if await analyze_closed_stop_position(client,pos):
     done+=1
   except Exception as e:
    errors+=1
    log(f"Stop analiz {pos.get('symbol')} #{pos.get('id')}: {e}",'WARN')
 return {'analyzed':done,'errors':errors}

def stop_analysis_rows():
 c=db()
 rows=[dict(x) for x in c.execute("""SELECT * FROM positions
   WHERE status='CLOSED' AND (close_reason IN ('STOP','STOP_INFERRED') OR ghost_max_r IS NOT NULL)
   ORDER BY id DESC LIMIT 200""").fetchall()]
 c.close()
 for x in rows:
  ghost=float(x.get('ghost_max_r') or 0)
  x['early_stop']=bool(ghost>=1.0)
  x['very_early_stop']=bool(ghost>=2.0)
  x['analysis_status']='TAMAM' if x.get('ghost_complete') else '2 SAAT DOLMADI'
 return rows

scan_lock=asyncio.Lock()
kline_sem=asyncio.Semaphore(4)

async def limited_klines(client,symbol,tf,limit=120):
 async with kline_sem:
  return await klines(client,symbol,tf,limit)

async def analyze_symbol(client,sym):
 try:
  rows15,rows1h,rows4h=await asyncio.gather(
   limited_klines(client,sym,'15m'),
   limited_klines(client,sym,'1h'),
   limited_klines(client,sym,'4h')
  )
  m={'15m':metrics(rows15),'1h':metrics(rows1h),'4h':metrics(rows4h)}
  L,S,details=scores(m)
  m['long_score']=L
  m['short_score']=S
  m['score_details']=details
  effective_threshold=int(state['paper_test_threshold'] or settings['signal_threshold'])
  m['effective_threshold']=effective_threshold
  m['signal']='LONG' if L>=effective_threshold and L>S else ('SHORT' if S>=effective_threshold and S>L else 'BEKLE')
  m['near_opportunity']=70<=max(L,S)<=79
  return sym,m,None
 except Exception as e:
  return sym,None,e

async def scan_once(force_universe=False):
 if scan_lock.locked():
  return
 async with scan_lock:
  started=time.monotonic()
  state['scanning']=True
  state['error']=None
  try:
   async with httpx.AsyncClient(
    headers={'User-Agent':'MEXC-Futures-Paper-Trader/2.0'},
    limits=httpx.Limits(max_connections=10,max_keepalive_connections=10)
   ) as client:
    try:
     syms=await top_volume_universe(client,force=force_universe)
    except Exception as e:
     state['error']=f'Top-30 havuzu: {e}'
     log(state['error'],'ERROR')
     syms=settings.get('symbols',['BTC_USDT'])

    results=await asyncio.gather(*(analyze_symbol(client,sym) for sym in syms))
    fresh_market={}
    for sym,m,err in results:
     if err:
      state['error']=f'{sym}: {err}'
      log(state['error'],'ERROR')
      continue
     fresh_market[sym]={'price':m['15m']['price'],**m}
     state['live_prices'][sym]=m['15m']['price']

    # Publish the full scanner snapshot at once so the UI doesn't watch 30 partial rows.
    state['market']=fresh_market
    state['public_api']='BAĞLI' if fresh_market else 'HATA'

    # Open eligible PAPER trades only after all analysis tasks finish.
    for sym in syms:
     m=fresh_market.get(sym)
     if m and m['signal']!='BEKLE':
      paper_open(sym,m['signal'],m,max(m['long_score'],m['short_score']))

   manage()
   state['last_scan']=datetime.now().isoformat(timespec='seconds')
  finally:
   state['scan_duration_sec']=round(time.monotonic()-started,2)
   state['scanning']=False

async def fetch_futures_prices(client):
 r=await mexc_get(client,'https://api.mexc.com/api/v1/contract/ticker',timeout=10)
 body=r.text
 if r.status_code!=200:
  raise RuntimeError(f'Ticker HTTP {r.status_code} | {body[:200]}')
 j=r.json()
 if not j.get('success',False):
  raise RuntimeError(f"Ticker MEXC code={j.get('code')} | {j.get('message') or body[:160]}")
 prices={}
 for x in (j.get('data') or []):
  sym=str(x.get('symbol') or '').upper()
  raw=x.get('lastPrice') or x.get('fairPrice') or x.get('indexPrice')
  try:
   p=float(raw)
  except:
   continue
  if sym and p>0:
   prices[sym]=p
 return prices

async def position_engine():
 # Lightweight loop: one ticker request, then TP1/TP2/Stop checks.
 while True:
  if state['running'] and not state['panic']:
   try:
    c=db()
    open_syms=[x['symbol'] for x in c.execute("SELECT DISTINCT symbol FROM positions WHERE status='OPEN'").fetchall()]
    c.close()
    if open_syms:
     async with httpx.AsyncClient(
      headers={'User-Agent':'MEXC-Futures-Paper-Trader/2.0'},
      limits=httpx.Limits(max_connections=4,max_keepalive_connections=4)
     ) as client:
      all_prices=await fetch_futures_prices(client)
     for sym in open_syms:
      if sym in all_prices:
       state['live_prices'][sym]=all_prices[sym]
       if sym in state.get('market',{}):
        state['market'][sym]['price']=all_prices[sym]
     state['last_price_update']=datetime.now().isoformat(timespec='seconds')
     manage()
   except Exception as e:
    # Fast price-loop errors should not stop the heavy scanner.
    state['error']=f'Hızlı pozisyon takibi: {e}'
    _task_error('position_engine',e)
   else:
    _task_success('position_engine')
  await asyncio.sleep(3)

async def engine():
 while True:
  if state['running'] and not state['panic']:
   await scan_once()
  _task_success('scanner')
  await asyncio.sleep(max(5,int(settings['scan_seconds'])))

async def ghost_analysis_engine():
 # Re-check recent stop-outs; a stopped trade needs up to 2 hours before ghost result is final.
 while True:
  try:
   await run_stop_backfill(limit=40,only_pending=True)
   _task_success('ghost_analyzer')
  except Exception as e:
   _task_error('ghost_analyzer',e)
   log(f'Ghost analiz motoru: {e}','WARN')
  await asyncio.sleep(300)

async def supervise_task(name,runner):
 info=task_status[name]
 while True:
  info['alive']=True
  info['running']=True
  info['restart_delay_seconds']=None
  try:
   await runner()
   raise RuntimeError('Background task unexpectedly returned')
  except asyncio.CancelledError:
   info['alive']=False
   info['running']=False
   info['restart_delay_seconds']=None
   raise
  except Exception as e:
   info['running']=False
   info['restart_count']+=1
   info['consecutive_failures']+=1
   _task_error(name,e)
   delay=SUPERVISOR_BACKOFF[min(info['consecutive_failures']-1,len(SUPERVISOR_BACKOFF)-1)]
   info['restart_delay_seconds']=delay
   try: log(f'{name} task durdu ({type(e).__name__}) · {delay} sn sonra yeniden başlatılacak','ERROR')
   except Exception: pass
   await asyncio.sleep(delay)

def start_background_tasks():
 runners={'scanner':engine,'position_engine':position_engine,'ghost_analyzer':ghost_analysis_engine}
 for name,runner in runners.items():
  current=background_tasks.get(name)
  if current and not current.done():
   continue
  task_status[name]=_new_task_status()
  background_tasks[name]=asyncio.create_task(supervise_task(name,runner),name=f'{name}-supervisor')

@app.on_event('startup')
async def startup():
 init_db()
 start_background_tasks()

@app.get('/',response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request=request, name='index.html')
@app.get('/api/health')
def health():
 database_ok=False
 c=None
 try:
  c=db()
  c.execute('SELECT 1').fetchone()
  database_ok=True
 except Exception:
  database_ok=False
 finally:
  if c:
   try: c.close()
   except: pass
 tasks={}
 for name in TASK_NAMES:
  info=dict(task_status[name])
  task=background_tasks.get(name)
  info['alive']=bool(task and not task.done() and info['alive'])
  tasks[name]=info
 timestamps=[x for x in (state.get('last_price_update'),state.get('last_scan')) if x]
 all_alive=all(x['alive'] for x in tasks.values())
 return {
  'service_status':'healthy' if database_ok and all_alive else 'degraded',
  'mode':'PAPER',
  'scanner':tasks['scanner'],
  'position_engine':tasks['position_engine'],
  'ghost_analyzer':tasks['ghost_analyzer'],
  'last_successful_market_data_timestamp':max(timestamps) if timestamps else None,
  'database_connectivity':database_ok,
 }
@app.get('/api/status')
def status():
 c=db(); raw_pos=c.execute("SELECT * FROM positions WHERE status='OPEN' ORDER BY id DESC").fetchall(); logs=[dict(x) for x in c.execute('SELECT * FROM logs ORDER BY id DESC LIMIT 80').fetchall()]; c.close()
 pos=[position_live_view(x) for x in raw_pos]
 unrealized=sum(float(x['unrealized_pnl']) for x in pos)
 realized_open=sum(float(x['realized_pnl']) for x in pos)
 stats=period_stats()
 total_closed=all_time_closed_pnl()
 total_realized=all_time_realized_pnl()
 current_balance=float(settings['paper_balance'])+total_realized
 # current_balance already includes realized partial TP/fees from OPEN positions.
 # Equity therefore adds only unrealized P/L; do not double-count realized_open.
 equity=current_balance+unrealized
 used_margin=used_open_margin()
 reserve=max(current_balance,0.0)*max(float(settings.get('min_free_balance_pct',20.0)),0.0)/100.0
 available_margin=max(0.0,current_balance-used_margin-reserve)
 return {'state':state,'settings':settings,'positions':pos,'logs':logs,'open_risk':open_risk(),'available_open_risk':max(0.0,float(settings['max_total_open_risk_usd'])-open_risk()),'period_stats':stats,'effective_threshold':int(state['paper_test_threshold'] or settings['signal_threshold']),'portfolio':{'starting_balance':float(settings['paper_balance']),'current_balance':current_balance,'all_time_closed_pnl':total_closed,'all_time_realized_pnl':total_realized,'unrealized_pnl':unrealized,'open_realized_pnl':realized_open,'today_closed_pnl':stats['daily']['pnl'],'equity':equity,'used_margin':used_margin,'reserve_balance':reserve,'available_margin':available_margin}}
@app.get('/api/history')
def history(period: str='all'):
 if period not in {'today','week','month','all'}: raise HTTPException(400,'Geçersiz geçmiş filtresi')
 rows=history_rows(period)
 pnl=sum(float(x.get('pnl') or 0) for x in rows); wins=sum(float(x.get('pnl') or 0)>0 for x in rows); losses=sum(float(x.get('pnl') or 0)<0 for x in rows)
 return {'rows':rows,'summary':{'pnl':pnl,'trades':len(rows),'wins':wins,'losses':losses,'win_rate':wins/len(rows)*100 if rows else 0}}

@app.get('/api/stop-analysis')
def stop_analysis():
 rows=stop_analysis_rows()
 completed=[x for x in rows if x.get('ghost_complete')]
 early=[x for x in completed if x.get('early_stop')]
 very=[x for x in completed if x.get('very_early_stop')]
 return {'rows':rows,'summary':{
  'tracked':len(rows),
  'completed':len(completed),
  'early_stops':len(early),
  'very_early_stops':len(very),
  'early_stop_pct':(len(early)/len(completed)*100 if completed else 0),
  'very_early_stop_pct':(len(very)/len(completed)*100 if completed else 0)
 }}

@app.post('/api/stop-analysis/backfill')
async def stop_analysis_backfill():
 result=await run_stop_backfill(limit=100,only_pending=False)
 log(f"Stop Analyzer geriye dönük tarama tamamlandı · {result['analyzed']} işlem · {result['errors']} hata")
 return {'ok':True,**result}

@app.post('/api/positions/{position_id}/close')
async def close_position(position_id:int):
 # Read first and close DB before any network wait.
 c=db()
 row=c.execute("SELECT * FROM positions WHERE id=?",(position_id,)).fetchone()
 c.close()
 if not row:
  raise HTTPException(404,'Pozisyon bulunamadı.')
 pos=dict(row)
 if pos['status']!='OPEN':
  raise HTTPException(409,'Bu pozisyon zaten kapalı.')

 symbol=pos['symbol']
 try:
  # Prefer the latest scanner price; otherwise get a fresh Futures candle.
  md=(state.get('market') or {}).get(symbol) or {}
  price=float(md.get('price') or 0)
  if price<=0:
   async with httpx.AsyncClient() as client:
    rows=await klines(client,symbol,'15m',10)
   price=float(rows[-1][4])

  sign=1 if pos['side']=='LONG' else -1
  rem=float(pos.get('remaining_qty') or 0)
  realized=float(pos.get('pnl') or 0)
  fee_paid=float(pos.get('fee_paid') or 0)
  fee_rate=max(float(settings.get('paper_fee_rate',0.0008) or 0),0.0)
  realized+=(price-float(pos['entry']))*sign*rem
  exit_fee=price*rem*fee_rate; realized-=exit_fee; fee_paid+=exit_fee
  now=datetime.now().isoformat(timespec='seconds')

  # Short write transaction; retry briefly if scanner is writing at the same instant.
  updated=False
  for attempt in range(6):
   c=None
   try:
    c=db()
    cur=c.execute(
     "UPDATE positions SET status='CLOSED',remaining_qty=0,closed_at=?,pnl=?,close_price=?,fee_paid=?,close_reason='MANUAL' WHERE id=? AND status='OPEN'",
     (now,realized,price,fee_paid,position_id)
    )
    if cur.rowcount!=1:
     c.rollback(); c.close()
     raise HTTPException(409,'Pozisyon bu sırada sistem tarafından zaten kapatılmış.')
    c.commit(); c.close()
    updated=True
    break
   except sqlite3.OperationalError as e:
    if c:
     try: c.close()
     except: pass
    if 'locked' not in str(e).lower() or attempt==5:
     raise
    await asyncio.sleep(0.20*(attempt+1))
  if not updated:
   raise HTTPException(503,'Veritabanı kilidi çözülemedi. Lütfen tekrar deneyin.')

  log(f"{symbol} manuel kapatıldı @ {price} | PnL ${realized:.2f}",'TRADE')
  return {'ok':True,'id':position_id,'symbol':symbol,'close_price':price,'pnl':realized}
 except HTTPException:
  raise
 except sqlite3.OperationalError as e:
  raise HTTPException(503,f'Veritabanı şu anda meşgul: {e}. Birkaç saniye sonra tekrar deneyin.')
 except Exception as e:
  log(f'Manuel kapatma hatası {symbol}: {type(e).__name__}: {e}','ERROR')
  raise HTTPException(500,f'Manuel kapatma hatası: {type(e).__name__}: {e}')

@app.post('/api/start')
async def start():
 state.update(running=True,panic=False,error=None)
 log('Motor başlatıldı · ilk tarama arka planda başladı')
 if not scan_lock.locked():
  asyncio.create_task(scan_once())
 return {'ok':True,'message':'Motor çalışıyor; ilk tarama arka planda başladı.'}
@app.post('/api/stop')
def stop():state['running']=False;log('Motor durduruldu');return {'ok':True}
@app.post('/api/panic')
def panic():state.update(running=False,panic=True);log('ACİL DURDURMA etkin','WARN');return {'ok':True}
@app.post('/api/scan')
async def scan():
 await scan_once(force_universe=True)
 return {'ok':True,'error':state['error'],'last_scan':state['last_scan'],'duration_sec':state['scan_duration_sec']}
@app.get('/api/public-test')
async def public_test():
 try:
  async with httpx.AsyncClient() as client: rows=await klines(client,'BTC_USDT','15m',10)
  state['public_api']='BAĞLI'; return {'ok':True,'message':f'MEXC Futures public API bağlı · BTC son fiyat ${rows[-1][4]:,.2f}'}
 except Exception as e: state['public_api']='HATA'; state['error']=str(e); log(f'Bağlantı testi: {e}','ERROR'); raise HTTPException(502,str(e))
class ApiCfg(BaseModel): api_key:str=''; api_secret:str=''
@app.post('/api/api-settings')
def api_settings(a:ApiCfg):
 try:
  if a.api_key.strip(): keychain_set('api_key',a.api_key.strip())
  if a.api_secret.strip(): keychain_set('api_secret',a.api_secret.strip())
  state['api_saved']=bool(keychain_get('api_key')); log('MEXC API bilgileri macOS Keychain içine kaydedildi'); return {'ok':True,'saved':state['api_saved']}
 except Exception as e: raise HTTPException(500,f'Keychain kayıt hatası: {e}')
@app.post('/api/private-test')
async def private_test():
 key=keychain_get('api_key'); secret=keychain_get('api_secret')
 if not key or not secret: raise HTTPException(400,'Önce API Key ve Secret kaydedin.')
 ts=str(int(time.time()*1000)); path='/api/v1/private/account/assets'; sig=hmac.new(secret.encode(),(key+ts).encode(),hashlib.sha256).hexdigest(); headers={'ApiKey':key,'Request-Time':ts,'Signature':sig,'Content-Type':'application/json'}
 try:
  async with httpx.AsyncClient() as client: r=await mexc_get(client,'https://api.mexc.com'+path,headers=headers,timeout=15)
  j=r.json() if 'json' in r.headers.get('content-type','') else {'message':r.text[:300]}
  if r.status_code==200 and j.get('success') is True: state['private_api']='BAĞLI'; log('MEXC özel API bağlantısı doğrulandı'); return {'ok':True,'message':'API Key/Secret doğrulandı. Hesap bağlantısı başarılı.'}
  state['private_api']='HATA'; raise HTTPException(400,f"MEXC yanıtı: HTTP {r.status_code} · {j.get('message') or j.get('msg') or j.get('code') or str(j)[:250]}")
 except HTTPException: raise
 except Exception as e: state['private_api']='HATA'; raise HTTPException(502,str(e))
class Settings(BaseModel):
 paper_balance:float;risk_per_trade_usd:float;top_volume_count:int;universe_refresh_minutes:int;max_alt_notional_usd:float;max_btc_notional_usd:float;max_total_open_risk_usd:float;daily_loss_limit_usd:float;leverage:int;signal_threshold:int;scan_seconds:int;stop_atr_mult:float;tp1_r:float;tp1_pct:float;tp2_r:float;tp2_pct:float;runner_pct:float;move_be_at_r:float

class TestThreshold(BaseModel):
 threshold: Optional[int] = None
@app.post('/api/test-threshold')
def test_threshold(t:TestThreshold):
 if t.threshold is not None and t.threshold not in (60,70): raise HTTPException(400,'Paper test eşiği yalnızca 60 veya 70 olabilir.')
 state['paper_test_threshold']=t.threshold
 effective=int(t.threshold or settings['signal_threshold'])
 log(f'PAPER test eşiği {effective} olarak ayarlandı' if t.threshold else f'PAPER test eşiği kapatıldı · normal eşik {effective}')
 return {'ok':True,'effective_threshold':effective,'temporary':t.threshold is not None}

@app.post('/api/settings')
def save(s:Settings):
 settings.update(s.model_dump()); c=db(); c.execute('UPDATE config SET data=? WHERE id=1',(json.dumps(settings),)); c.commit(); c.close(); log('Parametreler güncellendi'); return {'ok':True}
if __name__=='__main__':
 import uvicorn; uvicorn.run(app,host='127.0.0.1',port=8071)
