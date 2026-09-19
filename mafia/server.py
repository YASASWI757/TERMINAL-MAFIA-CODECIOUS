"""
GameServer -- the single source of truth for a match.

Architecture: one server process, N client processes (human terminals
and/or bots). Each human client is a thin terminal UI over a TCP socket;
the server holds ALL real game state (roles, votes, night actions) so a
client can never see information its role doesn't permit -- the server
simply never sends it.

Concurrency model:
    - One reader thread per connected human socket, continuously
      parsing incoming JSON lines. Chat messages are broadcast
      immediately; everything else (vote / night_action /
      sabotage_decision replies) is dropped onto that player's
      per-player inbox Queue.
    - The main game-loop thread drives the match phase by phase. To
      collect an action from a player, it calls the single helper
      `_collect_action`, which waits on that player's inbox with a
      bounded timeout. This ONE function is what makes disconnects,
      timeouts, and invalid input all safe by construction -- every
      phase (vote, night action, sabotage decision) goes through it,
      so there's exactly one place that logic can go wrong instead of
      four.
    - Bots have no socket at all. `_collect_action` spawns a short-lived
      thread that sleeps a small random delay (so bots don't act
      instantly/robotically) and then pushes their decision onto the
      SAME inbox a human's reader thread would have used -- so the
      collection code doesn't need to know or care whether it's
      waiting on a human or a bot.
    - Night actions (Detective / Mafia / Doctor / Saboteur) are all
      prompted and collected CONCURRENTLY, not one after another --
      none of them need to see each other's choice before acting, so
      running them sequentially only added dead-air waiting (e.g. the
      Doctor's prompt not even appearing until Detective AND Mafia had
      both finished, up to 2x the timeout of visible "nothing is
      happening"). Only the final resolution step needs every action
      already collected.

Disconnect policy (Option A, decided during planning): a disconnected
player is never removed or auto-eliminated. They stay "alive but idle"
-- their vote/action for that round simply doesn't happen (safe default:
abstain / no action) -- and can reconnect at any time using the same
name, which re-attaches their existing Player object (and its role,
alive status, etc.) to a new socket.

Eliminated players stay connected as spectators: they keep receiving
every broadcast (chat, phase changes, results, the final summary) but
are excluded from voting/night-action prompts and from sending chat,
and are told plainly why.
"""

import socket
import threading
import queue
import time
import random
import secrets
from typing import Optional

from . import protocol
from . import bot_logic
from .constants import (
    MIN_PLAYERS, DISCUSSION_TIMEOUT, VOTE_TIMEOUT, NIGHT_ACTION_TIMEOUT,
    BOT_NAME_POOL,
)
from .roles import Role, Alignment, build_role_assignments, ROLE_DESCRIPTIONS
from .player import Player
from .game_state import GameState
from .night import run_investigation, resolve_night_kill
from .voting import tally_votes, resolve_vote, check_win


def _match_option(raw: Optional[str], options: list) -> Optional[str]:
    """
    Case-insensitive, whitespace-trimmed matching against a list of
    valid option strings (player names, or keywords like 'abstain' /
    'skip'). Returns the canonically-cased option on a unique match,
    else None.

    Exists because exact-case-only matching was a real source of
    "I typed 'bob' and nothing happened" confusion during a live game
    -- a player's target choice should never silently fail just
    because of capitalization.
    """
    if raw is None:
        return None
    norm = raw.strip().lower()
    matches = [o for o in options if o.lower() == norm]
    return matches[0] if len(matches) == 1 else None


