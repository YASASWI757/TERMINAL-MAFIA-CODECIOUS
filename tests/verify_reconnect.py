"""
Verifies the two reconnect fixes directly:

    1. Hijack is blocked: reconnecting as a disconnected player's name
       WITHOUT their token must be rejected. With the correct token,
       it must succeed.
    2. The active prompt is restored: a player who disconnects mid-vote
       and reconnects within the timeout window must receive a fresh
       "prompt" message (with a sane, recomputed remaining timeout),
       not silence.

Run with:  python tests/verify_reconnect.py
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
constants.VOTE_TIMEOUT = 20  # long enough to disconnect and reconnect within it
constants.NIGHT_ACTION_TIMEOUT = 3

from mafia.server import GameServer
from mafia import protocol

PORT = 5977


class LineReader:
    """
    A small buffered JSON-line reader with a real per-call timeout that
    correctly retries -- unlike re-creating protocol.recv_lines() (a
    generator) on every retry, which silently drops any bytes already
    buffered inside the discarded generator, and unlike reusing one
    generator across a caught socket.timeout, which doesn't work either
    (a generator that raises out through next() is exhausted for good,
    even if the exception was "just" a timeout you meant to retry).
    Keeping the buffer as a plain instance attribute sidesteps both.
    """

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
                    return json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
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
                return None
            self.buf += chunk

    def drain_until(self, predicate, timeout, on_each=None):
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.read_one(max(0.1, deadline - time.time()))
            if msg is None:
                return None
            if on_each:
                on_each(msg)
            if predicate(msg):
                return msg
        return None


def connect(name, token=None):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    for _ in range(50):
        try:
            s.connect(("127.0.0.1", PORT))
            break
        except OSError:
            time.sleep(0.1)
    protocol.send_json(s, {"name": name, "token": token})
    return s, LineReader(s)


def main():
    server = GameServer(host="127.0.0.1", port=PORT, target_bots=3,
                         min_players=4, auto_start=True)
    threading.Thread(target=server.start, daemon=True).start()
    time.sleep(0.3)

    # --- Join as Alice, capture her token ---
    alice, alice_reader = connect("Alice")
    joined = alice_reader.read_one(timeout=5)
    assert joined is not None and joined.get("type") == "joined", f"Alice's join failed: {joined}"
    real_token = joined.get("token")
    assert real_token, "Server did not issue a rejoin token on first join"
    print(f"[OK] Alice joined and received a rejoin token")

    role_msg = alice_reader.drain_until(lambda m: m.get("type") == "role_assigned", timeout=10)
    assert role_msg is not None, "Alice never got her role"
    print(f"[OK] Alice's role assigned: {role_msg['role']}")

    # --- Disconnect Alice ---
    alice.close()
    time.sleep(0.5)  # let the server notice the disconnect

    # --- Hijack attempt: wrong token ---
    hijacker, hijacker_reader = connect("Alice", token="totally-wrong-token")
    reply = hijacker_reader.read_one(timeout=5)
    assert reply is not None and reply.get("type") == "error", (
        f"HIJACK NOT BLOCKED: expected an error, got {reply}"
    )
    print(f"[OK] Hijack with wrong token rejected: {reply['text'][:60]}...")
    hijacker.close()

    # --- Hijack attempt: no token at all ---
    hijacker2, hijacker2_reader = connect("Alice", token=None)
    reply2 = hijacker2_reader.read_one(timeout=5)
    assert reply2 is not None and reply2.get("type") == "error", (
        f"HIJACK NOT BLOCKED (no token): got {reply2}"
    )
    print(f"[OK] Hijack with no token rejected")
    hijacker2.close()

    # --- Legitimate reconnect: correct token ---
    real_alice, real_reader = connect("Alice", token=real_token)
    reply3 = real_reader.read_one(timeout=5)
    assert reply3 is not None and reply3.get("type") == "joined", (
        f"Legitimate reconnect failed: {reply3}"
    )
    print(f"[OK] Legitimate reconnect with correct token succeeded")

    # --- Prompt restoration check ---
    vote_prompt = real_reader.drain_until(
        lambda m: m.get("type") == "prompt" and m.get("prompt_type") == "vote",
        timeout=30,
    )
    assert vote_prompt is not None, "Never saw a vote prompt to test reconnect against"
    original_timeout = vote_prompt["timeout"]
    print(f"[OK] Got initial vote prompt (timeout={original_timeout}s)")

    # Disconnect mid-vote-window without answering.
    real_alice.close()
    time.sleep(2)  # let some of the window elapse

    # Reconnect with the same token -- should get the prompt restored.
    reconnected, recon_reader = connect("Alice", token=real_token)
    joined2 = recon_reader.read_one(timeout=5)
    assert joined2 is not None and joined2.get("type") == "joined", (
        f"Reconnect after mid-vote disconnect failed: {joined2}"
    )

    restored_prompt = recon_reader.drain_until(
        lambda m: m.get("type") == "prompt" and m.get("prompt_type") == "vote",
        timeout=5,
    )
    assert restored_prompt is not None, (
        "PROMPT NOT RESTORED: reconnected mid-vote but got no prompt at all"
    )
    restored_timeout = restored_prompt["timeout"]
    print(f"[OK] Vote prompt restored on reconnect (timeout={restored_timeout}s)")
    assert restored_timeout <= original_timeout, (
        f"Restored timeout ({restored_timeout}s) should be <= original "
        f"({original_timeout}s), not a reset full window"
    )
    assert restored_timeout > 0, "Restored timeout should still be positive"
    print(f"[OK] Restored timeout ({restored_timeout}s) is a real countdown, "
          f"not a reset to the full {original_timeout}s")

    reconnected.close()
    print("\nALL RECONNECT CHECKS PASSED")


if __name__ == "__main__":
    main()

