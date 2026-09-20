"""
Verifies the defensive hardening added around message processing in
both clients: a single malformed or unexpected message (e.g. missing
a field the handler expects) must never be able to crash the whole
client, or -- for the fallback client -- silently kill its background
listener thread (which would leave it "deaf" to everything from then
on, with no crash and no obvious symptom).

Uses a minimal fake server (not the real GameServer) for full control
over exactly what gets sent: a well-formed welcome, then a genuinely
malformed message ("vote_results" missing the "tally"/"eliminated"/
"eliminated_role" keys the old code accessed by direct indexing), then
a well-formed follow-up. If the client survived the malformed message,
the follow-up will show up in its output; if it didn't, the follow-up
never arrives.

Run with:  python tests/verify_malformed_message_survival.py
"""

import os
import pty
import struct
import fcntl
import termios
import socket
import subprocess
import sys
import threading
import time
import select
import json

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from mafia import protocol

PORT = 6099
MARKER = "SURVIVED_MALFORMED_MESSAGE_MARKER"


def set_pty_size(fd, rows=30, cols=100):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def fake_server():
    """A minimal stand-in for GameServer with full control over exactly
    what gets sent -- not the real game logic, just enough protocol to
    get a client connected and then feed it a bad message."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(2)

    for _ in range(2):  # serve exactly two connections (curses + simple)
        conn, _ = srv.accept()
        try:
            conn.settimeout(5)
            first_msg = next(protocol.recv_lines(conn))  # the join message
            protocol.send_json(conn, {"type": "joined", "text": "Welcome!", "token": "faketoken"})
            time.sleep(0.2)
            # Get the client out of the lobby screen first -- otherwise
            # log-based content (where "info" messages render) never
            # shows on screen at all, by design, regardless of whether
            # the client is actually still alive and processing.
            protocol.send_json(conn, {
                "type": "role_assigned", "role": "VILLAGER",
                "description": "Test role.", "teammates": [],
            })
            time.sleep(0.2)
            # Deliberately malformed: real vote_results always has
            # tally/eliminated/eliminated_role; the old (unguarded)
            # handler code accessed these with msg["..."], which would
            # raise KeyError here.
            protocol.send_json(conn, {"type": "vote_results"})
            time.sleep(0.3)
            # A well-formed follow-up -- if this shows up in the
            # client's output, the client survived the malformed one.
            protocol.send_json(conn, {"type": "info", "text": MARKER})
            time.sleep(1.5)
        except Exception:
            pass
        finally:
            conn.close()
    srv.close()


def test_curses_client():
    master_fd, slave_fd = pty.openpty()
    set_pty_size(slave_fd, 30, 100)
    env = dict(os.environ)
    env["TERM"] = "linux"
    env["LANG"] = "C.utf8"

    proc = subprocess.Popen(
        [sys.executable, "run_client.py", "--host", "127.0.0.1", "--port", str(PORT),
         "--name", "MalformedTestCurses"],
        stdin=slave_fd, stdout=slave_fd, stderr=subprocess.PIPE,
        cwd=PROJECT_ROOT, env=env, close_fds=True,
    )
    os.close(slave_fd)

    output = b""
    deadline = time.time() + 6
    while time.time() < deadline:
        r, _, _ = select.select([master_fd], [], [], 0.3)
        if master_fd in r:
            try:
                chunk = os.read(master_fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk

    still_alive = proc.poll() is None
    proc.terminate()
    try:
        _, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, stderr = proc.communicate(timeout=5)

    os.close(master_fd)
    stderr_text = stderr.decode(errors="replace")
    text = output.decode(errors="replace")

    if MARKER not in text:
        print("=== DIAGNOSTIC: raw captured output ===")
        print(repr(text[-3000:]))
        try:
            import pyte
            screen = pyte.Screen(100, 30)
            stream = pyte.Stream(screen)
            stream.feed(text)
            print("=== DIAGNOSTIC: rendered screen ===")
            print("\n".join(line.rstrip() for line in screen.display))
        except ImportError:
            pass

    assert still_alive, "Curses client process had already exited -- it crashed on the malformed message"
    assert "Traceback" not in stderr_text, f"Curses client raised an exception:\n{stderr_text}"
    assert MARKER in text, (
        f"Curses client never showed the follow-up message after the malformed one -- "
        f"it likely went unresponsive rather than cleanly surviving it"
    )
    print("[OK] Curses client survived a malformed message: stayed alive, no traceback, "
          "and correctly processed the well-formed message that came right after it")


def test_simple_client():
    proc = subprocess.Popen(
        [sys.executable, "run_client.py", "--host", "127.0.0.1", "--port", str(PORT),
         "--name", "MalformedTestSimple", "--no-curses"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=PROJECT_ROOT, text=True, bufsize=1,
    )

    captured = []

    def pump():
        for line in proc.stdout:
            captured.append(line)

    threading.Thread(target=pump, daemon=True).start()
    time.sleep(3.0)

    still_alive = proc.poll() is None
    proc.terminate()
    try:
        _, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, stderr = proc.communicate(timeout=5)

    assert still_alive, "Fallback client process had already exited -- it crashed on the malformed message"
    assert "Traceback" not in stderr, f"Fallback client raised an exception:\n{stderr}"
    assert any(MARKER in line for line in captured), (
        f"Fallback client never showed the follow-up message after the malformed one -- "
        f"its listener thread likely died silently rather than surviving it. Got: {captured}"
    )
    print("[OK] Fallback client survived a malformed message: stayed alive, no traceback, "
          "listener thread kept working, and correctly processed the well-formed message "
          "that came right after it")


def main():
    threading.Thread(target=fake_server, daemon=True).start()
    time.sleep(0.3)

    test_curses_client()
    test_simple_client()

    print("\nALL MALFORMED-MESSAGE SURVIVAL CHECKS PASSED")


if __name__ == "__main__":
    main()
