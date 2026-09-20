"""
Verifies the fix for a real race condition found during deliberate
worst-case stress testing: reconnect matching required the OLD
connection to already be marked disconnected (player.connected ==
False), but that flag is only set once the old connection's reader
thread notices its socket died (an EOF on recv()) -- which is NOT
synchronous with the client calling close(). TCP teardown takes a
small but real amount of time. A client that reconnects fast enough
(plausible on a flaky connection retrying immediately) could arrive
before the server had processed the old connection's death, and would
be wrongly rejected with "Game already in progress" even though it's
the same legitimate player with the correct token.

Also re-verifies (critically) that this fix did NOT weaken hijack
protection: a token mismatch must still be rejected even when the
existing player is still marked connected.

Run with:  python tests/verify_reconnect_race.py
"""

import socket
import threading
import time
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mafia import constants
constants.DISCUSSION_TIMEOUT = 30
constants.NIGHT_ACTION_TIMEOUT = 30
constants.VOTE_TIMEOUT = 30

from mafia.server import GameServer
from mafia import protocol

PORT = 6260


class Reader:
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
                try:
                    return json.loads(line.decode())
                except Exception:
                    continue
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


def main():
    server = GameServer(host="127.0.0.1", port=PORT, target_bots=3,
                         min_players=4, auto_start=True)
    threading.Thread(target=server.start, daemon=True).start()
    time.sleep(0.3)

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(("127.0.0.1", PORT))
    protocol.send_json(s, {"name": "ChurnTest", "token": None})
    r = Reader(s)
    joined = r.read_one(5)
    token = joined.get("token")
    assert token, "No token issued on first join"
    role = r.drain_until(lambda m: m.get("type") == "role_assigned", 10)
    assert role is not None, "Never got a role"
    s.close()

    # Rapidly reconnect many times in a row, as fast as possible -- no
    # delay between closing one connection and opening the next, which
    # is exactly the condition that exposes the race.
    failures = []
    N = 30
    for i in range(N):
        s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s2.connect(("127.0.0.1", PORT))
        protocol.send_json(s2, {"name": "ChurnTest", "token": token})
        r2 = Reader(s2)
        reply = r2.read_one(3)
        if not reply or reply.get("type") != "joined":
            failures.append((i, reply))
        s2.close()

    print(f"Completed {N} rapid reconnect cycles, failures: {len(failures)}")
    if failures:
        print(f"First few failures: {failures[:5]}")
    assert not failures, (
        f"{len(failures)}/{N} rapid reconnect cycles were wrongly rejected -- "
        f"the stale-connection race is back"
    )
    print(f"[OK] All {N} rapid reconnect cycles succeeded with zero failures")

    # Confirm state survived intact through all that churn.
    s3 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s3.connect(("127.0.0.1", PORT))
    protocol.send_json(s3, {"name": "ChurnTest", "token": token})
    r3 = Reader(s3)
    final_snapshot = r3.drain_until(lambda m: m.get("type") == "state_snapshot", 5)
    assert final_snapshot not in (None, "EOF"), "No state_snapshot after final reconnect"
    assert final_snapshot["role"] == role["role"], (
        f"Role changed unexpectedly through churn: {role['role']} -> {final_snapshot['role']}"
    )
    print(f"[OK] Role ({final_snapshot['role']}) correctly preserved through all {N} reconnect cycles")

    # --- Critical: confirm this fix did NOT weaken hijack protection.
    # A wrong-token attempt must still be rejected, including right
    # after this player is (still) marked connected from the snapshot
    # request above.
    hijack_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    hijack_sock.connect(("127.0.0.1", PORT))
    protocol.send_json(hijack_sock, {"name": "ChurnTest", "token": "totally-wrong-token"})
    hijack_reader = Reader(hijack_sock)
    hijack_reply = hijack_reader.read_one(5)
    assert hijack_reply is not None and hijack_reply.get("type") == "error", (
        f"HIJACK PROTECTION WEAKENED by the race fix: wrong-token attempt "
        f"was not rejected: {hijack_reply}"
    )
    print(f"[OK] Hijack protection still intact after the race fix: {hijack_reply['text'][:60]}...")
    hijack_sock.close()

    s3.close()
    print("\nALL RECONNECT-RACE CHECKS PASSED")


if __name__ == "__main__":
    main()
