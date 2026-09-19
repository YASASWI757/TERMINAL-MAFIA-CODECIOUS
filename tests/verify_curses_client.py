"""
Smoke test for the curses client -- curses needs a real TTY, so it
can't be driven the same way as the plain-text integration test. This
spawns run_client.py as a real subprocess attached to a pseudo-terminal
(pty), connects it to a real running GameServer, feeds it scripted
keystrokes through the pty, and checks that:
    - it starts without crashing (no traceback on stderr)
    - it produces real screen output (curses actually drew something)
    - it accepts a submitted answer (a night-action / vote keystroke)
    - it shuts down cleanly on SIGTERM with no traceback

This does NOT assert on exact screen content (parsing raw terminal
escape sequences meaningfully would need a full terminal emulator) --
it's a structural smoke test proving the new curses code path actually
runs end-to-end against a live server, not just that it compiles.

Run with:  python tests/verify_curses_client.py
"""

import os
import pty
import struct
import fcntl
import termios
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mafia import constants
constants.DISCUSSION_TIMEOUT = 5
constants.VOTE_TIMEOUT = 5
constants.NIGHT_ACTION_TIMEOUT = 5

from mafia.server import GameServer

PORT = 5933
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def set_pty_size(fd, rows=30, cols=100):
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def main():
    # Real server, bots-filled, auto-start -- same pattern as the other
    # integration test.
    server = GameServer(host="127.0.0.1", port=PORT, target_bots=3,
                         min_players=4, auto_start=True)
    threading.Thread(target=server.start, daemon=True).start()
    time.sleep(0.3)

    master_fd, slave_fd = pty.openpty()
    set_pty_size(slave_fd, 30, 100)

    env = dict(os.environ)
    env["TERM"] = "linux"
    env["LANG"] = "C.utf8"

    proc = subprocess.Popen(
        [sys.executable, "run_client.py", "--host", "127.0.0.1",
         "--port", str(PORT), "--name", "PtyTestHuman"],
        stdin=slave_fd, stdout=slave_fd, stderr=subprocess.PIPE,
        cwd=PROJECT_ROOT, env=env, close_fds=True,
    )
    os.close(slave_fd)

    def read_available(timeout=2.0):
        import select
        chunks = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            r, _, _ = select.select([master_fd], [], [], 0.2)
            if master_fd in r:
                try:
                    chunk = os.read(master_fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            elif chunks:
                break  # got something already and no more is arriving
        return b"".join(chunks)

    # 1. Let it start up and draw the initial screen.
    time.sleep(1.0)
    output1 = read_available(2.0)
    assert proc.poll() is None, f"Client process exited early (code {proc.poll()})"
    assert len(output1) > 0, "Curses client produced no screen output at all"
    print(f"[OK] Client started and drew a screen ({len(output1)} bytes of terminal output)")

    # 2. Wait for role assignment + night phase, then submit a keystroke
    #    for whatever night-action prompt we get (works regardless of
    #    which role we're assigned, since we just type the first
    #    plausible-looking token and hit Enter -- we're testing that
    #    keystrokes are accepted and don't crash the UI, not exercising
    #    exact game logic, which the other test suites already cover).
    time.sleep(4.0)
    os.write(master_fd, b"x\r")  # harmless keystroke + Enter (likely "invalid", that's fine)
    time.sleep(1.0)
    output2 = read_available(2.0)
    assert proc.poll() is None, f"Client crashed after keystroke (code {proc.poll()})"
    print(f"[OK] Client accepted a keystroke + Enter without crashing "
          f"({len(output2)} more bytes of output)")

    # 3. Let a bit more of the game play out (discussion/vote prompts),
    #    sending a couple more harmless keystrokes along the way.
    for _ in range(3):
        time.sleep(2.0)
        os.write(master_fd, b"hello\r")
        assert proc.poll() is None, "Client crashed mid-game"

    print("[OK] Client survived multiple rounds of keystroke input without crashing")

    # 4. Clean shutdown.
    proc.terminate()
    try:
        _, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, stderr = proc.communicate(timeout=5)

    stderr_text = stderr.decode(errors="replace")
    assert "Traceback" not in stderr_text, f"Client raised an exception:\n{stderr_text}"
    print("[OK] Client shut down cleanly on terminate (no traceback on stderr)")

    os.close(master_fd)
    print("\nALL CURSES CLIENT SMOKE-TEST CHECKS PASSED")


if __name__ == "__main__":
    main()
