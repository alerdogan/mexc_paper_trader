from typing import Optional
import asyncio, json, sqlite3, subprocess, hmac, hashlib, os, random, sys, time, uuid
from decimal import Decimal, ROUND_CEILING
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

BASE=Path(__file__).parent; DB=BASE/'trader.db'; app=FastAPI(title='MEXC Futures Paper Trader'); templates=Jinja2Templates(directory=str(BASE/'templates'))
SERVICE_STARTED_MONOTONIC=time.monotonic()
DEFAULTS={"symbols":["BTC_USDT"],"top_volume_count":30,"universe_refresh_minutes":15,"paper_balance":4000.0,"risk_per_trade_usd":20.0,"max_alt_notional_usd":5000.0,"max_btc_notional_usd":10000.0,"max_total_open_risk_usd":100.0,"daily_loss_limit_usd":300.0,"leverage":2,"signal_threshold":80,"scan_seconds":30,"stop_atr_mult":1.5,"tp1_r":1.0,"tp1_pct":30.0,"tp2_r":2.0,"tp2_pct":30.0,"runner_pct":40.0,"move_be_at_r":1.0,"min_free_balance_pct":20.0,"paper_fee_rate":0.0008}
settings=DEFAULTS.copy(); state={"running":False,"panic":False,"entry_paused":False,"last_scan":None,"market":{},"live_prices":{},"last_price_update":None,"scanning":False,"error":None,"feed":"MEXC FUTURES","public_api":None,"api_saved":False,"private_api":None,"paper_test_threshold":None,"universe":[],"universe_updated":None,"scan_duration_sec":None,"rate_limit_wait":None,"live_fee_position_check":None,"live_fee_audit":None}
TASK_NAMES=('scanner','position_engine','ghost_analyzer')
STRATEGY_LAB_MODELS=('CURRENT','NO_STOP_MINI','SMART_EXIT','TRAILING_RUNNER')
STRATEGY_LAB_VERSION='1.0'
SCORE_SNAPSHOT_VERSION='1.0'
MARKET_REGIME_SNAPSHOT_VERSION='1.0'
ENTRY_QUALITY_FILTER_VERSION='ENTRY_QUALITY_FILTER_V1'
REJECT_SHADOW_VERSION='REJECT_SHADOW_V1'
POST_ENTRY_FILTER_COHORT='POST_ENTRY_FILTER_V1'
ENTRY_QUALITY_FILTER_V2_RESEARCH_VERSION='ENTRY_QUALITY_FILTER_V2_RESEARCH'
ENTRY_QUALITY_FILTER_V3_RESEARCH_VERSION='ENTRY_QUALITY_FILTER_V3_RESEARCH'
# Verified from systemd journal: filter-bearing process startup completed at this UTC instant.
ENTRY_QUALITY_FILTER_V1_DEPLOYED_AT='2026-09-04T07:45:28'
SUPERVISOR_BACKOFF=(1,2,5,10,30)
background_tasks={}
live_fee_test_lock=asyncio.Lock()
LIVE_FEE_TEST_EXPIRY_SECONDS=600
LIVE_FEE_TEST_ESTIMATED_TAKER_RATE=0.0008

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
 c.execute('''CREATE TABLE IF NOT EXISTS strategy_lab_experiments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,source_position_id INTEGER NOT NULL UNIQUE,
  symbol TEXT NOT NULL,side TEXT NOT NULL,entry REAL NOT NULL,initial_stop REAL NOT NULL,
  qty REAL NOT NULL,score REAL,opened_at TEXT NOT NULL,current_stopped_at TEXT,created_at TEXT NOT NULL
 )''')
 c.execute('''CREATE TABLE IF NOT EXISTS strategy_lab_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,experiment_id INTEGER NOT NULL,model TEXT NOT NULL,
  model_version TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'OPEN',entry REAL NOT NULL,
  stop REAL NOT NULL,qty REAL NOT NULL,remaining_qty REAL NOT NULL,realized_pnl REAL NOT NULL DEFAULT 0,
  fee_paid REAL NOT NULL DEFAULT 0,tp1_done INTEGER NOT NULL DEFAULT 0,tp2_done INTEGER NOT NULL DEFAULT 0,
  mae_r REAL NOT NULL DEFAULT 0,mfe_r REAL NOT NULL DEFAULT 0,stop_triggered_at TEXT,
  first_post_stop_profit_at TEXT,closed_at TEXT,close_price REAL,close_reason TEXT,updated_at TEXT NOT NULL,
  UNIQUE(experiment_id,model),FOREIGN KEY(experiment_id) REFERENCES strategy_lab_experiments(id)
 )''')
 c.execute('CREATE INDEX IF NOT EXISTS idx_strategy_lab_runs_status ON strategy_lab_runs(status)')
 c.execute('''CREATE TABLE IF NOT EXISTS position_score_snapshots(
  position_id INTEGER PRIMARY KEY,score_version TEXT NOT NULL,signal_side TEXT NOT NULL,captured_at TEXT NOT NULL,
  trend_score REAL,trend_4h_score REAL,trend_1h_score REAL,trend_15m_score REAL,
  support_resistance_score REAL,volume_score REAL,rsi_score REAL,fibonacci_score REAL,total_score REAL NOT NULL,
  rsi_value REAL,volume_ratio REAL,support_price REAL,resistance_price REAL,
  support_distance REAL,resistance_distance REAL,support_distance_pct REAL,resistance_distance_pct REAL,
  fibonacci_near INTEGER,fibonacci_level REAL,fibonacci_distance REAL,fibonacci_distance_pct REAL,
  trend_15m INTEGER,trend_1h INTEGER,trend_4h INTEGER,score_details_json TEXT,
  FOREIGN KEY(position_id) REFERENCES positions(id)
 )''')
 c.execute('''CREATE TABLE IF NOT EXISTS market_regime_snapshots(
  source_position_id INTEGER PRIMARY KEY,snapshot_version TEXT NOT NULL,captured_at TEXT NOT NULL,
  regime_classification TEXT NOT NULL,position_side TEXT NOT NULL,market_alignment TEXT NOT NULL,
  universe_size INTEGER NOT NULL,bullish_count INTEGER NOT NULL,bearish_count INTEGER NOT NULL,
  neutral_count INTEGER NOT NULL,bullish_pct REAL NOT NULL,bearish_pct REAL NOT NULL,
  trend_changed_15m_count INTEGER,trend_changed_15m_denominator INTEGER,trend_changed_15m_pct REAL,
  btc_trend_15m INTEGER,btc_trend_1h INTEGER,btc_trend_4h INTEGER,
  eth_trend_15m INTEGER,eth_trend_1h INTEGER,eth_trend_4h INTEGER,
  btc_15m_price REAL,btc_15m_ema20 REAL,btc_15m_ema50 REAL,btc_15m_momentum TEXT,
  btc_15m_rsi REAL,btc_15m_volume_ratio REAL,
  eth_15m_price REAL,eth_15m_ema20 REAL,eth_15m_ema50 REAL,eth_15m_momentum TEXT,
  eth_15m_rsi REAL,eth_15m_volume_ratio REAL,
  FOREIGN KEY(source_position_id) REFERENCES positions(id)
 )''')
 c.execute('''CREATE TABLE IF NOT EXISTS live_fee_tests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,status TEXT NOT NULL,armed_at TEXT NOT NULL,
  candidate_at TEXT,expires_at TEXT,symbol TEXT,side TEXT,score REAL,reference_price REAL,
  contract_size REAL,contracts REAL,leverage INTEGER,estimated_taker_rate REAL,
  estimated_entry_fee REAL,estimated_exit_fee REAL,max_estimated_loss REAL,
  execution_started_at TEXT,entry_order_id TEXT,entry_fill_json TEXT,
  exit_order_id TEXT,exit_fill_json TEXT,funding REAL DEFAULT 0,result_json TEXT,
  error TEXT,completed_at TEXT
 )''')
 c.execute('''CREATE TABLE IF NOT EXISTS entry_filter_rejections(
  id INTEGER PRIMARY KEY AUTOINCREMENT,filter_version TEXT NOT NULL,status TEXT NOT NULL,
  symbol TEXT NOT NULL,side TEXT NOT NULL,score REAL NOT NULL,rejected_at TEXT NOT NULL,
  reasons_json TEXT NOT NULL,reference_price REAL,coin_rsi REAL,btc_15m_volume_ratio REAL,
  eth_15m_volume_ratio REAL,coin_volume_score REAL,coin_trend_15m INTEGER,
  coin_trend_1h INTEGER,coin_trend_4h INTEGER,score_snapshot_json TEXT NOT NULL,
  market_regime_snapshot_json TEXT NOT NULL
 )''')
 c.execute('CREATE INDEX IF NOT EXISTS idx_entry_filter_rejections_version_time ON entry_filter_rejections(filter_version,rejected_at)')
 c.execute('CREATE INDEX IF NOT EXISTS idx_entry_filter_rejections_symbol ON entry_filter_rejections(symbol,rejected_at)')
 c.execute('''CREATE TABLE IF NOT EXISTS entry_filter_active_setups(
  symbol TEXT NOT NULL,side TEXT NOT NULL,rejection_id INTEGER NOT NULL,
  reasons_json TEXT NOT NULL,first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,
  PRIMARY KEY(symbol,side)
 )''')
 c.execute('''CREATE TABLE IF NOT EXISTS reject_shadow_trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT,rejection_id INTEGER NOT NULL UNIQUE,
  shadow_version TEXT NOT NULL,status TEXT NOT NULL,symbol TEXT NOT NULL,side TEXT NOT NULL,
  entry_at TEXT NOT NULL,close_at TEXT,entry_price REAL NOT NULL,exit_price REAL,
  initial_stop REAL NOT NULL,current_stop REAL NOT NULL,qty REAL NOT NULL,remaining_qty REAL NOT NULL,
  initial_risk_usd REAL NOT NULL,signal_score REAL NOT NULL,reasons_json TEXT NOT NULL,
  coin_rsi REAL,btc_15m_volume_ratio REAL,eth_15m_volume_ratio REAL,coin_volume_score REAL,
  score_snapshot_json TEXT NOT NULL,market_regime_snapshot_json TEXT NOT NULL,
  mae REAL NOT NULL DEFAULT 0,mae_r REAL NOT NULL DEFAULT 0,mfe REAL NOT NULL DEFAULT 0,mfe_r REAL NOT NULL DEFAULT 0,
  tp1_hit INTEGER NOT NULL DEFAULT 0,tp2_hit INTEGER NOT NULL DEFAULT 0,
  gross_simulated_pnl REAL NOT NULL DEFAULT 0,simulated_fee REAL NOT NULL DEFAULT 0,
  net_simulated_pnl REAL NOT NULL DEFAULT 0,final_exit_reason TEXT,
  filter_result TEXT NOT NULL DEFAULT 'OPEN',avoided_loss REAL NOT NULL DEFAULT 0,
  missed_profit REAL NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
  FOREIGN KEY(rejection_id) REFERENCES entry_filter_rejections(id)
 )''')
 c.execute('CREATE INDEX IF NOT EXISTS idx_reject_shadow_status ON reject_shadow_trades(status)')
 c.execute('''CREATE TABLE IF NOT EXISTS entry_quality_filter_v2_research_meta(
  version TEXT PRIMARY KEY,deployed_at TEXT NOT NULL
 )''')
 c.execute('''INSERT OR IGNORE INTO entry_quality_filter_v2_research_meta(version,deployed_at)
  VALUES(?,?)''',(ENTRY_QUALITY_FILTER_V2_RESEARCH_VERSION,datetime.now().isoformat(timespec='seconds')))
 c.execute('''CREATE TABLE IF NOT EXISTS entry_quality_filter_v2_research(
  id INTEGER PRIMARY KEY AUTOINCREMENT,position_id INTEGER NOT NULL UNIQUE,version TEXT NOT NULL,
  symbol TEXT NOT NULL,side TEXT NOT NULL,opened_at TEXT NOT NULL,score REAL,
  rsi REAL,coin_volume_ratio REAL,resistance_distance_pct REAL,support_distance_pct REAL,
  bullish_pct REAL,bearish_pct REAL,btc_15m_volume_ratio REAL,eth_15m_volume_ratio REAL,
  trend_15m INTEGER,trend_1h INTEGER,trend_4h INTEGER,matched_rules_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN',tp1_hit INTEGER NOT NULL DEFAULT 0,tp2_hit INTEGER NOT NULL DEFAULT 0,
  mae_r REAL NOT NULL DEFAULT 0,mfe_r REAL NOT NULL DEFAULT 0,close_reason TEXT,final_net_pnl REAL,
  closed_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
  FOREIGN KEY(position_id) REFERENCES positions(id)
 )''')
 c.execute('CREATE INDEX IF NOT EXISTS idx_entry_quality_v2_status ON entry_quality_filter_v2_research(status)')
 c.execute('''CREATE TABLE IF NOT EXISTS entry_quality_filter_v3_research_meta(
  version TEXT PRIMARY KEY,deployed_at TEXT NOT NULL
 )''')
 c.execute('''INSERT OR IGNORE INTO entry_quality_filter_v3_research_meta(version,deployed_at)
  VALUES(?,?)''',(ENTRY_QUALITY_FILTER_V3_RESEARCH_VERSION,datetime.now().isoformat(timespec='seconds')))
 c.execute('''CREATE TABLE IF NOT EXISTS entry_quality_filter_v3_research(
  id INTEGER PRIMARY KEY AUTOINCREMENT,position_id INTEGER NOT NULL UNIQUE,version TEXT NOT NULL,
  symbol TEXT NOT NULL,side TEXT NOT NULL,opened_at TEXT NOT NULL,score REAL,
  rsi REAL,coin_volume_ratio REAL,resistance_distance_pct REAL,support_distance_pct REAL,
  bullish_pct REAL,bearish_pct REAL,btc_15m_volume_ratio REAL,eth_15m_volume_ratio REAL,
  trend_15m INTEGER,trend_1h INTEGER,trend_4h INTEGER,matched_rules_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN',tp1_hit INTEGER NOT NULL DEFAULT 0,tp2_hit INTEGER NOT NULL DEFAULT 0,
  mae REAL NOT NULL DEFAULT 0,mae_r REAL NOT NULL DEFAULT 0,mfe REAL NOT NULL DEFAULT 0,mfe_r REAL NOT NULL DEFAULT 0,
  close_reason TEXT,gross_pnl REAL,fee REAL,final_net_pnl REAL,closed_at TEXT,
  created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
  FOREIGN KEY(position_id) REFERENCES positions(id)
 )''')
 c.execute('CREATE INDEX IF NOT EXISTS idx_entry_quality_v3_status ON entry_quality_filter_v3_research(status)')
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
 state['api_saved']=credentials_available()

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
CREDENTIAL_ENV={'api_key':'MEXC_API_KEY','api_secret':'MEXC_API_SECRET'}
def credential_get(account):
 env_name=CREDENTIAL_ENV.get(account)
 value=os.environ.get(env_name,'').strip() if env_name else ''
 if value:
  return value
 return keychain_get(account) if sys.platform=='darwin' else ''
