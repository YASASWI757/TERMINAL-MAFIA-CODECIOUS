"""
Unit tests for the core rules engine -- no sockets, no threading.
Run with:  python -m unittest tests.test_engine -v
"""

import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mafia.player import Player
from mafia.roles import Role, Alignment, build_role_assignments
from mafia.night import run_investigation, resolve_night_kill
from mafia.voting import tally_votes, resolve_vote, check_win


def make_players(n, bots=0):
    players = []
    for i in range(n):
        is_bot = i < bots
        players.append(Player(id=f"P{i}", name=f"Player{i}", is_bot=is_bot))
    return players


class TestRoleAssignment(unittest.TestCase):
    def test_requires_minimum_four_players(self):
        players = make_players(3)
        with self.assertRaises(ValueError):
            build_role_assignments(players)

    def test_four_players_has_mafia_detective_saboteur(self):
        players = make_players(4)
        build_role_assignments(players)
        roles = sorted(p.role.value for p in players)
        self.assertIn("MAFIA", roles)
        self.assertIn("DETECTIVE", roles)
        self.assertIn("SABOTEUR", roles)
        self.assertNotIn("DOCTOR", roles)      # only appears at 6+
        self.assertNotIn("DOUBLE_AGENT", roles)  # only appears at 8+

    def test_saboteur_always_goes_to_a_human(self):
        # Run many times since assignment is randomized.
        for _ in range(50):
            players = make_players(6, bots=4)  # 2 humans, 4 bots
            build_role_assignments(players)
            saboteur = next(p for p in players if p.role == Role.SABOTEUR)
            self.assertFalse(saboteur.is_bot, "Saboteur must never be assigned to a bot")

    def test_no_humans_means_no_saboteur(self):
        players = make_players(6, bots=6)  # all bots
        build_role_assignments(players)
        roles = [p.role for p in players]
        self.assertNotIn(Role.SABOTEUR, roles)

    def test_six_players_adds_doctor(self):
        players = make_players(6)
        build_role_assignments(players)
        roles = [p.role for p in players]
        self.assertIn(Role.DOCTOR, roles)

    def test_eight_players_adds_double_agent(self):
        players = make_players(8)
        build_role_assignments(players)
        roles = [p.role for p in players]
        self.assertIn(Role.DOUBLE_AGENT, roles)

    def test_every_player_gets_exactly_one_role_and_all_slots_filled(self):
        players = make_players(9)
        build_role_assignments(players)
        for p in players:
            self.assertIsNotNone(p.role)
            self.assertIsNotNone(p.true_alignment)
            self.assertIsNotNone(p.apparent_alignment)

    def test_double_agent_true_vs_apparent_alignment(self):
        # Force enough players to guarantee a Double Agent, then find them.
        for _ in range(20):
            players = make_players(8)
            build_role_assignments(players)
            da = next((p for p in players if p.role == Role.DOUBLE_AGENT), None)
            if da:
                self.assertEqual(da.true_alignment, Alignment.MAFIA)
                self.assertEqual(da.apparent_alignment, Alignment.VILLAGER)
                return
        self.fail("Double Agent never appeared in 20 tries at 8 players -- check role pool logic")


class TestNightResolution(unittest.TestCase):
    def test_investigation_uses_apparent_alignment_not_true(self):
        da = Player(id="1", name="DA", role=Role.DOUBLE_AGENT,
                    true_alignment=Alignment.MAFIA, apparent_alignment=Alignment.VILLAGER)
        result = run_investigation(da)
        self.assertEqual(result, Alignment.VILLAGER, "Detective must see the Double Agent as innocent")

    def test_investigation_reveals_real_mafia_correctly(self):
        mafia = Player(id="1", name="M", role=Role.MAFIA,
                        true_alignment=Alignment.MAFIA, apparent_alignment=Alignment.MAFIA)
        self.assertEqual(run_investigation(mafia), Alignment.MAFIA)

    def test_kill_with_no_doctor_save_succeeds(self):
        victim = Player(id="1", name="V")
        result = resolve_night_kill(mafia_target=victim, doctor_target=None)
        self.assertEqual(result, victim)

    def test_doctor_save_cancels_kill(self):
        victim = Player(id="1", name="V")
        result = resolve_night_kill(mafia_target=victim, doctor_target=victim)
        self.assertIsNone(result)

    def test_doctor_self_protect_works_like_any_other_save(self):
        doctor = Player(id="1", name="Doc")
        result = resolve_night_kill(mafia_target=doctor, doctor_target=doctor)
        self.assertIsNone(result, "Doctor must be able to save themselves")

    def test_doctor_protecting_wrong_person_does_not_save_victim(self):
        victim = Player(id="1", name="V")
        someone_else = Player(id="2", name="Other")
        result = resolve_night_kill(mafia_target=victim, doctor_target=someone_else)
        self.assertEqual(result, victim)

    def test_no_mafia_target_means_no_death(self):
        result = resolve_night_kill(mafia_target=None, doctor_target=None)
        self.assertIsNone(result)


