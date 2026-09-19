"""
Rule-based AI bots -- deliberately NOT an LLM call. This is intentional:

    - Zero risk of an API failure/latency spike crashing a live demo
    - Fully deterministic and unit-testable (e.g. "Mafia bots never vote
      a teammate" is a one-line assertion, not a hope)
    - No external dependency at all -- the whole game runs offline
    - Bots never see anything a real player in their position couldn't
      know: only true_alignment for their OWN team-awareness (Mafia/
      Double Agent knowing their teammates), and otherwise only public
      state (who's alive, who's been accused in discussion).

Every bot decision returns a PLAYER NAME (not id) or None/"abstain",
matching what a human would type at the same prompt.
"""

import random
from .roles import Role, Alignment
from .player import Player

GENERIC_LINES = [
    "I don't have much to go on yet.",
    "We need to think carefully about this vote.",
    "Something feels off tonight.",
    "Let's not rush the vote this time.",
]

SUSPECT_LINES = [
    "I'm starting to suspect {target}.",
    "Has anyone else noticed {target} acting strange?",
    "I don't trust {target}'s silence.",
    "My gut says {target} isn't telling us everything.",
]

MAFIA_DEFLECT_LINES = [
    "I think we should focus on {target}, they've been quiet.",
    "{target} hasn't given a straight answer yet.",
    "Honestly {target} seems the most suspicious to me.",
]


def _mafia_team_ids(game_state) -> set:
    return {p.id for p in game_state.players if p.true_alignment == Alignment.MAFIA}


def update_accusation_count(game_state, text: str) -> None:
    """Called on every chat line (human or bot) to track who's being
    named, which is the only signal bots use to weight votes/suspicion."""
    lowered = text.lower()
    for p in game_state.players:
        if p.name.lower() in lowered:
            game_state.accusation_count[p.id] = game_state.accusation_count.get(p.id, 0) + 1


def bot_discussion_line(bot: Player, game_state):
    alive_others = [p for p in game_state.alive_players() if p.id != bot.id]
    if not alive_others:
        return None

    if bot.true_alignment == Alignment.MAFIA:
        teammates = _mafia_team_ids(game_state)
        candidates = [p for p in alive_others if p.id not in teammates]
        template_pool = MAFIA_DEFLECT_LINES
    else:
        candidates = alive_others
        template_pool = SUSPECT_LINES

    if not candidates:
        return random.choice(GENERIC_LINES)

    weights = [1 + game_state.accusation_count.get(p.id, 0) * 2 for p in candidates]
    target = random.choices(candidates, weights=weights, k=1)[0]
    return random.choice(template_pool).format(target=target.name)


def bot_vote_target(bot: Player, game_state) -> str:
    alive_others = [p for p in game_state.alive_players() if p.id != bot.id]

    if bot.true_alignment == Alignment.MAFIA:
        teammates = _mafia_team_ids(game_state)
        alive_others = [p for p in alive_others if p.id not in teammates]

    # A Detective bot with a confirmed Mafia read mostly votes it, but
    # not every single time -- 100% certainty would look robotic.
    if bot.role == Role.DETECTIVE and bot.known_mafia_id:
        known = game_state.get_player(bot.known_mafia_id)
        if known and known.alive and random.random() < 0.8:
            return known.name

    if not alive_others:
        return "abstain"

    weights = [1 + game_state.accusation_count.get(p.id, 0) * 2 for p in alive_others]
    return random.choices(alive_others, weights=weights, k=1)[0].name


def bot_kill_target(bot: Player, game_state):
    candidates = [p for p in game_state.alive_players()
                  if p.true_alignment != Alignment.MAFIA]
    return random.choice(candidates).name if candidates else None


def bot_investigate_target(bot: Player, game_state):
    investigated = getattr(bot, "_investigated", set())
    candidates = [p for p in game_state.alive_players()
                  if p.id != bot.id and p.id not in investigated]
    if not candidates:
        candidates = [p for p in game_state.alive_players() if p.id != bot.id]
    if not candidates:
        return None
    choice = random.choice(candidates)
    investigated.add(choice.id)
    bot._investigated = investigated
    return choice.name


def bot_protect_target(bot: Player, game_state) -> str:
    # Mostly protects someone else; sometimes plays it safe on itself.
    if random.random() < 0.3:
        return bot.name
    candidates = [p for p in game_state.alive_players() if p.id != bot.id]
    return random.choice(candidates).name if candidates else bot.name
