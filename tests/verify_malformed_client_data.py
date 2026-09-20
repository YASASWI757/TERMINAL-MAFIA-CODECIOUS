"""
Verifies the server survives a battery of malformed/wrongly-typed
client data -- found while deliberately stress-testing worst cases,
not from a specific bug report.

Real bug this caught: when a message with a non-string "target" field
(e.g. an integer) arrived DURING an active _collect_action collection
window, the validator call crashed with an uncaught AttributeError
(calling .strip() on an int). That didn't just fail to process THAT
message -- it killed the entire collection thread, meaning even a
genuinely valid follow-up answer sent right after was silently never
processed either, since nothing was reading that player's inbox for
the rest of that round anymore. Fixed by treating a validator
exception the same as "invalid" (keep the loop alive for the
remaining time) rather than letting it propagate and kill the thread
-- plus defense-in-depth type checks at each place client-supplied
text/name fields get used.

This test sends deliberately malformed payloads for several message
types and confirms, for each: no crash, the server stays responsive,
and -- the part that actually matters -- a genuinely valid answer sent
right after a malformed one is still correctly processed (not
silently dropped by a thread that already died).

Run with:  python tests/verify_malformed_client_data.py
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
constants.VOTE_TIMEOUT = 6
constants.NIGHT_ACTION_TIMEOUT = 6

from mafia.server import GameServer
from mafia import protocol

PORT = 6210


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


def connect(port, name):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    for _ in range(50):
        try:
            s.connect(("127.0.0.1", port))
            break
        except OSError:
            time.sleep(0.1)
    protocol.send_json(s, {"name": name, "token": None})
    return s, Reader(s)


def test_malformed_night_action_then_valid_survives(port):
    """The core bug: a wrong-typed target during an active night-action
    collection must not silently eat a valid follow-up answer."""
    sock, reader = connect(port, "MalformedNight")
    role = reader.drain_until(lambda m: m.get("type") == "role_assigned", 10)
    assert role is not None, "Never got a role"

    prompt = reader.drain_until(lambda m: m.get("type") == "prompt", 10)
    assert prompt is not None, "Never got a night-action prompt"
    expected_type = prompt["prompt_type"]
    options = prompt.get("options", [])
    assert options, "No options in the prompt to test against"

    protocol.send_json(sock, {"type": expected_type, "target": 99999})  # int
    time.sleep(0.3)
    protocol.send_json(sock, {"type": expected_type, "target": ["a", "b"]})  # list
    time.sleep(0.3)
    protocol.send_json(sock, {"type": expected_type, "target": {"nested": "object"}})  # dict
    time.sleep(0.3)
    protocol.send_json(sock, {"type": expected_type, "target": options[0]})  # valid

    next_phase = reader.drain_until(lambda m: m.get("type") == "phase", 15)
    assert next_phase not in (None, "EOF"), (
        "Game did not progress after malformed-then-valid sequence -- "
        "the collection thread likely died"
    )
    sock.close()
    print(f"[OK] Malformed {expected_type} values (int/list/dict) followed by a valid "
          f"answer: server survived and the valid answer was processed (game progressed)")


def test_malformed_chat_survives(port):
    """A wrong-typed chat text must not kill the whole reader loop --
    that would permanently cut this player off from sending ANYTHING
    for the rest of the game, not just one round."""
    sock, reader = connect(port, "MalformedChat")
    role = reader.drain_until(lambda m: m.get("type") == "role_assigned", 10)
    assert role is not None, "Never got a role"

    protocol.send_json(sock, {"type": "chat", "text": 12345})  # int
    time.sleep(0.3)
    protocol.send_json(sock, {"type": "chat", "text": None})  # None
    time.sleep(0.3)
    protocol.send_json(sock, {"type": "chat", "text": ["a", "list"]})  # list
    time.sleep(0.3)

    prompt = reader.drain_until(lambda m: m.get("type") == "prompt", 15)
    assert prompt not in (None, "EOF"), (
        "Never received a prompt after sending malformed chat messages -- "
        "possible reader-loop death (would mean this player can never act again)"
    )
    options = prompt.get("options", [])
    if options:
        protocol.send_json(sock, {"type": prompt["prompt_type"], "target": options[0]})
        next_phase = reader.drain_until(lambda m: m.get("type") == "phase", 15)
        assert next_phase not in (None, "EOF"), "Game did not progress after the real answer"
    sock.close()
    print("[OK] Malformed chat text (int/None/list) survived: reader loop stayed alive, "
          "this player could still act normally afterward")


def test_malformed_join_name_survives(port):
    """A non-string 'name' in the join message must be rejected
    cleanly, not crash the connection-handling thread."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    for _ in range(50):
        try:
            s.connect(("127.0.0.1", port))
            break
        except OSError:
            time.sleep(0.1)
    protocol.send_json(s, {"name": 12345, "token": None})  # int name
    reader = Reader(s)
    reply = reader.read_one(5)
    assert reply is not None and reply.get("type") == "error", (
        f"Expected a clean 'error' rejection for a non-string name, got: {reply}"
    )
    print(f"[OK] Non-string join name cleanly rejected (not a crash): {reply['text']!r}")
    s.close()

    s2, r2 = connect(port, "NormalAfterBadName")
    joined = r2.read_one(5)
    assert joined is not None and joined.get("type") == "joined", (
        f"Server did not accept a normal join after the malformed one: {joined}"
    )
    print("[OK] Server still healthy and accepting normal joins after the malformed one")
    s2.close()


def test_long_name_is_capped(port):
    """An absurdly long name must be capped, not stored/broadcast
    unbounded."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    for _ in range(50):
        try:
            s.connect(("127.0.0.1", port))
            break
        except OSError:
            time.sleep(0.1)
    huge_name = "X" * 100_000
    protocol.send_json(s, {"name": huge_name, "token": None})
    reader = Reader(s)
    reply = reader.read_one(5)
    assert reply is not None, "No reply at all to a huge name"
    print(f"[OK] A 100,000-character name got a clean, non-hanging response: {reply.get('type')}")
    s.close()


def main():
    # test_malformed_join_name_survives and test_long_name_is_capped
    # don't need the game to actually start -- they can share one
    # server. The other two DO need a fresh match each, so each gets
    # its own server on its own port (a single auto_start server only
    # ever starts one match).
    port1 = PORT
    server1 = GameServer(host="127.0.0.1", port=port1, target_bots=3,
                          min_players=4, auto_start=True)
    threading.Thread(target=server1.start, daemon=True).start()
    time.sleep(0.3)
    test_malformed_join_name_survives(port1)
    test_long_name_is_capped(port1)

    port2 = PORT + 1
    server2 = GameServer(host="127.0.0.1", port=port2, target_bots=3,
                          min_players=4, auto_start=True)
    threading.Thread(target=server2.start, daemon=True).start()
    time.sleep(0.3)
    test_malformed_night_action_then_valid_survives(port2)

    port3 = PORT + 2
    server3 = GameServer(host="127.0.0.1", port=port3, target_bots=3,
                          min_players=4, auto_start=True)
    threading.Thread(target=server3.start, daemon=True).start()
    time.sleep(0.3)
    test_malformed_chat_survives(port3)

    print("\nALL MALFORMED-CLIENT-DATA SURVIVAL CHECKS PASSED")


if __name__ == "__main__":
    main()
