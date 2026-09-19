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
"""

import argparse
from mafia.server import GameServer


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
    args = parser.parse_args()

    server = GameServer(
        host=args.host,
        port=args.port,
        target_bots=args.bots,
        min_players=args.min_players,
        auto_start=args.auto_start,
    )
    server.start()


if __name__ == "__main__":
    main()
