from flask import Flask, request, jsonify
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)

TIMEZONE = "America/Los_Angeles"
FORCED_EXIT_HOUR = 12

trade = None


def exec_log(msg):
    print(f"[EXECUTION] {msg}")


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


@app.route("/webhook", methods=["POST"])
def webhook():
    global trade

    data = request.get_json(force=True)
    event = data.get("event")

    if event == "entry":
        if trade and trade["status"] == "active":
            return jsonify({"ok": False, "msg": "trade already active", "trade": trade})

        symbol = data["symbol"]
        direction = data["direction"]
        entry = float(data["entry_price"])
        stop = float(data["stop_price"])
        size = float(data.get("position_size", 2))

        if not validate_trade(direction, entry, stop):
            return jsonify({"ok": False, "msg": "invalid stop placement"})

        risk, be, tp1 = calc_levels(direction, entry, stop)

        trade = {
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
            "status": "active"
        }

        exec_log(f"ENTER {symbol} {direction} size={size} entry={entry} stop={stop}")
        exec_log(f"PLACE TP1 at {tp1} (half)")

        return jsonify({"ok": True, "trade": trade})

    if event == "price_update":
        if not trade or trade["status"] != "active":
            return jsonify({"ok": False, "msg": "no active trade"})

        price = float(data["price"])

        if not trade["moved_to_be"]:
            if trade["direction"] == "long" and price >= trade["be_trigger"]:
                trade["current_stop"] = trade["entry_price"]
                trade["moved_to_be"] = True
                exec_log(f"MOVE STOP TO BE @ {trade['current_stop']}")
            elif trade["direction"] == "short" and price <= trade["be_trigger"]:
                trade["current_stop"] = trade["entry_price"]
                trade["moved_to_be"] = True
                exec_log(f"MOVE STOP TO BE @ {trade['current_stop']}")

        if not trade["tp1_hit"]:
            if trade["direction"] == "long" and price >= trade["tp1_price"]:
                trade["tp1_hit"] = True
                qty = trade["position_size"] / 2
                trade["remaining_size"] -= qty
                exec_log(f"TP1 HIT @ {trade['tp1_price']} | closed {qty}")
            elif trade["direction"] == "short" and price <= trade["tp1_price"]:
                trade["tp1_hit"] = True
                qty = trade["position_size"] / 2
                trade["remaining_size"] -= qty
                exec_log(f"TP1 HIT @ {trade['tp1_price']} | closed {qty}")

        return jsonify({"ok": True, "trade": trade})

    if event == "time_check":
        if not trade or trade["status"] != "active":
            return jsonify({"ok": True})

        now = datetime.now(ZoneInfo(TIMEZONE))

        if now.hour >= FORCED_EXIT_HOUR and trade["remaining_size"] > 0:
            exec_log(f"FORCED EXIT @ {now}")
            trade["status"] = "closed"
            trade["remaining_size"] = 0

        return jsonify({"ok": True, "trade": trade})

    if event == "state":
        return jsonify({"ok": True, "trade": trade})

    return jsonify({"ok": False, "msg": "unknown event"})


if __name__ == "__main__":
    app.run(port=5000, debug=True)