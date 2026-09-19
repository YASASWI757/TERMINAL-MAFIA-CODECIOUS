"""
Confirms the double-echo chat bug stays fixed: a player's own chat
message must never be broadcast back to them (their own terminal
already shows it via normal typing / local echo -- receiving it again
from the server was the original bug).

Run with:  python tests/verify_no_echo.py
"""

import socket
import threading
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mafia import constants
constants.DISCUSSION_TIMEOUT = 4
constants.VOTE_TIMEOUT = 2
constants.NIGHT_ACTION_TIMEOUT = 2

from mafia.server import GameServer
from mafia import protocol

PORT = 5944
UNIQUE_TEXT = "UNIQUE_MARKER_TEXT_12345"


def run_fake_human(name, port, log, done_event):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    for _ in range(50):
        try:
            s.connect(("127.0.0.1", port))
            break
        except OSError:
            time.sleep(0.1)
    protocol.send_json(s, {"name": name})
    sent_chat = {"done": False}

    def respond(msg):
        t = msg.get("type")
        if t == "phase" and msg.get("name") == "DISCUSSION" and not sent_chat["done"]:
            protocol.send_json(s, {"type": "chat", "text": UNIQUE_TEXT})
            sent_chat["done"] = True
        elif t == "prompt":
            pt = msg["prompt_type"]
            options = [o for o in msg.get("options", []) if o not in ("abstain", "skip")]
            if pt == "sabotage_decision":
                protocol.send_json(s, {"type": "sabotage_decision", "target": "skip"})
            elif pt == "vote":
                protocol.send_json(s, {"type": "vote", "target": "abstain"})
            elif pt == "night_action" and options:
                protocol.send_json(s, {"type": "night_action", "target": options[0]})

    try:
        for msg in protocol.recv_lines(s):
            log.append(msg)
            respond(msg)
            if msg.get("type") == "game_over":
                break
    finally:
        done_event.set()
        s.close()


def main():
    server = GameServer(host="127.0.0.1", port=PORT, target_bots=3,
                         min_players=4, auto_start=True)
    threading.Thread(target=server.start, daemon=True).start()
    time.sleep(0.3)

    log = []
    done_event = threading.Event()
    threading.Thread(
        target=run_fake_human, args=("EchoTestHuman", PORT, log, done_event), daemon=True
    ).start()
    finished = done_event.wait(timeout=90)
    assert finished, "Game did not finish"

    chat_echoes = [m for m in log if m.get("type") == "chat" and m.get("text") == UNIQUE_TEXT]
    print(f"Chat messages with our own unique marker text received back: {len(chat_echoes)}")
    assert len(chat_echoes) == 0, (
        f"DOUBLE-ECHO BUG STILL PRESENT: sender received their own message back "
        f"{len(chat_echoes)} time(s)"
    )
    print("PASS: sender never received their own chat message echoed back.")


if __name__ == "__main__":
    main()
