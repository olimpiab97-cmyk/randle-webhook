from flask import Flask, request, jsonify

app = Flask(__name__)

trade = {}

@app.route("/webhook", methods=["POST"])
def webhook():
    global trade
    data = request.get_json()

    event = data.get("event")

    if event == "entry":
        direction = data["direction"]
        entry_price = float(data["price"])
        atr = float(data["atr"])

        if direction == "long":
            stop = entry_price - atr
            tp1 = entry_price + atr
            be_trigger = entry_price + (0.5 * atr)
        else:
            stop = entry_price + atr
            tp1 = entry_price - atr
            be_trigger = entry_price - (0.5 * atr)

        trade = {
            "symbol": data["symbol"],
            "direction": direction,
            "entry_price": entry_price,
            "atr": atr,
            "original_stop": stop,
            "current_stop": stop,
            "position_size": 2,
            "remaining_size": 2,
            "status": "active",
            "tp1_price": tp1,
            "tp1_qty": 1,
            "tp1_filled": False,
            "be_trigger": be_trigger,
            "moved_to_be": False
        }

        return jsonify({"ok": True, "message": "entry saved", "trade": trade})

    elif event == "price":
        if not trade or trade.get("status") != "active":
            return jsonify({"ok": True, "message": "no active trade"})

        price = float(data["price"])

        if not trade["moved_to_be"]:
            if trade["direction"] == "long" and price >= trade["be_trigger"]:
                trade["current_stop"] = trade["entry_price"]
                trade["moved_to_be"] = True
            elif trade["direction"] == "short" and price <= trade["be_trigger"]:
                trade["current_stop"] = trade["entry_price"]
                trade["moved_to_be"] = True

        if not trade["tp1_filled"]:
            if trade["direction"] == "long" and price >= trade["tp1_price"]:
                trade["tp1_filled"] = True
                trade["remaining_size"] -= trade["tp1_qty"]
            elif trade["direction"] == "short" and price <= trade["tp1_price"]:
                trade["tp1_filled"] = True
                trade["remaining_size"] -= trade["tp1_qty"]

        return jsonify({"ok": True, "message": "price processed", "trade": trade})

    elif event == "state":
        return jsonify({"ok": True, "trade": trade})

    return jsonify({"ok": False, "message": "unknown event"}), 400

if __name__ == "__main__":
    app.run(debug=True)
