"""
Player data model. One Player instance exists for the entire match,
whether they're a human (backed by a live socket that can be swapped
out on reconnect) or a bot (no socket at all).
"""

from dataclasses import dataclass, field
from typing import Optional
import queue

from .roles import Role, Alignment


@dataclass
class Player:
    id: str                      # stable identity across reconnects (we use name)
    name: str
    is_bot: bool = False
    conn: object = None          # live socket, or None if disconnected/bot
    connected: bool = True

    role: Optional[Role] = None
    true_alignment: Optional[Alignment] = None
    apparent_alignment: Optional[Alignment] = None

    alive: bool = True
    saboteur_used: bool = False

    # Bot-only memory: a confirmed Mafia read from investigation, so a
    # Detective bot can act on its own information during voting.
    known_mafia_id: Optional[str] = None

    # Every message addressed to this player (vote / night_action /
    # sabotage_decision replies) lands here. Chat messages are handled
    # separately and broadcast immediately -- they never go in the inbox.
    inbox: "queue.Queue" = field(default_factory=queue.Queue)
