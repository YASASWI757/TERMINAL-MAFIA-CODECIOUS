"""
Verifies the fix for the lobby screen staying stuck after reconnect:
the server only ever re-sends role_assigned once, at the start of a
match -- never on reconnect -- so state_snapshot (which IS sent on
every reconnect once the game has started) is the only signal a
reconnecting client gets that the game is already underway. Without
explicitly clearing in_lobby there, a reconnecting player's screen
stayed on "LOBBY / Players connected: 0 / 0 / Waiting for the host..."
forever, even while the real game kept progressing normally
underneath (this is exactly what the bug report's screenshot showed).

Uses a real curses client attached to a real pty, disconnected and
reconnected with the same --name (so it picks up its saved token file
automatically), and renders the actual terminal output via pyte (a
terminal emulator library) to check what's genuinely on screen --
not just that the process didn't crash.

Run with:  python tests/verify_lobby_clears_on_reconnect.py
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
constants.DISCUSSION_TIMEOUT = 30  # generous -- don't want the match to finish mid-test
constants.NIGHT_ACTION_TIMEOUT = 30
constants.VOTE_TIMEOUT = 30

from mafia.server import GameServer

PORT = 6088
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(PROJECT_ROOT, ".mafia_rejoin_ReconnectLobbyTest.token")


def set_pty_size(fd, rows=35, cols=90):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def spawn_client():
    master_fd, slave_fd = pty.openpty()
    set_pty_size(slave_fd, 35, 90)
    env = dict(os.environ)
    env["TERM"] = "linux"
    env["LANG"] = "C.utf8"
    proc = subprocess.Popen(
        [sys.executable, "run_client.py", "--host", "127.0.0.1", "--port", str(PORT),
         "--name", "ReconnectLobbyTest"],
        stdin=slave_fd, stdout=slave_fd, stderr=subprocess.PIPE,
        cwd=PROJECT_ROOT, env=env, close_fds=True,
    )
    os.close(slave_fd)
    return proc, master_fd


def capture(master_fd, duration):
    output = b""
    deadline = time.time() + duration
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
    return output


def render(output, cols=90, rows=35):
    import pyte
    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    stream.feed(output.decode("utf-8", errors="replace"))
    return "\n".join(line.rstrip() for line in screen.display)


def main():
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)  # start clean

    server = GameServer(host="127.0.0.1", port=PORT, target_bots=3,
                         min_players=4, auto_start=True)
    threading.Thread(target=server.start, daemon=True).start()
    time.sleep(0.3)

    # --- First connection: join, let the match actually start ---
    proc1, fd1 = spawn_client()
    output1 = capture(fd1, 4)
    proc1.terminate()
    try:
        proc1.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc1.kill()

    screen1 = render(output1)
    assert "YOUR ROLE" in screen1 or "LOBBY" in screen1, (
        f"First connection never showed anything recognizable:\n{screen1}"
    )
    assert os.path.exists(TOKEN_FILE), "No token file was saved after the first join"
    print("[OK] First connection joined and the match started (token file saved)")

    # Give the server a moment to notice the disconnect.
    time.sleep(1.0)

    # --- Second connection: same name, same folder -> should reconnect,
    # and should NOT be stuck showing the lobby screen, since the game
    # already started. ---
    proc2, fd2 = spawn_client()
    output2 = capture(fd2, 4)
    proc2.terminate()
    try:
        proc2.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc2.kill()

    screen2 = render(output2)
    print("--- Reconnected client's screen ---")
    print(screen2)
    print("--- end screen ---")

    stuck_on_lobby = "Waiting for the host to start the match" in screen2
    assert not stuck_on_lobby, (
        "BUG STILL PRESENT: reconnected client is stuck showing the lobby "
        "screen ('Waiting for the host to start the match...') even though "
        "the game had already started"
    )
    print("[OK] Reconnected client is NOT stuck on the lobby screen")

    showed_reconnect_info = "Reconnected" in screen2 or "Round" in screen2 or "Phase" in screen2
    assert showed_reconnect_info, (
        f"Reconnected client didn't show any recognizable game-state info either:\n{screen2}"
    )
    print("[OK] Reconnected client shows real game state instead")

    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)

    print("\nALL LOBBY-CLEARS-ON-RECONNECT CHECKS PASSED")


if __name__ == "__main__":
    main()
