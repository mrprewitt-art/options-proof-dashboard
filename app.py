import os
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TRADE_INGEST_SECRET = os.getenv("TRADE_INGEST_SECRET", "").strip()
BRAND_NAME = os.getenv("BRAND_NAME", "Private Options Alerts").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
MONTHLY_STARS = int(os.getenv("MONTHLY_STARS", "500"))
SOURCE_PREFIX = "sniper-prod-v3:"

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")

app = FastAPI(title="Sniper Closed Results")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS closed_trades (
    id BIGSERIAL PRIMARY KEY,
    source_trade_id TEXT UNIQUE,
    ticker TEXT NOT NULL,
    option_type TEXT NOT NULL,
    strike TEXT,
    expiry TEXT,
    entry_price DOUBLE PRECISION NOT NULL,
    tp1_price DOUBLE PRECISION,
    tp1_return_pct DOUBLE PRECISION,
    tp1_size_pct DOUBLE PRECISION,
    tp1_at TIMESTAMPTZ,
    tp2_price DOUBLE PRECISION,
    tp2_return_pct DOUBLE PRECISION,
    tp2_size_pct DOUBLE PRECISION,
    tp2_at TIMESTAMPTZ,
    exit_price DOUBLE PRECISION NOT NULL,
    exit_return_pct DOUBLE PRECISION,
    pnl_pct DOUBLE PRECISION NOT NULL,
    opened_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ NOT NULL,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_closed_trades_closed ON closed_trades(closed_at DESC);
"""

MIGRATIONS = [
    "ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS tp1_price DOUBLE PRECISION",
    "ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS tp1_return_pct DOUBLE PRECISION",
    "ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS tp1_size_pct DOUBLE PRECISION",
    "ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS tp1_at TIMESTAMPTZ",
    "ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS tp2_price DOUBLE PRECISION",
    "ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS tp2_return_pct DOUBLE PRECISION",
    "ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS tp2_size_pct DOUBLE PRECISION",
    "ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS tp2_at TIMESTAMPTZ",
    "ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS exit_return_pct DOUBLE PRECISION",
]

@contextmanager
def connect():
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        yield conn

with connect() as conn:
    conn.execute(SCHEMA)
    for stmt in MIGRATIONS:
        conn.execute(stmt)
    # Public site stays results-only. No live lifecycle/event table.
    conn.execute("DROP TABLE IF EXISTS alert_events")

class ClosedTradeIn(BaseModel):
    ticker: str
    option_type: str
    strike: Optional[str] = None
    expiry: Optional[str] = None
    entry_price: float = Field(ge=0)
    tp1_price: Optional[float] = Field(default=None, ge=0)
    tp1_return_pct: Optional[float] = None
    tp1_size_pct: Optional[float] = Field(default=None, ge=0, le=100)
    tp1_at: Optional[str] = None
    tp2_price: Optional[float] = Field(default=None, ge=0)
    tp2_return_pct: Optional[float] = None
    tp2_size_pct: Optional[float] = Field(default=None, ge=0, le=100)
    tp2_at: Optional[str] = None
    exit_price: float = Field(ge=0)
    exit_return_pct: Optional[float] = None
    # pnl_pct is the total realized trade return after any partial TPs.
    # For legacy/no-TP trades it is identical to the final exit return.
    pnl_pct: float
    opened_at: Optional[str] = None
    closed_at: str
    source_trade_id: str
    notes: Optional[str] = None


def _stats(conn) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE pnl_pct > 0),
               COUNT(*) FILTER (WHERE pnl_pct <= 0),
               COALESCE(AVG(pnl_pct), 0),
               COALESCE(MAX(pnl_pct), 0)
        FROM closed_trades
        WHERE source_trade_id LIKE %s
        """,
        (SOURCE_PREFIX + "%",),
    ).fetchone()
    total = int(row[0] or 0)
    wins = int(row[1] or 0)
    return {
        "total_trades": total,
        "wins": wins,
        "losses_or_flat": int(row[2] or 0),
        "win_rate": round(wins / total * 100, 2) if total else 0.0,
        "avg_pnl_pct": round(float(row[3] or 0), 2),
        "best_pnl_pct": round(float(row[4] or 0), 2),
    }


