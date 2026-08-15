RENDER + NEON PUBLIC OPTIONS DASHBOARD
======================================

ARCHITECTURE
------------
Keep your existing Telegram membership bot on your PC.

Public cloud side:
    Trading bot
       |
       | POST closed results
       v
    Render FastAPI web service
       |
       v
    Neon Postgres
       |
       v
    Public landing page

This means Render does NOT manage Telegram membership and does NOT need your
Telegram bot token.

FILES
-----
app.py                  Render FastAPI app
templates/index.html    Green/gold landing page
requirements.txt
render.yaml
.env.example
migrate_sqlite_to_neon.py

NEON SETUP
----------
1. Create a free Neon project.
2. Click Connect.
3. Select/copy a pooled connection string.
4. Keep the connection string private.
5. For local migration, put it in a temporary .env as DATABASE_URL.

MIGRATE CURRENT RESULTS
-----------------------
From C:\Options Alert Bot, install psycopg if needed:

    .venv\Scripts\pip install "psycopg[binary]"

Copy migrate_sqlite_to_neon.py into C:\Options Alert Bot.

Temporarily add DATABASE_URL=<your Neon URL> to C:\Options Alert Bot\.env,
then run:

    .venv\Scripts\python migrate_sqlite_to_neon.py

It imports current closed_trades. Duplicate source_trade_id values are skipped.

GITHUB
------
Create a small repository containing ONLY this public-dashboard package.
Do NOT commit a real .env or any secret.

RENDER
------
Create New -> Web Service and connect the GitHub repository.

Render settings:
    Runtime: Python 3
    Build:   pip install -r requirements.txt
    Start:   uvicorn app:app --host 0.0.0.0 --port $PORT
    Plan:    Free

Environment variables in Render:
    DATABASE_URL         = Neon pooled connection string
    TRADE_INGEST_SECRET  = same proof secret used locally
    BOT_USERNAME         = membership bot username without @
    BRAND_NAME           = desired public brand
    MONTHLY_STARS        = 500

After deploy, Render gives:
    https://YOUR-SERVICE.onrender.com

TRADING BOT CHANGE
------------------
In the trading bot .env change:

    PROOF_FEED_URL=https://YOUR-SERVICE.onrender.com/api/trades/closed

Keep PROOF_FEED_SECRET exactly the same as TRADE_INGEST_SECRET.

IMPORTANT FREE-RENDER NOTE
--------------------------
A free Render web service sleeps after 15 minutes without inbound traffic and
can take about a minute to wake. Neon keeps the database persistent.

For the first cloud version:
- dashboard visitors may see a cold-start delay
- if a trade POST lands during cold start, your proof-feed client should retry
- never make a proof-dashboard failure affect trading logic

LOCAL MEMBERSHIP BOT
--------------------
Keep:
    C:\Options Alert Bot\run.bat

running for Telegram payments/access.

After the public dashboard moves to Render, you no longer need the local FastAPI
page to be public. It can remain your local administration/test copy.
