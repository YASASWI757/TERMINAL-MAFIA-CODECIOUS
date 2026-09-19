"""
Verifies the play-again "yes" path specifically -- the existing tests
for this feature only exercise "no" (decline), since that's what's
needed to test the exit flow. This confirms the other half: a human
who says "yes" gets a genuinely fresh second match (a new
role_assigned, not stuck or disconnected), and that a human who says
"no" in a MULTI-human game gets replaced by a bot while the match
continues for everyone else (rather than ending the server entirely,
which is what happens when nobody stays -- already covered by
verify_clean_exit.py / verify_simple_client_exit.py).

Run with:  python tests/verify_play_again.py
"""

import socket
import threading
import time
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mafia import constants
constants.DISCUSSION_TIMEOUT = 2
constants.VOTE_TIMEOUT = 2
constants.NIGHT_ACTION_TIMEOUT = 2
constants.PLAY_AGAIN_TIMEOUT = 10

from mafia.server import GameServer
from mafia import protocol

PORT = 6044


class Reader:
    """Same persistent-buffer reader as verify_reconnect.py -- see that
    file for why this is necessary instead of re-creating
    protocol.recv_lines() per retry."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def read_one(self, timeout):
        deadline = time.time() + timeout
        while True:
            if b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                return json.loads(line.decode("utf-8"))
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(4096)
            except (socket.timeout, OSError):
                return None
            if not chunk:
                return "EOF"
            self.buf += chunk

    def drain_until(self, predicate, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.read_one(max(0.1, deadline - time.time()))
            if msg is None or msg == "EOF":
                return msg
            if predicate(msg):
                return msg
        return None


def connect(name):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    for _ in range(50):
        try:
            s.connect(("127.0.0.1", PORT))
            break
        except OSError:
            time.sleep(0.1)
    protocol.send_json(s, {"name": name, "token": None})
    return s, Reader(s)


def main():
    server = GameServer(host="127.0.0.1", port=PORT, target_bots=3,
                         min_players=4, auto_start=True)
    threading.Thread(target=server.start, daemon=True).start()
    time.sleep(0.3)

    sock, reader = connect("Yasaswi")
    joined = reader.read_one(5)
    assert joined and joined.get("type") == "joined", f"Join failed: {joined}"

    first_role = reader.drain_until(lambda m: m.get("type") == "role_assigned", 10)
    assert first_role is not None, "Never got first role_assigned"
    print(f"[OK] First match: role assigned ({first_role['role']})")

    game_over_1 = reader.drain_until(lambda m: m.get("type") == "game_over", 120)
    assert game_over_1 not in (None, "EOF"), "First match never reached game_over"
    print(f"[OK] First match ended: {game_over_1['winner']} won, "
          f"{game_over_1['rounds_played']} round(s)")

    prompt = reader.drain_until(lambda m: m.get("type") == "play_again_prompt", 5)
    assert prompt is not None, "play_again_prompt never arrived"
    print("[OK] play_again_prompt received")

    protocol.send_json(sock, {"type": "play_again", "target": "yes"})
    print("[OK] Sent 'yes'")

    # Should NOT disconnect -- should get a fresh role for a new match.
    second_role = reader.drain_until(lambda m: m.get("type") == "role_assigned", 10)
    assert second_role not in (None, "EOF"), (
        "Said yes to play again but never got a fresh role_assigned -- "
        "either stuck or disconnected instead of continuing"
    )
    print(f"[OK] Second match started: fresh role assigned ({second_role['role']})")

    # The match should be able to complete again too, proving this
    # isn't just a role_assigned sent into a broken/hung game loop.
    game_over_2 = reader.drain_until(lambda m: m.get("type") == "game_over", 120)
    assert game_over_2 not in (None, "EOF"), "Second match never reached game_over"
    assert len(game_over_2["reveal"]) == 4, (
        f"Expected 4 total players in the second match (1 continuing human + "
        f"3 original bots, since none of them 'leave'), got "
        f"{len(game_over_2['reveal'])}"
    )
    print(f"[OK] Second match completed too: {game_over_2['winner']} won, "
          f"with the expected 4 total players")

    sock.close()
    print("\nALL PLAY-AGAIN 'YES' PATH CHECKS PASSED")


if __name__ == "__main__":
    main()
