"""Enrol AdmissionOS users by their Telegram ID and/or WhatsApp number.

  python scripts/manage_users.py add samar --name "Samar" --tier executive --telegram 123456789 --whatsapp 919876543210
  python scripts/manage_users.py set samar --whatsapp 919876543210      (add or change a channel / tier / name)
  python scripts/manage_users.py list
  python scripts/manage_users.py disable samar
  python scripts/manage_users.py enable samar
  python scripts/manage_users.py remove samar

Telegram ID: the bot replies with it when an unknown person messages it (or use @userinfobot).
WhatsApp: full international number, digits only (91 + 10-digit mobile for India).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import auth  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("add", "set"):
        p = sub.add_parser(name)
        p.add_argument("handle", help="short unique id for the person, e.g. dm or ravi.k")
        p.add_argument("--name", required=(name == "add"))
        p.add_argument("--tier", choices=auth.TIERS, required=(name == "add"))
        p.add_argument("--telegram", help="Telegram user ID (numeric)")
        p.add_argument("--whatsapp", help="WhatsApp number, e.g. 919876543210")
    sub.add_parser("list")
    for name in ("disable", "enable", "remove"):
        sub.add_parser(name).add_argument("handle")
    args = ap.parse_args()

    users = auth.load_users()
    handle = getattr(args, "handle", "").strip().lower()
    existing = next((u for u in users if u["handle"] == handle), None) if handle else None

    if args.cmd == "list":
        if not users:
            print("(no users yet)")
        for u in users:
            flag = "active" if u.get("active", True) else "DISABLED"
            print(f"{u['handle']:14} {u['tier']:12} {flag:9} tg={u.get('telegram_id') or '-':12} wa={u.get('whatsapp') or '-':15} {u.get('name', '')}")
        return

    if args.cmd == "add":
        if existing:
            sys.exit("Handle already exists - use `set` to change it.")
        if not (args.telegram or args.whatsapp):
            sys.exit("Give at least one of --telegram / --whatsapp.")
        existing = {"handle": handle, "name": args.name, "tier": args.tier, "active": True}
        users.append(existing)
        args.cmd = "set"
    elif not existing:
        sys.exit("No such handle.")

    if args.cmd == "set":
        if args.name:
            existing["name"] = args.name
        if args.tier:
            existing["tier"] = args.tier
        if args.telegram:
            existing["telegram_id"] = auth.normalize("telegram", args.telegram)
        if args.whatsapp:
            existing["whatsapp"] = auth.normalize("whatsapp", args.whatsapp)
        for channel, field in auth.CHANNEL_FIELD.items():     # one identity may belong to one person only
            value = existing.get(field)
            clash = next((u for u in users if u is not existing and u.get(field) == value), None) if value else None
            if clash:
                sys.exit(f"{channel} identity {value} already belongs to {clash['handle']}.")
    elif args.cmd == "disable":
        existing["active"] = False
    elif args.cmd == "enable":
        existing["active"] = True
    elif args.cmd == "remove":
        users = [u for u in users if u is not existing]
    auth.save_users(users)
    print("OK")


if __name__ == "__main__":
    main()
