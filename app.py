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

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")

app = FastAPI(title="Public Options Proof Dashboard")
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
    exit_price DOUBLE PRECISION NOT NULL,
    pnl_pct DOUBLE PRECISION NOT NULL,
    opened_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ NOT NULL,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_closed_trades_closed
ON closed_trades(closed_at DESC);
"""


@contextmanager
def connect():
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        yield conn


def init_db():
    with connect() as conn:
        conn.execute(SCHEMA)


init_db()


class ClosedTradeIn(BaseModel):
    ticker: str
    option_type: str
    strike: Optional[str] = None
    expiry: Optional[str] = None
    entry_price: float = Field(ge=0)
    exit_price: float = Field(ge=0)
    pnl_pct: float
    opened_at: Optional[str] = None
    closed_at: str
    source_trade_id: Optional[str] = None
    notes: Optional[str] = None


def _stats(conn, actual_options: bool) -> dict:
    if actual_options:
        where = """
            strike IS NOT NULL AND BTRIM(strike) <> ''
            AND expiry IS NOT NULL AND BTRIM(expiry) <> ''
        """
    else:
        where = """
            NOT (
                strike IS NOT NULL AND BTRIM(strike) <> ''
                AND expiry IS NOT NULL AND BTRIM(expiry) <> ''
            )
        """

    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE pnl_pct > 0) AS wins,
            COUNT(*) FILTER (WHERE pnl_pct <= 0) AS nonwins,
            COALESCE(AVG(pnl_pct),0) AS avg_pnl,
            COALESCE(MAX(pnl_pct),0) AS best_pnl
        FROM closed_trades
        WHERE {where}
        """
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


def dashboard_data(limit: int = 25) -> dict:
    option_where = """
        strike IS NOT NULL AND BTRIM(strike) <> ''
        AND expiry IS NOT NULL AND BTRIM(expiry) <> ''
    """

    underlying_where = f"NOT ({option_where})"

    with connect() as conn:
        option_stats = _stats(conn, True)
        underlying_stats = _stats(conn, False)

        option_rows = conn.execute(
            f"""
            SELECT ticker,option_type,strike,expiry,entry_price,exit_price,
                   pnl_pct,opened_at,closed_at,notes
            FROM closed_trades
            WHERE {option_where}
            ORDER BY closed_at DESC,id DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()

        underlying_rows = conn.execute(
            f"""
            SELECT ticker,option_type,strike,expiry,entry_price,exit_price,
                   pnl_pct,opened_at,closed_at,notes
            FROM closed_trades
            WHERE {underlying_where}
            ORDER BY closed_at DESC,id DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()

    cols = [
        "ticker","option_type","strike","expiry","entry_price","exit_price",
        "pnl_pct","opened_at","closed_at","notes"
    ]

    return {
        "option_stats": option_stats,
        "underlying_stats": underlying_stats,
        "recent_options": [dict(zip(cols, r)) for r in option_rows],
        "recent_underlying": [dict(zip(cols, r)) for r in underlying_rows],
    }


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "brand": BRAND_NAME,
            "bot_username": BOT_USERNAME,
            "stars": MONTHLY_STARS,
        },
    )


@app.get("/api/dashboard")
async def get_dashboard():
    return dashboard_data()


@app.post("/api/trades/closed")
async def closed_trade(
    trade: ClosedTradeIn,
    x_trade_secret: Optional[str] = Header(default=None),
):
    if not TRADE_INGEST_SECRET:
        raise HTTPException(503, "Trade ingestion is not configured")
    if x_trade_secret != TRADE_INGEST_SECRET:
        raise HTTPException(401, "Invalid trade secret")

    option_type = trade.option_type.strip().upper()
    if option_type not in {"CALL", "PUT"}:
        raise HTTPException(400, "option_type must be CALL or PUT")

    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO closed_trades(
                source_trade_id,ticker,option_type,strike,expiry,
                entry_price,exit_price,pnl_pct,opened_at,closed_at,notes
            )
            VALUES(
                %s,%s,%s,%s,%s,%s,%s,%s,
                NULLIF(%s,'')::timestamptz,%s::timestamptz,%s
            )
            ON CONFLICT (source_trade_id) DO NOTHING
            RETURNING id
            """,
            (
                trade.source_trade_id,
                trade.ticker.strip().upper(),
                option_type,
                trade.strike,
                trade.expiry,
                float(trade.entry_price),
                float(trade.exit_price),
                float(trade.pnl_pct),
                trade.opened_at or "",
                trade.closed_at,
                trade.notes,
            ),
        ).fetchone()

    return {"ok": True, "created": bool(row)}


@app.get("/health")
async def health():
    with connect() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"ok": True, "database": "neon-postgres"}
