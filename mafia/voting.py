"""
Pure voting logic -- no networking, no I/O, fully unit-testable.

Rules locked in during planning:
    - Each alive player casts exactly one vote: a target name, or abstain.
    - A vote from the player the Saboteur targeted is silently discarded
      (nullified) before tallying -- that player is never told.
    - A tie for the most votes means NO ONE is eliminated that day (the
      simplest, least ambiguous tie rule -- deliberately chosen over a
      random tiebreak or runoff to remove an entire class of "wait, what
      happens on a tie?" bugs).
    - Win condition is checked after every elimination (night or day):
      Mafia-aligned == 0 -> Villagers win. Mafia-aligned >= everyone
      else -> Mafia win.
"""

from typing import Dict, Optional, List
from .roles import Alignment
from .player import Player


def tally_votes(votes: Dict[str, Optional[str]],
                 sabotage_target_id: Optional[str]) -> Dict[str, int]:
    """
    votes: voter_id -> target_id (or None for abstain).
    sabotage_target_id: a voter_id whose vote is discarded this round.
    Returns: target_id -> vote count.
    """
    counts: Dict[str, int] = {}
    for voter_id, target_id in votes.items():
        if voter_id == sabotage_target_id:
            continue
        if target_id is None:
            continue
        counts[target_id] = counts.get(target_id, 0) + 1
    return counts


def resolve_vote(counts: Dict[str, int]) -> Optional[str]:
    """Majority wins. A tie for first place means no elimination."""
    if not counts:
        return None
    top_count = max(counts.values())
    leaders = [pid for pid, c in counts.items() if c == top_count]
    if len(leaders) != 1:
        return None
    return leaders[0]


def check_win(players: List[Player]) -> Optional[str]:
    """Returns 'VILLAGERS', 'MAFIA', or None if the game continues."""
    alive = [p for p in players if p.alive]
    mafia_count = sum(1 for p in alive if p.true_alignment == Alignment.MAFIA)
    villager_count = len(alive) - mafia_count

    if mafia_count == 0:
        return "VILLAGERS"
    if mafia_count >= villager_count:
        return "MAFIA"
    return None
