import argparse
import os
import sqlite3

import psycopg
from dotenv import load_dotenv


load_dotenv()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sqlite", default=r"data\growth_stack.db")
    args = p.parse_args()

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is missing from .env")

    src = sqlite3.connect(args.sqlite)
    src.row_factory = sqlite3.Row

    rows = src.execute(
        """
        SELECT
            source_trade_id,ticker,option_type,strike,expiry,
            entry_price,exit_price,pnl_pct,opened_at,closed_at,notes
        FROM closed_trades
        ORDER BY id
        """
    ).fetchall()

    with psycopg.connect(database_url, autocommit=True) as dst:
        dst.execute(
            """
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
            )
            """
        )

        created = 0
        skipped = 0
        for r in rows:
            x = dst.execute(
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
                    r["source_trade_id"],r["ticker"],r["option_type"],r["strike"],r["expiry"],
                    r["entry_price"],r["exit_price"],r["pnl_pct"],r["opened_at"] or "",
                    r["closed_at"],r["notes"],
                ),
            ).fetchone()

            if x:
                created += 1
            else:
                skipped += 1

    print(f"Rows read: {len(rows)}")
    print(f"Inserted: {created}")
    print(f"Already present: {skipped}")


if __name__ == "__main__":
    main()
