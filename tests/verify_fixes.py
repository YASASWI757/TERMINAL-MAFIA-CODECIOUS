"""
Ad-hoc verification script for the fixes made after initial delivery:
    - case-insensitive target name matching
    - elimination_log populated correctly
    - a personally-addressed "eliminated" notice (distinct from the
      public death_announcement)
    - dead players get an explicit "you can't vote" notice
    - the night phase runs roles concurrently, not sequentially
      (timed check)

Not part of the permanent CI-style suite (it deliberately sends a
lowercase name on purpose, which is scenario-specific) -- run directly:
    python tests/verify_fixes.py
"""

import socket
import threading
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mafia import constants
constants.DISCUSSION_TIMEOUT = 3
constants.VOTE_TIMEOUT = 3
constants.NIGHT_ACTION_TIMEOUT = 4  # long enough that concurrency actually matters to time

from mafia.server import GameServer
from mafia import protocol

PORT = 5911


def run_fake_human(name, port, log, done_event, lowercase_votes=True):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    for _ in range(50):
        try:
            s.connect(("127.0.0.1", port))
            break
        except OSError:
            time.sleep(0.1)
    protocol.send_json(s, {"name": name})

    def respond(msg):
        t = msg.get("type")
        if t == "prompt":
            pt = msg["prompt_type"]
            options = [o for o in msg.get("options", []) if o not in ("abstain", "skip")]
            if pt == "sabotage_decision":
                protocol.send_json(s, {"type": "sabotage_decision", "target": "SKIP"})  # weird case on purpose
            elif pt == "vote":
                if options:
                    # Intentionally send a lowercased, whitespace-padded
                    # version of a real name to prove matching is
                    # case-insensitive and trimmed.
                    choice = options[0]
                    sent = f"  {choice.lower()}  " if lowercase_votes else choice
                    protocol.send_json(s, {"type": "vote", "target": sent})
                else:
                    protocol.send_json(s, {"type": "vote", "target": "abstain"})
            elif pt == "night_action":
                if options:
                    sent = options[0].lower() if lowercase_votes else options[0]
                    protocol.send_json(s, {"type": "night_action", "target": sent})

    try:
        for msg in protocol.recv_lines(s):
            log.append(msg)
            respond(msg)
            if msg.get("type") == "game_over":
                break
    finally:
        done_event.set()
        s.close()


def main():
    server = GameServer(host="127.0.0.1", port=PORT, target_bots=3,
                         min_players=4, auto_start=True)
    threading.Thread(target=server.start, daemon=True).start()
    time.sleep(0.3)

    log = []
    done_event = threading.Event()
    threading.Thread(
        target=run_fake_human, args=("TestHuman", PORT, log, done_event), daemon=True
    ).start()

    # Time how long the first NIGHT phase actually takes, to confirm
    # roles are being collected concurrently (should be roughly
    # NIGHT_ACTION_TIMEOUT, not a multiple of it).
    night_start = None
    night_end = None
    for _ in range(200):
        if night_start is None and any(m.get("type") == "phase" and m.get("name") == "NIGHT" for m in log):
            night_start = time.time()
        if night_start is not None and any(
            m.get("type") == "phase" and m.get("name") == "DISCUSSION" for m in log
        ):
            night_end = time.time()
            break
        time.sleep(0.05)

    finished = done_event.wait(timeout=90)
    assert finished, "Game did not finish"

    # --- Check 1: game completed ---
    game_over = next((m for m in log if m["type"] == "game_over"), None)
    assert game_over is not None, "No game_over received"
    print("[OK] Game completed:", game_over["winner"])

    # --- Check 2: elimination_log present and well-formed ---
    elim_log = game_over.get("elimination_log")
    assert elim_log is not None, "elimination_log missing from game_over"
    print(f"[OK] elimination_log present with {len(elim_log)} entrie(s):")
    for e in elim_log:
        assert {"round", "phase", "player", "role"} <= e.keys(), f"Malformed entry: {e}"
        print(f"       Round {e['round']} [{e['phase']}]: {e['player']} -- {e['role']}")

    # --- Check 3: rounds_played present ---
    assert "rounds_played" in game_over, "rounds_played missing from game_over"
    print(f"[OK] rounds_played: {game_over['rounds_played']}")

    # --- Check 4: case-insensitive matching worked at all (i.e. the
    #     human's lowercase vote/night_action target was NOT silently
    #     rejected -- we can't directly observe server-side matching
    #     from the client's log, but we CAN confirm the human's vote
    #     was not treated as invalid by checking we got no repeated
    #     "Invalid input" info spam for it, and the game still
    #     resolved normally instead of hanging).
    invalid_msgs = [m for m in log if m.get("type") == "info" and "Invalid input" in m.get("text", "")]
    print(f"[OK] 'Invalid input' warnings received despite lowercase/whitespace input: {len(invalid_msgs)} "
          f"(expected 0 -- case-insensitive matching should accept it cleanly)")
    assert len(invalid_msgs) == 0, "Case-insensitive matching failed -- lowercase input was rejected"

    # --- Check 5: if TestHuman was ever eliminated, they should have
    #     received a personal 'eliminated' message distinct from the
    #     public death_announcement, and later an info message telling
    #     them they can't vote.
    eliminated_msgs = [m for m in log if m.get("type") == "eliminated"]
    cant_vote_msgs = [m for m in log if m.get("type") == "info" and "can't vote" in m.get("text", "")]
    print(f"[OK] 'eliminated' personal notices received: {len(eliminated_msgs)}")
    print(f"[OK] 'can't vote' spectator notices received: {len(cant_vote_msgs)}")
    if eliminated_msgs:
        my_elim_entry = next(
            (e for e in elim_log if e["player"] == "TestHuman"), None
        )
        # The "can't vote" notice only fires at the START of a voting
        # phase for someone already dead -- it can't fire for the very
        # round that killed them (there's no "later" voting phase to
        # warn them away from if the game ended right after). Only
        # expect it if the match continued past their elimination round.
        if my_elim_entry and game_over["rounds_played"] > my_elim_entry["round"]:
            assert len(cant_vote_msgs) >= 1, \
                "Player was eliminated and the game continued, but they were " \
                "never told they can't vote in the following round"
        else:
            print("       (player was eliminated on the game's final round/vote -- "
                  "no subsequent voting phase existed to send the notice in, so 0 is correct)")

    # --- Check 6: night phase timing (concurrency check) ---
    if night_start and night_end:
        duration = night_end - night_start
        print(f"[OK] First NIGHT phase took {duration:.1f}s "
              f"(NIGHT_ACTION_TIMEOUT={constants.NIGHT_ACTION_TIMEOUT}s -- "
              f"sequential would take up to ~4x that if all 4 roles present)")
        # Generous upper bound: concurrent should be close to ~1x timeout,
        # not anywhere near 2x+ (which sequential would produce with 2+ roles).
        assert duration < constants.NIGHT_ACTION_TIMEOUT * 1.8, (
            f"Night phase took {duration:.1f}s -- looks sequential, not concurrent"
        )

    print("\nALL FIX-VERIFICATION CHECKS PASSED")


if __name__ == "__main__":
    main()
