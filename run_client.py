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
import threading
import sys

from mafia import protocol


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

    listener = threading.Thread(target=_listen, args=(sock, state), daemon=True)
    listener.start()

    print(f"Connected as {args.name}. Waiting for the game to start...")
    print("(Tip: if you get disconnected, just re-run this same command to rejoin.)\n")

    while True:
        try:
            line = input()
        except EOFError:
            break
        line = line.strip()
        if not line:
            continue
        _send_current(sock, state, line)


def _listen(sock, state):
    try:
        for msg in protocol.recv_lines(sock):
            _handle_message(msg, state)
    except (ConnectionError, OSError):
        print("\n[Connection lost -- the server may have ended, or your network dropped. "
              "Re-run this command with the same --name to try reconnecting.]")


def _handle_message(msg, state):
    t = msg.get("type")

    if t == "joined":
        print(f"[SERVER] {msg['text']}")

    elif t == "error":
        print(f"[ERROR] {msg['text']}")

    elif t == "role_assigned":
        print("\n" + "=" * 55)
        print(f"YOUR ROLE: {msg['role']}")
        print(msg["description"])
        if msg.get("teammates"):
            print(f"Your fellow Mafia-aligned teammate(s): {', '.join(msg['teammates'])}")
        print("=" * 55 + "\n")

    elif t == "phase":
        print(f"\n--- {msg['name']} (Round {msg['round']}) ---")
        if msg["name"] == "DISCUSSION":
            print(f"Alive: {', '.join(msg['alive_players'])}")
            print(f"You have {msg['timeout']}s to discuss. Just type a line and press Enter to chat.")
            state["prompt_type"] = "chat"
        elif msg["name"] == "NIGHT":
            print("The town sleeps. If you have a night action, you'll be prompted for it.")
            state["prompt_type"] = None
        elif msg["name"] == "VOTING":
            print("Voting is open -- wait for your prompt below.")

    elif t == "death_announcement":
        if msg["player"]:
            print(f"\n{msg['player']} was found dead this morning. They were... {msg['role']}!")
        else:
            print("\nNo one died last night.")

    elif t == "chat":
        print(f"[{msg['sender']}]: {msg['text']}")

    elif t == "private_result":
        print(f"\n[PRIVATE RESULT] {msg['text']}\n")

    elif t == "info":
        print(f"[INFO] {msg['text']}")

    elif t == "prompt":
        state["prompt_type"] = msg["prompt_type"]
        options = msg.get("options", [])
        print(f"\n({msg.get('timeout')}s) Options: {', '.join(options)}")
        if msg["prompt_type"] == "sabotage_decision":
            print("Type a player name to sabotage their vote tomorrow, or 'skip'.")
        elif msg["prompt_type"] == "night_action":
            action = msg.get("action", "act")
            print(f"Enter your {action} target:")
        elif msg["prompt_type"] == "vote":
            print("Enter your vote (a player name, or 'abstain'):")

    elif t == "vote_results":
        print("\n--- VOTE RESULTS ---")
        for name, count in msg["tally"].items():
            print(f"  {name}: {count} vote(s)")
        if msg["eliminated"]:
            print(f"{msg['eliminated']} has been eliminated. They were {msg['eliminated_role']}!")
        else:
            print("No majority reached -- no one was eliminated.")

    elif t == "state_snapshot":
        print(f"\n[Reconnected] Round {msg['round']}, Phase {msg['phase']}, "
              f"Your role: {msg['role']}, You are: {'ALIVE' if msg['you_alive'] else 'dead'}")

    elif t == "game_over":
        print("\n" + "#" * 55)
        print(f"GAME OVER -- {msg['winner']} WIN!")
        print("Final roles:")
        for r in msg["reveal"]:
            status = "alive" if r["alive"] else "dead"
            print(f"  {r['name']:20s} {r['role']:15s} ({status})")
        print("#" * 55)


def _send_current(sock, state, line):
    prompt_type = state.get("prompt_type")
    try:
        if prompt_type == "chat":
            protocol.send_json(sock, {"type": "chat", "text": line})
        elif prompt_type == "vote":
            protocol.send_json(sock, {"type": "vote", "target": line})
        elif prompt_type == "night_action":
            protocol.send_json(sock, {"type": "night_action", "target": line})
        elif prompt_type == "sabotage_decision":
            protocol.send_json(sock, {"type": "sabotage_decision", "target": line})
        else:
            print("[INFO] Nothing is expecting input right now.")
    except OSError:
        print("[ERROR] Could not send -- connection may be lost.")


if __name__ == "__main__":
    main()
