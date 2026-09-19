"""
All the tunable numbers for the game live here so they're easy to find
and change in one place without hunting through server.py.
"""

# Minimum humans+bots required before a match can start.
MIN_PLAYERS = 4

# How long (seconds) each phase waits for input before moving on.
# These are deliberately generous for a live demo; tighten them for
# a faster automated/headless run if needed.
DISCUSSION_TIMEOUT = 90
VOTE_TIMEOUT = 30
NIGHT_ACTION_TIMEOUT = 25

# Display names handed out to AI bot players.
BOT_NAME_POOL = [
    "Nova", "Rex", "Ivy", "Finn", "Luna", "Zane",
    "Mira", "Kai", "Wren", "Otto", "Sage", "Remy",
]
