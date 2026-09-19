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

    # A per-player secret issued on first join and required (must match)
    # to reconnect as this player later. Without this, reconnect was
    # matched by name alone -- meaning anyone who knew (or guessed) a
    # disconnected player's name could reconnect AS them and inherit
    # their role, private info, and vote, no proof required. This closes
    # that: only a client holding the matching token can re-attach.
    rejoin_token: Optional[str] = None

    # The most recent "prompt"-type message sent to this player, and
    # when it expires -- kept so that if they reconnect mid-window, we
    # can re-send them the exact prompt (with a freshly recomputed
    # remaining timeout) instead of leaving them stuck with no prompt
    # at all despite still technically being able to act in time.
    active_prompt: Optional[dict] = None
    active_prompt_deadline: Optional[float] = None

    # Bot-only memory: a confirmed Mafia read from investigation, so a
    # Detective bot can act on its own information during voting.
    known_mafia_id: Optional[str] = None

    # Every message addressed to this player (vote / night_action /
    # sabotage_decision replies) lands here. Chat messages are handled
    # separately and broadcast immediately -- they never go in the inbox.
    inbox: "queue.Queue" = field(default_factory=queue.Queue)
