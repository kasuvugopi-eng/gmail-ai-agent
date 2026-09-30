"""Unified execution script for the Gmail AI Agent and Telegram Bot.

This module provides a single entry-point to concurrently run the incremental
Gmail scanning agent and the Telegram bot listener using Python's threading
interface.
"""

import threading
import time

from bot_listener import start_listening
from main import run_agent


def email_worker_loop(interval_seconds: int = 60) -> None:
    """Periodically executes the Gmail scanning agent in a background loop.

    Continuously polls for unread Gmail messages, classifies content via
    LangGraph, manages state persistence, and queues batch actions or drafts.

    Args:
        interval_seconds (int, optional): The delay duration between consecutive
            scan cycles in seconds. Defaults to 60.
    """
    print(f"[*] Email scanner loop active (Interval: {interval_seconds}s)...")
    while True:
        try:
            print("\n[*] Polling for unread emails...")
            run_agent()
        except Exception as exc:
            print(f"[!] Error encountered during email polling cycle: {exc}")

        time.sleep(interval_seconds)


def main() -> None:
    """Initializes and runs the unified agent and listener service.

    Spawns a daemon thread for the periodic email scanning worker and starts
    the long-polling Telegram event listener on the primary execution thread.
    """
    print("=" * 60)
    print("STARTING GMAIL AI AGENT AND TELEGRAM BOT SERVICE")
    print("=" * 60)

    email_thread = threading.Thread(
        target=email_worker_loop,
        args=(60,),
        daemon=True,
    )
    email_thread.start()

    start_listening()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Service execution interrupted by user. Exiting cleanly...")