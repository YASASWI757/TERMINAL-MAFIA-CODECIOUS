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

from mafia import protocol

try:
    import curses
    _HAS_CURSES = True
except ImportError:
    _HAS_CURSES = False


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

    # ---- log ----

    def _log(self, text):
        for line in str(text).split("\n"):
            self.log_lines.append(line)
        if len(self.log_lines) > 1000:  # cap memory for a very long match
            self.log_lines = self.log_lines[-1000:]

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
            self._log("[Connection lost -- the server may have ended, or your network "
                      "dropped. Re-run this command with the same --name to reconnect.]")

        elif t == "joined":
            self._log(f"[SERVER] {msg['text']}")

        elif t == "error":
            self._log(f"[ERROR] {msg['text']}")

        elif t == "role_assigned":
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

        elif t == "death_announcement":
            if msg["player"]:
                self._play_death_animation()
                self._log(f"{msg['player']} was found dead this morning. They were... {msg['role']}!")
            else:
                self._log("No one died last night.")

        elif t == "eliminated":
            self._log(msg["text"])
            self.prompt_type = None
            self.prompt_hint = "(nothing expected -- spectating)"
            self._clear_countdown()

        elif t == "chat":
            self._log(f"[{msg['sender']}]: {msg['text']}")

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

        elif t == "state_snapshot":
            self._log(f"[Reconnected] Round {msg['round']}, Phase {msg['phase']}, "
                      f"Role: {msg['role']}, You are: {'ALIVE' if msg['you_alive'] else 'dead'}")

        elif t == "game_over":
            self._clear_countdown()
            self.prompt_type = None
            self.prompt_hint = "(game over)"
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

    # ---- drawing ----

    def _draw(self):
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
    state = {"prompt_type": None}

    def _listen():
        try:
            for msg in protocol.recv_lines(sock):
                _handle_message_simple(msg, state)
        except (ConnectionError, OSError):
            print("\n[Connection lost -- the server may have ended, or your network "
                  "dropped. Re-run this command with the same --name to reconnect.]")

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
        line = line.strip()
        if not line:
            continue
        _send_current_simple(sock, state, line)


def _handle_message_simple(msg, state):
    t = msg.get("type")
    if t == "joined":
        print(f"[SERVER] {msg['text']}")
    elif t == "error":
        print(f"[ERROR] {msg['text']}")
    elif t == "role_assigned":
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
    elif t == "death_announcement":
        if msg["player"]:
            print(f"\n{msg['player']} was found dead this morning. They were... {msg['role']}!")
        else:
            print("\nNo one died last night.")
    elif t == "eliminated":
        print(f"\n{msg['text']}\n")
        state["prompt_type"] = None
    elif t == "chat":
        print(f"[{msg['sender']}]: {msg['text']}")
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
    elif t == "state_snapshot":
        print(f"\n[Reconnected] Round {msg['round']}, Phase {msg['phase']}, "
              f"Role: {msg['role']}, You are: {'ALIVE' if msg['you_alive'] else 'dead'}")
    elif t == "game_over":
        state["prompt_type"] = None
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

    protocol.send_json(sock, {"name": args.name})

    try:
        if _HAS_CURSES and not args.no_curses:
            _run_curses_client(sock, args.name)
        else:
            _run_simple_client(sock, args.name)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()


if __name__ == "__main__":
    main()
