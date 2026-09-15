"""Pack / unpack the bot's secret files into an AES-256 encrypted zip that CAN live in the repository.

  python scripts/secrets_tool.py pack      -> writes secrets.zip from .env, data/users.json, data/google-service-account.json, data/access_requests.json
  python scripts/secrets_tool.py unpack    -> restores those files from secrets.zip

The passphrase is read from the ADMISSIONOS_SECRETS_PASSPHRASE environment variable, or typed at the prompt.
It is never stored in the repository.
"""
import getpass
import os
import sys
from pathlib import Path

import pyzipper

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "secrets.zip"
FILES = [".env", "data/users.json", "data/google-service-account.json", "data/access_requests.json"]


def passphrase(confirm: bool) -> bytes:
    p = os.environ.get("ADMISSIONOS_SECRETS_PASSPHRASE")
    if not p:
        p = getpass.getpass("Secrets passphrase: ")
        if confirm and p != getpass.getpass("Repeat passphrase: "):
            sys.exit("Passphrases do not match.")
    if len(p) < 12:
        sys.exit("Use at least 12 characters.")
    return p.encode("utf-8")


def pack() -> None:
    pw = passphrase(confirm=True)
    present = [f for f in FILES if (ROOT / f).exists()]
    if not present:
        sys.exit("Nothing to pack - no secret files found.")
    with pyzipper.AESZipFile(ARCHIVE, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as z:
        z.setpassword(pw)
        for f in present:
            z.write(ROOT / f, f)
    print(f"Packed {len(present)} file(s) into {ARCHIVE.name}: {', '.join(present)}")


def unpack() -> None:
    if not ARCHIVE.exists():
        sys.exit("secrets.zip not found - clone the repository first.")
    pw = passphrase(confirm=False)
    try:
        with pyzipper.AESZipFile(ARCHIVE) as z:
            z.setpassword(pw)
            names = z.namelist()
            for name in names:
                target = ROOT / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(name))
    except RuntimeError as exc:
        sys.exit(f"Could not open the archive - wrong passphrase? ({exc})")
    print(f"Restored {len(names)} file(s): {', '.join(names)}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"pack": pack, "unpack": unpack}.get(cmd, lambda: sys.exit(__doc__))()
