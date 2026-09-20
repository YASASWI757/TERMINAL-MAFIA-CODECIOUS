"""
Terminal Mafia -- human player client.

Usage:
    python run_client.py --host 127.0.0.1 --port 5555 --name Alice

Run this once per human player, each in its own terminal window (same
machine) or on each player's own machine (LAN/hotspot, using the
server's LAN IP as --host).

If your connection drops, just re-run the exact same command with the
same --name -- the server will re-attach you to your existing role and
game state.

UI: this uses a curses split-screen layout (scrolling message log on
top, a pinned input line at the bottom, a live countdown status bar)
on platforms where the `curses` module is available -- Linux and Mac
ship it in the standard library, no install needed. curses fully owns
the terminal screen and redraws it deterministically every frame, so
incoming messages (chat, countdown ticks, death announcements, etc.)
can never corrupt or duplicate what you're mid-typing -- this replaces
an earlier approach built on Python's plain `input()`, which handed
control to GNU Readline and had no reliable way to coordinate with
background output.

On platforms without `curses` (stock Windows Python), this falls back
automatically to a simpler line-based client -- still fully playable,
just without the split-screen layout and live countdown (run `pip
install windows-curses` on Windows to get the full UI instead).
"""

import argparse
import socket
import sys
import threading
import time
import queue
import textwrap
import os

from mafia import protocol

try:
    import curses
    _HAS_CURSES = True
except ImportError:
    _HAS_CURSES = False


# ----------------------------------------------------------------------
# Reconnect token persistence
# ----------------------------------------------------------------------
# The server now requires proof you're the same client that originally
# joined as a given name before it'll reconnect you as that player --
# otherwise anyone who knew (or guessed) a disconnected player's name
# could reconnect AS them. The server issues a token on first join; we
# save it to a small local file so re-running this same command from
# the same folder "just works" the same way it always did, with no new
# flag or step for the player to remember.

def _token_file_path(name):
    safe_name = "".join(c for c in name if c.isalnum() or c in ("-", "_")) or "player"
    return f".mafia_rejoin_{safe_name}.token"


def _load_saved_token(name):
    path = _token_file_path(name)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                token = f.read().strip()
                return token or None
        except OSError:
            return None
    return None


def _save_token(name, token):
    if not token:
        return
    try:
        with open(_token_file_path(name), "w") as f:
            f.write(token)
    except OSError:
        pass  # non-fatal -- a future reconnect just won't be pre-authenticated


# A short, silent-film-style ASCII beat played in place (redrawn over
# itself, not scrolled) right before the death reveal text. Plain
# characters only, no image/video conversion -- just a handful of
# hand-built frames with guaranteed column alignment (generated once
# with a small script, then baked in as constants here). Runs for
# roughly 1.5 seconds total. Curses-only: the plain-text fallback
# client keeps its original one-line death announcement, same as the
# live countdown is a curses-only touch.
DEATH_ANIMATION_FRAMES = [
    " .--.                              O\n"
    "( oo )>  *                        /|\\\n"
    " `--'                             / \\",

    " .--.                              O\n"
    "( oo )>          *                /|\\\n"
    " `--'                             / \\",

    " .--.                              O\n"
    "( oo )>                  *        /|\\\n"
    " `--'                             / \\",

    " .--.                              O\n"
    "( oo )*                           \\|/\n"
    " `--'                              |",

    " .--.\n"
    "( oo )>\n"
    " `--'                          _O__\n"
    "                               \\_/`.",
]
DEATH_ANIMATION_FRAME_DELAY = 0.28  # seconds per frame


