"""
Terminal Mafia -- human player client.

Usage:
    python run_client.py --host 127.0.0.1 --port 5555 --name Alice

Run this once per human player, each in its own terminal window (same
machine) or on each player's own machine (LAN/hotspot, using the
server's LAN IP as --host).

If your connection drops, just re-run the exact same command with the
same --name -- the server will re-attach you to your existing role and
game state.
"""

import argparse
import socket
import sys
import threading
import time

from mafia import protocol

try:
    import readline  # noqa: F401  -- Unix/Mac: linked into input() automatically
    _HAS_READLINE = True
except ImportError:
    _HAS_READLINE = False

_print_lock = threading.Lock()


def safe_print(text=""):
    """
    Thread-safe print used by every part of this client (the listener
    thread and the main thread both go through this). On platforms
    with GNU readline (Linux/Mac -- the vast majority of real terminals
    people will use for a hackathon demo), this also clears the
    current line before printing and redraws whatever the player has
    already typed but not yet submitted, so an incoming chat message
    or countdown tick doesn't visually mangle mid-typed input. On
    platforms without readline (e.g. stock Windows Python), it falls
    back to plain sequential printing.
    """
    with _print_lock:
        if _HAS_READLINE:
            buf = readline.get_line_buffer()
            sys.stdout.write("\r\033[K")
            print(text)
            if buf:
                sys.stdout.write(buf)
                sys.stdout.flush()
        else:
            print(text)