def credentials_available():
 return bool(credential_get('api_key') and credential_get('api_secret'))
def redact_credentials(value,*credentials):
 text=str(value)
 for credential in credentials:
  if credential:
   text=text.replace(credential,'[REDACTED]')
 return text[:300]
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
 nearest_fib=min(fibs,key=lambda f:abs(p-f)); fib_distance=abs(p-nearest_fib)
 return {'price':p,'ema20':e20,'ema50':e50,'rsi':rsi(closes),'atr':atr(rows),'support':min(x[3] for x in recent),'resistance':max(x[2] for x in recent),'volume_ratio':(sum(vols[-5:])/5)/(sum(vols[-25:-5])/20 or 1),'trend':1 if p>e20>e50 else (-1 if p<e20<e50 else 0),'fib_near':fib_distance/(p or 1)<.003,'nearest_fib':nearest_fib,'fib_distance':fib_distance,'fib_distance_pct':fib_distance/(p or 1)*100}
def scores(m):
 L=S=0; details=[]
 trend_names={'4h':'4 saat trend','1h':'1 saat trend','15m':'15 dk trend'}
 for tf,w in [('4h',20),('1h',15),('15m',10)]:
  lp=w if m[tf]['trend']==1 else 0; sp=w if m[tf]['trend']==-1 else 0
  L+=lp; S+=sp; details.append({'component':'trend','timeframe':tf,'name':trend_names[tf],'long':lp,'short':sp,'note':'Yukarı' if m[tf]['trend']==1 else ('Aşağı' if m[tf]['trend']==-1 else 'Yatay/kararsız')})
 rr=m['15m']['rsi']; lp=15 if 52<=rr<=70 else 0; sp=15 if 30<=rr<=48 else 0; L+=lp; S+=sp; details.append({'component':'rsi','name':'RSI','long':lp,'short':sp,'note':f'{rr:.1f}'})
 vr=m['15m']['volume_ratio']; lp=sp=0
 if vr>=1.25:
  if m['15m']['trend']>=0:lp=15; L+=15
  if m['15m']['trend']<=0:sp=15; S+=15
 details.append({'component':'volume','name':'Hacim teyidi','long':lp,'short':sp,'note':f'{vr:.2f}x'})
 p=m['15m']['price']; a=m['15m']['atr'] or p*.005; lp=15 if m['15m']['resistance']-p>=2*a else 0; sp=15 if p-m['15m']['support']>=2*a else 0; L+=lp; S+=sp
 details.append({'component':'support_resistance','name':'Destek / direnç alanı','long':lp,'short':sp,'note':f'D {m["15m"]["support"]:.6g} · R {m["15m"]["resistance"]:.6g}'})
 lp=sp=0
 if m['15m']['fib_near']:
  if m['1h']['trend']==1:lp=10; L+=10
  if m['1h']['trend']==-1:sp=10; S+=10
 details.append({'component':'fibonacci','name':'Fibonacci yakınlığı','long':lp,'short':sp,'note':'Yakın' if m['15m']['fib_near'] else 'Yakın değil'})
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

