"""
Pure night-phase resolution logic -- no networking, no I/O, fully
unit-testable in isolation.

Resolution order (decided during planning, and important to preserve):
    1. Detective investigates      (read-only, can't conflict with anything)
    2. Mafia chooses a kill target (Double Agent is informed, doesn't act
       unless the real Mafia is dead -- see server.py)
    3. Doctor chooses a protect target (can be themselves)
    4. Saboteur arms a vote-nullify for tomorrow (independent, no conflict)
    5. Resolve: if Doctor's target == Mafia's target, the kill is
       cancelled. Otherwise the Mafia's target dies.
"""

from typing import Optional
from .roles import Alignment
from .player import Player


def run_investigation(target: Player) -> Alignment:
    """
    A Detective's investigation always reads apparent_alignment, not
    true_alignment -- this is the one place in the whole engine where
    the Double Agent's disguise actually applies.
    """
    return target.apparent_alignment


def resolve_night_kill(mafia_target: Optional[Player],
                        doctor_target: Optional[Player]) -> Optional[Player]:
    """
    Returns the Player who dies tonight, or None if nobody dies
    (no kill target was chosen, or the Doctor's save matched the kill).
    """
    if mafia_target is None:
        return None
    if doctor_target is not None and doctor_target.id == mafia_target.id:
        return None
    return mafia_target
