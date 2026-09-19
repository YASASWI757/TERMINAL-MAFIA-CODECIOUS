"""
End-to-end integration test: starts a REAL GameServer on a real TCP
socket, connects a REAL socket client (scripted to auto-respond to
every prompt), fills the rest of the lobby with bots, and asserts the
whole game runs to completion without exceptions or hangs.

This is what actually proves the networking/threading/timeout plumbing
works, not just the isolated rules logic (see test_engine.py for that).

Uses shortened timeouts (via monkeypatched constants) so the whole run
takes seconds instead of minutes.

Run with:  python tests/test_integration_headless.py
"""

import socket
import threading
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mafia import constants
# Shrink timeouts for a fast test run -- must happen BEFORE server import
# uses them, since server.py reads these as module-level constants.
constants.DISCUSSION_TIMEOUT = 3
constants.VOTE_TIMEOUT = 3
constants.NIGHT_ACTION_TIMEOUT = 3

from mafia.server import GameServer
from mafia import protocol

TEST_PORT = 5599


def run_fake_human(name, port, log, done_event):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    for _ in range(50):
        try:
            s.connect(("127.0.0.1", port))
            break
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    else:
        raise RuntimeError("Could not connect to test server")

    protocol.send_json(s, {"name": name})

    def respond(msg):
        t = msg.get("type")
        if t == "phase" and msg.get("name") == "DISCUSSION":
            protocol.send_json(s, {"type": "chat", "text": "Just watching for now."})
        elif t == "prompt":
            pt = msg["prompt_type"]
            options = msg.get("options", [])
            if pt == "sabotage_decision":
                protocol.send_json(s, {"type": "sabotage_decision", "target": "skip"})
            elif pt == "vote":
                choice = next((o for o in options if o != "abstain"), "abstain")
                protocol.send_json(s, {"type": "vote", "target": choice})
            elif pt == "night_action":
                if options:
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
    server = GameServer(host="127.0.0.1", port=TEST_PORT, target_bots=3,
                         min_players=4, auto_start=True)
    server_thread = threading.Thread(target=server.start, daemon=True)
    server_thread.start()
    time.sleep(0.3)

    log = []
    done_event = threading.Event()
    human_thread = threading.Thread(
        target=run_fake_human, args=("TestHuman", TEST_PORT, log, done_event), daemon=True
    )
    human_thread.start()

    finished = done_event.wait(timeout=90)

    assert finished, "Game did not reach game_over within 90s -- possible hang"

    msg_types_seen = {m.get("type") for m in log}
    required = {"joined", "role_assigned", "phase", "game_over"}
    missing = required - msg_types_seen
    assert not missing, f"Missing expected message types: {missing}"

    game_over_msg = next(m for m in log if m["type"] == "game_over")
    assert game_over_msg["winner"] in ("VILLAGERS", "MAFIA"), \
        f"Unexpected winner value: {game_over_msg['winner']}"
    assert len(game_over_msg["reveal"]) == 4, "Expected 4 total players in final reveal"

    print("PASS: full networked game ran end-to-end without errors.")
    print(f"      Winner: {game_over_msg['winner']}")
    print(f"      Messages exchanged: {len(log)}")
    print(f"      Final roles: {[(r['name'], r['role']) for r in game_over_msg['reveal']]}")


if __name__ == "__main__":
    main()
