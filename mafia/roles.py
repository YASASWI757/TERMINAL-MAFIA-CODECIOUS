"""
Role definitions and role-assignment logic.

Design notes (matching what we locked in during planning):

- Villager / Mafia / Detective / Doctor: classic roles, standard behavior.
- Saboteur: Villager-aligned. Always assigned to a HUMAN player (never a
  bot) -- it's a deception mechanic that only makes sense with a real
  person behind it. Once per game they can silently nullify one player's
  vote; the target is never told.
- Double Agent: TRUE alignment is Mafia (counts toward the Mafia win
  condition), but APPARENT alignment is Villager (what a Detective
  investigation reports). They know who the real Mafia is but have no
  independent night action of their own while the real Mafia is alive.

Role pool by player count:
    4-5 players : Mafia, Detective, (Saboteur if a human is present), rest Villager
    6-7 players : + Doctor
    8+ players  : + Double Agent
"""

from enum import Enum
import random


class Role(Enum):
    VILLAGER = "VILLAGER"
    MAFIA = "MAFIA"
    DETECTIVE = "DETECTIVE"
    DOCTOR = "DOCTOR"
    SABOTEUR = "SABOTEUR"
    DOUBLE_AGENT = "DOUBLE_AGENT"


class Alignment(Enum):
    VILLAGER = "VILLAGER"
    MAFIA = "MAFIA"


# What side a role is actually on (used for the win condition).
TRUE_ALIGNMENT = {
    Role.VILLAGER: Alignment.VILLAGER,
    Role.MAFIA: Alignment.MAFIA,
    Role.DETECTIVE: Alignment.VILLAGER,
    Role.DOCTOR: Alignment.VILLAGER,
    Role.SABOTEUR: Alignment.VILLAGER,
    Role.DOUBLE_AGENT: Alignment.MAFIA,
}

# What side a role LOOKS like to a Detective investigation.
# Identical to TRUE_ALIGNMENT for everyone except the Double Agent,
# who is disguised as an innocent Villager.
APPARENT_ALIGNMENT = {
    **TRUE_ALIGNMENT,
    Role.DOUBLE_AGENT: Alignment.VILLAGER,
}

ROLE_DESCRIPTIONS = {
    Role.VILLAGER:
        "You are a VILLAGER. You have no special power. Find and eliminate "
        "the Mafia through discussion and voting.",
    Role.MAFIA:
        "You are MAFIA. Each night you choose a player to eliminate. "
        "Mafia wins when Mafia-aligned players are >= remaining players.",
    Role.DETECTIVE:
        "You are the DETECTIVE. Each night, investigate one player to "
        "learn their alignment (Mafia or Villager).",
    Role.DOCTOR:
        "You are the DOCTOR. Each night, protect one player (yourself "
        "included) -- if the Mafia targets them, the kill is cancelled.",
    Role.SABOTEUR:
        "You are the SABOTEUR (Villager-aligned). Once per game you may "
        "silently nullify one player's vote for the next tally. They will "
        "never be told it happened.",
    Role.DOUBLE_AGENT:
        "You are the DOUBLE AGENT. You appear as an innocent Villager to "
        "any Detective investigation, but you are secretly Mafia-aligned "
        "and know who the real Mafia is.",
}


def build_role_assignments(players):
    """
    Mutates each Player's .role, .true_alignment, .apparent_alignment
    in place, and returns the same list for convenience.

    Raises ValueError if there are fewer than 4 players (the game's
    stated minimum).
    """
    total = len(players)
    if total < 4:
        raise ValueError("Terminal Mafia requires at least 4 players.")

    humans = [p for p in players if not p.is_bot]

    pool = [Role.MAFIA, Role.DETECTIVE]
    if total >= 6:
        pool.append(Role.DOCTOR)
    if total >= 8:
        pool.append(Role.DOUBLE_AGENT)

    include_saboteur = len(humans) >= 1
    fixed_count = len(pool) + (1 if include_saboteur else 0)
    pool += [Role.VILLAGER] * (total - fixed_count)
    random.shuffle(pool)

    assignments = {}
    remaining_players = list(players)

    if include_saboteur:
        saboteur = random.choice(humans)
        assignments[saboteur.id] = Role.SABOTEUR
        remaining_players = [p for p in remaining_players if p.id != saboteur.id]

    for player, role in zip(remaining_players, pool):
        assignments[player.id] = role

    for player in players:
        role = assignments[player.id]
        player.role = role
        player.true_alignment = TRUE_ALIGNMENT[role]
        player.apparent_alignment = APPARENT_ALIGNMENT[role]

    return players
