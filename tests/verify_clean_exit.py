"""
Directly verifies the fix for "the terminal doesn't close": after
game_over, the curses client must actually exit on its own (once the
player presses Enter at the "Press Enter to close" prompt) -- not sit
there in raw terminal mode forever, which would leave curses.wrapper's
endwin() never called and the user's terminal stuck.

Runs the real client as a subprocess attached to a pseudo-terminal
against a real (short-timeout) server, lets a full game play out
(without ever sending it a deliberate answer -- the disconnect-safe
timeout path already covers that), waits for the GAME OVER banner to
appear in the captured output, sends a single Enter keystroke, and
asserts the process exits **on its own** shortly after -- no
SIGTERM/SIGKILL needed -- with the expected "Terminal restored."
confirmation printed and no traceback on stderr.

Run with:  python tests/verify_clean_exit.py
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
import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mafia import constants
constants.DISCUSSION_TIMEOUT = 2
constants.VOTE_TIMEOUT = 2
constants.NIGHT_ACTION_TIMEOUT = 2

from mafia.server import GameServer

PORT = 5966
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def set_pty_size(fd, rows=30, cols=100):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def main():
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
         "--port", str(PORT), "--name", "ExitTestHuman"],
        stdin=slave_fd, stdout=slave_fd, stderr=subprocess.PIPE,
        cwd=PROJECT_ROOT, env=env, close_fds=True,
    )
    os.close(slave_fd)

    captured = b""

    def pump(timeout):
        nonlocal captured
        deadline = time.time() + timeout
        while time.time() < deadline:
            r, _, _ = select.select([master_fd], [], [], 0.2)
            if master_fd in r:
                try:
                    chunk = os.read(master_fd, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                captured += chunk
            if b"Press Enter to close" in captured:
                return

    # Let the whole match play out with no deliberate input from us --
    # the disconnect-safe timeout path already handles that; we just
    # need it to actually finish.
    pump(timeout=60)
    assert b"GAME OVER" in captured, (
        "Game never reached GAME OVER within 60s -- can't test the exit "
        "prompt without a finished match"
    )
    assert b"Press Enter to close" in captured, (
        "GAME OVER happened but the 'Press Enter to close' prompt never appeared"
    )
    print("[OK] Match completed and the exit prompt appeared")

    # This is the actual fix under test: pressing Enter here must make
    # the process exit ON ITS OWN.
    os.write(master_fd, b"\r")

    exited_on_its_own = False
    try:
        proc.wait(timeout=8)
        exited_on_its_own = True
    except subprocess.TimeoutExpired:
        pass

    assert exited_on_its_own, (
        "Client did NOT exit on its own after Enter at the close prompt -- "
        "this is the bug: the curses loop would spin forever and the "
        "terminal would never be restored"
    )
    print(f"[OK] Client exited on its own after Enter (exit code {proc.returncode})")

    # Drain whatever's left (the "Terminal restored." line prints AFTER
    # curses.wrapper returns, in plain mode, once the process is exiting).
    time.sleep(0.3)
    r, _, _ = select.select([master_fd], [], [], 1.0)
    if master_fd in r:
        try:
            captured += os.read(master_fd, 65536)
        except OSError:
            pass

    assert b"Terminal restored" in captured, (
        "Process exited but never printed the post-curses confirmation -- "
        "suggests it exited via a crash path rather than the clean one"
    )
    print("[OK] 'Terminal restored. Goodbye!' confirmation was printed")

    _, stderr = proc.communicate(timeout=5)
    stderr_text = stderr.decode(errors="replace")
    assert "Traceback" not in stderr_text, f"Client raised an exception:\n{stderr_text}"
    print("[OK] No traceback on stderr")

    os.close(master_fd)
    print("\nALL CLEAN-EXIT CHECKS PASSED")


if __name__ == "__main__":
    main()
