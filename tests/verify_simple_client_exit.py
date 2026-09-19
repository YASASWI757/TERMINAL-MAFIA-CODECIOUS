"""
Verifies the fallback client fix: it must exit on its own after
"Press Enter to close" once the game ends, not run forever (the same
bug that was already fixed for the curses client, but had not been
applied to the --no-curses fallback path).

Unlike the curses client, this one doesn't need a pty -- it's plain
stdin/stdout, so ordinary subprocess pipes work.

Run with:  python tests/verify_simple_client_exit.py
"""

import subprocess
import sys
import os
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mafia import constants
constants.DISCUSSION_TIMEOUT = 2
constants.VOTE_TIMEOUT = 2
constants.NIGHT_ACTION_TIMEOUT = 2

from mafia.server import GameServer

PORT = 5999
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    server = GameServer(host="127.0.0.1", port=PORT, target_bots=3,
                         min_players=4, auto_start=True)
    threading.Thread(target=server.start, daemon=True).start()
    time.sleep(0.3)

    proc = subprocess.Popen(
        [sys.executable, "run_client.py", "--host", "127.0.0.1", "--port", str(PORT),
         "--name", "SimpleExitHuman", "--no-curses"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=PROJECT_ROOT, text=True, bufsize=1,
    )

    captured = []

    def pump_stdout():
        for line in proc.stdout:
            captured.append(line)

    threading.Thread(target=pump_stdout, daemon=True).start()

    # Let the whole match play out with no deliberate input -- the
    # disconnect-safe timeout path already covers unanswered prompts.
    deadline = time.time() + 60
    while time.time() < deadline:
        if any("Press Enter to close" in line for line in captured):
            break
        time.sleep(0.3)

    joined_text = "".join(captured)
    assert "GAME OVER" in joined_text, "Match never reached GAME OVER within 60s"
    assert "Press Enter to close" in joined_text, "Exit prompt never appeared"
    print("[OK] Match completed and the exit prompt appeared")

    # This is the actual fix under test.
    try:
        proc.stdin.write("\n")
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass

    try:
        proc.wait(timeout=8)
        exited_on_its_own = True
    except subprocess.TimeoutExpired:
        exited_on_its_own = False

    assert exited_on_its_own, (
        "Fallback client did NOT exit on its own after Enter -- it would have "
        "run forever, same bug as the curses client had before that fix"
    )
    print(f"[OK] Fallback client exited on its own after Enter (exit code {proc.returncode})")

    time.sleep(0.2)
    joined_text = "".join(captured)
    assert "Goodbye!" in joined_text, "Expected a closing confirmation message but didn't see one"
    print("[OK] 'Goodbye!' confirmation was printed")

    stderr_text = proc.stderr.read()
    assert "Traceback" not in stderr_text, f"Client raised an exception:\n{stderr_text}"
    print("[OK] No traceback on stderr")

    print("\nALL FALLBACK-CLIENT EXIT CHECKS PASSED")


if __name__ == "__main__":
    main()