class Countdown:
    """
    A client-side, locally-computed countdown for the current timed
    prompt (discussion / vote / night action / sabotage decision).
    Prints a handful of checkpoint reminders (not a per-second live
    tick -- see README for why) via safe_print, and can be cancelled
    early the moment a new phase/prompt arrives or the player submits
    their answer, so stale "X seconds left!" messages never show up
    after they've already acted.
    """

    def __init__(self):
        self._stop = threading.Event()
        self._thread = None

    def start(self, seconds: int, label: str):
        self.stop()
        stop_event = threading.Event()
        self._stop = stop_event
        self._thread = threading.Thread(
            target=self._run, args=(seconds, label, stop_event), daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self, seconds, label, stop_event):
        start = time.time()
        checkpoints = sorted(
            {seconds, seconds // 2, 10, 5, 3, 2, 1},
            reverse=True,
        )
        checkpoints = [c for c in checkpoints if 0 < c <= seconds]

        for remaining in checkpoints:
            target_time = start + (seconds - remaining)
            wait = target_time - time.time()
            if wait > 0 and stop_event.wait(wait):
                return  # cancelled
            if stop_event.is_set():
                return
            if remaining == seconds:
                safe_print(f"\u23f3 {label}: you have {remaining}s.")
            else:
                safe_print(f"\u23f3 {label}: {remaining}s left...")

        final_wait = start + seconds - time.time()
        if final_wait > 0 and stop_event.wait(final_wait):
            return
        if not stop_event.is_set():
            safe_print(f"\u23f0 Time's up for {label}.")


def main():
    parser = argparse.ArgumentParser(description="Terminal Mafia -- player client")
    parser.add_argument("--host", default="127.0.0.1", help="Server address")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--name", required=True, help="Your player name (also used to reconnect)")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect((args.host, args.port))
    except (ConnectionRefusedError, OSError) as e:
        print(f"Could not connect to {args.host}:{args.port} -- {e}")
        sys.exit(1)

    protocol.send_json(sock, {"name": args.name})

    state = {"prompt_type": None}
    countdown = Countdown()

    listener = threading.Thread(target=_listen, args=(sock, state, countdown), daemon=True)
    listener.start()

    safe_print(f"Connected as {args.name}. Waiting for the game to start...")
    safe_print("(Tip: if you get disconnected, just re-run this same command to rejoin.)\n")

    while True:
        try:
            line = input()
        except EOFError:
            break
        line = line.strip()
        if not line:
            continue
        _send_current(sock, state, countdown, line)


def _listen(sock, state, countdown):
    try:
        for msg in protocol.recv_lines(sock):
            _handle_message(msg, state, countdown)
    except (ConnectionError, OSError):
        safe_print("\n[Connection lost -- the server may have ended, or your network dropped. "
                   "Re-run this command with the same --name to try reconnecting.]")


def _handle_message(msg, state, countdown):
    t = msg.get("type")

    if t == "joined":
        safe_print(f"[SERVER] {msg['text']}")

    elif t == "error":
        safe_print(f"[ERROR] {msg['text']}")

    elif t == "role_assigned":
        safe_print("\n" + "=" * 55)
        safe_print(f"YOUR ROLE: {msg['role']}")
        safe_print(msg["description"])
        if msg.get("teammates"):
            safe_print(f"Your fellow Mafia-aligned teammate(s): {', '.join(msg['teammates'])}")
        safe_print("=" * 55 + "\n")

    elif t == "phase":
        safe_print(f"\n--- {msg['name']} (Round {msg['round']}) ---")
        if msg["name"] == "DISCUSSION":
            safe_print(f"Alive: {', '.join(msg['alive_players'])}")
            safe_print(f"You have {msg['timeout']}s to discuss. Just type a line and press Enter to chat.")
            state["prompt_type"] = "chat"
            countdown.start(msg["timeout"], "Discussion")
        elif msg["name"] == "NIGHT":
            safe_print("The town sleeps. If you have a night action, you'll be prompted for it.")
            state["prompt_type"] = None
            countdown.stop()
        elif msg["name"] == "VOTING":
            safe_print("Voting is open -- wait for your prompt below.")
            countdown.stop()

    elif t == "death_announcement":
        if msg["player"]:
            safe_print(f"\n{msg['player']} was found dead this morning. They were... {msg['role']}!")
        else:
            safe_print("\nNo one died last night.")

    elif t == "eliminated":
        # First-person notice sent only to the player who was JUST
        # eliminated -- distinct from the public death_announcement
        # above, so there's zero ambiguity about what happened to them
        # and what they can still do (spectate, not act).
        safe_print(f"\n{msg['text']}\n")
        state["prompt_type"] = None
        countdown.stop()

    elif t == "chat":
        safe_print(f"[{msg['sender']}]: {msg['text']}")

    elif t == "private_result":
        safe_print(f"\n[PRIVATE RESULT] {msg['text']}\n")

    elif t == "info":
        safe_print(f"[INFO] {msg['text']}")

    elif t == "prompt":
        state["prompt_type"] = msg["prompt_type"]
        options = msg.get("options", [])
        timeout = msg.get("timeout", 0)
        safe_print(f"\n({timeout}s) Options: {', '.join(options)}")
        if msg["prompt_type"] == "sabotage_decision":
            safe_print("Type a player name to sabotage their vote tomorrow, or 'skip'.")
            countdown.start(timeout, "Sabotage decision")
        elif msg["prompt_type"] == "night_action":
            action = msg.get("action", "act")
            safe_print(f"Enter your {action} target:")
            countdown.start(timeout, action.capitalize())
        elif msg["prompt_type"] == "vote":
            safe_print("Enter your vote (a player name, or 'abstain'):")
            countdown.start(timeout, "Vote")

    elif t == "vote_results":
        safe_print("\n--- VOTE RESULTS ---")
        for name, count in msg["tally"].items():
            safe_print(f"  {name}: {count} vote(s)")
        if msg["eliminated"]:
            safe_print(f"{msg['eliminated']} has been eliminated. They were {msg['eliminated_role']}!")
        else:
            safe_print("No majority reached -- no one was eliminated.")

    elif t == "state_snapshot":
        safe_print(f"\n[Reconnected] Round {msg['round']}, Phase {msg['phase']}, "
                   f"Your role: {msg['role']}, You are: {'ALIVE' if msg['you_alive'] else 'dead'}")

    elif t == "game_over":
        countdown.stop()
        state["prompt_type"] = None
        safe_print("\n" + "#" * 55)
        safe_print(f"GAME OVER -- {msg['winner']} WIN! ({msg.get('rounds_played', '?')} rounds played)")

        log = msg.get("elimination_log") or []
        if log:
            safe_print("\nMatch summary:")
            for entry in log:
                safe_print(f"  Round {entry['round']} [{entry['phase']}]: "
                           f"{entry['player']} -- {entry['role']}")

        safe_print("\nFinal roles:")
        for r in msg["reveal"]:
            status = "alive" if r["alive"] else "dead"
            safe_print(f"  {r['name']:20s} {r['role']:15s} ({status})")
        safe_print("#" * 55)


def _send_current(sock, state, countdown, line):
    prompt_type = state.get("prompt_type")
    try:
        if prompt_type == "chat":
            protocol.send_json(sock, {"type": "chat", "text": line})
            # Discussion stays open for everyone -- don't stop the
            # countdown just because this player sent one message.
        elif prompt_type == "vote":
            protocol.send_json(sock, {"type": "vote", "target": line})
            countdown.stop()
        elif prompt_type == "night_action":
            protocol.send_json(sock, {"type": "night_action", "target": line})
            countdown.stop()
        elif prompt_type == "sabotage_decision":
            protocol.send_json(sock, {"type": "sabotage_decision", "target": line})
            countdown.stop()
        else:
            safe_print("[INFO] Nothing is expecting input right now.")
    except OSError:
        safe_print("[ERROR] Could not send -- connection may be lost.")


if __name__ == "__main__":
    main()
