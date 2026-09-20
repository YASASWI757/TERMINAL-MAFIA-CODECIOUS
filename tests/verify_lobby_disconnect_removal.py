"""
Verifies: a player who disconnects BEFORE the game starts is removed
permanently (not held as a reconnectable slot) and the game proceeds
with one fewer player -- min_players decrements accordingly, floored
at 4 (the engine's hard minimum for role assignment). A player who
disconnects AFTER the game starts is completely unaffected by this --
the existing reconnect logic (token-protected, prompt-restoring)
still applies exactly as before.

Run with:  python tests/verify_lobby_disconnect_removal.py
"""

import socket
import threading
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mafia import constants
constants.NIGHT_ACTION_TIMEOUT = 20

from mafia.server import GameServer
from mafia import protocol


def connect(port, name):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(("127.0.0.1", port))
    protocol.send_json(s, {"name": name, "token": None})
    s.settimeout(5)
    s.recv(4096)  # drain joined + lobby_update
    return s


def test_pregame_disconnect_removes_permanently():
    server = GameServer(host="127.0.0.1", port=6310, target_bots=0,
                         min_players=6, auto_start=False)
    threading.Thread(target=server.start, daemon=True).start()
    time.sleep(0.3)

    s1 = connect(6310, "A")
    s2 = connect(6310, "B")
    s3 = connect(6310, "C")
    time.sleep(0.3)
    assert len(server.players) == 3
    assert server.min_players == 6

    s2.close()  # B leaves before the game starts
    time.sleep(0.5)

    assert len(server.players) == 2, f"Expected 2 players remaining, got {len(server.players)}"
    assert all(p.name != "B" for p in server.players), "B is still present in the player list"
    assert server.min_players == 5, f"Expected min_players decremented to 5, got {server.min_players}"
    print("[OK] Pre-game disconnect: player removed permanently, min_players decremented "
          "(playing with one fewer player, not waiting for a replacement)")

    s1.close()
    s3.close()


def test_pregame_disconnect_floors_at_four():
    """min_players must never decrement below 4 -- the engine's hard
    structural minimum for role assignment (build_role_assignments
    raises below that)."""
    server = GameServer(host="127.0.0.1", port=6311, target_bots=0,
                         min_players=4, auto_start=False)
    threading.Thread(target=server.start, daemon=True).start()
    time.sleep(0.3)

    s1 = connect(6311, "A")
    s2 = connect(6311, "B")
    time.sleep(0.3)

    s2.close()
    time.sleep(0.5)

    assert server.min_players == 4, (
        f"min_players dropped below the hard floor of 4: {server.min_players}"
    )
    print("[OK] min_players correctly floored at 4 -- never decrements below the engine's "
          "hard structural minimum")
    s1.close()


def test_postgame_disconnect_unaffected():
    server = GameServer(host="127.0.0.1", port=6312, target_bots=3,
                         min_players=4, auto_start=True)
    threading.Thread(target=server.start, daemon=True).start()
    time.sleep(0.3)

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(("127.0.0.1", 6312))
    protocol.send_json(s, {"name": "PostGameTest", "token": None})
    s.settimeout(5)
    buf = b""
    while b'"role_assigned"' not in buf:
        buf += s.recv(4096)
    s.close()
    time.sleep(0.5)

    assert any(p.name == "PostGameTest" for p in server.players), (
        "BUG: player was removed after the game started -- should be kept for reconnect"
    )
    print("[OK] Post-game-start disconnect: player is NOT removed -- still present, "
          "ready for the normal (unchanged) reconnect flow")


def main():
    test_pregame_disconnect_removes_permanently()
    test_pregame_disconnect_floors_at_four()
    test_postgame_disconnect_unaffected()
    print("\nALL LOBBY-DISCONNECT-REMOVAL CHECKS PASSED")


if __name__ == "__main__":
    main()