def dashboard_data(limit: int = 50) -> dict:
    with connect() as conn:
        stats = _stats(conn)
        rows = conn.execute(
            """
            SELECT ticker, option_type, strike, expiry, entry_price,
                   tp1_price, tp1_return_pct, tp1_size_pct, tp1_at,
                   tp2_price, tp2_return_pct, tp2_size_pct, tp2_at,
                   exit_price, COALESCE(exit_return_pct, pnl_pct) AS exit_return_pct,
                   pnl_pct, opened_at, closed_at, notes
            FROM closed_trades
            WHERE source_trade_id LIKE %s
            ORDER BY closed_at DESC, id DESC
            LIMIT %s
            """,
            (SOURCE_PREFIX + "%", limit),
        ).fetchall()
    cols = [
        "ticker", "option_type", "strike", "expiry", "entry_price",
        "tp1_price", "tp1_return_pct", "tp1_size_pct", "tp1_at",
        "tp2_price", "tp2_return_pct", "tp2_size_pct", "tp2_at",
        "exit_price", "exit_return_pct", "pnl_pct", "opened_at", "closed_at", "notes",
    ]
    return {"option_stats": stats, "recent_options": [dict(zip(cols, r)) for r in rows]}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"brand": BRAND_NAME, "bot_username": BOT_USERNAME, "stars": MONTHLY_STARS},
    )


@app.get("/api/dashboard")
async def get_dashboard():
    return dashboard_data()


def _auth(secret: Optional[str]):
    if not TRADE_INGEST_SECRET:
        raise HTTPException(503, "Trade ingestion is not configured")
    if secret != TRADE_INGEST_SECRET:
        raise HTTPException(401, "Invalid trade secret")


@app.post("/api/trades/closed")
async def closed_trade(trade: ClosedTradeIn, x_trade_secret: Optional[str] = Header(default=None)):
    _auth(x_trade_secret)
    source_id = trade.source_trade_id.strip()
    if not source_id.startswith(SOURCE_PREFIX):
        raise HTTPException(400, "Only Sniper Production closed trades are accepted")
    option_type = trade.option_type.strip().upper()
    if option_type not in {"CALL", "PUT"}:
        raise HTTPException(400, "option_type must be CALL or PUT")
    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO closed_trades(
                source_trade_id, ticker, option_type, strike, expiry,
                entry_price,
                tp1_price, tp1_return_pct, tp1_size_pct, tp1_at,
                tp2_price, tp2_return_pct, tp2_size_pct, tp2_at,
                exit_price, exit_return_pct, pnl_pct,
                opened_at, closed_at, notes
            )
            VALUES(
                %s,%s,%s,%s,%s,%s,
                %s,%s,%s,NULLIF(%s,'')::timestamptz,
                %s,%s,%s,NULLIF(%s,'')::timestamptz,
                %s,%s,%s,
                NULLIF(%s,'')::timestamptz,%s::timestamptz,%s
            )
            ON CONFLICT(source_trade_id) DO NOTHING
            RETURNING id
            """,
            (
                source_id, trade.ticker.strip().upper(), option_type, trade.strike, trade.expiry,
                float(trade.entry_price),
                trade.tp1_price, trade.tp1_return_pct, trade.tp1_size_pct, trade.tp1_at or "",
                trade.tp2_price, trade.tp2_return_pct, trade.tp2_size_pct, trade.tp2_at or "",
                float(trade.exit_price), trade.exit_return_pct, float(trade.pnl_pct),
                trade.opened_at or "", trade.closed_at, trade.notes,
            ),
        ).fetchone()
    return {"ok": True, "created": bool(row)}


@app.get("/health")
async def health():
    with connect() as conn:
        conn.execute("SELECT 1").fetchone()
    return {
        "ok": True,
        "database": "neon-postgres",
        "results_only": True,
        "tp_ready": True,
        "production_feed": "SNIPER_PRODUCTION_V3",
    }
