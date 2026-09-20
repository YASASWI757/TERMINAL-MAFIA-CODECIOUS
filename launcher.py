#!/usr/bin/env python3
"""
Terminal Mafia -- single-executable launcher.

This is the entry point PyInstaller bundles into the one executable
file for the "Executable File" deliverable. It doesn't duplicate any
game logic -- it just dispatches straight into the existing
run_server.py / run_client.py main() functions, so the executable and
"python run_server.py" / "python run_client.py" always stay in sync
with zero drift between them.

Usage (same whether run as `python launcher.py ...` from source or as
the built executable, e.g. `./terminal-mafia ...` or
`terminal-mafia.exe ...`):

    terminal-mafia server [--host H] [--port P] [--bots N] ...
    terminal-mafia client [--host H] [--port P] --name YourName

Run with no arguments at all for an interactive menu -- convenient for
a judge who just double-clicks the executable without knowing the CLI
flags; it asks a couple of quick questions and then launches exactly
as if those flags had been passed on the command line.
"""

import sys
import os

# Force line-buffered stdout/stderr -- without this, the frozen
# executable's output can sit in an internal buffer and never actually
# reach the terminal until the process exits (observed directly: a
# built executable produced zero visible output for 10+ seconds of a
# real running game, even though the game was progressing normally
# underneath). Running from source via "python run_server.py" doesn't
# have this problem since Python's own stdio defaults differ there;
# the frozen executable needs this set explicitly and unconditionally.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass  # not all stdio wrappers support reconfigure(); harmless to skip

# Make sure the bundled package is importable regardless of how
# PyInstaller lays out the frozen bundle (harmless no-op when run from
# source, where this is already the case).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_server   # noqa: E402  (import after sys.path fix, and at
import run_client   # noqa: E402   module level so PyInstaller's static
                     #              analysis bundles both automatically)


def _interactive_menu():
    print("=" * 50)
    print("   TERMINAL MAFIA")
    print("=" * 50)
    print()
    print("1) Host a game (start the server)")
    print("2) Join a game (start a client)")
    print()
    choice = ""
    while choice not in ("1", "2"):
        choice = input("Choose 1 or 2: ").strip()

    if choice == "1":
        host = input("Bind address [0.0.0.0 -- allows LAN connections]: ").strip() or "0.0.0.0"
        port = input("Port [5555]: ").strip() or "5555"
        bots = input("Number of AI bots to fill the lobby with [0]: ").strip() or "0"
        return ["server", "--host", host, "--port", port, "--bots", bots]

    host = input("Server address [127.0.0.1]: ").strip() or "127.0.0.1"
    port = input("Port [5555]: ").strip() or "5555"
    name = ""
    while not name:
        name = input("Your name: ").strip()
    return ["client", "--host", host, "--port", port, "--name", name]


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("server", "client"):
        args = _interactive_menu()

    mode, rest = args[0], args[1:]
    # run_server.main() / run_client.main() each build their own
    # argparse.ArgumentParser reading sys.argv directly -- rewriting
    # sys.argv here (dropping the "server"/"client" token) is what
    # lets us reuse them completely unmodified.
    sys.argv = [sys.argv[0]] + rest

    if mode == "server":
        run_server.main()
    else:
        run_client.main()


if __name__ == "__main__":
    main()