class TestVoting(unittest.TestCase):
    def test_simple_majority_wins(self):
        votes = {"v1": "A", "v2": "A", "v3": "B"}
        counts = tally_votes(votes, sabotage_target_id=None)
        self.assertEqual(resolve_vote(counts), "A")

    def test_tie_results_in_no_elimination(self):
        votes = {"v1": "A", "v2": "B"}
        counts = tally_votes(votes, sabotage_target_id=None)
        self.assertIsNone(resolve_vote(counts))

    def test_all_abstain_results_in_no_elimination(self):
        votes = {"v1": None, "v2": None}
        counts = tally_votes(votes, sabotage_target_id=None)
        self.assertIsNone(resolve_vote(counts))

    def test_sabotaged_vote_is_silently_dropped(self):
        # v1's vote for A would normally break the tie in A's favor;
        # sabotaging v1 should leave A and B tied -> no elimination.
        votes = {"v1": "A", "v2": "A", "v3": "B", "v4": "B"}
        # Without sabotage: A=2, B=2 -> tie already. Let's make A the
        # clear leader, then sabotage the deciding vote.
        votes = {"v1": "A", "v2": "A", "v3": "B"}
        counts_no_sabotage = tally_votes(votes, sabotage_target_id=None)
        self.assertEqual(resolve_vote(counts_no_sabotage), "A")

        # Sabotaging v2 (one of A's two votes) should drop A to 1 vote,
        # tying with B's 1 vote -- which means no elimination at all.
        counts_with_sabotage = tally_votes(votes, sabotage_target_id="v2")
        self.assertEqual(counts_with_sabotage.get("A", 0), 1)
        self.assertEqual(counts_with_sabotage.get("B", 0), 1)
        self.assertIsNone(resolve_vote(counts_with_sabotage))


class TestWinCondition(unittest.TestCase):
    def test_villagers_win_when_no_mafia_left(self):
        players = [
            Player(id="1", name="A", alive=True, true_alignment=Alignment.VILLAGER),
            Player(id="2", name="B", alive=True, true_alignment=Alignment.VILLAGER),
            Player(id="3", name="C", alive=False, true_alignment=Alignment.MAFIA),
        ]
        self.assertEqual(check_win(players), "VILLAGERS")

    def test_mafia_wins_when_mafia_equals_villagers(self):
        players = [
            Player(id="1", name="A", alive=True, true_alignment=Alignment.VILLAGER),
            Player(id="2", name="B", alive=True, true_alignment=Alignment.MAFIA),
        ]
        self.assertEqual(check_win(players), "MAFIA")

    def test_game_continues_when_villagers_still_outnumber_mafia(self):
        players = [
            Player(id="1", name="A", alive=True, true_alignment=Alignment.VILLAGER),
            Player(id="2", name="B", alive=True, true_alignment=Alignment.VILLAGER),
            Player(id="3", name="C", alive=True, true_alignment=Alignment.MAFIA),
        ]
        self.assertIsNone(check_win(players))

    def test_double_agent_counts_as_mafia_for_win_condition(self):
        players = [
            Player(id="1", name="A", alive=True, true_alignment=Alignment.VILLAGER),
            Player(id="2", name="DA", alive=True, true_alignment=Alignment.MAFIA,
                   role=Role.DOUBLE_AGENT),  # apparent villager, TRUE mafia
        ]
        self.assertEqual(check_win(players), "MAFIA",
                          "Double Agent must count toward the Mafia win condition")


if __name__ == "__main__":
    unittest.main(verbosity=2)
