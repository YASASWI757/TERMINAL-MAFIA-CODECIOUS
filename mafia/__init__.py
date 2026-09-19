"""
Terminal Mafia -- a local/LAN, terminal-based social deduction game.

This package contains the whole game engine:
    roles.py       role definitions, alignments, role-pool assignment
    player.py      Player data model
    game_state.py  shared mutable game state for one match
    night.py       night-phase resolution logic (investigate / kill / save)
    voting.py      day-phase vote tallying, tie rules, win condition
    bot_logic.py   AI bot decision-making (discussion, votes, night actions)
    protocol.py    newline-delimited JSON message framing over TCP
    server.py      the authoritative game server (networking + game loop)
"""

__version__ = "1.0.0"
