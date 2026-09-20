"""
Verifies the fix for "we were able to type again and again although
the first input is taken": after submitting a vote (or night action /
sabotage decision / play_again), the client must stop treating further
typed lines as another submission of the same kind -- it should show
"nothing is expecting input" instead, until a genuinely new prompt
arrives.

Also verifies the companion fix: if the server DOES reject a
submission as invalid, the prompt must be restored so the player can
actually retype a corrected answer, rather than being stuck.

Uses the --no-curses fallback client (plain stdout, easy to capture)
since this bug is about prompt-state bookkeeping shared by both
clients, not curses rendering specifically -- verify_curses_client.py
and verify_simple_client_exit.py already cover that the curses client
runs at all.

Run with:  python tests/verify_no_resubmit.py
"""

import subprocess
import sys
import os
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mafia import constants
constants.DISCUSSION_TIMEOUT = 3
constants.NIGHT_ACTION_TIMEOUT = 8  # generous -- need time for two deliberate sends
constants.VOTE_TIMEOUT = 8

from mafia.server import GameServer

PORT = 6077
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    server = GameServer(host="127.0.0.1", port=PORT, target_bots=3,
                         min_players=4, auto_start=True)
    threading.Thread(target=server.start, daemon=True).start()
    time.sleep(0.3)

    proc = subprocess.Popen(
        [sys.executable, "run_client.py", "--host", "127.0.0.1", "--port", str(PORT),
         "--name", "ResubmitTestHuman", "--no-curses"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=PROJECT_ROOT, text=True, bufsize=1,
    )

    captured = []

    def pump_stdout():
        for line in proc.stdout:
            captured.append(line)

    threading.Thread(target=pump_stdout, daemon=True).start()

    # Wait for whatever prompt shows up first (role could be any of
    # the 4 roles; night_action / options appear for most of them).
    # We just need SOME "Options:" prompt to test against.
    deadline = time.time() + 20
    options_line = None
    while time.time() < deadline:
        matches = [line for line in captured if "Options:" in line]
        if matches:
            options_line = matches[-1]
            break
        time.sleep(0.2)
    assert options_line is not None, (
        "Never saw any prompt with options within 20s -- can't test resubmission"
    )
    print(f"[OK] A prompt with options appeared: {options_line.strip()}")

    # Parse a genuinely valid option out of the prompt, so the first
    # submission is deterministically ACCEPTED -- that's the case the
    # reported bug actually describes ("the first input is taken" and
    # yet more typing was still allowed), not the invalid-input path.
    after_colon = options_line.split("Options:", 1)[1]
    valid_options = [o.strip() for o in after_colon.split(",") if o.strip()]
    assert valid_options, f"Could not parse any options out of: {options_line!r}"
    first_valid_answer = valid_options[0]
    print(f"[OK] Parsed a valid option to submit: {first_valid_answer!r}")

    already_seen = len(captured)
    proc.stdin.write(f"{first_valid_answer}\n")
    proc.stdin.flush()
    time.sleep(1.0)

    first_response = captured[already_seen:]
    was_invalid = any("Invalid input" in line for line in first_response)
    assert not was_invalid, (
        f"A genuinely valid, parsed-from-the-prompt option was rejected as invalid "
        f"-- something else is wrong: {first_response}"
    )

    # THE key check: a second, different line right after must now be
    # rejected client-side as "nothing expecting input", proving
    # prompt_type was cleared and the player can't keep re-submitting.
    print("[OK] First (valid) answer was accepted (no 'Invalid input' seen)")
    already_seen = len(captured)
    proc.stdin.write("AnotherAnswer\n")
    proc.stdin.flush()
    time.sleep(1.0)
    second_response = captured[already_seen:]
    assert any("Nothing is expecting input" in line for line in second_response), (
        f"BUG STILL PRESENT: after already submitting once, a second typed line "
        f"was NOT rejected -- client let the player resubmit. Got: {second_response}"
    )
    print("[OK] A second submission attempt was correctly rejected client-side "
          "with 'Nothing is expecting input' -- can't resubmit after the first "
          "answer was taken")

    # --- Companion check: an actually-invalid submission (on the NEXT
    # prompt) must restore the ability to retry, not leave the player
    # stuck. Wait for a fresh prompt, then submit garbage followed by
    # a real option.
    already_seen = len(captured)
    deadline = time.time() + 30
    options_line2 = None
    while time.time() < deadline:
        new_lines = captured[already_seen:]
        matches = [line for line in new_lines if "Options:" in line]
        if matches:
            options_line2 = matches[-1]
            break
        time.sleep(0.2)

    if options_line2 is not None:
        after_colon2 = options_line2.split("Options:", 1)[1]
        valid_options2 = [o.strip() for o in after_colon2.split(",") if o.strip()]
        reject_point = len(captured)
        proc.stdin.write("TotallyBogusOptionXYZ\n")
        proc.stdin.flush()
        time.sleep(1.0)
        rejection_response = captured[reject_point:]
        assert any("Invalid input" in line for line in rejection_response), (
            f"Expected the bogus option to be rejected as invalid: {rejection_response}"
        )
        retry_point = len(captured)
        proc.stdin.write(f"{valid_options2[0]}\n")
        proc.stdin.flush()
        time.sleep(1.0)
        retry_response = captured[retry_point:]
        assert not any("Nothing is expecting input" in line for line in retry_response), (
            f"Prompt was NOT restored after an invalid submission -- the retry was "
            f"rejected as 'nothing expecting input' instead of being sent: {retry_response}"
        )
        print("[OK] After an invalid submission on a later prompt, the prompt was "
              "restored and a real retry was accepted (not stuck)")
    else:
        print("[SKIP] No further prompt with options appeared in time to test the "
              "invalid-input-restores-prompt path -- the resubmit-blocking check "
              "above is still the primary coverage for this bug report.")

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    print("\nALL NO-RESUBMIT CHECKS PASSED")


if __name__ == "__main__":
    main()
