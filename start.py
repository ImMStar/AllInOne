"""Runs the Instagram bot (bot.py) and the caption bot (caption_bot.py) together.

Environment variables:
  BOT_TOKEN          token of the Instagram downloader bot (unchanged)
  CAPTION_BOT_TOKEN  token of the caption editor bot
  PORT               set by Render; used only by bot.py's status page
"""
import os
import signal
import subprocess
import sys
import time


def main():
    base = os.environ.copy()
    if "CAPTION_BOT_TOKEN" not in base:
        sys.exit("CAPTION_BOT_TOKEN is not set")

    # Instagram bot: keeps BOT_TOKEN and PORT exactly as they are.
    ig_env = dict(base)

    # Caption bot: its own token, and no PORT so it does not start a second
    # status page on the same port.
    cap_env = dict(base)
    cap_env["BOT_TOKEN"] = base["CAPTION_BOT_TOKEN"]
    cap_env.pop("PORT", None)

    procs = [
        subprocess.Popen([sys.executable, "bot.py"], env=ig_env),
        subprocess.Popen([sys.executable, "caption_bot.py"], env=cap_env),
    ]

    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    try:
        while True:
            for p in procs:
                code = p.poll()
                if code is not None:
                    # One bot stopped: exit so Render restarts both.
                    sys.exit(code or 1)
            time.sleep(2)
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()


if __name__ == "__main__":
    main()