def strategy_lab_create(position_id):
 def write(c):
  pos=c.execute('SELECT * FROM positions WHERE id=?',(position_id,)).fetchone()
  if not pos:
   return
  now=datetime.now().isoformat(timespec='seconds')
  cur=c.execute('''INSERT OR IGNORE INTO strategy_lab_experiments(
   source_position_id,symbol,side,entry,initial_stop,qty,score,opened_at,created_at
   ) VALUES(?,?,?,?,?,?,?,?,?)''',(pos['id'],pos['symbol'],pos['side'],pos['entry'],pos['initial_stop'],pos['qty'],pos['score'],pos['opened_at'],now))
  experiment=c.execute('SELECT id FROM strategy_lab_experiments WHERE source_position_id=?',(position_id,)).fetchone()
  if not experiment:
   return
  fee_rate=max(float(settings.get('paper_fee_rate',0.0008) or 0),0.0)
  entry_fee=float(pos['entry'])*float(pos['qty'])*fee_rate
  for model in STRATEGY_LAB_MODELS:
   c.execute('''INSERT OR IGNORE INTO strategy_lab_runs(
    experiment_id,model,model_version,status,entry,stop,qty,remaining_qty,realized_pnl,fee_paid,updated_at
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(experiment['id'],model,STRATEGY_LAB_VERSION,'OPEN',pos['entry'],pos['initial_stop'],pos['qty'],pos['qty'],-entry_fee,entry_fee,now))
 _sqlite_write_with_retry(write)

def _strategy_lab_close(c,run,price,reason,now):
 sign=1 if run['side']=='LONG' else -1
 rem=float(run['remaining_qty'] or 0)
 realized=float(run['realized_pnl'] or 0)+(price-float(run['entry']))*sign*rem
 fee=float(run['fee_paid'] or 0)+price*rem*max(float(settings.get('paper_fee_rate',0.0008) or 0),0.0)
 realized-=price*rem*max(float(settings.get('paper_fee_rate',0.0008) or 0),0.0)
 c.execute('''UPDATE strategy_lab_runs SET status='CLOSED',remaining_qty=0,realized_pnl=?,fee_paid=?,
  closed_at=?,close_price=?,close_reason=?,updated_at=? WHERE id=? AND status='OPEN' ''',
  (realized,fee,now,price,reason,now,run['id']))

def _strategy_lab_manage_once(c):
 rows=c.execute('''SELECT r.*,e.source_position_id,e.symbol,e.side,e.initial_stop,e.current_stopped_at,
  p.status AS source_status,p.remaining_qty AS source_remaining_qty,p.pnl AS source_pnl,
  p.fee_paid AS source_fee_paid,p.tp1_done AS source_tp1_done,p.tp2_done AS source_tp2_done,
  p.closed_at AS source_closed_at,p.close_price AS source_close_price,p.close_reason AS source_close_reason,
  p.mae_r AS source_mae_r,p.mfe_r AS source_mfe_r,p.stop AS source_stop
  FROM strategy_lab_runs r JOIN strategy_lab_experiments e ON e.id=r.experiment_id
  JOIN positions p ON p.id=e.source_position_id WHERE r.status='OPEN'
  ORDER BY r.experiment_id,CASE r.model WHEN 'CURRENT' THEN 0 ELSE 1 END,r.id''').fetchall()
 now_dt=datetime.now(); now=now_dt.isoformat(timespec='seconds')
 fee_rate=max(float(settings.get('paper_fee_rate',0.0008) or 0),0.0)
 for raw in rows:
  run=dict(raw)
  price=float((state.get('live_prices') or {}).get(run['symbol']) or (state.get('market',{}).get(run['symbol']) or {}).get('price') or 0)
  if run['source_status']=='CLOSED' and run['source_close_reason']=='EMERGENCY_CLOSE':
   continue
  if run['model']=='CURRENT':
   if run['source_status']=='CLOSED':
    stopped_at=run['source_closed_at'] if run['source_close_reason']=='STOP' else None
    if stopped_at:
     c.execute('UPDATE strategy_lab_experiments SET current_stopped_at=COALESCE(current_stopped_at,?) WHERE id=?',(stopped_at,run['experiment_id']))
    c.execute('''UPDATE strategy_lab_runs SET status='CLOSED',remaining_qty=?,realized_pnl=?,fee_paid=?,tp1_done=?,tp2_done=?,
     mae_r=?,mfe_r=?,stop=?,closed_at=?,close_price=?,close_reason=?,updated_at=? WHERE id=?''',
     (run['source_remaining_qty'],run['source_pnl'],run['source_fee_paid'],run['source_tp1_done'],run['source_tp2_done'],
      run['source_mae_r'],run['source_mfe_r'],run['source_stop'],run['source_closed_at'],run['source_close_price'],run['source_close_reason'],now,run['id']))
   else:
    c.execute('''UPDATE strategy_lab_runs SET remaining_qty=?,realized_pnl=?,fee_paid=?,tp1_done=?,tp2_done=?,
     mae_r=?,mfe_r=?,stop=?,updated_at=? WHERE id=?''',(run['source_remaining_qty'],run['source_pnl'],run['source_fee_paid'],
     run['source_tp1_done'],run['source_tp2_done'],run['source_mae_r'],run['source_mfe_r'],run['source_stop'],now,run['id']))
   continue
  if price<=0:
   continue
  sign=1 if run['side']=='LONG' else -1
  risk_distance=max(abs(float(run['entry'])-float(run['initial_stop'])),1e-12)
  progress=(price-float(run['entry']))*sign/risk_distance
  mae=max(float(run['mae_r'] or 0),max(0.0,-progress)); mfe=max(float(run['mfe_r'] or 0),max(0.0,progress))
  c.execute('UPDATE strategy_lab_runs SET mae_r=?,mfe_r=?,updated_at=? WHERE id=?',(mae,mfe,now,run['id']))
  run['mae_r']=mae; run['mfe_r']=mfe
  if run['source_status']=='CLOSED' and run['source_close_reason'] not in ('STOP','EMERGENCY_CLOSE'):
   _strategy_lab_close(c,run,price,'CURRENT_'+str(run['source_close_reason'] or 'CLOSE'),now)
   continue
  if run['model']=='TRAILING_RUNNER':
   rem=float(run['remaining_qty']); realized=float(run['realized_pnl']); fee=float(run['fee_paid'])
   stop=float(run['stop']); tp1_done=int(run['tp1_done'] or 0); tp2_done=int(run['tp2_done'] or 0)
   if tp2_done:
    trailing_r=max(1.0,mfe-1.0)
    candidate=float(run['entry'])+sign*trailing_r*risk_distance
    stop=max(stop,candidate) if sign==1 else min(stop,candidate)
   hit_stop=(run['side']=='LONG' and price<=stop) or (run['side']=='SHORT' and price>=stop)
   if hit_stop:
    _strategy_lab_close(c,run,price,'RUNNER_TRAILING_STOP' if tp2_done else 'STOP',now)
    continue
   if progress>=float(settings['tp1_r']) and not tp1_done:
    q=min(float(run['qty'])*float(settings['tp1_pct'])/100.0,rem)
    realized+=(price-float(run['entry']))*sign*q-price*q*fee_rate; fee+=price*q*fee_rate; rem-=q
    stop=float(run['entry']); tp1_done=1
   if progress>=float(settings['tp2_r']) and not tp2_done:
    q=min(float(run['qty'])*float(settings['tp2_pct'])/100.0,rem)
    realized+=(price-float(run['entry']))*sign*q-price*q*fee_rate; fee+=price*q*fee_rate; rem-=q
    stop=float(run['entry'])+sign*risk_distance; tp2_done=1
   if tp2_done:
    trailing_r=max(1.0,mfe-1.0)
    candidate=float(run['entry'])+sign*trailing_r*risk_distance
    stop=max(stop,candidate) if sign==1 else min(stop,candidate)
   c.execute('''UPDATE strategy_lab_runs SET remaining_qty=?,realized_pnl=?,fee_paid=?,stop=?,
    tp1_done=?,tp2_done=?,updated_at=? WHERE id=?''',(rem,realized,fee,stop,tp1_done,tp2_done,now,run['id']))
   continue
  trigger=run['stop_triggered_at']
  if not trigger:
   hit_stop=(run['side']=='LONG' and price<=float(run['stop'])) or (run['side']=='SHORT' and price>=float(run['stop']))
   if hit_stop:
    trigger=now
    c.execute('UPDATE strategy_lab_runs SET stop_triggered_at=?,updated_at=? WHERE id=?',(now,now,run['id']))
    if run['model']=='NO_STOP_MINI':
     close_qty=float(run['remaining_qty'])*0.85
     realized=float(run['realized_pnl'])+(price-float(run['entry']))*sign*close_qty-price*close_qty*fee_rate
     fee=float(run['fee_paid'])+price*close_qty*fee_rate
     run['remaining_qty']=float(run['remaining_qty'])-close_qty; run['realized_pnl']=realized; run['fee_paid']=fee
     c.execute('UPDATE strategy_lab_runs SET remaining_qty=?,realized_pnl=?,fee_paid=? WHERE id=?',(run['remaining_qty'],realized,fee,run['id']))
    else:
     continue
   else:
    rem=float(run['remaining_qty']); realized=float(run['realized_pnl']); fee=float(run['fee_paid'])
    stop=float(run['stop']); tp1_done=int(run['tp1_done'] or 0); tp2_done=int(run['tp2_done'] or 0)
    if progress>=float(settings['tp1_r']) and not tp1_done:
     q=min(float(run['qty'])*float(settings['tp1_pct'])/100.0,rem)
     realized+=(price-float(run['entry']))*sign*q-price*q*fee_rate; fee+=price*q*fee_rate; rem-=q
     stop=float(run['entry']); tp1_done=1
    if progress>=float(settings['tp2_r']) and not tp2_done:
     q=min(float(run['qty'])*float(settings['tp2_pct'])/100.0,rem)
     realized+=(price-float(run['entry']))*sign*q-price*q*fee_rate; fee+=price*q*fee_rate; rem-=q
     stop=float(run['entry'])+sign*risk_distance; tp2_done=1
    c.execute('''UPDATE strategy_lab_runs SET remaining_qty=?,realized_pnl=?,fee_paid=?,stop=?,
     tp1_done=?,tp2_done=?,updated_at=? WHERE id=?''',(rem,realized,fee,stop,tp1_done,tp2_done,now,run['id']))
    continue
  trigger_dt=_parse_iso(trigger)
  elapsed=(now_dt-trigger_dt).total_seconds() if trigger_dt else 0
  total=float(run['realized_pnl'])+(price-float(run['entry']))*sign*float(run['remaining_qty'])
  current_stopped_at=run['current_stopped_at']
  if not current_stopped_at:
   exp=c.execute('SELECT current_stopped_at FROM strategy_lab_experiments WHERE id=?',(run['experiment_id'],)).fetchone()
   current_stopped_at=exp['current_stopped_at'] if exp else None
  if current_stopped_at and total>0 and not run['first_post_stop_profit_at']:
   c.execute('UPDATE strategy_lab_runs SET first_post_stop_profit_at=? WHERE id=?',(now,run['id']))
   run['first_post_stop_profit_at']=now
  if total>0:
   _strategy_lab_close(c,run,price,'RECOVERED_PROFIT',now)
  elif run['model']=='NO_STOP_MINI' and progress<=-2.0:
   _strategy_lab_close(c,run,price,'HARD_STOP_2R',now)
  elif run['model']=='SMART_EXIT' and progress<=-1.5:
   _strategy_lab_close(c,run,price,'HARD_STOP_1_5R',now)
  elif run['model']=='SMART_EXIT' and elapsed>=900:
   _strategy_lab_close(c,run,price,'STOP_CONFIRMED_15M',now)
  elif elapsed>=7200:
   _strategy_lab_close(c,run,price,'TIMEOUT_2H',now)

def manage_strategy_lab():
 try:
  _sqlite_write_with_retry(_strategy_lab_manage_once)
  state['strategy_lab_error']=None
 except Exception as e:
  state['strategy_lab_error']=f'{type(e).__name__}: {e}'

def strategy_lab_open_symbols():
 c=db()
 rows=c.execute('''SELECT DISTINCT e.symbol FROM strategy_lab_runs r
  JOIN strategy_lab_experiments e ON e.id=r.experiment_id WHERE r.status='OPEN' ''').fetchall()
 c.close()
 return [r['symbol'] for r in rows]

def score_snapshot(m,side,total_score,captured_at):
 direction='long' if side=='LONG' else 'short'
 details=m.get('score_details') or []
 def component_score(component,timeframe=None):
  matches=[x for x in details if x.get('component')==component and (timeframe is None or x.get('timeframe')==timeframe)]
  return sum(float(x.get(direction) or 0) for x in matches) if matches else None
 m15=m.get('15m') or {}; price=float(m15.get('price') or 0)
 support=m15.get('support'); resistance=m15.get('resistance')
 support_distance=price-float(support) if support is not None else None
 resistance_distance=float(resistance)-price if resistance is not None else None
 return {
  'score_version':SCORE_SNAPSHOT_VERSION,'signal_side':side,'captured_at':captured_at,
  'trend_score':component_score('trend'),
  'trend_4h_score':component_score('trend','4h'),'trend_1h_score':component_score('trend','1h'),
  'trend_15m_score':component_score('trend','15m'),
  'support_resistance_score':component_score('support_resistance'),
  'volume_score':component_score('volume'),'rsi_score':component_score('rsi'),
  'fibonacci_score':component_score('fibonacci'),'total_score':float(total_score),
  'rsi_value':m15.get('rsi'),'volume_ratio':m15.get('volume_ratio'),
  'support_price':support,'resistance_price':resistance,
  'support_distance':support_distance,'resistance_distance':resistance_distance,
  'support_distance_pct':support_distance/(price or 1)*100 if support_distance is not None else None,
  'resistance_distance_pct':resistance_distance/(price or 1)*100 if resistance_distance is not None else None,
  'fibonacci_near':int(bool(m15.get('fib_near'))) if 'fib_near' in m15 else None,
  'fibonacci_level':m15.get('nearest_fib'),'fibonacci_distance':m15.get('fib_distance'),
  'fibonacci_distance_pct':m15.get('fib_distance_pct'),
  'trend_15m':m15.get('trend'),'trend_1h':(m.get('1h') or {}).get('trend'),
  'trend_4h':(m.get('4h') or {}).get('trend'),
  'score_details_json':json.dumps(details,ensure_ascii=False,separators=(',',':')),
 }

def _insert_score_snapshot(c,position_id,snapshot):
 columns=['position_id',*snapshot.keys()]
 c.execute(
  f"INSERT INTO position_score_snapshots ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
  (position_id,*snapshot.values())
 )

def _trend_majority(market):
 trends=[int((market.get(tf) or {}).get('trend') or 0) for tf in ('15m','1h','4h')]
 total=sum(trends)
 return 1 if total>=2 else (-1 if total<=-2 else 0)

def _ema_momentum(market):
 m15=market.get('15m') or {}
 price=float(m15.get('price') or 0); ema20=float(m15.get('ema20') or 0); ema50=float(m15.get('ema50') or 0)
 if price>ema20>ema50:
  return 'BULLISH_STACK'
 if price<ema20<ema50:
  return 'BEARISH_STACK'
 return 'MIXED'

def market_regime_snapshot(current_market,previous_market,universe_symbols,position_side,captured_at):
 universe=[current_market[s] for s in universe_symbols if s in current_market]
 trends=[int((m.get('15m') or {}).get('trend') or 0) for m in universe]
 bullish=sum(t==1 for t in trends); bearish=sum(t==-1 for t in trends); neutral=len(trends)-bullish-bearish
 size=len(trends); bullish_pct=bullish/(size or 1)*100; bearish_pct=bearish/(size or 1)*100
 comparable=[]
 for symbol in universe_symbols:
  current=current_market.get(symbol); previous=previous_market.get(symbol)
  if current and previous:
   comparable.append((int((previous.get('15m') or {}).get('trend') or 0),int((current.get('15m') or {}).get('trend') or 0)))
 changed=sum(before!=after for before,after in comparable)
 changed_denominator=len(comparable)
 changed_pct=changed/changed_denominator*100 if changed_denominator else None
 btc=current_market.get('BTC_USDT') or {}; eth=current_market.get('ETH_USDT') or {}
 btc_majority=_trend_majority(btc); eth_majority=_trend_majority(eth)
 def short_term_reversal(market):
  t15=int((market.get('15m') or {}).get('trend') or 0)
  t1=int((market.get('1h') or {}).get('trend') or 0)
  t4=int((market.get('4h') or {}).get('trend') or 0)
  return t1==t4 and t1!=0 and t15==-t1
 breadth_conflict=(btc_majority==eth_majority==1 and bearish_pct>=60) or (btc_majority==eth_majority==-1 and bullish_pct>=60)
 reversal_risk=(changed_pct is not None and changed_pct>=30) or (short_term_reversal(btc) and short_term_reversal(eth)) or breadth_conflict
 if reversal_risk:
  regime='REVERSAL_RISK'
 elif btc_majority==eth_majority==1 and bullish_pct>=60:
  regime='BULLISH'
 elif btc_majority==eth_majority==-1 and bearish_pct>=60:
  regime='BEARISH'
 else:
  regime='MIXED'
 aligned=(position_side=='LONG' and regime=='BULLISH') or (position_side=='SHORT' and regime=='BEARISH')
 conflict=(position_side=='LONG' and regime=='BEARISH') or (position_side=='SHORT' and regime=='BULLISH')
 alignment='ALIGNED' if aligned else ('CONFLICT' if conflict else 'UNCERTAIN')
 def anchor_values(market,prefix):
  m15=market.get('15m') or {}
  return {
   f'{prefix}_trend_15m':m15.get('trend'),f'{prefix}_trend_1h':(market.get('1h') or {}).get('trend'),
   f'{prefix}_trend_4h':(market.get('4h') or {}).get('trend'),f'{prefix}_15m_price':m15.get('price'),
   f'{prefix}_15m_ema20':m15.get('ema20'),f'{prefix}_15m_ema50':m15.get('ema50'),
   f'{prefix}_15m_momentum':_ema_momentum(market),f'{prefix}_15m_rsi':m15.get('rsi'),
   f'{prefix}_15m_volume_ratio':m15.get('volume_ratio'),
  }
 return {
  'snapshot_version':MARKET_REGIME_SNAPSHOT_VERSION,'captured_at':captured_at,
  'regime_classification':regime,'position_side':position_side,'market_alignment':alignment,
  'universe_size':size,'bullish_count':bullish,'bearish_count':bearish,'neutral_count':neutral,
  'bullish_pct':bullish_pct,'bearish_pct':bearish_pct,'trend_changed_15m_count':changed if changed_denominator else None,
  'trend_changed_15m_denominator':changed_denominator if changed_denominator else None,'trend_changed_15m_pct':changed_pct,
  **anchor_values(btc,'btc'),**anchor_values(eth,'eth'),
 }

def _insert_market_regime_snapshot(c,position_id,snapshot):
 columns=['source_position_id',*snapshot.keys()]
 c.execute(
  f"INSERT INTO market_regime_snapshots ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
  (position_id,*snapshot.values())
 )

def _long_entry_quality_reasons(m,current_market):
 m15=m.get('15m') or {}
 snapshot=score_snapshot(m,'LONG',float(m.get('long_score') or 0),datetime.now().isoformat(timespec='seconds'))
 coin_rsi=float(m15.get('rsi')) if m15.get('rsi') is not None else None
 btc_ratio=((current_market.get('BTC_USDT') or {}).get('15m') or {}).get('volume_ratio')
 eth_ratio=((current_market.get('ETH_USDT') or {}).get('15m') or {}).get('volume_ratio')
 volume_score=snapshot.get('volume_score')
 reasons=[]
 if coin_rsi is not None and coin_rsi>=64:reasons.append('COIN_RSI_GTE_64')
 if btc_ratio is not None and float(btc_ratio)<0.55:reasons.append('BTC_15M_VOLUME_RATIO_LT_0_55')
 if eth_ratio is not None and float(eth_ratio)<0.55:reasons.append('ETH_15M_VOLUME_RATIO_LT_0_55')
 if volume_score is not None and float(volume_score)==0:reasons.append('COIN_VOLUME_SCORE_EQ_0')
 return reasons,snapshot

def _record_entry_filter_rejection(symbol,m,score,current_market,previous_market,universe_symbols,captured_at,reasons,score_data):
 regime=market_regime_snapshot(current_market,previous_market,universe_symbols,'LONG',captured_at)
 m15=m.get('15m') or {}
 btc_ratio=((current_market.get('BTC_USDT') or {}).get('15m') or {}).get('volume_ratio')
 eth_ratio=((current_market.get('ETH_USDT') or {}).get('15m') or {}).get('volume_ratio')
 def write(c):
  active=c.execute('SELECT * FROM entry_filter_active_setups WHERE symbol=? AND side=?',(symbol,'LONG')).fetchone()
  if active:
   combined=list(dict.fromkeys([*json.loads(active['reasons_json']),*reasons]))
   if combined!=json.loads(active['reasons_json']):
    c.execute('UPDATE entry_filter_rejections SET reasons_json=? WHERE id=?',
     (json.dumps(combined,separators=(',',':')),active['rejection_id']))
    c.execute('UPDATE reject_shadow_trades SET reasons_json=?,updated_at=? WHERE rejection_id=?',
     (json.dumps(combined,separators=(',',':')),captured_at,active['rejection_id']))
   c.execute('UPDATE entry_filter_active_setups SET reasons_json=?,last_seen_at=? WHERE symbol=? AND side=?',
    (json.dumps(combined,separators=(',',':')),captured_at,symbol,'LONG'))
   return 'UPDATED' if combined!=json.loads(active['reasons_json']) else 'DUPLICATE'
  cur=c.execute('''INSERT INTO entry_filter_rejections(
   filter_version,status,symbol,side,score,rejected_at,reasons_json,reference_price,
   coin_rsi,btc_15m_volume_ratio,eth_15m_volume_ratio,coin_volume_score,
   coin_trend_15m,coin_trend_1h,coin_trend_4h,score_snapshot_json,market_regime_snapshot_json
   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(
   ENTRY_QUALITY_FILTER_VERSION,'ENTRY_FILTER_REJECTED',symbol,'LONG',float(score),captured_at,
   json.dumps(reasons,separators=(',',':')),m15.get('price'),m15.get('rsi'),btc_ratio,eth_ratio,
   score_data.get('volume_score'),m15.get('trend'),(m.get('1h') or {}).get('trend'),
   (m.get('4h') or {}).get('trend'),json.dumps(score_data,ensure_ascii=False,separators=(',',':')),
   json.dumps(regime,ensure_ascii=False,separators=(',',':'))))
  plan=_current_entry_plan(symbol,'LONG',m)
  c.execute('''INSERT INTO reject_shadow_trades(
   rejection_id,shadow_version,status,symbol,side,entry_at,entry_price,initial_stop,current_stop,
   qty,remaining_qty,initial_risk_usd,signal_score,reasons_json,coin_rsi,btc_15m_volume_ratio,
   eth_15m_volume_ratio,coin_volume_score,score_snapshot_json,market_regime_snapshot_json,
   simulated_fee,net_simulated_pnl,created_at,updated_at
   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(
   cur.lastrowid,REJECT_SHADOW_VERSION,'OPEN',symbol,'LONG',captured_at,plan['entry'],plan['initial_stop'],
   plan['current_stop'],plan['qty'],plan['qty'],plan['initial_risk_usd'],float(score),
   json.dumps(reasons,separators=(',',':')),m15.get('rsi'),btc_ratio,eth_ratio,score_data.get('volume_score'),
   json.dumps(score_data,ensure_ascii=False,separators=(',',':')),
   json.dumps(regime,ensure_ascii=False,separators=(',',':')),plan['entry_fee'],-plan['entry_fee'],captured_at,captured_at))
  c.execute('''INSERT INTO entry_filter_active_setups(symbol,side,rejection_id,reasons_json,first_seen_at,last_seen_at)
   VALUES(?,?,?,?,?,?)''',(symbol,'LONG',cur.lastrowid,json.dumps(reasons,separators=(',',':')),captured_at,captured_at))
  return 'CREATED'
 action=_sqlite_write_with_retry(write)
 if action!='DUPLICATE':
  log(f'{ENTRY_QUALITY_FILTER_VERSION} {symbol} LONG ENTRY_FILTER_REJECTED {action} · {",".join(reasons)}','RESEARCH')

def _clear_entry_filter_setup(symbol):
 def write(c):c.execute('DELETE FROM entry_filter_active_setups WHERE symbol=? AND side=?',(symbol,'LONG'))
 _sqlite_write_with_retry(write)

def process_paper_signal(symbol,m,current_market,previous_market,universe_symbols):
 side=m.get('signal')
 if side!='LONG':_clear_entry_filter_setup(symbol)
 if side not in ('LONG','SHORT') or has_open(symbol):return None
 score=max(float(m.get('long_score') or 0),float(m.get('short_score') or 0))
 captured_at=datetime.now().isoformat(timespec='seconds')
 regime=market_regime_snapshot(current_market,previous_market,universe_symbols,side,captured_at)
 if side=='LONG':
  reasons,score_data=_long_entry_quality_reasons(m,current_market)
  if reasons:
   _record_entry_filter_rejection(symbol,m,score,current_market,previous_market,universe_symbols,
    captured_at,reasons,score_data)
   return 'ENTRY_FILTER_REJECTED'
  _clear_entry_filter_setup(symbol)
 return paper_open(symbol,side,m,score,regime)

def _live_fee_row(statuses):
 c=db(); marks=','.join('?' for _ in statuses)
 row=c.execute(f"SELECT * FROM live_fee_tests WHERE status IN ({marks}) ORDER BY id DESC LIMIT 1",tuple(statuses)).fetchone()
 c.close(); return dict(row) if row else None

def _live_fee_update(test_id,**values):
 if not values:return
 def write(c):
  c.execute(f"UPDATE live_fee_tests SET {','.join(k+'=?' for k in values)} WHERE id=?",(*values.values(),test_id))
 _sqlite_write_with_retry(write)

def _contract_detail(data,symbol):
 items=data if isinstance(data,list) else [data]
 return next((x for x in items if str(x.get('symbol'))==symbol),None)

async def maybe_prepare_live_fee_test(client,current_market,previous_market,universe_symbols):
 armed=_live_fee_row(('ARMED',))
 if not armed:return
 try:
  positions=_active_contract_positions(
   await mexc_private_request(client,'GET','/api/v1/private/position/open_positions') or [])
 except Exception as e:
  safe=redact_credentials(e,credential_get('api_key'),credential_get('api_secret'))
  state['live_fee_position_check']={'checked_at':datetime.now().isoformat(timespec='seconds'),'error':safe}
  _live_fee_update(armed['id'],error='Gerçek açık Futures pozisyonları doğrulanamadı; aday hazırlanmadı')
  return
 long_symbols=sorted({str(x.get('symbol')) for x in positions if int(x.get('positionType') or 0)==1})
 short_symbols=sorted({str(x.get('symbol')) for x in positions if int(x.get('positionType') or 0)==2})
 occupied_symbols=set(long_symbols)|set(short_symbols)
 state['live_fee_position_check']={'checked_at':datetime.now().isoformat(timespec='seconds'),
  'long_symbols':long_symbols,'short_symbols':short_symbols,'open_position_count':len(positions),'error':None}
 ordered_symbols=list(dict.fromkeys(['BTC_USDT',*universe_symbols]))
 candidates=[(symbol,current_market.get(symbol) or {}) for symbol in ordered_symbols
  if symbol not in occupied_symbols
  if float(((current_market.get(symbol) or {}).get('15m') or {}).get('price') or 0)>0]
 if not candidates:
  _live_fee_update(armed['id'],error='Açık pozisyonsuz ve güncel fiyatlı aday sembol bulunamadı')
  return
 r=await mexc_get(client,'https://api.mexc.com/api/v1/contract/detail',timeout=15)
 payload=r.json() if r.status_code==200 else {}
 details=payload.get('data') if payload.get('success') else []
 selected=None
 for symbol,market in candidates:
  detail=_contract_detail(details,symbol)
  if not detail or not detail.get('apiAllowed',True):continue
  if detail.get('state') not in (None,0):continue
  if detail.get('settleCoin') not in (None,'USDT'):continue
  if float(detail.get('contractSize') or 0)<=0 or float(detail.get('minVol') or 0)<=0 or float(detail.get('volUnit') or 0)<=0:continue
  selected=(symbol,market,detail); break
 if not selected:
  _live_fee_update(armed['id'],error='Likit evrende API ile işleme uygun contract bulunamadı')
  return
 symbol,market,detail=selected
 price=float((market.get('15m') or {}).get('price') or 0); contract_size=float(detail.get('contractSize') or 0)
 min_vol=Decimal(str(detail.get('minVol') or 1)); vol_unit=Decimal(str(detail.get('volUnit') or 1))
 contracts=(min_vol/vol_unit).to_integral_value(rounding=ROUND_CEILING)*vol_unit
 leverage=max(1,int(detail.get('minLeverage') or 1)); notional=price*contract_size*float(contracts)
 taker=max(float(detail.get('takerFeeRate') or 0),LIVE_FEE_TEST_ESTIMATED_TAKER_RATE)
 estimated_fee=notional*taker; max_loss=notional*0.005+estimated_fee*2
 side=market.get('signal') if market.get('signal') in ('LONG','SHORT') else ('SHORT' if (market.get('15m') or {}).get('trend')==-1 else 'LONG')
 score=max(float(market.get('long_score') or 0),float(market.get('short_score') or 0))
 now=datetime.now(); expires=now+timedelta(seconds=LIVE_FEE_TEST_EXPIRY_SECONDS)
 _live_fee_update(armed['id'],status='PREPARED',candidate_at=now.isoformat(timespec='seconds'),
  expires_at=expires.isoformat(timespec='seconds'),symbol=symbol,side=side,score=score,
  reference_price=price,contract_size=contract_size,contracts=float(contracts),leverage=leverage,
  estimated_taker_rate=taker,estimated_entry_fee=estimated_fee,estimated_exit_fee=estimated_fee,
  max_estimated_loss=max_loss,error=None)
 log(f'LIVE_FEE_TEST adayı hazır: {symbol} {side} · gerçek emir DEVRE DIŞI','WARN')

def _signed_headers(key,secret,timestamp,request_param):
 signature=hmac.new(secret.encode(),(key+timestamp+request_param).encode(),hashlib.sha256).hexdigest()
 return {'ApiKey':key,'Request-Time':timestamp,'Signature':signature,'Content-Type':'application/json'}

async def mexc_private_request(client,method,path,params=None,body=None):
 global mexc_last_request_at
 key=credential_get('api_key'); secret=credential_get('api_secret')
 if not key or not secret:raise RuntimeError('Private API credential yapılandırılmamış')
 params=params or {}
 async with mexc_request_lock:
  wait=MEXC_MIN_REQUEST_INTERVAL-(time.monotonic()-mexc_last_request_at)
  if wait>0:await asyncio.sleep(wait)
  ts=str(int(time.time()*1000))
  if method=='GET':
   request_param=urlencode(sorted((k,str(v)) for k,v in params.items()))
   response=await client.get('https://api.mexc.com'+path,params=params,headers=_signed_headers(key,secret,ts,request_param),timeout=15)
  else:
   request_param=json.dumps(body or {},separators=(',',':'),ensure_ascii=False)
   response=await client.post('https://api.mexc.com'+path,content=request_param.encode(),headers=_signed_headers(key,secret,ts,request_param),timeout=15)
  mexc_last_request_at=time.monotonic()
 try:payload=response.json()
 except Exception:raise RuntimeError(f'Private API geçersiz yanıt: HTTP {response.status_code}')
 if response.status_code!=200 or payload.get('success') is not True:
  detail=redact_credentials(payload.get('message') or payload.get('msg') or payload.get('code') or response.status_code,key,secret)
  raise RuntimeError(f'Private API başarısız: {detail}')
 return payload.get('data')

async def _order_details(client,order_id):
 for _ in range(20):
  order=await mexc_private_request(client,'GET',f'/api/v1/private/order/get/{order_id}')
  if int(order.get('state') or 0)==3 and float(order.get('dealVol') or 0)>0:return order
  if int(order.get('state') or 0) in (4,5):
   if float(order.get('dealVol') or 0)>0:return order
   raise RuntimeError('Order fill olmadan iptal/geçersiz duruma geçti')
  await asyncio.sleep(.25)
 raise RuntimeError('Order fill doğrulama zaman aşımı')

async def _order_fills(client,order_id,contract_size):
 deals=await mexc_private_request(client,'GET',f'/api/v1/private/order/deal_details/{order_id}') or []
 if not deals:raise RuntimeError('Order trade/fill detayı bulunamadı')
 vol=sum(float(x.get('vol') or 0) for x in deals); weighted=sum(float(x.get('price') or 0)*float(x.get('vol') or 0) for x in deals)
 avg=weighted/vol if vol else 0; fee=sum(float(x.get('fee') or 0) for x in deals)
 currencies=sorted({str(x.get('feeCurrency') or '') for x in deals})
 return {'executed_contracts':vol,'executed_qty':vol*contract_size,'average_fill_price':avg,
  'notional':avg*vol*contract_size,'actual_fee':fee,'fee_currency':','.join(currencies),
  'trade_ids':[str(x.get('id')) for x in deals],'timestamps':[x.get('timestamp') for x in deals],
  'is_taker':all(bool(x.get('isTaker',x.get('taker',False))) for x in deals)}

def _active_contract_positions(rows):
 return [x for x in (rows or []) if float(x.get('holdVol') or 0)>0]

def _live_fee_external_oid(test_id,leg):
 if leg not in ('e','x'):raise ValueError('LIVE_FEE_TEST externalOid leg yalnız e veya x olabilir')
 prefix=f'lft{int(test_id)}{leg}'
 random_length=32-len(prefix)
 if random_length<12:raise ValueError('LIVE_FEE_TEST id güvenli benzersiz externalOid için fazla uzun')
 return prefix+uuid.uuid4().hex[:random_length]

async def _live_fee_position_after_entry(client,symbol,side):
 expected_type=1 if side=='LONG' else 2
 for _ in range(20):
  positions=_active_contract_positions(
   await mexc_private_request(client,'GET','/api/v1/private/position/open_positions') or [])
  symbol_positions=[x for x in positions if str(x.get('symbol'))==symbol]
  if not symbol_positions:
   await asyncio.sleep(.25); continue
  matches=[x for x in symbol_positions if int(x.get('positionType') or 0)==expected_type]
  if len(symbol_positions)!=1 or len(matches)!=1:
   raise RuntimeError('KRİTİK: Entry sonrası tek ve beklenen yönde HEDGE pozisyonu doğrulanamadı')
  position=matches[0]
  if not position.get('positionId'):
   raise RuntimeError('KRİTİK: Entry sonrası gerçek positionId alınamadı')
  return position
 raise RuntimeError('KRİTİK: Entry sonrası gerçek HEDGE pozisyonu sorgulanamadı')

async def _assert_live_fee_position_closed(client,symbol,position_id):
 remaining=None
 for _ in range(20):
  positions=_active_contract_positions(
   await mexc_private_request(client,'GET','/api/v1/private/position/open_positions') or [])
  symbol_positions=[x for x in positions if str(x.get('symbol'))==symbol]
  unexpected=[x for x in symbol_positions if str(x.get('positionId'))!=str(position_id)]
  if unexpected:
   raise RuntimeError('KRİTİK: Exit sonrası beklenmeyen karşıt/açık HEDGE pozisyonu tespit edildi')
  remaining=next((x for x in symbol_positions if str(x.get('positionId'))==str(position_id)),None)
  if not remaining:return
  await asyncio.sleep(.25)
 raise RuntimeError(f"KRİTİK: Exit sonrası positionId {position_id} üzerinde {remaining.get('holdVol')} contract kaldı")

def has_open(sym):
 c=db(); r=c.execute("SELECT 1 FROM positions WHERE status='OPEN' AND symbol=?",(sym,)).fetchone(); c.close(); return bool(r)
def _entry_quality_v2_rules(score_data,regime):
 if str(score_data.get('signal_side') or '')!='LONG' or float(regime.get('bullish_pct') or 0)<60:
  return []
 rules=[]
 btc=regime.get('btc_15m_volume_ratio'); eth=regime.get('eth_15m_volume_ratio')
 resistance=score_data.get('resistance_distance_pct')
 if btc is not None and float(btc)<1.0:rules.append('RULE_A')
 if resistance is not None and float(resistance)<1.5:rules.append('RULE_B')
 if eth is not None and float(eth)<1.0:rules.append('RULE_C')
 return rules

def _insert_entry_quality_v2_research(c,position_id,symbol,side,opened,score,score_data,regime):
 if side!='LONG':return
 rules=_entry_quality_v2_rules(score_data,regime)
 c.execute('''INSERT OR IGNORE INTO entry_quality_filter_v2_research(
  position_id,version,symbol,side,opened_at,score,rsi,coin_volume_ratio,resistance_distance_pct,
  support_distance_pct,bullish_pct,bearish_pct,btc_15m_volume_ratio,eth_15m_volume_ratio,
  trend_15m,trend_1h,trend_4h,matched_rules_json,created_at,updated_at
  ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(
  position_id,ENTRY_QUALITY_FILTER_V2_RESEARCH_VERSION,symbol,side,opened,float(score),
  score_data.get('rsi_value'),score_data.get('volume_ratio'),score_data.get('resistance_distance_pct'),
  score_data.get('support_distance_pct'),regime.get('bullish_pct'),regime.get('bearish_pct'),
  regime.get('btc_15m_volume_ratio'),regime.get('eth_15m_volume_ratio'),score_data.get('trend_15m'),
  score_data.get('trend_1h'),score_data.get('trend_4h'),json.dumps(rules,separators=(',',':')),opened,opened))

def _entry_quality_v3_rules(score_data,regime):
 if str(score_data.get('signal_side') or '')!='LONG':return []
 rules=[]
 support=score_data.get('support_distance_pct'); rsi=score_data.get('rsi_value')
 bearish=float(regime.get('bearish_pct') or 0)
 if support is not None and float(support)>=3.0:rules.append('RULE_A')
 if rsi is not None and float(rsi)>=55 and bearish>=20:rules.append('RULE_B')
 if support is not None and bearish>=20 and float(support)>=1.5:rules.append('RULE_C')
 return rules

def _insert_entry_quality_v3_research(c,position_id,symbol,side,opened,score,score_data,regime):
 # V3 observes only CURRENT LONG positions for which V2 matched no rule.
 if side!='LONG' or _entry_quality_v2_rules(score_data,regime):return
 rules=_entry_quality_v3_rules(score_data,regime)
 c.execute('''INSERT OR IGNORE INTO entry_quality_filter_v3_research(
  position_id,version,symbol,side,opened_at,score,rsi,coin_volume_ratio,resistance_distance_pct,
  support_distance_pct,bullish_pct,bearish_pct,btc_15m_volume_ratio,eth_15m_volume_ratio,
  trend_15m,trend_1h,trend_4h,matched_rules_json,created_at,updated_at
  ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(
  position_id,ENTRY_QUALITY_FILTER_V3_RESEARCH_VERSION,symbol,side,opened,float(score),
  score_data.get('rsi_value'),score_data.get('volume_ratio'),score_data.get('resistance_distance_pct'),
  score_data.get('support_distance_pct'),regime.get('bullish_pct'),regime.get('bearish_pct'),
  regime.get('btc_15m_volume_ratio'),regime.get('eth_15m_volume_ratio'),score_data.get('trend_15m'),
  score_data.get('trend_1h'),score_data.get('trend_4h'),json.dumps(rules,separators=(',',':')),opened,opened))

def _current_entry_plan(sym,side,m):
 p=float(m['15m']['price'])
 dist=max(float(m['15m']['atr'])*float(settings['stop_atr_mult']),p*.002)
 stop=p-dist if side=='LONG' else p+dist
 qty=float(settings['risk_per_trade_usd'])/dist
 cap=float(settings['max_btc_notional_usd'] if sym=='BTC_USDT' else settings['max_alt_notional_usd'])
 qty=min(qty,cap/p)
 fee_rate=max(float(settings.get('paper_fee_rate',0.0008) or 0),0.0)
 return {'entry':p,'initial_stop':stop,'current_stop':stop,'qty':qty,
  'initial_risk_usd':qty*dist,'entry_fee':p*qty*fee_rate}

def _current_position_transition(position,price):
 """Pure CURRENT TP/stop transition shared by PAPER and REJECT_SHADOW."""
 p=float(price); entry=float(position['entry']); initial_stop=float(position['initial_stop'])
 sign=1 if position['side']=='LONG' else -1
 risk_distance=abs(entry-initial_stop)
 progress=(p-entry)*sign/risk_distance if risk_distance else 0.0
 result=dict(position)
 result['mae']=max(float(position.get('mae') or 0),max(0.0,(entry-p)*sign))
 result['mfe']=max(float(position.get('mfe') or 0),max(0.0,(p-entry)*sign))
 result['mae_r']=max(float(position.get('mae_r') or 0),max(0.0,-progress))
 result['mfe_r']=max(float(position.get('mfe_r') or 0),max(0.0,progress))
 result['events']=[]; result['closed']=False; result['exit_price']=None
 gross=float(position.get('gross_pnl') or 0); fees=float(position.get('fee_paid') or 0)
 rem=float(position['remaining_qty']); stop=float(position['stop'])
 fee_rate=max(float(settings.get('paper_fee_rate',0.0008) or 0),0.0)
 if (position['side']=='LONG' and p<=stop) or (position['side']=='SHORT' and p>=stop):
  gross+=(p-entry)*sign*rem; fees+=p*rem*fee_rate
  result.update(closed=True,remaining_qty=0.0,gross_pnl=gross,fee_paid=fees,
   net_pnl=gross-fees,exit_price=p,close_reason='STOP')
  result['events'].append('STOP'); return result
 if progress>=float(settings['tp1_r']) and not position.get('tp1_done'):
  q=float(position['qty'])*float(settings['tp1_pct'])/100
  gross+=(p-entry)*sign*q; fees+=p*q*fee_rate; rem-=q; stop=entry
  result['tp1_done']=True; result['events'].append('TP1')
 if progress>=float(settings['tp2_r']) and not position.get('tp2_done'):
  q=min(float(position['qty'])*float(settings['tp2_pct'])/100,rem)
  gross+=(p-entry)*sign*q; fees+=p*q*fee_rate; rem-=q; stop=entry+sign*risk_distance
  result['tp2_done']=True; result['events'].append('TP2')
 result.update(remaining_qty=rem,stop=stop,gross_pnl=gross,fee_paid=fees,net_pnl=gross-fees)
 return result

def paper_open(sym,side,m,sc,regime_snapshot=None):
 if state.get('entry_paused'):
  return
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
 plan=_current_entry_plan(sym,side,m)
 p=plan['entry']; stop=plan['initial_stop']; qty=plan['qty']; actual=plan['initial_risk_usd']
 leverage=max(float(settings.get('leverage',1) or 1),1.0)
 required_margin=(p*qty)/leverage
 available_margin=available_paper_margin()
 if required_margin>available_margin:
  log(f'{sym} {side} açılmadı | margin yetersiz: gereken ${required_margin:.2f}, kullanılabilir ${available_margin:.2f}','RISK')
  return
 entry_fee=plan['entry_fee']
 opened=datetime.now().isoformat(timespec='seconds')
 snapshot=score_snapshot(m,side,sc,opened)
 if regime_snapshot is None:
  cached_market=state.get('market') or {}
  cached_universe=[x.get('symbol') for x in (state.get('universe') or []) if x.get('symbol')]
  regime_snapshot=market_regime_snapshot(cached_market,{},cached_universe or list(cached_market),side,opened)
 else:
  regime_snapshot={**regime_snapshot,'captured_at':opened}
 def write(c):
  cur=c.execute('''INSERT INTO positions(symbol,side,status,entry,stop,initial_stop,qty,remaining_qty,risk_usd,score,opened_at,pnl,fee_paid,mae_r,mfe_r,tracking_started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(sym,side,'OPEN',p,stop,stop,qty,qty,actual,sc,opened,-entry_fee,entry_fee,0.0,0.0,opened))
  _insert_score_snapshot(c,cur.lastrowid,snapshot)
  _insert_market_regime_snapshot(c,cur.lastrowid,regime_snapshot)
  _insert_entry_quality_v2_research(c,cur.lastrowid,sym,side,opened,sc,snapshot,regime_snapshot)
  _insert_entry_quality_v3_research(c,cur.lastrowid,sym,side,opened,sc,snapshot,regime_snapshot)
  return cur.lastrowid
 position_id=_sqlite_write_with_retry(write)
 try:
  strategy_lab_create(position_id)
 except Exception as e:
  state['strategy_lab_error']=f'{type(e).__name__}: {e}'
  log(f'Strategy Lab deney oluşturma hatası: {type(e).__name__}','WARN')
 log(f'{sym} {side} PAPER açıldı | risk ${actual:.2f} | margin ${required_margin:.2f} | giriş fee ${entry_fee:.4f} | skor {sc}')
def _manage_once(c):
 events=[]
 rows=c.execute("SELECT * FROM positions WHERE status='OPEN'").fetchall()
 for pos in rows:
  md=state.get('market',{}).get(pos['symbol']) or {}
  p=float((state.get('live_prices') or {}).get(pos['symbol']) or md.get('price') or 0)
  if p<=0:
   continue
  data=dict(pos); data.update(entry=pos['entry'],stop=pos['stop'],gross_pnl=float(pos['pnl'] or 0)+float(pos['fee_paid'] or 0))
  result=_current_position_transition(data,p)
  if result['closed']:
   c.execute(
    "UPDATE positions SET status='CLOSED',remaining_qty=0,closed_at=?,pnl=?,close_price=?,fee_paid=?,close_reason='STOP',mae_r=?,mfe_r=? WHERE id=? AND status='OPEN'",
    (datetime.now().isoformat(timespec='seconds'),result['net_pnl'],p,result['fee_paid'],result['mae_r'],result['mfe_r'],pos['id'])
   )
   events.append((f"{pos['symbol']} kapandı | PnL ${result['net_pnl']:.2f}",'TRADE'))
   continue
  c.execute('''UPDATE positions SET tp1_done=?,tp2_done=?,remaining_qty=?,pnl=?,stop=?,fee_paid=?,mae_r=?,mfe_r=?
   WHERE id=? AND status='OPEN' ''',(int(bool(result.get('tp1_done'))),int(bool(result.get('tp2_done'))),
   result['remaining_qty'],result['net_pnl'],result['stop'],result['fee_paid'],result['mae_r'],result['mfe_r'],pos['id']))
  for event in result['events']:events.append((f"{pos['symbol']} {event}",'TRADE'))
 return events

def _manage_reject_shadows_once(c):
 rows=c.execute("SELECT * FROM reject_shadow_trades WHERE status='OPEN'").fetchall()
 for row in rows:
  md=state.get('market',{}).get(row['symbol']) or {}
  price=float((state.get('live_prices') or {}).get(row['symbol']) or md.get('price') or 0)
  if price<=0:continue
  data=dict(row); data.update(entry=row['entry_price'],stop=row['current_stop'],
   tp1_done=row['tp1_hit'],tp2_done=row['tp2_hit'],gross_pnl=row['gross_simulated_pnl'],fee_paid=row['simulated_fee'])
  result=_current_position_transition(data,price); now=datetime.now().isoformat(timespec='seconds')
  if result['closed']:
   net=result['net_pnl']; filter_result='AVOIDED_LOSS' if net<0 else ('MISSED_PROFIT' if net>0 else 'BREAK_EVEN')
   c.execute('''UPDATE reject_shadow_trades SET status='CLOSED',close_at=?,exit_price=?,remaining_qty=0,
    current_stop=?,mae=?,mae_r=?,mfe=?,mfe_r=?,tp1_hit=?,tp2_hit=?,gross_simulated_pnl=?,simulated_fee=?,
    net_simulated_pnl=?,final_exit_reason=?,filter_result=?,avoided_loss=?,missed_profit=?,updated_at=? WHERE id=? AND status='OPEN' ''',
    (now,result['exit_price'],result['stop'],result['mae'],result['mae_r'],result['mfe'],result['mfe_r'],
     int(bool(result.get('tp1_done'))),int(bool(result.get('tp2_done'))),result['gross_pnl'],result['fee_paid'],net,
     result['close_reason'],filter_result,max(0.0,-net),max(0.0,net),now,row['id']))
  else:
   c.execute('''UPDATE reject_shadow_trades SET remaining_qty=?,current_stop=?,mae=?,mae_r=?,mfe=?,mfe_r=?,
    tp1_hit=?,tp2_hit=?,gross_simulated_pnl=?,simulated_fee=?,net_simulated_pnl=?,updated_at=? WHERE id=? AND status='OPEN' ''',
    (result['remaining_qty'],result['stop'],result['mae'],result['mae_r'],result['mfe'],result['mfe_r'],
     int(bool(result.get('tp1_done'))),int(bool(result.get('tp2_done'))),result['gross_pnl'],result['fee_paid'],result['net_pnl'],now,row['id']))

def _sync_entry_quality_v2_research_once(c):
 now=datetime.now().isoformat(timespec='seconds')
 c.execute('''UPDATE entry_quality_filter_v2_research AS v SET
  status=(SELECT p.status FROM positions p WHERE p.id=v.position_id),
  tp1_hit=COALESCE((SELECT p.tp1_done FROM positions p WHERE p.id=v.position_id),tp1_hit),
  tp2_hit=COALESCE((SELECT p.tp2_done FROM positions p WHERE p.id=v.position_id),tp2_hit),
  mae_r=COALESCE((SELECT p.mae_r FROM positions p WHERE p.id=v.position_id),mae_r),
  mfe_r=COALESCE((SELECT p.mfe_r FROM positions p WHERE p.id=v.position_id),mfe_r),
  close_reason=(SELECT p.close_reason FROM positions p WHERE p.id=v.position_id),
  final_net_pnl=CASE WHEN (SELECT p.status FROM positions p WHERE p.id=v.position_id)='CLOSED'
   THEN (SELECT p.pnl FROM positions p WHERE p.id=v.position_id) ELSE NULL END,
  closed_at=CASE WHEN (SELECT p.status FROM positions p WHERE p.id=v.position_id)='CLOSED'
   THEN (SELECT p.closed_at FROM positions p WHERE p.id=v.position_id) ELSE NULL END,
  updated_at=? WHERE EXISTS(SELECT 1 FROM positions p WHERE p.id=v.position_id)''',(now,))

def _sync_entry_quality_v3_research_once(c):
 now=datetime.now().isoformat(timespec='seconds')
 c.execute('''UPDATE entry_quality_filter_v3_research AS v SET
  status=(SELECT p.status FROM positions p WHERE p.id=v.position_id),
  tp1_hit=COALESCE((SELECT p.tp1_done FROM positions p WHERE p.id=v.position_id),tp1_hit),
  tp2_hit=COALESCE((SELECT p.tp2_done FROM positions p WHERE p.id=v.position_id),tp2_hit),
  mae=COALESCE((SELECT p.mae_r*ABS(p.entry-p.initial_stop) FROM positions p WHERE p.id=v.position_id),mae),
  mae_r=COALESCE((SELECT p.mae_r FROM positions p WHERE p.id=v.position_id),mae_r),
  mfe=COALESCE((SELECT p.mfe_r*ABS(p.entry-p.initial_stop) FROM positions p WHERE p.id=v.position_id),mfe),
  mfe_r=COALESCE((SELECT p.mfe_r FROM positions p WHERE p.id=v.position_id),mfe_r),
  close_reason=(SELECT p.close_reason FROM positions p WHERE p.id=v.position_id),
  gross_pnl=CASE WHEN (SELECT p.status FROM positions p WHERE p.id=v.position_id)='CLOSED'
   THEN COALESCE((SELECT p.pnl+p.fee_paid FROM positions p WHERE p.id=v.position_id),0) ELSE NULL END,
  fee=CASE WHEN (SELECT p.status FROM positions p WHERE p.id=v.position_id)='CLOSED'
   THEN COALESCE((SELECT p.fee_paid FROM positions p WHERE p.id=v.position_id),0) ELSE NULL END,
  final_net_pnl=CASE WHEN (SELECT p.status FROM positions p WHERE p.id=v.position_id)='CLOSED'
   THEN (SELECT p.pnl FROM positions p WHERE p.id=v.position_id) ELSE NULL END,
  closed_at=CASE WHEN (SELECT p.status FROM positions p WHERE p.id=v.position_id)='CLOSED'
   THEN (SELECT p.closed_at FROM positions p WHERE p.id=v.position_id) ELSE NULL END,
  updated_at=? WHERE EXISTS(SELECT 1 FROM positions p WHERE p.id=v.position_id)''',(now,))

def manage():
 events=_sqlite_write_with_retry(_manage_once)
 _sqlite_write_with_retry(_manage_reject_shadows_once)
 _sqlite_write_with_retry(_sync_entry_quality_v2_research_once)
 _sqlite_write_with_retry(_sync_entry_quality_v3_research_once)
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
  leverage=max(float(settings.get('leverage',1) or 1),1.0)
  position_notional=float(d.get('entry') or 0)*float(d.get('qty') or 0)
  d['position_notional_usd']=position_notional
  d['margin_usd']=position_notional/leverage
  d['leverage_used']=leverage
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

    previous_market=state.get('market') or {}
    analysis_syms=list(dict.fromkeys([*syms,'BTC_USDT','ETH_USDT']))
    results=await asyncio.gather(*(analyze_symbol(client,sym) for sym in analysis_syms))
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

    # Research-only one-shot candidate capture. This path never submits an order.
    await maybe_prepare_live_fee_test(client,fresh_market,previous_market,syms)

    # Open eligible PAPER trades only after all analysis tasks finish.
    for sym in syms:
     m=fresh_market.get(sym)
     if m:
      process_paper_signal(sym,m,fresh_market,previous_market,syms)

   manage()
   manage_strategy_lab()
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
  # Bot stop pauses new scans/entries, but existing PAPER positions keep TP/SL protection.
  if not state['panic']:
   try:
    c=db()
    open_syms=[x['symbol'] for x in c.execute("SELECT DISTINCT symbol FROM positions WHERE status='OPEN'").fetchall()]
    open_syms += [x['symbol'] for x in c.execute("SELECT DISTINCT symbol FROM reject_shadow_trades WHERE status='OPEN'").fetchall()]
    c.close()
    open_syms=list(dict.fromkeys(open_syms+strategy_lab_open_symbols()))
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
     manage_strategy_lab()
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
  'mexc_api_connection':state.get('public_api') or 'NOT_TESTED',
  'scanner':tasks['scanner'],
  'position_engine':tasks['position_engine'],
  'ghost_analyzer':tasks['ghost_analyzer'],
  'last_successful_market_data_timestamp':max(timestamps) if timestamps else None,
  'database_connectivity':database_ok,
  'uptime_seconds':max(0,int(time.monotonic()-SERVICE_STARTED_MONOTONIC)),
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

@app.get('/api/entry-filter-rejections')
def entry_filter_rejections():
 c=db(); rows=[dict(x) for x in c.execute('''SELECT r.*,s.status AS shadow_status,s.entry_price AS shadow_entry_price,
  s.exit_price AS shadow_exit_price,s.mae_r AS shadow_mae_r,s.mfe_r AS shadow_mfe_r,
  s.tp1_hit AS shadow_tp1_hit,s.tp2_hit AS shadow_tp2_hit,s.final_exit_reason AS shadow_exit_reason,
  s.net_simulated_pnl AS shadow_net_pnl,s.filter_result AS shadow_filter_result
  FROM entry_filter_rejections r LEFT JOIN reject_shadow_trades s ON s.rejection_id=r.id
  WHERE r.filter_version=? ORDER BY r.id DESC LIMIT 500''',(ENTRY_QUALITY_FILTER_VERSION,)).fetchall()]
 shadow_summary=dict(c.execute('''SELECT COUNT(*) AS total,
  COALESCE(SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END),0) AS open_count,
  COALESCE(SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END),0) AS closed_count,
  COALESCE(SUM(CASE WHEN filter_result='AVOIDED_LOSS' THEN 1 ELSE 0 END),0) AS avoided_loss_count,
  COALESCE(SUM(avoided_loss),0) AS avoided_loss_usd,
  COALESCE(SUM(CASE WHEN filter_result='MISSED_PROFIT' THEN 1 ELSE 0 END),0) AS missed_profit_count,
  COALESCE(SUM(missed_profit),0) AS missed_profit_usd,
  COALESCE(-SUM(CASE WHEN status='CLOSED' THEN net_simulated_pnl ELSE 0 END),0) AS net_filter_impact
  FROM reject_shadow_trades''').fetchone())
 shadow_rows=[dict(x) for x in c.execute('SELECT * FROM reject_shadow_trades').fetchall()]; c.close()
 reason_counts={}
 for row in rows:
  row['reasons']=json.loads(row.pop('reasons_json'))
  for reason in row['reasons']:reason_counts[reason]=reason_counts.get(reason,0)+1
  row.pop('score_snapshot_json',None); row.pop('market_regime_snapshot_json',None)
 return {'filter_version':ENTRY_QUALITY_FILTER_VERSION,'shadow_version':REJECT_SHADOW_VERSION,
  'total':len(rows),'reason_counts':reason_counts,'shadow_summary':shadow_summary,
  'shadow_comparison':{'all_symbols':_reject_shadow_metrics(shadow_rows),
   'aster_excluded':_reject_shadow_metrics([x for x in shadow_rows if x.get('symbol')!='ASTER_USDT'])},'rows':rows}

def _safe_rate(numerator,denominator):
 return float(numerator)/float(denominator)*100 if denominator else 0.0

def _profit_factor(pnls):
 gains=sum(x for x in pnls if x>0); losses=-sum(x for x in pnls if x<0)
 return gains/losses if losses else (None if gains else 0.0)

def _max_drawdown(pnls):
 equity=peak=drawdown=0.0
 for pnl in pnls:
  equity+=pnl; peak=max(peak,equity); drawdown=max(drawdown,peak-equity)
 return drawdown

def _position_metrics(rows):
 rows=[dict(x) for x in rows]; closed=[x for x in rows if x.get('status')=='CLOSED']
 pnls=[float(x.get('pnl') or 0) for x in closed]
 fees=sum(float(x.get('fee_paid') or 0) for x in closed)
 wins=sum(x>0 for x in pnls); losses=sum(x<0 for x in pnls)
 is_stop=lambda x:x.get('close_reason') in ('STOP','STOP_INFERRED')
 initial_stops=sum(is_stop(x) and not x.get('tp1_done') and not x.get('tp2_done') for x in closed)
 tp1=sum(bool(x.get('tp1_done')) for x in rows); tp2=sum(bool(x.get('tp2_done')) for x in rows)
 return {
  'count':len(rows),'open':len(rows)-len(closed),'closed':len(closed),'wins':wins,'losses':losses,
  'win_rate':_safe_rate(wins,len(closed)),'gross_pnl':sum(pnls)+fees,'fees':fees,'net_pnl':sum(pnls),
  'expectancy':sum(pnls)/len(closed) if closed else 0.0,'profit_factor':_profit_factor(pnls),
  'max_drawdown':_max_drawdown(pnls),'initial_stop_count':initial_stops,
  'initial_stop_rate':_safe_rate(initial_stops,len(closed)),'tp1_reached_count':tp1,
  'tp1_reached_rate':_safe_rate(tp1,len(rows)),'tp2_reached_count':tp2,
  'tp2_reached_rate':_safe_rate(tp2,len(rows)),
  'tp1_then_stop_count':sum(is_stop(x) and x.get('tp1_done') and not x.get('tp2_done') for x in closed),
  'tp2_protective_stop_count':sum(is_stop(x) and x.get('tp2_done') for x in closed),
  'average_mae_r':sum(float(x.get('mae_r') or 0) for x in rows)/len(rows) if rows else 0.0,
  'average_mfe_r':sum(float(x.get('mfe_r') or 0) for x in rows)/len(rows) if rows else 0.0,
  'net_pnl_per_trade':sum(pnls)/len(closed) if closed else 0.0,
 }

def _split_position_metrics(rows,key):
 values=sorted({str(x[key]) for x in rows})
 return {value:_position_metrics([x for x in rows if str(x[key])==value]) for value in values}

def _reject_shadow_metrics(rows):
 closed=[dict(x) for x in rows if x['status']=='CLOSED']
 net=[float(x.get('net_simulated_pnl') or 0) for x in closed]
 avoided=[x for x in closed if x.get('filter_result')=='AVOIDED_LOSS']
 missed=[x for x in closed if x.get('filter_result')=='MISSED_PROFIT']
 return {'closed':len(closed),'avoided_loss_count':len(avoided),
  'avoided_loss_usd':sum(float(x.get('avoided_loss') or 0) for x in avoided),
  'missed_profit_count':len(missed),'missed_profit_usd':sum(float(x.get('missed_profit') or 0) for x in missed),
  'net_filter_impact':-sum(net),'expectancy':sum(net)/len(closed) if closed else 0.0}

def _v2_research_metrics(rows):
 rows=[dict(x) for x in rows]; closed=[x for x in rows if x.get('status')=='CLOSED']
 pnls=[float(x.get('final_net_pnl') or 0) for x in closed]
 wins=sum(x>0 for x in pnls); losses=sum(x<0 for x in pnls)
 initial=sum(x.get('close_reason') in ('STOP','STOP_INFERRED') and not x.get('tp1_hit') and not x.get('tp2_hit') for x in closed)
 return {'total':len(rows),'open':len(rows)-len(closed),'closed':len(closed),'wins':wins,'losses':losses,
  'win_rate':_safe_rate(wins,len(closed)),'initial_stop_count':initial,
  'initial_stop_rate':_safe_rate(initial,len(closed)),
  'tp1_rate':_safe_rate(sum(bool(x.get('tp1_hit')) for x in closed),len(closed)),
  'tp2_rate':_safe_rate(sum(bool(x.get('tp2_hit')) for x in closed),len(closed)),
  'total_pnl':sum(pnls),'expectancy':sum(pnls)/len(closed) if closed else 0.0,
  'hypothetical_filter_impact':-sum(pnls)}

_v3_research_metrics=_v2_research_metrics

@app.get('/api/entry-quality-filter-v2-research')
def entry_quality_filter_v2_research():
 c=db()
 meta=c.execute('SELECT deployed_at FROM entry_quality_filter_v2_research_meta WHERE version=?',
  (ENTRY_QUALITY_FILTER_V2_RESEARCH_VERSION,)).fetchone()
 rows=[dict(x) for x in c.execute('SELECT * FROM entry_quality_filter_v2_research ORDER BY id').fetchall()]; c.close()
 for row in rows:row['matched_rules']=json.loads(row.pop('matched_rules_json'))
 groups={rule:_v2_research_metrics([x for x in rows if rule in x['matched_rules']]) for rule in ('RULE_A','RULE_B','RULE_C')}
 any_rule=[x for x in rows if x['matched_rules']]; no_rule=[x for x in rows if not x['matched_rules']]
 return {'version':ENTRY_QUALITY_FILTER_V2_RESEARCH_VERSION,
  'deployed_at':meta['deployed_at']+'Z' if meta else None,'rules':groups,
  'any_v2_rule':_v2_research_metrics(any_rule),'no_v2_rule':_v2_research_metrics(no_rule),
  'rows':list(reversed(rows[-200:]))}

@app.get('/api/entry-quality-filter-v3-research')
def entry_quality_filter_v3_research():
 c=db()
 meta=c.execute('SELECT deployed_at FROM entry_quality_filter_v3_research_meta WHERE version=?',
  (ENTRY_QUALITY_FILTER_V3_RESEARCH_VERSION,)).fetchone()
 rows=[dict(x) for x in c.execute('SELECT * FROM entry_quality_filter_v3_research ORDER BY id').fetchall()]; c.close()
 for row in rows:row['matched_rules']=json.loads(row.pop('matched_rules_json'))
 groups={rule:_v3_research_metrics([x for x in rows if rule in x['matched_rules']]) for rule in ('RULE_A','RULE_B','RULE_C')}
 any_rule=[x for x in rows if x['matched_rules']]; no_rule=[x for x in rows if not x['matched_rules']]
 return {'version':ENTRY_QUALITY_FILTER_V3_RESEARCH_VERSION,
  'deployed_at':meta['deployed_at']+'Z' if meta else None,'rules':groups,
  'any_v3_rule':_v3_research_metrics(any_rule),'no_v3_rule':_v3_research_metrics(no_rule),
  'rows':list(reversed(rows[-200:]))}

@app.get('/api/post-filter-analytics')
def post_filter_analytics():
 c=db()
 positions=[dict(x) for x in c.execute("SELECT * FROM positions WHERE mode='PAPER' ORDER BY opened_at,id").fetchall()]
 shadows=[dict(x) for x in c.execute('SELECT * FROM reject_shadow_trades').fetchall()]
 c.close()
 post=[x for x in positions if str(x.get('opened_at') or '')>ENTRY_QUALITY_FILTER_V1_DEPLOYED_AT]
 pre=[x for x in positions if str(x.get('opened_at') or '')<=ENTRY_QUALITY_FILTER_V1_DEPLOYED_AT]
 post_metrics=_position_metrics(post); pre_metrics=_position_metrics(pre)
 return {'cohort':POST_ENTRY_FILTER_COHORT,'deploy_timestamp':ENTRY_QUALITY_FILTER_V1_DEPLOYED_AT+'Z',
  'post_start_exclusive':ENTRY_QUALITY_FILTER_V1_DEPLOYED_AT+'Z','post':post_metrics,'pre':pre_metrics,
  'comparison':{'pre':pre_metrics,'post':post_metrics},
  'by_side':_split_position_metrics(post,'side'),'by_symbol':_split_position_metrics(post,'symbol'),
  'shadows':{'all_symbols':_reject_shadow_metrics(shadows),
   'aster_excluded':_reject_shadow_metrics([x for x in shadows if x.get('symbol')!='ASTER_USDT'])}}

@app.get('/api/history')
def history(period: str='all'):
 if period not in {'today','week','month','all'}: raise HTTPException(400,'Geçersiz geçmiş filtresi')
 rows=history_rows(period)
 pnl=sum(float(x.get('pnl') or 0) for x in rows); wins=sum(float(x.get('pnl') or 0)>0 for x in rows); losses=sum(float(x.get('pnl') or 0)<0 for x in rows)
 return {'rows':rows,'summary':{'pnl':pnl,'trades':len(rows),'wins':wins,'losses':losses,'win_rate':wins/len(rows)*100 if rows else 0}}

@app.get('/api/strategy-lab')
def strategy_lab():
 c=db()
 experiments=[dict(x) for x in c.execute('''SELECT * FROM strategy_lab_experiments
  ORDER BY id DESC LIMIT 200''').fetchall()]
 runs=[dict(x) for x in c.execute('''SELECT r.*,e.symbol,e.side,e.initial_stop,e.current_stopped_at
  FROM strategy_lab_runs r JOIN strategy_lab_experiments e ON e.id=r.experiment_id
  ORDER BY r.experiment_id DESC,r.id''').fetchall()]
 c.close()
 by_experiment={x['id']:{**x,'models':{}} for x in experiments}
 model_rows={name:[] for name in STRATEGY_LAB_MODELS}
 for run in runs:
  price=float((state.get('live_prices') or {}).get(run['symbol']) or (state.get('market',{}).get(run['symbol']) or {}).get('price') or run.get('close_price') or run['entry'])
  sign=1 if run['side']=='LONG' else -1
  run['net_pnl']=float(run['realized_pnl'] or 0)+(price-float(run['entry']))*sign*float(run['remaining_qty'] or 0)
  run['current_price']=price
  risk_distance=max(abs(float(run['entry'])-float(run['initial_stop'])),1e-12)
  run['current_r']=(price-float(run['entry']))*sign/risk_distance
  run['max_favorable_r']=float(run['mfe_r'] or 0)
  run['runner_trailing_stop_price']=float(run['stop']) if run['model']=='TRAILING_RUNNER' and run['tp2_done'] else None
  run['runner_trailing_stop_r']=((float(run['stop'])-float(run['entry']))*sign/risk_distance) if run['model']=='TRAILING_RUNNER' and run['tp2_done'] else None
  model_rows.setdefault(run['model'],[]).append(run)
  if run['experiment_id'] in by_experiment:
   by_experiment[run['experiment_id']]['models'][run['model']]=run
 summaries={}
 current_stop_total=sum(1 for x in experiments if x.get('current_stopped_at'))
 for model in STRATEGY_LAB_MODELS:
  items=model_rows.get(model,[]); closed=[x for x in items if x['status']=='CLOSED']
  wins=[x for x in closed if x['net_pnl']>0]; losses=[x for x in closed if x['net_pnl']<0]
  gross_profit=sum(x['net_pnl'] for x in wins); gross_loss=abs(sum(x['net_pnl'] for x in losses))
  recovered=sum(1 for x in items if x.get('first_post_stop_profit_at'))
  recovered_closed=sum(1 for x in closed if x.get('first_post_stop_profit_at') and x['net_pnl']>0)
  summaries[model]={
   'trades':len(items),'open':len(items)-len(closed),'closed':len(closed),
   'net_pnl':sum(x['net_pnl'] for x in items),'wins':len(wins),'losses':len(losses),
   'win_rate':len(wins)/len(closed)*100 if closed else 0,
   'profit_factor':gross_profit/gross_loss if gross_loss else (gross_profit if gross_profit else 0),
   'avg_pnl':sum(x['net_pnl'] for x in closed)/len(closed) if closed else 0,
   'avg_mae_r':sum(float(x['mae_r'] or 0) for x in items)/len(items) if items else 0,
   'avg_mfe_r':sum(float(x['mfe_r'] or 0) for x in items)/len(items) if items else 0,
   'current_stop_total':current_stop_total,'recovered_after_current_stop':recovered,
   'recovered_after_current_stop_pct':recovered/current_stop_total*100 if current_stop_total else 0,
   'profitable_close_after_current_stop':recovered_closed,
  }
 return {'model_version':STRATEGY_LAB_VERSION,'error':state.get('strategy_lab_error'),
  'current_stop_total':current_stop_total,'models':summaries,'experiments':list(by_experiment.values())}

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

class BulkCloseRequest(BaseModel):
 position_ids:list[int]

@app.get('/api/positions/open-summary')
def open_positions_summary():
 c=db()
 rows=[dict(x) for x in c.execute("SELECT id,symbol,side FROM positions WHERE status='OPEN' ORDER BY id").fetchall()]
 c.close()
 return {'count':len(rows),'positions':rows}

async def bulk_market_prices(symbols):
 async with httpx.AsyncClient(
  headers={'User-Agent':'MEXC-Futures-Paper-Trader/2.0'},
  limits=httpx.Limits(max_connections=4,max_keepalive_connections=4)
 ) as client:
  all_prices=await fetch_futures_prices(client)
 return {symbol:all_prices[symbol] for symbol in symbols if symbol in all_prices and float(all_prices[symbol])>0}

async def _bulk_close_positions(request,stop_bot=False):
 if stop_bot:
  state.update(running=False,entry_paused=True)
 ids=list(dict.fromkeys(int(x) for x in request.position_ids if int(x)>0))
 if not ids:
  return {'ok':True,'requested':0,'closed':[],'already_closed':[],'failed':[],'bot_stopped':stop_bot}
 placeholders=','.join('?' for _ in ids)
 c=db()
 found=[dict(x) for x in c.execute(f'SELECT * FROM positions WHERE id IN ({placeholders})',ids).fetchall()]
 c.close()
 by_id={x['id']:x for x in found}
 already=[{'id':position_id,'symbol':by_id.get(position_id,{}).get('symbol'),'reason':'ALREADY_CLOSED_OR_NOT_FOUND'} for position_id in ids if position_id not in by_id or by_id[position_id]['status']!='OPEN']
 open_rows=[by_id[position_id] for position_id in ids if position_id in by_id and by_id[position_id]['status']=='OPEN']
 symbols=list(dict.fromkeys(x['symbol'] for x in open_rows))
 try:
  prices=await bulk_market_prices(symbols) if symbols else {}
 except Exception as e:
  prices={}
  market_error=f'{type(e).__name__}: public fiyat alınamadı'
 else:
  market_error=None
 failed=[{'id':x['id'],'symbol':x['symbol'],'reason':market_error or 'Güncel public piyasa fiyatı bulunamadı'} for x in open_rows if x['symbol'] not in prices]
 closable=[x for x in open_rows if x['symbol'] in prices]
 fee_rate=max(float(settings.get('paper_fee_rate',0.0008) or 0),0.0)
 now=datetime.now().isoformat(timespec='seconds')
 def write(c):
  closed=[]; raced=[]
  for selected in closable:
   pos=c.execute("SELECT * FROM positions WHERE id=? AND status='OPEN'",(selected['id'],)).fetchone()
   if not pos:
    raced.append({'id':selected['id'],'symbol':selected['symbol'],'reason':'ALREADY_CLOSED'})
    continue
   price=float(prices[selected['symbol']]); sign=1 if pos['side']=='LONG' else -1
   rem=float(pos['remaining_qty'] or 0); realized=float(pos['pnl'] or 0); fee_paid=float(pos['fee_paid'] or 0)
   realized+=(price-float(pos['entry']))*sign*rem
   exit_fee=price*rem*fee_rate; realized-=exit_fee; fee_paid+=exit_fee
   cur=c.execute("""UPDATE positions SET status='CLOSED',remaining_qty=0,closed_at=?,pnl=?,close_price=?,
    fee_paid=?,close_reason='EMERGENCY_CLOSE' WHERE id=? AND status='OPEN'""",
    (now,realized,price,fee_paid,pos['id']))
   if cur.rowcount==1:
    closed.append({'id':pos['id'],'symbol':pos['symbol'],'close_price':price,'pnl':realized})
   else:
    raced.append({'id':pos['id'],'symbol':pos['symbol'],'reason':'ALREADY_CLOSED'})
  return closed,raced
 closed,raced=_sqlite_write_with_retry(write) if closable else ([],[])
 already.extend(raced)
 for item in closed:
  log(f"{item['symbol']} toplu PAPER kapanış @ {item['close_price']} | PnL ${item['pnl']:.2f}",'TRADE')
 if failed:
  log(f"Toplu PAPER kapanış kısmi kaldı · {len(failed)} pozisyon için güncel fiyat alınamadı",'WARN')
 return {'ok':not failed,'requested':len(ids),'closed':closed,'already_closed':already,'failed':failed,'bot_stopped':stop_bot}

@app.post('/api/positions/close-all')
async def close_all_positions(request:BulkCloseRequest):
 return await _bulk_close_positions(request,stop_bot=False)

@app.post('/api/positions/close-all-and-stop')
async def close_all_positions_and_stop(request:BulkCloseRequest):
 return await _bulk_close_positions(request,stop_bot=True)

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
 state.update(running=True,panic=False,entry_paused=False,error=None)
 log('Motor başlatıldı · ilk tarama arka planda başladı')
 if not scan_lock.locked():
  asyncio.create_task(scan_once())
 return {'ok':True,'message':'Motor çalışıyor; ilk tarama arka planda başladı.'}
@app.post('/api/stop')
def stop():state.update(running=False,entry_paused=True);log('Bot durduruldu · yeni PAPER girişleri kapalı');return {'ok':True}
@app.post('/api/panic')
def panic():state.update(running=False,panic=True,entry_paused=True);log('ACİL DURDURMA etkin','WARN');return {'ok':True}
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
 if sys.platform!='darwin':
  raise HTTPException(503,'Linux credential yönetimi yalnız systemd EnvironmentFile üzerinden kullanılabilir.')
 try:
  if a.api_key.strip(): keychain_set('api_key',a.api_key.strip())
  if a.api_secret.strip(): keychain_set('api_secret',a.api_secret.strip())
  state['api_saved']=credentials_available(); log('MEXC API bilgileri macOS Keychain içine kaydedildi'); return {'ok':True,'saved':state['api_saved']}
 except Exception as e: raise HTTPException(500,f'Keychain kayıt hatası: {e}')
@app.post('/api/private-test')
async def private_test():
 key=credential_get('api_key'); secret=credential_get('api_secret')
 if not key or not secret: raise HTTPException(503,'Private API credential yapılandırılmamış; PAPER modu normal çalışmaya devam eder.')
 ts=str(int(time.time()*1000)); path='/api/v1/private/account/assets'; sig=hmac.new(secret.encode(),(key+ts).encode(),hashlib.sha256).hexdigest(); headers={'ApiKey':key,'Request-Time':ts,'Signature':sig,'Content-Type':'application/json'}
 try:
  async with httpx.AsyncClient() as client: r=await mexc_get(client,'https://api.mexc.com'+path,headers=headers,timeout=15)
  j=r.json() if 'json' in r.headers.get('content-type','') else {'message':r.text[:300]}
  if r.status_code==200 and j.get('success') is True: state['private_api']='BAĞLI'; log('MEXC özel API bağlantısı doğrulandı'); return {'ok':True,'message':'API Key/Secret doğrulandı. Hesap bağlantısı başarılı.'}
  detail=redact_credentials(j.get('message') or j.get('msg') or j.get('code') or str(j),key,secret)
  state['private_api']='HATA'; raise HTTPException(400,f"MEXC yanıtı: HTTP {r.status_code} · {detail}")
 except HTTPException: raise
 except Exception as e: state['private_api']='HATA'; raise HTTPException(502,redact_credentials(e,key,secret))

def _require_local(request):
 host=request.client.host if request.client else ''
 if host not in ('127.0.0.1','::1','localhost'):
  raise HTTPException(403,'LIVE_FEE_TEST yalnız localhost üzerinden yönetilebilir.')

@app.get('/api/live-fee-test/status')
def live_fee_test_status():
 c=db(); row=c.execute('SELECT * FROM live_fee_tests ORDER BY id DESC LIMIT 1').fetchone(); c.close()
 if not row:return {'status':'DISARMED'}
 data=dict(row)
 for field in ('entry_fill_json','exit_fill_json','result_json'):
  data[field[:-5] if field.endswith('_json') else field]=json.loads(data[field]) if data.get(field) else None
  data.pop(field,None)
 data['confirmation_required']=f"ONAY LIVE_FEE_TEST {data['id']} {data.get('symbol') or ''} {data.get('side') or ''}" if data['status']=='PREPARED' else None
 data['position_check']=state.get('live_fee_position_check')
 return data

@app.get('/api/live-fee-test/audit')
async def live_fee_test_audit(request:Request):
 _require_local(request)
 latest=_live_fee_row(('ARMED','PREPARED','STALE','EXPIRED','PREFLIGHT_FAILED','OPEN_ALARM','COMPLETED','CANCELLED'))
 symbol=(latest or {}).get('symbol')
 if not symbol:raise HTTPException(409,'Audit için sembollü LIVE_FEE_TEST kaydı yok.')
 try:
  async with httpx.AsyncClient(headers={'User-Agent':'MEXC-Live-Fee-Read-Only-Audit/1.0'}) as client:
   positions=_active_contract_positions(
    await mexc_private_request(client,'GET','/api/v1/private/position/open_positions') or [])
   symbol_positions=[x for x in positions if str(x.get('symbol'))==symbol]
   orders=await mexc_private_request(client,'GET',f'/api/v1/private/order/list/open_orders/{symbol}') or []
  result={'symbol':symbol,'checked_at':datetime.now().isoformat(timespec='seconds'),
   'open_position_count':len(symbol_positions),'open_order_count':len(orders),
   'positions':[{'position_id':str(x.get('positionId')),'position_type':x.get('positionType'),
    'hold_vol':x.get('holdVol')} for x in symbol_positions]}
  state['live_fee_audit']=result
  return result
 except Exception as e:
  raise HTTPException(502,redact_credentials(e,credential_get('api_key'),credential_get('api_secret')))

@app.post('/api/live-fee-test/arm')
def arm_live_fee_test(request:Request):
 _require_local(request)
 c=db(); completed=c.execute("SELECT 1 FROM live_fee_tests WHERE status='COMPLETED' LIMIT 1").fetchone(); c.close()
 if completed:raise HTTPException(409,'Tek seferlik LIVE_FEE_TEST daha önce tamamlandı; yeniden kurulamaz.')
 if _live_fee_row(('ARMED','PREPARED','EXECUTING')):
  raise HTTPException(409,'Aktif veya alarm durumunda bir LIVE_FEE_TEST zaten var.')
 alarm=_live_fee_row(('OPEN_ALARM',)); audit=state.get('live_fee_audit') or {}
 audit_at=datetime.fromisoformat(audit['checked_at']) if audit.get('checked_at') else None
 audit_clear=bool(alarm and audit_at and datetime.now()-audit_at<=timedelta(minutes=2)
  and audit.get('symbol')==alarm.get('symbol') and audit.get('open_position_count')==0
  and audit.get('open_order_count')==0)
 if alarm and not audit_clear:
  raise HTTPException(409,'OPEN_ALARM için aynı sembolde taze ve temiz read-only audit gerekli.')
 now=datetime.now().isoformat(timespec='seconds')
 def write(c):
  cur=c.execute("INSERT INTO live_fee_tests(status,armed_at) VALUES('ARMED',?)",(now,)); return cur.lastrowid
 test_id=_sqlite_write_with_retry(write)
 log('LIVE_FEE_TEST yalnız aday beklemek üzere ARMED; gerçek emir DEVRE DIŞI','WARN')
 return {'id':test_id,'status':'ARMED','orders_enabled':False}

@app.post('/api/live-fee-test/cancel')
def cancel_live_fee_test(request:Request):
 _require_local(request)
 def write(c):
  cur=c.execute("""UPDATE live_fee_tests SET status='CANCELLED',completed_at=?,error=NULL
   WHERE id=(SELECT id FROM live_fee_tests WHERE status IN ('ARMED','PREPARED') ORDER BY id DESC LIMIT 1)
   AND status IN ('ARMED','PREPARED')""",(datetime.now().isoformat(timespec='seconds'),))
  return cur.rowcount
 if _sqlite_write_with_retry(write)!=1:
  raise HTTPException(409,'İptal edilebilir ARMED/PREPARED LIVE_FEE_TEST yok; çalışan veya alarmdaki test iptal edilemez.')
 log('LIVE_FEE_TEST kullanıcı tarafından iptal edildi; gerçek emir DEVRE DIŞI','WARN')
 return {'status':'CANCELLED','orders_enabled':False}

class LiveFeeExecute(BaseModel): confirmation:str

@app.post('/api/live-fee-test/execute')
async def execute_live_fee_test(body:LiveFeeExecute,request:Request):
 _require_local(request)
 async with live_fee_test_lock:
  candidate=_live_fee_row(('PREPARED',))
  if not candidate:raise HTTPException(409,'Onaya hazır LIVE_FEE_TEST adayı yok.')
  expected=f"ONAY LIVE_FEE_TEST {candidate['id']} {candidate['symbol']} {candidate['side']}"
  if not hmac.compare_digest(body.confirmation,expected):raise HTTPException(403,'Açık onay metni eşleşmiyor.')
  if datetime.now()>datetime.fromisoformat(candidate['expires_at']):
   _live_fee_update(candidate['id'],status='EXPIRED',error='Aday onay süresi doldu')
   raise HTTPException(409,'Adayın onay süresi doldu; hiçbir emir gönderilmedi.')
  market=(state.get('market') or {}).get(candidate['symbol']) or {}
  if float(((market.get('15m') or {}).get('price') or market.get('price') or 0))<=0:
   _live_fee_update(candidate['id'],status='STALE',error='Sembol için güncel referans fiyatı yok')
   raise HTTPException(409,'Sembol için güncel referans fiyatı yok; hiçbir emir gönderilmedi.')
  _live_fee_update(candidate['id'],status='EXECUTING',execution_started_at=datetime.now().isoformat(timespec='seconds'))
  entry_submitted=False; entry_order_id=None
  try:
   async with httpx.AsyncClient(headers={'User-Agent':'MEXC-Futures-Live-Fee-Test/1.0'}) as client:
    position_mode=int(await mexc_private_request(client,'GET','/api/v1/private/position/position_mode') or 0)
    if position_mode not in (1,2):raise RuntimeError(f'Desteklenmeyen MEXC position mode: {position_mode}')
    symbol=candidate['symbol']; side=candidate['side']; contracts=float(candidate['contracts']); leverage=int(candidate['leverage'])
    open_positions=_active_contract_positions(
     await mexc_private_request(client,'GET','/api/v1/private/position/open_positions') or [])
    if any(str(x.get('symbol'))==symbol for x in open_positions):
     raise RuntimeError(f'{symbol} üzerinde açık gerçek LONG/SHORT Futures pozisyonu var')
    external=_live_fee_external_oid(candidate['id'],'e')
    entry_payload={'symbol':symbol,'price':0,'vol':contracts,'leverage':leverage,
     'side':1 if side=='LONG' else 3,'type':5,'openType':1,'positionMode':position_mode,'externalOid':external}
    entry_submitted=True
    entry_order_id=str(await mexc_private_request(client,'POST','/api/v1/private/order/submit',body=entry_payload))
    await _order_details(client,entry_order_id)
    entry_fill=await _order_fills(client,entry_order_id,float(candidate['contract_size']))
    position=await _live_fee_position_after_entry(client,symbol,side)
    position_id=str(position['positionId']); position_contracts=float(position.get('holdVol') or 0)
    executed_contracts=float(entry_fill['executed_contracts'])
    if position_contracts<=0 or position_contracts-executed_contracts>1e-12:
     raise RuntimeError('KRİTİK: Gerçek pozisyon miktarı doğrulanmış entry fill miktarını aşıyor')
    close_contracts=min(position_contracts,executed_contracts)
    entry_fill['position_contracts']=position_contracts
    entry_fill['close_contracts']=close_contracts
    _live_fee_update(candidate['id'],entry_order_id=entry_order_id,entry_fill_json=json.dumps(entry_fill,separators=(',',':')))
    exit_reference=float(((state.get('market') or {}).get(symbol) or {}).get('price') or entry_fill['average_fill_price'])
    exit_payload={'symbol':symbol,'price':0,'vol':close_contracts,'leverage':leverage,
     'side':4 if side=='LONG' else 2,'type':5,'openType':1,'positionMode':position_mode,
     'positionId':position_id,'externalOid':_live_fee_external_oid(candidate['id'],'x')}
    if position_mode==2:exit_payload['reduceOnly']=True
    exit_order_id=str(await mexc_private_request(client,'POST','/api/v1/private/order/submit',body=exit_payload))
    await _order_details(client,exit_order_id)
    exit_fill=await _order_fills(client,exit_order_id,float(candidate['contract_size']))
    if float(exit_fill['executed_contracts'])-close_contracts>1e-12:
     raise RuntimeError('KRİTİK: Exit fill miktarı doğrulanmış entry miktarını aştı')
    await _assert_live_fee_position_closed(client,symbol,position_id)
    funding_data=await mexc_private_request(client,'GET','/api/v1/private/position/funding_records',params={
     'symbol':symbol,'position_id':position_id,'page_num':1,'page_size':100})
    funding_rows=(funding_data or {}).get('resultList') or []; funding=sum(float(x.get('funding') or 0) for x in funding_rows)
    sign=1 if side=='LONG' else -1
    gross=(exit_fill['average_fill_price']-entry_fill['average_fill_price'])*sign*entry_fill['executed_qty']
    actual_fee=entry_fill['actual_fee']+exit_fill['actual_fee']; net=gross+funding-actual_fee
    paper_fee=(entry_fill['notional']+exit_fill['notional'])*float(settings.get('paper_fee_rate',.0008))
    reference=float(candidate['reference_price']); entry_slippage=(entry_fill['average_fill_price']-reference)*sign/(reference or 1)*10000
    exit_slippage=(exit_reference-exit_fill['average_fill_price'])*sign/(exit_reference or 1)*10000
    result={'entry':entry_fill,'exit':exit_fill,'entry_slippage_bps':entry_slippage,'exit_slippage_bps':exit_slippage,
     'gross_price_pnl':gross,'funding':funding,'net_realized_pnl':net,'paper_fee':paper_fee,
     'actual_fee':actual_fee,'actual_minus_paper_fee':actual_fee-paper_fee,'position_id':position_id}
    _live_fee_update(candidate['id'],status='COMPLETED',exit_order_id=exit_order_id,
     exit_fill_json=json.dumps(exit_fill,separators=(',',':')),funding=funding,
     result_json=json.dumps(result,separators=(',',':')),completed_at=datetime.now().isoformat(timespec='seconds'),error=None)
    log(f'LIVE_FEE_TEST tamamlandı: {symbol} · tek kullanımlık mod DISARMED','WARN')
    return {'id':candidate['id'],'status':'COMPLETED','result':result}
  except Exception as e:
   key=credential_get('api_key'); secret=credential_get('api_secret'); safe=redact_credentials(e,key,secret)
   alarm=entry_submitted
   _live_fee_update(candidate['id'],status='OPEN_ALARM' if alarm else 'PREFLIGHT_FAILED',
    entry_order_id=entry_order_id,error=safe,completed_at=datetime.now().isoformat(timespec='seconds'))
   log(('KRİTİK LIVE_FEE_TEST AÇIK POZİSYON RİSKİ: ' if alarm else 'LIVE_FEE_TEST emir öncesi durdu: ')+safe,'ERROR')
   raise HTTPException(502,('AÇIK LIVE POZİSYON ALARMI: ' if alarm else 'Emir gönderilmedi: ')+safe)
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
