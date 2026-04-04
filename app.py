from flask import Flask, request, jsonify
from datetime import datetime
from zoneinfo import ZoneInfo
import uuid
import os
import psycopg

app = Flask(__name__)

TIMEZONE = "America/Los_Angeles"
FORCED_EXIT_HOUR = 12
MAX_ACTIVE_TRADES = 2

DATABASE_URL = os.environ.get("DATABASE_URL")


def exec_log(msg):
    print(f"[EXECUTION] {msg}")


def get_conn():
    return psycopg.connect(DATABASE_URL)


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
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
        """
    )
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
    if abs(entry - stop) <= 0:
        return False
    return True


def fetch_all_trades():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM trades ORDER BY created_at ASC;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {row["trade_id"]: dict(row) for row in rows}


def fetch_active_trades():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM trades WHERE status = 'active' ORDER BY created_at ASC;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {row["trade_id"]: dict(row) for row in rows}


def fetch_trade(trade_id):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
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
        active_trades = fetch_active_trades()

        if len(active_trades) >= MAX_ACTIVE_TRADES:
            return jsonify({"ok": False, "msg": "max active trades reached", "trades": active_trades})

        symbol = data["symbol"]
        direction = data["direction"]
        entry = float(data["entry_price"])
        stop = float(data["stop_price"])
        size = float(data.get("position_size", 2))

        if not validate_trade(direction, entry, stop):
            return jsonify({"ok": False, "msg": "invalid stop placement"})

        risk, be, tp1 = calc_levels(direction, entry, stop)
        trade_id = str(uuid.uuid4())
        created_at = datetime.now(ZoneInfo(TIMEZONE)).isoformat()

        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO trades (
                trade_id, symbol, direction, entry_price, stop_price, current_stop,
                risk, be_trigger, tp1_price, position_size, remaining_size,
                tp1_hit, moved_to_be, status, created_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s
            );
            """,
            (
                trade_id, symbol, direction, entry, stop, stop,
                risk, be, tp1, size, size,
                False, False, "active", created_at
            )
        )
        conn.commit()
        cur.close()
        conn.close()

        trade = fetch_trade(trade_id)

        exec_log(f"ENTER {symbol} {direction} id={trade_id} size={size} entry={entry} stop={stop}")
        exec_log(f"PLACE TP1 id={trade_id} at {tp1} (half)")

        return jsonify({"ok": True, "trade_id": trade_id, "trade": trade})

    if event == "price_update":
        price = float(data["price"])
        updated = []

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM trades WHERE status = 'active' ORDER BY created_at ASC;")
        active_rows = cur.fetchall()

        for trade in active_rows:
            trade_id = trade["trade_id"]
            changed = False

            if not trade["moved_to_be"]:
                if trade["direction"] == "long" and price >= trade["be_trigger"]:
                    cur.execute(
                        """
                        UPDATE trades
                        SET current_stop = entry_price, moved_to_be = TRUE
                        WHERE trade_id = %s;
                        """,
                        (trade_id,)
                    )
                    exec_log(f"MOVE STOP TO BE id={trade_id} @ {trade['entry_price']}")
                    updated.append(trade_id)
                    changed = True

                elif trade["direction"] == "short" and price <= trade["be_trigger"]:
                    cur.execute(
                        """
                        UPDATE trades
                        SET current_stop = entry_price, moved_to_be = TRUE
                        WHERE trade_id = %s;
                        """,
                        (trade_id,)
                    )
                    exec_log(f"MOVE STOP TO BE id={trade_id} @ {trade['entry_price']}")
                    updated.append(trade_id)
                    changed = True

            if not trade["tp1_hit"]:
                if trade["direction"] == "long" and price >= trade["tp1_price"]:
                    qty = trade["position_size"] / 2
                    new_remaining = trade["remaining_size"] - qty
                    cur.execute(
                        """
                        UPDATE trades
                        SET tp1_hit = TRUE, remaining_size = %s
                        WHERE trade_id = %s;
                        """,
                        (new_remaining, trade_id)
                    )
                    exec_log(f"TP1 HIT id={trade_id} @ {trade['tp1_price']} | closed {qty}")
                    if trade_id not in updated:
                        updated.append(trade_id)
                    changed = True

                elif trade["direction"] == "short" and price <= trade["tp1_price"]:
                    qty = trade["position_size"] / 2
                    new_remaining = trade["remaining_size"] - qty
                    cur.execute(
                        """
                        UPDATE trades
                        SET tp1_hit = TRUE, remaining_size = %s
                        WHERE trade_id = %s;
                        """,
                        (new_remaining, trade_id)
                    )
                    exec_log(f"TP1 HIT id={trade_id} @ {trade['tp1_price']} | closed {qty}")
                    if trade_id not in updated:
                        updated.append(trade_id)
                    changed = True

            if changed:
                pass

        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"ok": True, "updated_trades": updated, "trades": fetch_all_trades()})

    if event == "time_check":
        now = datetime.now(ZoneInfo(TIMEZONE))
        exited = []

        conn = get_conn()
        cur = conn.cursor(row_factory=psycopg.rows.dict_row)
        cur.execute("SELECT * FROM trades WHERE status = 'active' ORDER BY created_at ASC;")
        active_rows = cur.fetchall()

        for trade in active_rows:
            trade_id = trade["trade_id"]
            if now.hour >= FORCED_EXIT_HOUR and trade["remaining_size"] > 0:
                cur.execute(
                    """
                    UPDATE trades
                    SET status = 'closed',
                        closed_at = %s,
                        exit_reason = %s,
                        exit_price = %s,
                        remaining_size = 0
                    WHERE trade_id = %s;
                    """,
                    (now.isoformat(), "forced_time_exit", None, trade_id)
                )
                exec_log(f"FORCED EXIT id={trade_id} @ {now.isoformat()}")
                exited.append(trade_id)

        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"ok": True, "exited_trades": exited, "trades": fetch_all_trades()})

    if event == "exit":
        trade_id = data.get("trade_id")
        exit_price = data.get("exit_price")
        reason = data.get("reason", "manual_exit")

        if not trade_id:
            return jsonify({"ok": False, "msg": "missing trade_id"})

        trade = fetch_trade(trade_id)
        if not trade:
            return jsonify({"ok": False, "msg": "trade_id not found"})

        if trade["status"] != "active":
            return jsonify({"ok": False, "msg": "trade already closed"})

        closed_at = datetime.now(ZoneInfo(TIMEZONE)).isoformat()

        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE trades
            SET status = 'closed',
                exit_price = %s,
                exit_reason = %s,
                closed_at = %s,
                remaining_size = 0
            WHERE trade_id = %s;
            """,
            (float(exit_price) if exit_price is not None else None, reason, closed_at, trade_id)
        )
        conn.commit()
        cur.close()
        conn.close()

        updated_trade = fetch_trade(trade_id)

        exec_log(f"EXIT {updated_trade['symbol']} id={trade_id} @ {exit_price} reason={reason}")

        return jsonify({
            "ok": True,
            "msg": "trade closed",
            "trade": updated_trade
        })

    if event == "state":
        return jsonify({
            "ok": True,
            "trades": fetch_all_trades(),
            "active_trades": fetch_active_trades()
        })

    return jsonify({"ok": False, "msg": "unknown event"})


@app.route("/")
def home():
    return jsonify({
        "ok": True,
        "message": "Webhook server is live",
        "active_trade_count": len(fetch_active_trades())
    })


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)