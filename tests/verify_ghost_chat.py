"""
Directly verifies ghost chat's isolation guarantees -- this is the
test that actually matters for this feature, since the whole point
was "0% risk of leaking to the living."

Uses white-box access to the running GameServer (this test runs in
the same process, so it can reach into server.game_state directly) to
deterministically mark specific players dead/alive, rather than
relying on unpredictable gameplay to produce a real elimination --
exact: we know precisely who is and isn't a ghost at every step, which
is what a test of an isolation guarantee needs.

Timing matters here, and got this test wrong on the first attempt: the
white-box mutation MUST NOT happen while the server's own game-loop
thread could still be concurrently mutating the same Player objects
(night resolution, vote resolution). Doing so raced against the
server's own thread and occasionally let a real, legitimate night-kill
or vote elimination land on top of the mutation -- which isn't a ghost
chat bug at all, just an artifact of an unsynchronized test. The fix:
wait for a REAL game_over first. Once that's received, the server is
guaranteed to be parked inside _offer_play_again, which never touches
.alive until either a player answers or its own timeout elapses --
neither of which this test triggers during its checks -- so the
mutation and everything after it happens in a genuinely quiet window.

Checks:
    1. A ghost's message reaches OTHER ghosts.
    2. A ghost's message is NEVER received by a living player -- not
       "we didn't happen to see it", but a real drain-and-check with a
       real wait window.
    3. A living player's attempt to use the ghost_chat channel (e.g. a
       rogue or buggy client) is silently ignored -- nobody receives it,
       living or dead.

Regular chat's own correctness (double-echo fix, dead-players-can't-
speak-in-it) is NOT re-tested here -- that's verify_no_echo.py's job.

Run with:  python tests/verify_ghost_chat.py
"""

import socket
import threading
import time
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mafia import constants
constants.DISCUSSION_TIMEOUT = 3
constants.VOTE_TIMEOUT = 3
constants.NIGHT_ACTION_TIMEOUT = 3

from mafia.server import GameServer
from mafia import protocol

PORT = 6066


class Reader:
    """Persistent-buffer reader -- see verify_reconnect.py for why this
    exists instead of re-creating protocol.recv_lines() per retry."""

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

    def collect_for(self, duration):
        """Reads everything that arrives over `duration` seconds and
        returns it all as a list -- used to prove something did NOT
        arrive, which needs to actually wait out the window rather
        than returning the instant nothing is immediately available."""
        deadline = time.time() + duration
        collected = []
        while time.time() < deadline:
            msg = self.read_one(max(0.05, deadline - time.time()))
            if msg is None or msg == "EOF":
                continue
            collected.append(msg)
        return collected


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
    server = GameServer(host="127.0.0.1", port=PORT, target_bots=1,
                         min_players=4, auto_start=True)
    threading.Thread(target=server.start, daemon=True).start()
    time.sleep(0.3)

    ghost1_sock, ghost1_reader = connect("Ghost1")
    ghost2_sock, ghost2_reader = connect("Ghost2")
    alive_sock, alive_reader = connect("Alive1")

    for name, reader in (("Ghost1", ghost1_reader), ("Ghost2", ghost2_reader), ("Alive1", alive_reader)):
        role = reader.drain_until(lambda m: m.get("type") == "role_assigned", 10)
        assert role is not None, f"{name} never got a role -- game didn't start"
    print("[OK] All 3 humans + 1 bot joined and the match started")

    # Let the real match play out to a genuine conclusion -- no
    # scripted responses needed, the disconnect-safe timeout path
    # covers every prompt none of these three sockets ever answers.
    # This is the fix for the race described in the module docstring:
    # once game_over is confirmed, the server is guaranteed to be
    # parked inside _offer_play_again, not concurrently mutating
    # anyone's .alive. All three clients receive the same broadcast,
    # so waiting on just one is sufficient.
    game_over = ghost1_reader.drain_until(lambda m: m.get("type") == "game_over", 120)
    assert game_over not in (None, "EOF"), "Match never reached a real game_over within 120s"
    print(f"[OK] Real match played out to game_over ({game_over['winner']} won) -- "
          f"server is now safely parked awaiting play-again answers")

    # NOW it's safe to do the white-box mutation: nothing else will
    # touch .alive until this test either answers play_again or lets
    # PLAY_AGAIN_TIMEOUT (20s by default) elapse, and every check below
    # finishes well inside that window.
    gs = server.game_state
    ghost1_player = gs.get_player_by_name("Ghost1")
    ghost2_player = gs.get_player_by_name("Ghost2")
    alive_player = gs.get_player_by_name("Alive1")
    assert ghost1_player and ghost2_player and alive_player, "Could not find all 3 players in game_state"
    ghost1_player.alive = False
    ghost2_player.alive = False
    alive_player.alive = True
    print("[OK] Ghost1 and Ghost2 set dead, Alive1 set alive -- deterministically, "
          "in the safe post-game_over window")

    # --- Check 1 & 2: ghost-to-ghost delivery, and non-delivery to the living ---
    MARKER_1 = "SECRET_GHOST_MESSAGE_ALPHA"
    protocol.send_json(ghost1_sock, {"type": "ghost_chat", "text": MARKER_1})

    received_by_ghost2 = ghost2_reader.drain_until(
        lambda m: m.get("type") == "ghost_chat" and m.get("text") == MARKER_1, 5
    )
    assert received_by_ghost2 is not None, "Ghost2 never received Ghost1's ghost_chat message"
    assert received_by_ghost2["sender"] == "Ghost1"
    print("[OK] Ghost1's message was delivered to Ghost2 (other ghost)")

    # Real wait, not an instant check -- proves it never arrives, not
    # just that it hadn't arrived yet by some earlier point.
    alive_saw = alive_reader.collect_for(4)
    leaked = [m for m in alive_saw if m.get("type") == "ghost_chat" or MARKER_1 in json.dumps(m)]
    assert not leaked, f"LEAK: living player Alive1 received ghost chat content: {leaked}"
    assert alive_player.alive is True, "Test invariant broken: Alive1 should still be alive here"
    print("[OK] Alive1 (living) received NOTHING related to the ghost message "
          f"({len(alive_saw)} unrelated message(s) during the wait window)")

    # --- Check 3: a living player using the ghost channel is ignored ---
    MARKER_2 = "ROGUE_ALIVE_GHOST_ATTEMPT"
    assert alive_player.alive is True, "Test invariant broken before Check 3: Alive1 should still be alive"
    protocol.send_json(alive_sock, {"type": "ghost_chat", "text": MARKER_2})

    ghost1_saw = ghost1_reader.collect_for(4)
    ghost2_saw = ghost2_reader.collect_for(2)  # a bit shorter, just corroborating
    leaked_to_ghost1 = [m for m in ghost1_saw if MARKER_2 in json.dumps(m)]
    leaked_to_ghost2 = [m for m in ghost2_saw if MARKER_2 in json.dumps(m)]
    assert not leaked_to_ghost1, f"Living player's ghost_chat attempt reached Ghost1: {leaked_to_ghost1}"
    assert not leaked_to_ghost2, f"Living player's ghost_chat attempt reached Ghost2: {leaked_to_ghost2}"
    print("[OK] A living player's ghost_chat attempt was silently ignored -- nobody received it")

    for s in (ghost1_sock, ghost2_sock, alive_sock):
        s.close()
    print("\nALL GHOST-CHAT ISOLATION CHECKS PASSED")


if __name__ == "__main__":
    main()
