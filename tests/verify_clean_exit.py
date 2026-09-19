"""
Verifies the curses client actually exits after game over instead of
hanging forever. Since play-again is now offered after every match
with a human in it, the flow is: wait for the play-again prompt (not
the earlier "Press Enter to close", which gets functionally
superseded by it), decline, wait for the resulting connection-lost
message, then confirm Enter at THAT point makes the process exit on
its own -- no SIGTERM needed.
"""

# Run with:  python tests/verify_clean_exit.py

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

    def pump(timeout, until_marker=b"Press Enter to close"):
        """
        Reads until until_marker appears in NEWLY-arrived data (not
        just anywhere in the cumulative buffer) -- important here
        because "Press Enter to close" legitimately appears TWICE in
        this flow (once right after GAME OVER, superseded by the
        play-again prompt; again for real once the connection drops
        after declining), and a naive "is it in captured" check would
        false-positive on the first occurrence when waiting for the
        second.
        """
        nonlocal captured
        start_len = len(captured)
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
            if until_marker in captured[start_len:]:
                return

    # Let the whole match play out with no deliberate input from us --
    # the disconnect-safe timeout path already handles that; we just
    # need it to actually finish. Budget is generous because bots have
    # a randomized 1.5-6s "thinking" delay that can occasionally exceed
    # this test's shortened 2s timeouts, stretching some rounds out
    # (a known, harmless artifact documented elsewhere in this suite).
    pump(timeout=120, until_marker=b"Play again?")
    assert b"GAME OVER" in captured, (
        "Game never reached GAME OVER within 60s -- can't test the exit "
        "prompt without a finished match"
    )
    assert b"Play again?" in captured, "The play-again prompt never appeared after GAME OVER"
    print("[OK] Match completed and the play-again prompt appeared")

    # Decline -- with only this one human in the game, "no" means
    # nobody stays, so the server ends and the connection drops. Wait
    # for the connection-lost message specifically (not "Press Enter
    # to close" again -- that text is logged permanently into
    # scrollback the first time and stays visible in every subsequent
    # redraw, so re-matching on it would false-positive on the old
    # occurrence rather than the genuinely new state).
    os.write(master_fd, b"no\r")
    pump(timeout=15, until_marker=b"Server closed the connection")
    assert b"Server closed the connection" in captured[-4000:], (
        "Declining play-again never led to the connection-lost message"
    )
    print("[OK] Declining play-again led to the connection dropping and a real exit prompt")

    # This is the actual fix under test: pressing Enter here must make
    # the process exit ON ITS OWN.
    os.write(master_fd, b"\r")

    exited_on_its_own = False
    try:
        proc.wait(timeout=8)
        exited_on_its_own = True
    except subprocess.TimeoutExpired:
        pass

    if not exited_on_its_own:
        print("=== DIAGNOSTIC: tail of captured output ===")
        print(captured[-2000:].decode(errors="replace"))
        print("=== process still alive:", proc.poll() is None, "===")

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