# Hand-built 5-row block-letter banner (generated once with a small
# script for guaranteed column alignment, then baked in as a constant
# here -- same approach as the death animation frames). Shown on the
# lobby screen before the game starts.
TITLE_BANNER = [
    "\u2588\u2588\u2588\u2588\u2588 \u2588\u2588\u2588\u2588\u2588 \u2588\u2588\u2588\u2588  \u2588   \u2588 \u2588\u2588\u2588\u2588\u2588 \u2588   \u2588  \u2588\u2588\u2588  \u2588",
    "  \u2588   \u2588     \u2588   \u2588 \u2588\u2588 \u2588\u2588   \u2588   \u2588\u2588  \u2588 \u2588   \u2588 \u2588",
    "  \u2588   \u2588\u2588\u2588\u2588  \u2588\u2588\u2588\u2588  \u2588 \u2588 \u2588   \u2588   \u2588 \u2588 \u2588 \u2588\u2588\u2588\u2588\u2588 \u2588",
    "  \u2588   \u2588     \u2588  \u2588  \u2588   \u2588   \u2588   \u2588  \u2588\u2588 \u2588   \u2588 \u2588",
    "  \u2588   \u2588\u2588\u2588\u2588\u2588 \u2588   \u2588 \u2588   \u2588 \u2588\u2588\u2588\u2588\u2588 \u2588   \u2588 \u2588   \u2588 \u2588\u2588\u2588\u2588\u2588",
    "         \u2588   \u2588  \u2588\u2588\u2588  \u2588\u2588\u2588\u2588\u2588 \u2588\u2588\u2588\u2588\u2588  \u2588\u2588\u2588",
    "         \u2588\u2588 \u2588\u2588 \u2588   \u2588 \u2588       \u2588   \u2588   \u2588",
    "         \u2588 \u2588 \u2588 \u2588\u2588\u2588\u2588\u2588 \u2588\u2588\u2588\u2588    \u2588   \u2588\u2588\u2588\u2588\u2588",
    "         \u2588   \u2588 \u2588   \u2588 \u2588       \u2588   \u2588   \u2588",
    "         \u2588   \u2588 \u2588   \u2588 \u2588     \u2588\u2588\u2588\u2588\u2588 \u2588   \u2588",
]
TAGLINE = "deception \u00b7 deduction \u00b7 survival"


# ======================================================================
# Curses UI (primary experience -- Linux / Mac, or Windows with
# `windows-curses` installed)
# ======================================================================

