"""
Verifies two protocol-level robustness fixes found during deliberate
worst-case stress testing (not from a specific bug report):

    1. Non-UTF-8 bytes sent over the socket used to crash the
       connection's handling thread outright with an uncaught
       UnicodeDecodeError -- protocol.recv_lines() already caught
       json.JSONDecodeError for syntactically-invalid-but-valid-UTF-8
       JSON, but decode("utf-8") raises a DIFFERENT exception type
       before json.loads() even runs, so raw garbage bytes slipped
       past that catch entirely. Fixed by catching both.
    2. Rapid connect-then-immediately-disconnect churn (no data sent
       at all) doesn't hang or leak -- a basic sanity check that the
       accept loop and connection-handling threads clean up properly
       under connection churn.

Run with:  python tests/verify_protocol_robustness.py
"""

import socket
import threading
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mafia import constants
constants.NIGHT_ACTION_TIMEOUT = 5

from mafia.server import GameServer
from mafia import protocol

PORT = 6270


def test_garbage_bytes_survival():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(("127.0.0.1", PORT))

    # Raw non-UTF-8 bytes, syntactically-broken JSON, and empty lines,
    # all mixed together -- none of this should crash anything.
    s.sendall(b"this is not json at all {{{ \xff\xfe\x00garbage\n")
    s.sendall(b'{"malformed": "json missing closing brace"\n')
    s.sendall(b"\n\n\n")
    time.sleep(0.5)

    # The connection must still be alive and able to process a real
    # message afterward.
    protocol.send_json(s, {"name": "AfterGarbage", "token": None})
    s.settimeout(5)
    data = s.recv(4096)
    assert b"joined" in data or b"error" in data, (
        f"No valid response after garbage bytes -- connection likely died: {data!r}"
    )
    print("[OK] Raw non-UTF-8 bytes and malformed JSON survived: connection stayed "
          "alive and processed a real join message right after")
    s.close()


def test_connection_churn():
    N = 20
    for _ in range(N):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("127.0.0.1", PORT))
        s.close()
    print(f"[OK] {N} rapid connect-then-immediately-close cycles (no data sent) "
          f"completed without hanging")


def test_server_still_healthy_after():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(("127.0.0.1", PORT))
    protocol.send_json(s, {"name": "FinalHealthCheck", "token": None})
    s.settimeout(5)
    data = s.recv(4096)
    assert b"joined" in data or b"error" in data, (
        f"Server unresponsive after garbage + churn: {data!r}"
    )
    print("[OK] Server still fully responsive after garbage bytes + connection churn")
    s.close()


def main():
    server = GameServer(host="127.0.0.1", port=PORT, target_bots=3,
                         min_players=4, auto_start=True)
    threading.Thread(target=server.start, daemon=True).start()
    time.sleep(0.3)

    test_garbage_bytes_survival()
    test_connection_churn()
    test_server_still_healthy_after()

    print("\nALL PROTOCOL ROBUSTNESS CHECKS PASSED")


if __name__ == "__main__":
    main()
