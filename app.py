from flask import Flask, request, jsonify
from datetime import datetime
from zoneinfo import ZoneInfo
import uuid
import os
import psycopg
from psycopg.rows import dict_row

app = Flask(__name__)

TIMEZONE = "America/Los_Angeles"
FORCED_EXIT_HOUR = 12
MAX_ACTIVE_TRADES = 2

DATABASE_URL = os.environ.get("DATABASE_URL")


def exec_log(msg):
    print(f"[EXECUTION] {msg}")


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL)


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            trade_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price DOUBLE PRECISION NOT NULL,
            stop_price DOUBLE PRECISION NOT NULL,
            current_stop DOUBLE PRECISION NOT NULL,
            risk DOUBLE PRECISION NOT NULL,
            be_trigger DOUBLE PRECISION NOT NULL,
            tp1_price DOUBLE PRECISION NOT NULL,
            position_size DOUBLE PRECISION NOT NULL,
            remaining_size DOUBLE PRECISION NOT NULL,
            tp1_hit BOOLEAN NOT NULL DEFAULT FALSE,
            moved_to_be BOOLEAN NOT NULL DEFAULT FALSE,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            closed_at TEXT,
            exit_price DOUBLE PRECISION,
            exit_reason TEXT
        );
    """)
    conn.commit()
    cur.close()
    conn.close()


def calc_levels(direction, entry, stop):
    if direction == "long":
        risk = entry - stop
        be = entry + (risk * 0.5)
        tp1 = entry + risk
    else:
        risk = stop - entry
        be = entry - (risk * 0.5)
        tp1 = entry - risk
    return risk, be, tp1


def validate_trade(direction, entry, stop):
    if direction == "long" and stop >= entry:
        return False
    if direction == "short" and stop <= entry:
        return False
    return True


def fetch_all_trades():
    conn = get_conn()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT * FROM trades ORDER BY created_at ASC;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {row["trade_id"]: dict(row) for row in rows}


def fetch_active_trades():
    conn = get_conn()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT * FROM trades WHERE status = 'active';")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {row["trade_id"]: dict(row) for row in rows}


def fetch_trade(trade_id):
    conn = get_conn()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT * FROM trades WHERE trade_id = %s;", (trade_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else None


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    event = data.get("event")

    if event == "entry":
        active = fetch_active_trades()

        if len(active) >= MAX_ACTIVE_TRADES:
            return jsonify({"ok": False, "msg": "max active trades reached"})

        symbol = data["symbol"]
        direction = data["direction"]
        entry = float(data["entry_price"])
        stop = float(data["stop_price"])
        size = float(data.get("position_size", 2))

        if not validate_trade(direction, entry, stop):
            return jsonify({"ok": False, "msg": "invalid stop"})

        risk, be, tp1 = calc_levels(direction, entry, stop)
        trade_id = str(uuid.uuid4())
        created_at = datetime.now(ZoneInfo(TIMEZONE)).isoformat()

        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO trades VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL,NULL)
        """, (
            trade_id, symbol, direction, entry, stop, stop,
            risk, be, tp1, size, size,
            False, False, "active", created_at
        ))
        conn.commit()
        cur.close()
        conn.close()

        exec_log(f"ENTER {symbol} {direction}")

        return jsonify({"ok": True})

    elif event == "price_update":
        price = float(data["price"])
        symbol = data.get("symbol")

        conn = get_conn()
        cur = conn.cursor(row_factory=dict_row)

        if symbol:
            cur.execute("SELECT * FROM trades WHERE status='active' AND symbol=%s;", (symbol,))
        else:
            cur.execute("SELECT * FROM trades WHERE status='active';")

        trades = cur.fetchall()

        for t in trades:
            trade_id = t["trade_id"]

            if not t["moved_to_be"]:
                if (t["direction"] == "long" and price >= t["be_trigger"]) or \
                   (t["direction"] == "short" and price <= t["be_trigger"]):

                    cur.execute("""
                        UPDATE trades SET current_stop=entry_price, moved_to_be=TRUE WHERE trade_id=%s
                    """, (trade_id,))
                    exec_log(f"BE MOVE {trade_id}")

            if not t["tp1_hit"]:
                if (t["direction"] == "long" and price >= t["tp1_price"]) or \
                   (t["direction"] == "short" and price <= t["tp1_price"]):

                    new_size = t["remaining_size"] / 2
                    cur.execute("""
                        UPDATE trades SET tp1_hit=TRUE, remaining_size=%s WHERE trade_id=%s
                    """, (new_size, trade_id))
                    exec_log(f"TP1 HIT {trade_id}")

        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"ok": True})

    elif event == "state":
        return jsonify(fetch_all_trades())

    return jsonify({"ok": False})


@app.route("/")
def home():
    active_count = 0
    try:
        active_count = len(fetch_active_trades()) if DATABASE_URL else 0
    except Exception as e:
        print(f"[HOME ERROR] {e}")

    return jsonify({
        "ok": True,
        "message": "Webhook server is live",
        "db_configured": bool(DATABASE_URL),
        "active_trade_count": active_count
    })


if DATABASE_URL:
    try:
        init_db()
        print("[STARTUP] init_db succeeded")
    except Exception as e:
        print(f"[STARTUP ERROR] {e}")
else:
    print("[STARTUP] DATABASE_URL not set - skipping init_db()")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)