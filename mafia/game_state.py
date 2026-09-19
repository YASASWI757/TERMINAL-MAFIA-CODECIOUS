"""
GameState holds everything about a single in-progress match that isn't
tied to networking. Keeping this separate from server.py means the
core rules (who's alive, who's suspected, whose vote is sabotaged) can
be understood and tested without touching a single socket.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple

from .player import Player


@dataclass
class GameState:
    players: List[Player]
    round_number: int = 0
    phase: str = "LOBBY"  # LOBBY / NIGHT / ANNOUNCE / DISCUSSION / VOTING / GAME_OVER

    # (sender_name, text) pairs, in order -- used by bots to gauge who's
    # being accused, and kept for an eventual match-history/replay feature.
    discussion_log: List[Tuple[str, str]] = field(default_factory=list)

    # player_id -> number of times they've been named in discussion.
    # Drives weighted bot voting/discussion so bots "follow the room"
    # instead of voting uniformly at random.
    accusation_count: Dict[str, int] = field(default_factory=dict)

    # player_id whose vote will be silently discarded in the NEXT tally
    # (armed by the Saboteur, consumed and cleared after that tally).
    sabotage_armed: Optional[str] = None

    last_night_death: Optional[Player] = None

    # Full match history: one entry per elimination, in order. Used to
    # build the end-of-game summary sent to every player (not just the
    # bare final role list).
    elimination_log: List[Dict] = field(default_factory=list)

    def alive_players(self) -> List[Player]:
        return [p for p in self.players if p.alive]

    def get_player(self, player_id: str) -> Optional[Player]:
        return next((p for p in self.players if p.id == player_id), None)

    def get_player_by_name(self, name: str) -> Optional[Player]:
        return next((p for p in self.players if p.name == name), None)
