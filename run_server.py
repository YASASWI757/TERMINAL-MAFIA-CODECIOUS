"""
Terminal Mafia -- game server entry point.

Same-machine demo (multiple terminal windows on one laptop):
    python run_server.py --port 5555 --bots 2
    # then in separate terminals:
    python run_client.py --host 127.0.0.1 --port 5555 --name Alice
    python run_client.py --host 127.0.0.1 --port 5555 --name Bob

LAN / hotspot (multiple laptops on the same network):
    python run_server.py --host 0.0.0.0 --port 5555 --bots 1
    # find the host machine's LAN IP (e.g. `ip addr` / `ifconfig` / `ipconfig`)
    # on each other laptop:
    python run_client.py --host <server-LAN-IP> --port 5555 --name Charlie

Headless smoke test (no human players at all, for quickly verifying
the engine end-to-end):
    python run_server.py --bots 4 --auto-start

Round timers (discussion / voting / night actions) default to the
values in mafia/constants.py, but the host can customize them either
way:
    - Pass them on the command line to skip the prompt entirely:
      python run_server.py --discussion-timeout 60 --vote-timeout 20 --night-timeout 20
    - Or just leave them out -- unless --auto-start is set, you'll be
      asked for each one interactively before the lobby opens (press
      Enter on any of them to accept the default shown).
"""

import argparse
from mafia import constants
from mafia.server import GameServer


def _prompt_seconds(label, default):
    while True:
        raw = input(f"  {label} [{default}]: ").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            print(f"    Not a whole number of seconds -- try again, or press Enter for {default}.")
            continue
        if value <= 0:
            print(f"    Must be a positive number of seconds -- try again, or press Enter for {default}.")
            continue
        return value


def main():
    parser = argparse.ArgumentParser(description="Terminal Mafia -- game server")
    parser.add_argument("--host", default="0.0.0.0",
                         help="Bind address (0.0.0.0 allows LAN connections; "
                              "use 127.0.0.1 to restrict to this machine)")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--bots", type=int, default=0,
                         help="Number of AI bot players to fill the lobby with")
    parser.add_argument("--min-players", type=int, default=4)
    parser.add_argument("--auto-start", action="store_true",
                         help="Skip the ENTER-to-start prompt (useful for headless testing)")
    parser.add_argument("--discussion-timeout", type=int, default=None,
                         help="Seconds for the discussion phase (skips the interactive "
                              "prompt for this one if given)")
    parser.add_argument("--vote-timeout", type=int, default=None,
                         help="Seconds for voting (skips the interactive prompt if given)")
    parser.add_argument("--night-timeout", type=int, default=None,
                         help="Seconds for each night action (skips the interactive "
                              "prompt if given)")
    args = parser.parse_args()

    discussion_timeout = args.discussion_timeout
    vote_timeout = args.vote_timeout
    night_timeout = args.night_timeout

    need_prompt = (discussion_timeout is None or vote_timeout is None or night_timeout is None)
    if need_prompt and not args.auto_start:
        print("Configure round timers for this match (press Enter to accept the default):")
        if discussion_timeout is None:
            discussion_timeout = _prompt_seconds("Discussion phase (seconds)",
                                                  constants.DISCUSSION_TIMEOUT)
        if vote_timeout is None:
            vote_timeout = _prompt_seconds("Voting phase (seconds)",
                                            constants.VOTE_TIMEOUT)
        if night_timeout is None:
            night_timeout = _prompt_seconds("Night action phase (seconds)",
                                             constants.NIGHT_ACTION_TIMEOUT)
        print()
    else:
        # auto-start (headless) mode: never block on input, just fall
        # back to the constants.py defaults for anything not passed
        # explicitly on the command line.
        discussion_timeout = discussion_timeout if discussion_timeout is not None else constants.DISCUSSION_TIMEOUT
        vote_timeout = vote_timeout if vote_timeout is not None else constants.VOTE_TIMEOUT
        night_timeout = night_timeout if night_timeout is not None else constants.NIGHT_ACTION_TIMEOUT

    print(f"Timers: discussion={discussion_timeout}s, vote={vote_timeout}s, "
          f"night_action={night_timeout}s")
    print()

    server = GameServer(
        host=args.host,
        port=args.port,
        target_bots=args.bots,
        min_players=args.min_players,
        auto_start=args.auto_start,
        discussion_timeout=discussion_timeout,
        vote_timeout=vote_timeout,
        night_action_timeout=night_timeout,
    )
    server.start()


if __name__ == "__main__":
    main()
