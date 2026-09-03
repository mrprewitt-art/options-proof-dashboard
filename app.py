import json, os
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Any
import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

load_dotenv()
DATABASE_URL=os.getenv('DATABASE_URL','').strip(); TRADE_INGEST_SECRET=os.getenv('TRADE_INGEST_SECRET','').strip()
BRAND_NAME=os.getenv('BRAND_NAME','Private Options Alerts').strip(); BOT_USERNAME=os.getenv('BOT_USERNAME','').strip().lstrip('@'); MONTHLY_STARS=int(os.getenv('MONTHLY_STARS','500'))
if not DATABASE_URL: raise RuntimeError('DATABASE_URL is required')
app=FastAPI(title='Public Options Proof Dashboard'); templates=Jinja2Templates(directory=str(Path(__file__).parent/'templates'))
SCHEMA="""
CREATE TABLE IF NOT EXISTS closed_trades (
 id BIGSERIAL PRIMARY KEY, source_trade_id TEXT UNIQUE, ticker TEXT NOT NULL, option_type TEXT NOT NULL,
 strike TEXT, expiry TEXT, entry_price DOUBLE PRECISION NOT NULL, exit_price DOUBLE PRECISION NOT NULL,
 pnl_pct DOUBLE PRECISION NOT NULL, opened_at TIMESTAMPTZ, closed_at TIMESTAMPTZ NOT NULL, notes TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS idx_closed_trades_closed ON closed_trades(closed_at DESC);
CREATE TABLE IF NOT EXISTS alert_events (
 id BIGSERIAL PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, source TEXT NOT NULL, alert_id BIGINT,
 event_type TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT, strategy TEXT, signal_time TIMESTAMPTZ,
 observed_at TIMESTAMPTZ NOT NULL, spot DOUBLE PRECISION, message TEXT NOT NULL, details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS idx_alert_events_observed ON alert_events(observed_at DESC);
"""
@contextmanager
def connect():
    with psycopg.connect(DATABASE_URL,autocommit=True) as conn: yield conn
with connect() as c:c.execute(SCHEMA)

class ClosedTradeIn(BaseModel):
    ticker:str; option_type:str; strike:Optional[str]=None; expiry:Optional[str]=None
    entry_price:float=Field(ge=0); exit_price:float=Field(ge=0); pnl_pct:float
    opened_at:Optional[str]=None; closed_at:str; source_trade_id:Optional[str]=None; notes:Optional[str]=None
class AlertEventIn(BaseModel):
    event_id:str; source:str='SNIPER_PRODUCTION_V3'; alert_id:Optional[int]=None; event_type:str; ticker:str
    side:Optional[str]=None; strategy:Optional[str]=None; signal_time:Optional[str]=None; observed_at:str
    spot:Optional[float]=None; message:str; details:dict[str,Any]={}

def _stats(conn):
    row=conn.execute("""SELECT COUNT(*),COUNT(*) FILTER(WHERE pnl_pct>0),COUNT(*) FILTER(WHERE pnl_pct<=0),COALESCE(AVG(pnl_pct),0),COALESCE(MAX(pnl_pct),0)
                        FROM closed_trades WHERE source_trade_id LIKE 'sniper-prod-v3:%'""").fetchone()
    total=int(row[0] or 0); wins=int(row[1] or 0)
    return {'total_trades':total,'wins':wins,'losses_or_flat':int(row[2] or 0),'win_rate':round(wins/total*100,2) if total else 0.0,'avg_pnl_pct':round(float(row[3] or 0),2),'best_pnl_pct':round(float(row[4] or 0),2)}