class GameServer:
    def __init__(self, host="0.0.0.0", port=5555, target_bots=0,
                 min_players=MIN_PLAYERS, auto_start=False):
        self.host = host
        self.port = port
        self.target_bots = target_bots
        self.min_players = min_players
        self.auto_start = auto_start

        self.lock = threading.Lock()
        self.players: list[Player] = []
        self.game_state: Optional[GameState] = None
        self.started = False

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen()
        print(f"[SERVER] Listening on {self.host}:{self.port}")
        print(f"[SERVER] Waiting for players to join (need {self.min_players} total, "
              f"{self.target_bots} will be bots)...")

        accept_thread = threading.Thread(target=self._accept_loop, args=(sock,), daemon=True)
        accept_thread.start()

        if self.auto_start:
            deadline = time.time() + 30
            while time.time() < deadline:
                with self.lock:
                    if len(self.players) + self.target_bots >= self.min_players:
                        break
                time.sleep(0.2)
        else:
            print(f"[SERVER] Press ENTER once enough players have joined to start.")
            while True:
                input()
                with self.lock:
                    human_count = len(self.players)
                total_planned = human_count + self.target_bots
                if total_planned >= self.min_players:
                    break
                print(f"[SERVER] Need at least {self.min_players} total players "
                      f"(have {human_count} humans + {self.target_bots} bots planned). "
                      f"Keep waiting, or restart with more --bots.")

        self._fill_bots()
        self._run_game()

    def _accept_loop(self, sock):
        while True:
            conn, _addr = sock.accept()
            threading.Thread(target=self._handle_new_connection, args=(conn,), daemon=True).start()

    def _handle_new_connection(self, conn):
        try:
            first_msg = next(protocol.recv_lines(conn))
        except StopIteration:
            conn.close()
            return

        name = (first_msg.get("name") or "").strip()
        provided_token = first_msg.get("token")
        if not name:
            protocol.send_json(conn, {"type": "error", "text": "A name is required to join."})
            conn.close()
            return

        with self.lock:
            existing = next((p for p in self.players if p.name == name and not p.connected), None)
            if existing:
                # Reconnect only succeeds if this connection can prove
                # it's the same client that originally joined as this
                # player -- matching on name alone would let anyone who
                # knows (or just guesses) a disconnected player's name
                # reconnect AS them and inherit their role, private
                # info, and vote. The token is issued once on first
                # join and never shown to anyone else.
                if existing.rejoin_token is not None and provided_token != existing.rejoin_token:
                    protocol.send_json(conn, {
                        "type": "error",
                        "text": f"A player named '{name}' is already in this game, and this "
                                f"connection doesn't have their reconnect token. If this is "
                                f"really you, run the client from the same folder you joined "
                                f"from originally (it saves a token there for exactly this).",
                    })
                    conn.close()
                    return
                existing.conn = conn
                existing.connected = True
                player = existing
                print(f"[SERVER] {name} reconnected.")
            else:
                if self.started:
                    protocol.send_json(conn, {"type": "error", "text": "Game already in progress."})
                    conn.close()
                    return
                if any(p.name == name for p in self.players):
                    protocol.send_json(conn, {"type": "error", "text": "That name is already taken."})
                    conn.close()
                    return
                player = Player(id=name, name=name, is_bot=False, conn=conn)
                player.rejoin_token = secrets.token_hex(8)
                self.players.append(player)
                print(f"[SERVER] {name} joined. ({len(self.players)} human player(s) so far)")

        protocol.send_json(conn, {
            "type": "joined", "text": f"Welcome, {name}!", "token": player.rejoin_token,
        })
        if player.role is not None:
            self._send_state_snapshot(player)
        self._broadcast_lobby_update()

        self._reader_loop(player)

    def _reader_loop(self, player):
        try:
            for msg in protocol.recv_lines(player.conn):
                self._handle_incoming(player, msg)
        except (ConnectionError, OSError):
            pass
        finally:
            player.connected = False
            print(f"[SERVER] {player.name} disconnected.")

    def _handle_incoming(self, player, msg):
        msg_type = msg.get("type")
        if msg_type == "chat":
            self._handle_chat(player, msg.get("text", ""))
        else:
            # vote / night_action / sabotage_decision replies go to
            # whichever _collect_action call is currently waiting on
            # this player.
            player.inbox.put(msg)

    def _handle_chat(self, player, text):
        if not text.strip() or self.game_state is None:
            return
        if self.game_state.phase != "DISCUSSION":
            return
        if not player.alive:
            self._send_private(player, {
                "type": "info",
                "text": "You've been eliminated and can no longer speak in discussion "
                        "-- you're spectating for the rest of the match.",
            })
            return
        text = text.strip()[:300]
        # Sender is excluded here -- their own terminal already echoes
        # what they typed, so sending it back would show it twice.
        others = [p for p in self.players if p.id != player.id]
        self._broadcast_chat(player.name, text, players=others)
        self.game_state.discussion_log.append((player.name, text))
        bot_logic.update_accusation_count(self.game_state, text)

    # ------------------------------------------------------------------
    # Messaging helpers
    # ------------------------------------------------------------------

    def _broadcast(self, msg, players=None):
        targets = players if players is not None else self.players
        for p in targets:
            if not p.is_bot and p.connected and p.conn:
                try:
                    protocol.send_json(p.conn, msg)
                except OSError:
                    p.connected = False

    def _broadcast_chat(self, sender, text, players=None):
        self._broadcast({"type": "chat", "sender": sender, "text": text}, players=players)

    def _send_private(self, player, msg):
        if player.is_bot or not player.connected or not player.conn:
            return
        try:
            protocol.send_json(player.conn, msg)
        except OSError:
            player.connected = False

    def _send_prompt(self, player, msg, timeout):
        """
        Like _send_private, but also records what was sent and when it
        expires, so a mid-window reconnect can be given the same prompt
        back (see _send_state_snapshot) instead of silently missing it.
        """
        self._send_private(player, msg)
        player.active_prompt = msg
        player.active_prompt_deadline = time.time() + timeout

    def _clear_prompt(self, player):
        player.active_prompt = None
        player.active_prompt_deadline = None

    def _send_state_snapshot(self, player):
        gs = self.game_state
        self._send_private(player, {
            "type": "state_snapshot",
            "round": gs.round_number,
            "phase": gs.phase,
            "role": player.role.value if player.role else None,
            "alive_players": [p.name for p in gs.alive_players()],
            "you_alive": player.alive,
            "graveyard": gs.elimination_log,
        })

        # Restore whatever this player should currently be seeing, so
        # reconnecting mid-window doesn't leave them stuck with no
        # prompt even though they can technically still act in time
        # (the collection loop they're waiting on doesn't care whether
        # they were disconnected -- only the CLIENT needs to be told
        # what to ask for again).
        now = time.time()
        if gs.phase == "DISCUSSION" and gs.discussion_deadline and gs.discussion_deadline > now:
            self._send_private(player, {
                "type": "phase", "name": "DISCUSSION", "round": gs.round_number,
                "timeout": int(gs.discussion_deadline - now),
                "alive_players": [p.name for p in gs.alive_players()],
            })
        elif player.active_prompt and player.active_prompt_deadline and player.active_prompt_deadline > now:
            resend = dict(player.active_prompt)
            resend["timeout"] = int(player.active_prompt_deadline - now)
            self._send_private(player, resend)

    def _fill_bots(self):
        with self.lock:
            used_names = {p.name for p in self.players}
            available = [n for n in BOT_NAME_POOL if n not in used_names]
            random.shuffle(available)
            for i in range(self.target_bots):
                bot_name = available[i] if i < len(available) else f"Bot_{i + 1}"
                self.players.append(Player(id=bot_name, name=bot_name, is_bot=True))
        print(f"[SERVER] Added {self.target_bots} bot player(s).")
        self._broadcast_lobby_update()

    def _broadcast_lobby_update(self):
        """
        Tells every connected human who else is in the lobby so far.
        Without this, a joined player had no visibility into anyone
        else who'd joined before the game started -- they'd just sit
        there with no player list at all.
        """
        if self.started:
            return
        with self.lock:
            players_snapshot = [{"name": p.name, "is_bot": p.is_bot} for p in self.players]
        human_count = sum(1 for p in players_snapshot if not p["is_bot"])
        humans_needed = max(0, self.min_players - self.target_bots)
        self._broadcast({
            "type": "lobby_update",
            "connected": human_count,
            "needed": humans_needed,
            "target_bots": self.target_bots,
            "players": players_snapshot,
        })

    # ------------------------------------------------------------------
    # Action collection -- the single disconnect/timeout/invalid-input
    # safe path that every phase of the game goes through.
    # ------------------------------------------------------------------

    def _collect_action(self, player, timeout, expected_type, validator, bot_fn, game_state):
        """
        Returns a validated (RAW, as-typed) action value for `player`,
        or None if nothing valid arrived within `timeout` seconds
        (covers: player never responds, player disconnected, player
        sent something invalid and then ran out of time, or a bot
        found no legal move). Callers that need the canonical
        (correctly-cased) name should re-run `_match_option` on the
        returned value themselves.

        This function makes no distinction between "disconnected" and
        "connected but silent" -- both simply time out the same way,
        and both are still able to reconnect/respond mid-window.
        """
        # Drop stale messages left over from an earlier phase so they
        # can't be misread as the answer to *this* prompt.
        while not player.inbox.empty():
            try:
                player.inbox.get_nowait()
            except queue.Empty:
                break

        if player.is_bot:
            delay = random.uniform(1.5, 6.0)

            def _produce():
                time.sleep(delay)
                try:
                    value = bot_fn(player, game_state)
                except Exception:
                    value = None
                player.inbox.put({"type": expected_type, "target": value})

            threading.Thread(target=_produce, daemon=True).start()

        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                msg = player.inbox.get(timeout=remaining)
            except queue.Empty:
                return None
            if msg.get("type") != expected_type:
                continue  # stale/irrelevant message -- keep waiting
            value = msg.get("target")
            if validator(value):
                return value
            if not player.is_bot:
                self._send_private(player, {
                    "type": "info",
                    "text": "Invalid input -- try again before time runs out.",
                })
            # loop again with the remaining time

    # ------------------------------------------------------------------
    # Game loop
    # ------------------------------------------------------------------

    def _run_game(self):
        self.started = True
        gs = GameState(players=self.players)
        self.game_state = gs

        build_role_assignments(gs.players)
        gs.mafia_ids = {p.id for p in gs.players if p.true_alignment == Alignment.MAFIA}

        print("[SERVER] Roles assigned:")
        for p in gs.players:
            tag = " (bot)" if p.is_bot else ""
            print(f"   {p.name}{tag}: {p.role.value}")

        self._announce_roles(gs)

        while True:
            gs.round_number += 1
            self._night_phase(gs)
            winner = check_win(gs.players)
            if winner:
                self._end_game(gs, winner)
                return
            self._day_phase(gs)
            winner = check_win(gs.players)
            if winner:
                self._end_game(gs, winner)
                return

    def _announce_roles(self, gs):
        for p in gs.players:
            if p.is_bot:
                continue
            teammates = []
            if p.role in (Role.MAFIA, Role.DOUBLE_AGENT):
                teammates = [t.name for t in gs.players
                             if t.id != p.id and t.true_alignment == Alignment.MAFIA]
            self._send_private(p, {
                "type": "role_assigned",
                "role": p.role.value,
                "description": ROLE_DESCRIPTIONS[p.role],
                "teammates": teammates,
            })

    def _find_alive(self, gs, role):
        return next((p for p in gs.alive_players() if p.role == role), None)

    def _revealed_role_text(self, player) -> str:
        return ("DOUBLE AGENT (secretly Mafia-aligned)"
                if player.role == Role.DOUBLE_AGENT else player.role.value)

    def _notify_eliminated(self, gs, player, cause: str):
        """
        A clear, first-person, unmistakable notice sent directly to a
        player the moment THEY are the one eliminated -- distinct from
        the public third-person death announcement, so there's no
        ambiguity about what just happened to them or what they can
        still do.
        """
        self._send_private(player, {
            "type": "eliminated",
            "cause": cause,  # "NIGHT" or "VOTE"
            "text": f"\U0001F480 You were eliminated ({cause.lower()}). "
                    f"You can no longer vote or take night actions, but you'll "
                    f"keep seeing everything that happens as a spectator.",
        })

    # ---- NIGHT ----

    def _night_phase(self, gs):
        gs.phase = "NIGHT"
        self._broadcast({"type": "phase", "name": "NIGHT", "round": gs.round_number})
        print(f"\n[SERVER] === NIGHT {gs.round_number} ===")

        detective = self._find_alive(gs, Role.DETECTIVE)
        # If the real Mafia is dead, a surviving Double Agent takes over
        # kill duty -- otherwise the Mafia team would go permanently
        # silent (no kills, no way to close out the game) the moment
        # their one active killer is voted out.
        mafia_actor = self._find_alive(gs, Role.MAFIA) or self._find_alive(gs, Role.DOUBLE_AGENT)
        doctor = self._find_alive(gs, Role.DOCTOR)
        saboteur = self._find_alive(gs, Role.SABOTEUR)
        saboteur_active = saboteur and not saboteur.saboteur_used

        def alive_names_except(exclude_id):
            return [p.name for p in gs.alive_players() if p.id != exclude_id]

        # All four night roles are prompted and collected AT THE SAME
        # TIME -- none of them need to see another's choice before
        # acting, so there's no reason to make one wait on another.
        # Only the final resolution step (below) needs everyone's
        # answer already in hand.
        results = {}
        threads = []

        detective_options = alive_names_except(detective.id) if detective else []
        if detective:
            self._send_prompt(detective, {
                "type": "prompt", "prompt_type": "night_action", "action": "investigate",
                "timeout": NIGHT_ACTION_TIMEOUT, "options": detective_options,
            }, NIGHT_ACTION_TIMEOUT)

            def collect_detective():
                results["detective"] = self._collect_action(
                    detective, NIGHT_ACTION_TIMEOUT, "night_action",
                    lambda v: _match_option(v, detective_options) is not None,
                    bot_logic.bot_investigate_target, gs,
                )
            threads.append(threading.Thread(target=collect_detective, daemon=True))

        mafia_options = alive_names_except(mafia_actor.id) if mafia_actor else []
        if mafia_actor:
            self._send_prompt(mafia_actor, {
                "type": "prompt", "prompt_type": "night_action", "action": "kill",
                "timeout": NIGHT_ACTION_TIMEOUT, "options": mafia_options,
            }, NIGHT_ACTION_TIMEOUT)

            def collect_mafia():
                results["mafia"] = self._collect_action(
                    mafia_actor, NIGHT_ACTION_TIMEOUT, "night_action",
                    lambda v: _match_option(v, mafia_options) is not None,
                    bot_logic.bot_kill_target, gs,
                )
            threads.append(threading.Thread(target=collect_mafia, daemon=True))

        doctor_options = [p.name for p in gs.alive_players()] if doctor else []
        if doctor:
            self._send_prompt(doctor, {
                "type": "prompt", "prompt_type": "night_action", "action": "protect",
                "timeout": NIGHT_ACTION_TIMEOUT, "options": doctor_options,
            }, NIGHT_ACTION_TIMEOUT)

            def collect_doctor():
                results["doctor"] = self._collect_action(
                    doctor, NIGHT_ACTION_TIMEOUT, "night_action",
                    lambda v: _match_option(v, doctor_options) is not None,
                    bot_logic.bot_protect_target, gs,
                )
            threads.append(threading.Thread(target=collect_doctor, daemon=True))

        saboteur_options = alive_names_except(saboteur.id) + ["skip"] if saboteur_active else []
        if saboteur_active:
            self._send_prompt(saboteur, {
                "type": "prompt", "prompt_type": "sabotage_decision",
                "timeout": NIGHT_ACTION_TIMEOUT, "options": saboteur_options,
            }, NIGHT_ACTION_TIMEOUT)

            def collect_saboteur():
                results["saboteur"] = self._collect_action(
                    saboteur, NIGHT_ACTION_TIMEOUT, "sabotage_decision",
                    lambda v: _match_option(v, saboteur_options) is not None,
                    lambda *_: None, gs,  # Saboteur is always human -- no bot path
                )
            threads.append(threading.Thread(target=collect_saboteur, daemon=True))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=NIGHT_ACTION_TIMEOUT + 3)

        # Night is over -- none of these prompts are still "active" for
        # reconnect-resend purposes, whether or not each role answered.
        for p in (detective, mafia_actor, doctor, saboteur):
            if p:
                self._clear_prompt(p)

        # ---- Process each result now that all are in ----

        if detective and results.get("detective"):
            target_name = _match_option(results["detective"], detective_options)
            if target_name:
                target = gs.get_player_by_name(target_name)
                result = run_investigation(target)
                if detective.is_bot:
                    if result == Alignment.MAFIA:
                        detective.known_mafia_id = target.id
                else:
                    self._send_private(detective, {
                        "type": "private_result",
                        "text": f"{target.name} is {result.value}-ALIGNED.",
                    })

        mafia_target = None
        if mafia_actor and results.get("mafia"):
            target_name = _match_option(results["mafia"], mafia_options)
            if target_name:
                mafia_target = gs.get_player_by_name(target_name)
                double_agent = self._find_alive(gs, Role.DOUBLE_AGENT)
                if double_agent and double_agent.id != mafia_actor.id:
                    self._send_private(double_agent, {
                        "type": "info",
                        "text": f"[Mafia] Tonight's target: {mafia_target.name}",
                    })

        doctor_target = None
        if doctor and results.get("doctor"):
            target_name = _match_option(results["doctor"], doctor_options)
            if target_name:
                doctor_target = gs.get_player_by_name(target_name)

        if saboteur_active and results.get("saboteur"):
            decision = _match_option(results["saboteur"], saboteur_options)
            if decision and decision != "skip":
                target = gs.get_player_by_name(decision)
                gs.sabotage_armed = target.id
                saboteur.saboteur_used = True
                self._send_private(saboteur, {
                    "type": "info",
                    "text": f"Sabotage armed on {target.name} for tomorrow's vote.",
                })

        # ---- Resolve the kill ----
        death = resolve_night_kill(mafia_target, doctor_target)
        if death:
            death.alive = False
            print(f"[SERVER] {death.name} ({death.role.value}) was eliminated at night.")
            gs.elimination_log.append({
                "round": gs.round_number, "phase": "NIGHT",
                "player": death.name, "role": self._revealed_role_text(death),
            })
            self._notify_eliminated(gs, death, "NIGHT")
        else:
            print("[SERVER] No one died last night.")
        gs.last_night_death = death

    # ---- DAY ----

    def _day_phase(self, gs):
        gs.phase = "ANNOUNCE"
        if gs.last_night_death:
            d = gs.last_night_death
            self._broadcast({"type": "death_announcement", "player": d.name,
                              "role": self._revealed_role_text(d), "graveyard": gs.elimination_log})
        else:
            self._broadcast({"type": "death_announcement", "player": None, "role": None,
                              "graveyard": gs.elimination_log})

        gs.phase = "DISCUSSION"
        print(f"[SERVER] === DAY {gs.round_number}: discussion ===")
        self._broadcast({
            "type": "phase", "name": "DISCUSSION", "round": gs.round_number,
            "timeout": DISCUSSION_TIMEOUT,
            "alive_players": [p.name for p in gs.alive_players()],
        })
        self._run_discussion(gs)

        gs.phase = "VOTING"
        print(f"[SERVER] === DAY {gs.round_number}: voting ===")
        self._broadcast({"type": "phase", "name": "VOTING", "round": gs.round_number,
                          "timeout": VOTE_TIMEOUT})
        self._run_voting(gs)

    def _run_discussion(self, gs):
        alive_bots = [p for p in gs.alive_players() if p.is_bot]
        stop_event = threading.Event()
        high = max(3, DISCUSSION_TIMEOUT - 5)
        gs.discussion_deadline = time.time() + DISCUSSION_TIMEOUT

        def bot_talk(bot):
            time.sleep(random.uniform(2, high))
            if stop_event.is_set():
                return
            line = bot_logic.bot_discussion_line(bot, gs)
            if line:
                self._broadcast_chat(bot.name, line)
                gs.discussion_log.append((bot.name, line))
                bot_logic.update_accusation_count(gs, line)

        threads = [threading.Thread(target=bot_talk, args=(b,), daemon=True) for b in alive_bots]
        for t in threads:
            t.start()
        time.sleep(DISCUSSION_TIMEOUT)
        stop_event.set()
        gs.discussion_deadline = None

    def _run_voting(self, gs):
        alive = gs.alive_players()
        dead_humans = [p for p in gs.players if not p.alive and not p.is_bot]
        options = [p.name for p in alive] + ["abstain"]

        for p in alive:
            self._send_prompt(p, {"type": "prompt", "prompt_type": "vote",
                                   "timeout": VOTE_TIMEOUT, "options": options}, VOTE_TIMEOUT)
        # Eliminated players don't get a vote prompt at all -- tell them
        # plainly why, rather than leaving them to wonder why nothing
        # is happening on their screen.
        for p in dead_humans:
            self._send_private(p, {
                "type": "info",
                "text": "You're eliminated and can't vote -- watching as a spectator.",
            })

        results = {}

        def collect_for(p):
            value = self._collect_action(
                p, VOTE_TIMEOUT, "vote",
                lambda v: _match_option(v, options) is not None,
                bot_logic.bot_vote_target, gs,
            )
            results[p.id] = value

        threads = [threading.Thread(target=collect_for, args=(p,), daemon=True) for p in alive]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=VOTE_TIMEOUT + 3)

        # Voting is over -- these prompts are no longer "active" for
        # reconnect-resend purposes, whether or not each player voted.
        for p in alive:
            self._clear_prompt(p)

        votes = {}
        for p in alive:
            chosen_name = _match_option(results.get(p.id), options)
            if chosen_name is None or chosen_name == "abstain":
                votes[p.id] = None
            else:
                target = gs.get_player_by_name(chosen_name)
                votes[p.id] = target.id if target else None

        counts_by_id = tally_votes(votes, gs.sabotage_armed)
        gs.sabotage_armed = None  # the charge is consumed either way

        counts_by_name = {gs.get_player(pid).name: c for pid, c in counts_by_id.items()}
        eliminated_id = resolve_vote(counts_by_id)

        eliminated_player = None
        if eliminated_id:
            eliminated_player = gs.get_player(eliminated_id)
            eliminated_player.alive = False
            print(f"[SERVER] {eliminated_player.name} ({eliminated_player.role.value}) voted out.")
            gs.elimination_log.append({
                "round": gs.round_number, "phase": "DAY VOTE",
                "player": eliminated_player.name,
                "role": self._revealed_role_text(eliminated_player),
            })
            self._notify_eliminated(gs, eliminated_player, "VOTE")
        else:
            print("[SERVER] No majority -- no one eliminated.")

        self._broadcast({
            "type": "vote_results",
            "tally": counts_by_name,
            "eliminated": eliminated_player.name if eliminated_player else None,
            "eliminated_role": (self._revealed_role_text(eliminated_player)
                                 if eliminated_player else None),
            "graveyard": gs.elimination_log,
        })

    # ---- END ----

    def _end_game(self, gs, winner):
        # Uses _revealed_role_text, not the raw role value, so the
        # Double Agent's row reads "DOUBLE AGENT (secretly Mafia-
        # aligned)" here too, matching every other reveal in the game
        # instead of showing the bare enum name.
        reveal = [{"name": p.name, "role": self._revealed_role_text(p), "alive": p.alive}
                  for p in gs.players]
        gs.phase = "GAME_OVER"
        self._broadcast({
            "type": "game_over",
            "winner": winner,
            "rounds_played": gs.round_number,
            "elimination_log": gs.elimination_log,
            "reveal": reveal,
        })
        print(f"\n[SERVER] ===== GAME OVER -- {winner} WIN ({gs.round_number} rounds) =====")
        for entry in gs.elimination_log:
            print(f"   Round {entry['round']} [{entry['phase']}]: "
                  f"{entry['player']} -- {entry['role']}")
        for p in gs.players:
            tag = " (bot)" if p.is_bot else ""
            status = "alive" if p.alive else "dead"
            print(f"   {p.name}{tag:6s} {p.role.value:14s} ({status})")
