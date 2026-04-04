from flask import Flask, request, jsonify
from datetime import datetime
from zoneinfo import ZoneInfo
import uuid
import json
import os

app = Flask(__name__)

TIMEZONE = "America/Los_Angeles"
FORCED_EXIT_HOUR = 12
MAX_ACTIVE_TRADES = 2
DATA_FILE = "trades.json"

trades = {}


def exec_log(msg):
    print(f"[EXECUTION] {msg}")


def load_trades():
    global trades
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            trades = json.load(f)
    else:
        trades = {}


def save_trades():
    with open(DATA_FILE, "w") as f:
        json.dump(trades, f, indent=2)


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


def get_active_trades():
    return {tid: t for tid, t in trades.items() if t["status"] == "active"}


load_trades()


@app.route("/webhook", methods=["POST"])
def webhook():
    global trades

    data = request.get_json(force=True)
    event = data.get("event")

    if event == "entry":
        active_trades = get_active_trades()

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

        trades[trade_id] = {
            "trade_id": trade_id,
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry,
            "stop_price": stop,
            "current_stop": stop,
            "risk": risk,
            "be_trigger": be,
            "tp1_price": tp1,
            "position_size": size,
            "remaining_size": size,
            "tp1_hit": False,
            "moved_to_be": False,
            "status": "active",
            "created_at": datetime.now(ZoneInfo(TIMEZONE)).isoformat()
        }

        save_trades()

        exec_log(f"ENTER {symbol} {direction} id={trade_id} size={size} entry={entry} stop={stop}")
        exec_log(f"PLACE TP1 id={trade_id} at {tp1} (half)")

        return jsonify({"ok": True, "trade_id": trade_id, "trade": trades[trade_id]})

    if event == "price_update":
        price = float(data["price"])
        updated = []

        for trade_id, trade in trades.items():
            if trade["status"] != "active":
                continue

            if not trade["moved_to_be"]:
                if trade["direction"] == "long" and price >= trade["be_trigger"]:
                    trade["current_stop"] = trade["entry_price"]
                    trade["moved_to_be"] = True
                    exec_log(f"MOVE STOP TO BE id={trade_id} @ {trade['current_stop']}")
                    updated.append(trade_id)

                elif trade["direction"] == "short" and price <= trade["be_trigger"]:
                    trade["current_stop"] = trade["entry_price"]
                    trade["moved_to_be"] = True
                    exec_log(f"MOVE STOP TO BE id={trade_id} @ {trade['current_stop']}")
                    updated.append(trade_id)

            if not trade["tp1_hit"]:
                if trade["direction"] == "long" and price >= trade["tp1_price"]:
                    trade["tp1_hit"] = True
                    qty = trade["position_size"] / 2
                    trade["remaining_size"] -= qty
                    exec_log(f"TP1 HIT id={trade_id} @ {trade['tp1_price']} | closed {qty}")
                    updated.append(trade_id)

                elif trade["direction"] == "short" and price <= trade["tp1_price"]:
                    trade["tp1_hit"] = True
                    qty = trade["position_size"] / 2
                    trade["remaining_size"] -= qty
                    exec_log(f"TP1 HIT id={trade_id} @ {trade['tp1_price']} | closed {qty}")
                    updated.append(trade_id)

        save_trades()
        return jsonify({"ok": True, "updated_trades": updated, "trades": trades})

    if event == "time_check":
        now = datetime.now(ZoneInfo(TIMEZONE))
        exited = []

        for trade_id, trade in trades.items():
            if trade["status"] != "active":
                continue

            if now.hour >= FORCED_EXIT_HOUR and trade["remaining_size"] > 0:
                exec_log(f"FORCED EXIT id={trade_id} @ {now.isoformat()}")
                trade["status"] = "closed"
                trade["closed_at"] = now.isoformat()
                trade["exit_reason"] = "forced_time_exit"
                trade["exit_price"] = None
                trade["remaining_size"] = 0
                exited.append(trade_id)

        save_trades()
        return jsonify({"ok": True, "exited_trades": exited, "trades": trades})

    if event == "exit":
        trade_id = data.get("trade_id")
        exit_price = data.get("exit_price")
        reason = data.get("reason", "manual_exit")

        if not trade_id:
            return jsonify({"ok": False, "msg": "missing trade_id"})

        if trade_id not in trades:
            return jsonify({"ok": False, "msg": "trade_id not found"})

        trade = trades[trade_id]

        if trade["status"] != "active":
            return jsonify({"ok": False, "msg": "trade already closed"})

        trade["status"] = "closed"
        trade["exit_price"] = float(exit_price) if exit_price is not None else None
        trade["exit_reason"] = reason
        trade["closed_at"] = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
        trade["remaining_size"] = 0

        save_trades()

        exec_log(f"EXIT {trade['symbol']} id={trade_id} @ {exit_price} reason={reason}")

        return jsonify({
            "ok": True,
            "msg": "trade closed",
            "trade": trade
        })

    if event == "state":
        return jsonify({"ok": True, "trades": trades, "active_trades": get_active_trades()})

    return jsonify({"ok": False, "msg": "unknown event"})


@app.route("/")
def home():
    return jsonify({
        "ok": True,
        "message": "Webhook server is live",
        "active_trade_count": len(get_active_trades())
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)