def dashboard_data(limit=40):
    with connect() as c:
        stats=_stats(c)
        trades=c.execute("""SELECT ticker,option_type,strike,expiry,entry_price,exit_price,pnl_pct,opened_at,closed_at,notes
                           FROM closed_trades WHERE source_trade_id LIKE 'sniper-prod-v3:%' ORDER BY closed_at DESC,id DESC LIMIT %s""",(limit,)).fetchall()
        events=c.execute("""SELECT event_type,ticker,side,strategy,signal_time,observed_at,spot,message
                           FROM alert_events WHERE source='SNIPER_PRODUCTION_V3' ORDER BY observed_at DESC,id DESC LIMIT %s""",(limit,)).fetchall()
    tcols=['ticker','option_type','strike','expiry','entry_price','exit_price','pnl_pct','opened_at','closed_at','notes']
    ecols=['event_type','ticker','side','strategy','signal_time','observed_at','spot','message']
    return {'option_stats':stats,'recent_options':[dict(zip(tcols,r)) for r in trades],'recent_events':[dict(zip(ecols,r)) for r in events]}
@app.get('/',response_class=HTMLResponse)
async def home(request:Request):return templates.TemplateResponse(request=request,name='index.html',context={'brand':BRAND_NAME,'bot_username':BOT_USERNAME,'stars':MONTHLY_STARS})
@app.get('/api/dashboard')
async def get_dashboard():return dashboard_data()

def _auth(secret):
    if not TRADE_INGEST_SECRET: raise HTTPException(503,'Trade ingestion is not configured')
    if secret!=TRADE_INGEST_SECRET: raise HTTPException(401,'Invalid trade secret')
@app.post('/api/trades/closed')
async def closed_trade(trade:ClosedTradeIn,x_trade_secret:Optional[str]=Header(default=None)):
    _auth(x_trade_secret); typ=trade.option_type.strip().upper()
    if typ not in {'CALL','PUT'}: raise HTTPException(400,'option_type must be CALL or PUT')
    with connect() as c:
        row=c.execute("""INSERT INTO closed_trades(source_trade_id,ticker,option_type,strike,expiry,entry_price,exit_price,pnl_pct,opened_at,closed_at,notes)
                         VALUES(%s,%s,%s,%s,%s,%s,%s,%s,NULLIF(%s,'')::timestamptz,%s::timestamptz,%s)
                         ON CONFLICT(source_trade_id) DO NOTHING RETURNING id""",
                      (trade.source_trade_id,trade.ticker.strip().upper(),typ,trade.strike,trade.expiry,float(trade.entry_price),float(trade.exit_price),float(trade.pnl_pct),trade.opened_at or '',trade.closed_at,trade.notes)).fetchone()
    return {'ok':True,'created':bool(row)}
@app.post('/api/alerts/events')
async def alert_event(ev:AlertEventIn,x_trade_secret:Optional[str]=Header(default=None)):
    _auth(x_trade_secret)
    allowed={'ENTRY','TARGET','DEVELOPING','VALIDATED_HOLD','RUNNER_HOLD','EXTENDED_HOLD','EXIT_WATCH','SELL','SYSTEM_TEST'}
    et=ev.event_type.strip().upper()
    if et not in allowed: raise HTTPException(400,'unsupported event_type')
    with connect() as c:
        row=c.execute("""INSERT INTO alert_events(event_id,source,alert_id,event_type,ticker,side,strategy,signal_time,observed_at,spot,message,details_json)
                         VALUES(%s,%s,%s,%s,%s,%s,%s,NULLIF(%s,'')::timestamptz,%s::timestamptz,%s,%s,%s::jsonb)
                         ON CONFLICT(event_id) DO NOTHING RETURNING id""",
                      (ev.event_id,ev.source,ev.alert_id,et,ev.ticker.strip().upper(),(ev.side or '').upper(),ev.strategy or '',ev.signal_time or '',ev.observed_at,ev.spot,ev.message,json.dumps(ev.details or {}))).fetchone()
    return {'ok':True,'created':bool(row)}
@app.get('/health')
async def health():
    with connect() as c:c.execute('SELECT 1').fetchone()
    return {'ok':True,'database':'neon-postgres','alert_events':True,'production_feed':'SNIPER_PRODUCTION_V3'}
