#!/usr/bin/env python3
"""One-command start for Steam Backlog.

Sets up ./venv and installs requirements on first run (and again whenever
requirements.txt changes), then serves the app with gunicorn.

    python3 run.py                    # http://127.0.0.1:5002
    python3 run.py --port 8080
    python3 run.py --host 0.0.0.0     # reachable from other devices on your network

Standard library only, so it runs with the system Python before the
virtualenv exists.
"""

import argparse
import hashlib
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / "venv"
VENV_PYTHON = VENV / "bin" / "python"
GUNICORN = VENV / "bin" / "gunicorn"
REQUIREMENTS = ROOT / "requirements.txt"
# Hash of the requirements.txt last installed into ./venv.
STAMP = VENV / ".requirements.sha256"


def parse_args():
    parser = argparse.ArgumentParser(description="Run Steam Backlog.")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to listen on (default: 127.0.0.1, this machine only; "
        "use 0.0.0.0 for your whole network)",
    )
    parser.add_argument("--port", type=int, default=5002, help="port to listen on (default: 5002)")
    return parser.parse_args()


def ensure_venv():
    wanted = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    if VENV_PYTHON.exists() and STAMP.exists() and STAMP.read_text().strip() == wanted:
        return

    if not VENV_PYTHON.exists():
        print("First run: creating a virtualenv in ./venv ...")
        try:
            venv.create(VENV, with_pip=True)
        except subprocess.CalledProcessError:
            # Debian/Ubuntu ship venv's pip bootstrap as a separate package.
            sys.exit(
                "Couldn't create the virtualenv. On Debian/Ubuntu/Raspberry Pi OS, "
                "install it with:\n  sudo apt install python3-venv\nthen delete ./venv "
                "and run this again."
            )

    print("Installing requirements (this takes a minute) ...")
    subprocess.run(
        [str(VENV_PYTHON), "-m", "pip", "install", "--quiet", "-r", str(REQUIREMENTS)],
        check=True,
    )
    STAMP.write_text(wanted)


def main():
    if sys.version_info < (3, 11):
        sys.exit(f"Steam Backlog needs Python 3.11 or newer (this is {sys.version.split()[0]}).")

    args = parse_args()
    ensure_venv()

    where = "<this machine's IP>" if args.host == "0.0.0.0" else args.host
    print(f"\nSteam Backlog is running at http://{where}:{args.port}  (Ctrl+C to stop)\n")

    os.chdir(ROOT)
    # exec so Ctrl+C and service managers talk to gunicorn directly.
    os.execv(
        GUNICORN,
        [str(GUNICORN), "--workers", "2", "--threads", "2",
         "--bind", f"{args.host}:{args.port}", "app:app"],
    )


if __name__ == "__main__":
    main()