class ClientUI:
    """
    A small single-threaded-draw TUI. The network listener thread only
    ever pushes parsed messages onto `self.incoming` (a thread-safe
    Queue) -- it never touches curses or shared state directly. Every
    actual screen update and every state mutation happens in `run()`,
    on the main thread, once per loop iteration. This is deliberate:
    curses is not safe to draw to from multiple threads at once, so
    keeping ALL drawing on one thread sidesteps that entirely rather
    than working around it.
    """

    def __init__(self, stdscr, sock, name):
        self.stdscr = stdscr
        self.sock = sock
        self.name = name

        self.incoming = queue.Queue()
        self.log_lines = []          # raw (unwrapped) log entries, oldest first
        self.input_buffer = ""
        self.prompt_type = None
        self.prompt_hint = "(nothing expected)"
        self.countdown_deadline = None   # time.time() + seconds, or None
        self.countdown_label = ""
        self.running = True
        self.game_ended = False  # True once game_over has been received
        self.is_ghost = False    # True once this player has been eliminated

        # Lobby screen state -- shown until the game actually starts
        # (the first role_assigned message flips this off).
        self.in_lobby = True
        self.lobby_connected = 0
        self.lobby_needed = 0
        self.lobby_target_bots = 0
        self.lobby_players = []

    # ---- lifecycle ----

    def run(self):
        try:
            curses.curs_set(1)
        except curses.error:
            pass  # some terminals don't support cursor visibility changes
        self.stdscr.timeout(100)  # getch() blocks up to 100ms, then returns -1

        threading.Thread(target=self._listen, daemon=True).start()
        self._log(f"Connected as {self.name}. Waiting for the game to start...")

        while self.running:
            self._drain_incoming()
            self._handle_keypress()
            self._draw()

    def _listen(self):
        try:
            for msg in protocol.recv_lines(self.sock):
                self.incoming.put(msg)
        except (ConnectionError, OSError):
            pass
        self.incoming.put({"type": "_connection_lost"})

    # ---- input handling ----

    def _handle_keypress(self):
        try:
            ch = self.stdscr.getch()
        except curses.error:
            ch = -1
        if ch == -1 or ch == curses.KEY_RESIZE:
            return  # layout is recomputed from current size every _draw() call
        if ch in (curses.KEY_ENTER, 10, 13):
            self._submit_input()
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            self.input_buffer = self.input_buffer[:-1]
        elif ch == 3:  # Ctrl+C
            self.running = False
        elif 32 <= ch <= 126:  # printable ASCII
            self.input_buffer += chr(ch)
        # other control/navigation keys are ignored -- not needed for
        # single-line name/vote/chat entry

    def _submit_input(self):
        line = self.input_buffer.strip()
        self.input_buffer = ""
        if self.prompt_type == "exit":
            # Bare Enter (empty input is normal here) confirms the user
            # is done reading and lets the curses loop actually end, so
            # curses.wrapper can call endwin() and hand the terminal
            # back cleanly instead of spinning forever.
            self.running = False
            return
        if not line:
            return
        self._send_current(line)

    def _send_current(self, line):
        try:
            if self.prompt_type == "chat":
                protocol.send_json(self.sock, {"type": "chat", "text": line})
                # The server no longer echoes your own chat back to you
                # (that was the source of the duplicate-text bug) -- so
                # the client logs its own sent line locally instead.
                self._log(f"[You]: {line}")
            elif self.prompt_type == "vote":
                protocol.send_json(self.sock, {"type": "vote", "target": line})
                self._log(f"> Voted: {line}")
                self._clear_countdown()
            elif self.prompt_type == "night_action":
                protocol.send_json(self.sock, {"type": "night_action", "target": line})
                self._log(f"> Submitted: {line}")
                self._clear_countdown()
            elif self.prompt_type == "sabotage_decision":
                protocol.send_json(self.sock, {"type": "sabotage_decision", "target": line})
                self._log(f"> Submitted: {line}")
                self._clear_countdown()
            elif self.prompt_type == "play_again":
                protocol.send_json(self.sock, {"type": "play_again", "target": line})
                self._log(f"> Submitted: {line}")
                self._clear_countdown()
            elif self.prompt_type == "ghost_chat":
                protocol.send_json(self.sock, {"type": "ghost_chat", "text": line})
                self._log(f"[Ghost] You: {line}")
            else:
                self._log("[INFO] Nothing is expecting input right now.")
        except OSError:
            self._log("[ERROR] Could not send -- connection may be lost.")

    # ---- countdown ----

    def _set_countdown(self, seconds, label):
        self.countdown_deadline = time.time() + seconds
        self.countdown_label = label

    def _clear_countdown(self):
        self.countdown_deadline = None
        self.countdown_label = ""

    # ---- lobby screen ----

    def _draw_lobby(self):
        h, w = self.stdscr.getmaxyx()
        self.stdscr.erase()

        def put_centered(row, text, attr=curses.A_NORMAL):
            if row < 0 or row >= h:
                return
            col = max(2, (w - len(text)) // 2)
            try:
                self.stdscr.addnstr(row, col, text, max(0, w - col - 2), attr)
            except curses.error:
                pass

        def put_left(row, text, indent=4, attr=curses.A_NORMAL):
            if row < 0 or row >= h:
                return
            try:
                self.stdscr.addnstr(row, indent, text, max(0, w - indent - 2), attr)
            except curses.error:
                pass

        def border_row(row):
            if row < 0 or row >= h or w < 2:
                return
            try:
                self.stdscr.addstr(row, 0, "\u2551")
                self.stdscr.addstr(row, w - 1, "\u2551")
            except curses.error:
                pass

        row = 1
        for line in TITLE_BANNER:
            put_centered(row, line, curses.A_BOLD)
            row += 1
        row += 1
        put_centered(row, TAGLINE)
        row += 2

        sep = "\u2550" * max(0, w - 2)
        try:
            self.stdscr.addnstr(row, 1, sep, max(0, w - 2))
        except curses.error:
            pass
        row += 2

        put_left(row, "LOBBY", attr=curses.A_BOLD)
        row += 2

        if self.lobby_target_bots:
            status = (f"Players connected: {self.lobby_connected} / {self.lobby_needed} "
                      f"({self.lobby_target_bots} bot(s) will fill the rest)")
        else:
            status = f"Players connected: {self.lobby_connected} / {self.lobby_needed}"
        put_left(row, status)
        row += 2

        for p in self.lobby_players:
            tag = " (bot)" if p["is_bot"] else ""
            put_left(row, f"\u25cf {p['name']}{tag}")
            row += 1
            if row >= h - 3:
                break

        for r in range(0, min(row + 2, h)):
            border_row(r)

        put_left(min(h - 1, row + 1), "Waiting for the host to start the match...")

        self.stdscr.refresh()

    # ---- log ----

    def _log(self, text):
        for line in str(text).split("\n"):
            self.log_lines.append(line)
        if len(self.log_lines) > 1000:  # cap memory for a very long match
            self.log_lines = self.log_lines[-1000:]

    def _log_graveyard(self, graveyard):
        """Compact running tally of everyone eliminated so far, shown
        right after whatever just changed it -- interpretation of the
        'graveyard chart' ask: a quick-glance history rather than
        needing to scroll back through the whole log to remember who's
        already out and what they were."""
        if not graveyard:
            return
        parts = [f"{e['player']}({e['role']})" for e in graveyard]
        self._log("Graveyard: " + ", ".join(parts))

    # ---- death animation ----

    def _play_death_animation(self):
        """
        Briefly takes over the whole screen to play DEATH_ANIMATION_FRAMES
        in place (each frame redrawn over the last, not scrolled), then
        hands back to the normal log view. Runs on the main thread like
        all drawing here, so it briefly pauses keyboard handling for its
        ~1.5s runtime -- the same trade-off any short cutscene makes.
        """
        h, w = self.stdscr.getmaxyx()
        for frame in DEATH_ANIMATION_FRAMES:
            lines = frame.split("\n")
            frame_w = max(len(line) for line in lines)
            start_row = max(0, (h - len(lines)) // 2)
            start_col = max(0, (w - frame_w) // 2)
            self.stdscr.erase()
            for i, line in enumerate(lines):
                row = start_row + i
                if 0 <= row < h:
                    try:
                        self.stdscr.addnstr(row, start_col, line, max(0, w - start_col - 1))
                    except curses.error:
                        pass
            self.stdscr.refresh()
            time.sleep(DEATH_ANIMATION_FRAME_DELAY)
        time.sleep(0.3)  # brief hold on the final frame before returning to the log

    # ---- incoming server messages ----

    def _drain_incoming(self):
        while True:
            try:
                msg = self.incoming.get_nowait()
            except queue.Empty:
                return
            self._handle_message(msg)

    def _handle_message(self, msg):
        t = msg.get("type")

        if t == "_connection_lost":
            if self.game_ended:
                # Expected: the server process exits once the match is
                # over, which closes the socket. Not an error -- don't
                # show the "try reconnecting" warning for this case.
                self._log("[Server closed the connection now that the match has ended.]")
            else:
                self._log("[Connection lost -- the server may have ended, or your network "
                          "dropped. Re-run this command with the same --name to reconnect.]")
            self.prompt_type = "exit"
            self.prompt_hint = "Press Enter to close"
            self._clear_countdown()

        elif t == "joined":
            self._log(f"[SERVER] {msg['text']}")
            _save_token(self.name, msg.get("token"))

        elif t == "lobby_update":
            self.lobby_connected = msg["connected"]
            self.lobby_needed = msg["needed"]
            self.lobby_target_bots = msg["target_bots"]
            self.lobby_players = msg["players"]

        elif t == "error":
            self._log(f"[ERROR] {msg['text']}")

        elif t == "role_assigned":
            self.in_lobby = False
            self.game_ended = False  # a fresh match is starting (first one, or a replay)
            self.is_ghost = False    # everyone starts alive in a fresh match
            self._log("=" * 50)
            self._log(f"YOUR ROLE: {msg['role']}")
            self._log(msg["description"])
            if msg.get("teammates"):
                self._log(f"Fellow Mafia-aligned teammate(s): {', '.join(msg['teammates'])}")
            self._log("=" * 50)

        elif t == "phase":
            self._log("")
            self._log(f"--- {msg['name']} (Round {msg['round']}) ---")
            if msg["name"] == "DISCUSSION":
                self._log(f"Alive: {', '.join(msg['alive_players'])}")
                self._log(f"You have {msg['timeout']}s to discuss.")
                self.prompt_type = "chat"
                self.prompt_hint = "Chat"
                self._set_countdown(msg["timeout"], "Discussion")
            elif msg["name"] == "NIGHT":
                self._log("The town sleeps. If you have a night action, you'll be prompted below.")
                self.prompt_type = None
                self.prompt_hint = "(nothing expected)"
                self._clear_countdown()
            elif msg["name"] == "VOTING":
                self._log("Voting is open -- wait for your prompt below.")
                self._clear_countdown()

            # Ghosts are never asked to vote or act, and aren't bound by
            # the living's turn structure -- whatever the phase above
            # just set prompt_type to, a ghost's input always goes to
            # the separate ghost-chat channel instead. This runs after
            # the phase-specific logic above (not woven into it) so it
            # can never change what a LIVING player's prompt_type ends
            # up as -- for anyone with is_ghost still False, none of
            # this block does anything at all.
            if self.is_ghost:
                self.prompt_type = "ghost_chat"
                self.prompt_hint = "Ghost chat (only other eliminated players see this)"

        elif t == "death_announcement":
            if msg["player"]:
                self._play_death_animation()
                self._log(f"{msg['player']} was found dead this morning. They were... {msg['role']}!")
            else:
                self._log("No one died last night.")
            self._log_graveyard(msg.get("graveyard"))

        elif t == "eliminated":
            self.is_ghost = True
            self._log(msg["text"])
            self._log("You can now chat with other eliminated players -- the living can't see it.")
            self.prompt_type = "ghost_chat"
            self.prompt_hint = "Ghost chat (only other eliminated players see this)"
            self._clear_countdown()

        elif t == "chat":
            self._log(f"[{msg['sender']}]: {msg['text']}")

        elif t == "ghost_chat":
            self._log(f"[Ghost] {msg['sender']}: {msg['text']}")

        elif t == "private_result":
            self._log(f"[PRIVATE RESULT] {msg['text']}")

        elif t == "info":
            self._log(f"[INFO] {msg['text']}")

        elif t == "prompt":
            self.prompt_type = msg["prompt_type"]
            options = msg.get("options", [])
            timeout = msg.get("timeout", 0)
            self._log(f"({timeout}s) Options: {', '.join(options)}")
            if self.prompt_type == "sabotage_decision":
                self.prompt_hint = "Sabotage target (or 'skip')"
                self._set_countdown(timeout, "Sabotage decision")
            elif self.prompt_type == "night_action":
                action = msg.get("action", "act")
                self.prompt_hint = f"{action.capitalize()} target"
                self._set_countdown(timeout, action.capitalize())
            elif self.prompt_type == "vote":
                self.prompt_hint = "Vote (name or 'abstain')"
                self._set_countdown(timeout, "Vote")

        elif t == "vote_results":
            self._log("--- VOTE RESULTS ---")
            for name, count in msg["tally"].items():
                self._log(f"  {name}: {count} vote(s)")
            if msg["eliminated"]:
                self._log(f"{msg['eliminated']} has been eliminated. They were {msg['eliminated_role']}!")
            else:
                self._log("No majority reached -- no one was eliminated.")
            self._log_graveyard(msg.get("graveyard"))

        elif t == "state_snapshot":
            self._log(f"[Reconnected] Round {msg['round']}, Phase {msg['phase']}, "
                      f"Role: {msg['role']}, You are: {'ALIVE' if msg['you_alive'] else 'dead'}")
            self._log_graveyard(msg.get("graveyard"))
            if not msg["you_alive"]:
                self.is_ghost = True
                self.prompt_type = "ghost_chat"
                self.prompt_hint = "Ghost chat (only other eliminated players see this)"

        elif t == "game_over":
            self._clear_countdown()
            self.game_ended = True
            self._log("#" * 50)
            self._log(f"GAME OVER -- {msg['winner']} WIN! "
                      f"({msg.get('rounds_played', '?')} rounds played)")
            entries = msg.get("elimination_log") or []
            if entries:
                self._log("")
                self._log("Match summary:")
                for e in entries:
                    self._log(f"  Round {e['round']} [{e['phase']}]: {e['player']} -- {e['role']}")
            self._log("")
            self._log("Final roles:")
            for r in msg["reveal"]:
                status = "alive" if r["alive"] else "dead"
                self._log(f"  {r['name']:20s} {r['role']:15s} ({status})")
            self._log("#" * 50)
            self._log("")
            self._log("Press Enter to close.")
            # Deliberately NOT setting self.running = False here -- the
            # player needs a moment to actually read the summary above.
            # Enter (handled by _submit_input) is what ends the loop,
            # which is what lets curses.wrapper restore the terminal
            # cleanly instead of the screen staying frozen forever.
            self.prompt_type = "exit"
            self.prompt_hint = "Press Enter to close"

        elif t == "play_again_prompt":
            # Arrives right after game_over for any match that had at
            # least one human -- overrides the "Press Enter to close"
            # state that was just set, since there's something more to
            # decide first. If this player doesn't answer in time (or
            # says no), the server replaces them with a bot for the
            # next match; if nobody stays, the connection will drop
            # shortly after and the existing exit flow takes over from
            # there -- no separate handling needed for that case.
            timeout = msg.get("timeout", 20)
            self.prompt_type = "play_again"
            self.prompt_hint = "Play again? (yes/no)"
            self._log("")
            self._log(f"({timeout}s) Play again? (yes/no)")
            self._set_countdown(timeout, "Play again?")

    # ---- drawing ----

    def _draw(self):
        if self.in_lobby:
            self._draw_lobby()
            return

        h, w = self.stdscr.getmaxyx()
        log_h = max(1, h - 3)
        wrap_width = max(10, w - 1)
        self.stdscr.erase()

        # Word-wrap at draw time (not at log time) so a terminal resize
        # is reflected immediately without needing to reformat history.
        wrapped = []
        for entry in self.log_lines:
            if entry == "":
                wrapped.append("")
            else:
                wrapped.extend(textwrap.wrap(entry, wrap_width) or [""])

        for i, line in enumerate(wrapped[-log_h:]):
            try:
                self.stdscr.addnstr(i, 0, line, wrap_width)
            except curses.error:
                pass

        status_row = log_h
        if self.countdown_deadline is not None:
            remaining = max(0, int(self.countdown_deadline - time.time()))
            status_text = f" {self.countdown_label}: {remaining}s remaining "
        else:
            status_text = " "
        try:
            self.stdscr.addnstr(status_row, 0, status_text.ljust(wrap_width),
                                wrap_width, curses.A_REVERSE)
        except curses.error:
            pass

        sep_row = log_h + 1
        try:
            self.stdscr.addnstr(sep_row, 0, "-" * wrap_width, wrap_width)
        except curses.error:
            pass

        input_row = log_h + 2
        display = f"{self.prompt_hint} > {self.input_buffer}"
        try:
            self.stdscr.addnstr(input_row, 0, display, wrap_width)
        except curses.error:
            pass
        cursor_col = min(wrap_width, len(self.prompt_hint) + 3 + len(self.input_buffer))
        try:
            self.stdscr.move(input_row, cursor_col)
        except curses.error:
            pass

        self.stdscr.refresh()


def _run_curses_client(sock, name):
    import locale
    locale.setlocale(locale.LC_ALL, "")  # needed for proper UTF-8/emoji rendering

    def _entry(stdscr):
        ClientUI(stdscr, sock, name).run()

    curses.wrapper(_entry)


# ======================================================================
# Fallback client (no curses available -- e.g. stock Windows Python)
# ======================================================================

def _run_simple_client(sock, name):
    state = {"prompt_type": None, "name": name, "game_ended": False, "is_ghost": False}

    def _listen():
        try:
            for msg in protocol.recv_lines(sock):
                _handle_message_simple(msg, state)
        except (ConnectionError, OSError):
            pass
        if state["game_ended"]:
            print("\n[Server closed the connection now that the match has ended.]")
        else:
            print("\n[Connection lost -- the server may have ended, or your network "
                  "dropped. Re-run this command with the same --name to reconnect.]")
        state["prompt_type"] = "exit"

    threading.Thread(target=_listen, daemon=True).start()

    print(f"Connected as {name}. Waiting for the game to start...")
    print("(Tip: if you get disconnected, just re-run this same command to rejoin.)")
    print("(Note: running without the curses UI -- install 'windows-curses' for the "
          "full split-screen experience with a live countdown.)\n")

    while True:
        try:
            line = input()
        except EOFError:
            break
        if state.get("prompt_type") == "exit":
            # Bare Enter is expected here -- don't fall through to the
            # empty-input check below, which would otherwise swallow it
            # and leave this loop (and the process) running forever.
            break
        line = line.strip()
        if not line:
            continue
        _send_current_simple(sock, state, line)

    print("Goodbye!")


def _print_graveyard_simple(graveyard):
    if not graveyard:
        return
    parts = [f"{e['player']}({e['role']})" for e in graveyard]
    print("Graveyard: " + ", ".join(parts))


def _handle_message_simple(msg, state):
    t = msg.get("type")
    if t == "joined":
        print(f"[SERVER] {msg['text']}")
        _save_token(state.get("name"), msg.get("token"))
    elif t == "error":
        print(f"[ERROR] {msg['text']}")
    elif t == "role_assigned":
        state["game_ended"] = False  # a fresh match is starting (first one, or a replay)
        state["is_ghost"] = False    # everyone starts alive in a fresh match
        print("\n" + "=" * 50)
        print(f"YOUR ROLE: {msg['role']}")
        print(msg["description"])
        if msg.get("teammates"):
            print(f"Fellow Mafia-aligned teammate(s): {', '.join(msg['teammates'])}")
        print("=" * 50 + "\n")
    elif t == "phase":
        print(f"\n--- {msg['name']} (Round {msg['round']}) ---")
        if msg["name"] == "DISCUSSION":
            print(f"Alive: {', '.join(msg['alive_players'])}")
            print(f"You have {msg['timeout']}s to discuss. Type a line and press Enter to chat.")
            state["prompt_type"] = "chat"
        elif msg["name"] == "NIGHT":
            print("The town sleeps. If you have a night action, you'll be prompted for it.")
            state["prompt_type"] = None
        elif msg["name"] == "VOTING":
            print("Voting is open -- wait for your prompt below.")
        # Same override as the curses client: ghosts always land on the
        # separate ghost-chat channel regardless of what phase-specific
        # prompt_type was just set above -- this line does nothing at
        # all for anyone with is_ghost still False.
        if state.get("is_ghost"):
            state["prompt_type"] = "ghost_chat"
    elif t == "death_announcement":
        if msg["player"]:
            print(f"\n{msg['player']} was found dead this morning. They were... {msg['role']}!")
        else:
            print("\nNo one died last night.")
        _print_graveyard_simple(msg.get("graveyard"))
    elif t == "eliminated":
        state["is_ghost"] = True
        print(f"\n{msg['text']}\n")
        print("You can now chat with other eliminated players -- the living can't see it.")
        state["prompt_type"] = "ghost_chat"
    elif t == "chat":
        print(f"[{msg['sender']}]: {msg['text']}")
    elif t == "ghost_chat":
        print(f"[Ghost] {msg['sender']}: {msg['text']}")
    elif t == "private_result":
        print(f"\n[PRIVATE RESULT] {msg['text']}\n")
    elif t == "info":
        print(f"[INFO] {msg['text']}")
    elif t == "prompt":
        state["prompt_type"] = msg["prompt_type"]
        options = msg.get("options", [])
        print(f"\n({msg.get('timeout')}s) Options: {', '.join(options)}")
        if msg["prompt_type"] == "sabotage_decision":
            print("Type a player name to sabotage their vote tomorrow, or 'skip'.")
        elif msg["prompt_type"] == "night_action":
            print(f"Enter your {msg.get('action', 'act')} target:")
        elif msg["prompt_type"] == "vote":
            print("Enter your vote (a player name, or 'abstain'):")
    elif t == "vote_results":
        print("\n--- VOTE RESULTS ---")
        for name, count in msg["tally"].items():
            print(f"  {name}: {count} vote(s)")
        if msg["eliminated"]:
            print(f"{msg['eliminated']} has been eliminated. They were {msg['eliminated_role']}!")
        else:
            print("No majority reached -- no one was eliminated.")
        _print_graveyard_simple(msg.get("graveyard"))
    elif t == "state_snapshot":
        print(f"\n[Reconnected] Round {msg['round']}, Phase {msg['phase']}, "
              f"Role: {msg['role']}, You are: {'ALIVE' if msg['you_alive'] else 'dead'}")
        _print_graveyard_simple(msg.get("graveyard"))
        if not msg["you_alive"]:
            state["is_ghost"] = True
            state["prompt_type"] = "ghost_chat"
    elif t == "game_over":
        state["game_ended"] = True
        print("\n" + "#" * 50)
        print(f"GAME OVER -- {msg['winner']} WIN! ({msg.get('rounds_played', '?')} rounds played)")
        entries = msg.get("elimination_log") or []
        if entries:
            print("\nMatch summary:")
            for e in entries:
                print(f"  Round {e['round']} [{e['phase']}]: {e['player']} -- {e['role']}")
        print("\nFinal roles:")
        for r in msg["reveal"]:
            status = "alive" if r["alive"] else "dead"
            print(f"  {r['name']:20s} {r['role']:15s} ({status})")
        print("#" * 50)
        print("\nPress Enter to close.")
        # Same fix as the curses client: nothing previously set an exit
        # condition here, so this loop (and the process) just ran
        # forever after the game ended until the user manually Ctrl+C'd.
        state["prompt_type"] = "exit"

    elif t == "play_again_prompt":
        timeout = msg.get("timeout", 20)
        state["prompt_type"] = "play_again"
        print(f"\n({timeout}s) Play again? (yes/no)")


def _send_current_simple(sock, state, line):
    prompt_type = state.get("prompt_type")
    try:
        if prompt_type == "chat":
            protocol.send_json(sock, {"type": "chat", "text": line})
            print(f"[You]: {line}")  # server no longer echoes to sender
        elif prompt_type == "vote":
            protocol.send_json(sock, {"type": "vote", "target": line})
        elif prompt_type == "night_action":
            protocol.send_json(sock, {"type": "night_action", "target": line})
        elif prompt_type == "sabotage_decision":
            protocol.send_json(sock, {"type": "sabotage_decision", "target": line})
        elif prompt_type == "play_again":
            protocol.send_json(sock, {"type": "play_again", "target": line})
        elif prompt_type == "ghost_chat":
            protocol.send_json(sock, {"type": "ghost_chat", "text": line})
            print(f"[Ghost] You: {line}")
        else:
            print("[INFO] Nothing is expecting input right now.")
    except OSError:
        print("[ERROR] Could not send -- connection may be lost.")


# ======================================================================
# Entry point
# ======================================================================

def main():
    parser = argparse.ArgumentParser(description="Terminal Mafia -- player client")
    parser.add_argument("--host", default="127.0.0.1", help="Server address")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--name", required=True, help="Your player name (also used to reconnect)")
    parser.add_argument("--no-curses", action="store_true",
                         help="Force the simple line-based UI even if curses is available")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect((args.host, args.port))
    except (ConnectionRefusedError, OSError) as e:
        print(f"Could not connect to {args.host}:{args.port} -- {e}")
        sys.exit(1)

    protocol.send_json(sock, {"name": args.name, "token": _load_saved_token(args.name)})

    try:
        if _HAS_CURSES and not args.no_curses:
            _run_curses_client(sock, args.name)
            # By the time this line runs, curses.wrapper has already
            # called endwin() and restored the terminal to normal mode
            # -- this print is plain confirmation of that for the user,
            # since a screen that was previously stuck could otherwise
            # leave them unsure whether it's actually back to normal.
            print("Terminal restored. Goodbye!")
        else:
            _run_simple_client(sock, args.name)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()


if __name__ == "__main__":
    main()
