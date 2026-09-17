import sys
import sqlite3
import bot


def main():
    if len(sys.argv) != 3:
        raise SystemExit(2)
    action, did = sys.argv[1], sys.argv[2]
    with sqlite3.connect(bot.DB) as db:
        row = db.execute("SELECT 1 FROM deals WHERE deal_id=?", (did,)).fetchone()
    if not row:
        raise SystemExit(3)
    if action == "u":
        status, mid = bot.publish(did, urgent=True)
    elif action == "n":
        status, mid = bot.publish(did, urgent=False)
    elif action == "x":
        status, mid = bot.reject(did)
    else:
        raise SystemExit(4)
    if mid:
        bot.clear_buttons(mid)
    print(status)


if __name__ == "__main__":
    main()
