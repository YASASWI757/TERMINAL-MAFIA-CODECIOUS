# Terminal Mafia

A local-hosted, multiplayer, terminal-based social deduction game in the
spirit of Mafia / Among Us — built for the **Terminal Mafia** hackathon
track (36-hour build, "Build. Break. Defend.").

Zero external dependencies. Runs on stock Python 3.9+, standard library
only (`socket`, `threading`, `queue`, `json`, `dataclasses`, `enum`).

---

## Quick start

### Option A — same machine, multiple terminal windows

```bash
# Terminal 1: start the server, ask it to fill the rest of the lobby with 2 bots
python run_server.py --port 5555 --bots 2

# Terminal 2, 3, 4...: one human client per terminal
python run_client.py --host 127.0.0.1 --port 5555 --name Alice
python run_client.py --host 127.0.0.1 --port 5555 --name Bob
```

Once enough humans have joined (2 humans + 2 bots = 4, meeting the
minimum), press **ENTER** in the server terminal to start the match.

### Option B — LAN / hotspot, multiple laptops

```bash
# On the host machine:
python run_server.py --host 0.0.0.0 --port 5555 --bots 1

# Find the host's LAN IP (ip addr / ifconfig / ipconfig), then on each
# other laptop, connected to the same network or hotspot:
python run_client.py --host <HOST_LAN_IP> --port 5555 --name Charlie
```

### Option C — headless smoke test (no humans needed)

```bash
python run_server.py --bots 4 --auto-start
```

Fills the lobby entirely with bots and plays a full game to completion
in one terminal — useful for a quick sanity check or for watching how
a full game plays out before your first live demo.

### Reconnecting

If a client's connection drops mid-game, just re-run the exact same
`run_client.py` command with the same `--name`, **from the same
folder**. The server re-attaches you to your existing role, alive/dead
status, and current game phase — and if you were mid-vote or
mid-night-action, it re-sends you that exact prompt too. (Reconnect is
protected by a small local token file the client saves on first join —
see "Reconnect security" below for why, and why the folder matters.)

---

## Architecture

**One server, N clients**, talking newline-delimited JSON over TCP
(see `mafia/protocol.py`). The server is the single source of truth
for all game state — a client is only ever sent the information its
role is allowed to see, so hidden roles are enforced by the network
boundary itself, not by client-side trust.

```
mafia/
  protocol.py     wire format (JSON-lines over TCP)
  roles.py        role/alignment definitions + role-pool assignment
  player.py        Player data model
  game_state.py    shared mutable state for one match
  night.py         night-phase resolution (pure logic, no I/O)
  voting.py        vote tally, tie rule, win condition (pure logic, no I/O)
  bot_logic.py     rule-based AI bot decisions (discussion/vote/night actions)
  server.py        networking + the full game loop
run_server.py       server CLI entry point
run_client.py       human terminal client
tests/
  test_engine.py                unit tests for roles/night/voting (no network)
  test_integration_headless.py  full networked game, played start-to-finish
```

The rules engine (`night.py`, `voting.py`, `roles.py`) is deliberately
free of any networking or I/O, so it can be tested in complete
isolation — see **Testing** below.

### Concurrency model

- One reader thread per connected human socket, continuously parsing
  incoming messages. Chat is broadcast immediately; everything else
  (vote / night action / sabotage decision) is dropped onto that
  player's own inbox queue.
- The main game-loop thread drives phases one at a time. To collect an
  action from a player it calls a single function, `_collect_action`,
  which waits on that player's inbox with a bounded timeout. Every
  phase — night actions, voting, the Saboteur's decision — goes
  through this **one** function, which is what makes disconnect
  handling, timeouts, and invalid input all safe by construction
  instead of being four separately-written (and separately-buggy)
  code paths.
- Bots have no socket. The same `_collect_action` call spawns a
  short-lived thread that sleeps a small random delay (so bots don't
  act instantly/robotically) and pushes its decision onto the same
  inbox a human's reader thread would have used — the collection code
  never needs to know whether it's waiting on a human or a bot.

---

## Roles

| Role | Alignment | Power |
|---|---|---|
| **Villager** | Villager | None — the town's numbers and voice. |
| **Mafia** | Mafia | Each night, chooses a player to eliminate. |
| **Detective** | Villager | Each night, investigates one player and learns their alignment. |
| **Doctor** | Villager | Each night, protects one player (including themself) from the Mafia's kill. No cooldown. |
| **Saboteur** *(human-only)* | Villager | Once per game, silently nullifies one player's vote for the next tally. The target is never told. |
| **Double Agent** | **Mafia** (secretly) | Appears as an innocent Villager to Detective investigations, but counts as Mafia for the win condition. Knows who the real Mafia is. If the real Mafia dies, the Double Agent takes over the night kill so the Mafia team is never permanently silenced. |

**Role pool by player count:**

| Players | Roles included |
|---|---|
| 4–5 | Mafia, Detective, Saboteur *(if ≥1 human)*, rest Villager |
| 6–7 | + Doctor |
| 8+ | + Double Agent |

The Saboteur is always assigned to a human player, never a bot — it's
a deception mechanic that only makes sense with a real person behind
it, and it's enforced in `roles.py`, not just by convention.

### Night resolution order

1. Detective investigates (read-only, resolves first)
2. Mafia chooses a kill target
3. Doctor chooses a protect target
4. Saboteur arms next round's vote-nullify
5. Resolve: if the Doctor's target matches the Mafia's kill target, the
   kill is cancelled. Otherwise the target dies.

### Voting

- Each alive player votes for one name, or abstains.
- A vote from whoever the Saboteur targeted is silently discarded
  before tallying.
- **A tie for first place means no one is eliminated that round** —
  chosen deliberately over a random tiebreak or runoff, since it
  removes an entire category of "what happens on a tie?" ambiguity.
- Win condition is checked after *every* elimination (night or day):
  Mafia-aligned count reaching 0 → Villagers win. Mafia-aligned count
  ≥ everyone else → Mafia win.

---

## Disconnect / invalid input handling

Every action — night action, vote, sabotage decision — goes through
one function (`GameServer._collect_action`) that treats "disconnected,"
"connected but silent," and "sent something invalid" uniformly:

- **Timeout with no valid input** → treated as no action / abstain,
  the round moves on. The game is never blocked waiting on one player.
- **Player stays in the game as alive-but-idle** — never auto-kicked
  or auto-eliminated. This keeps win-condition math and role state
  simple, and means a flaky wifi drop mid-round doesn't corrupt the
  match.
- **Reconnect** — a rejoin with the same name re-attaches the existing
  Player object (role, alive status, everything) to the new socket and
  sends a state snapshot to resync the client's display.
- **Invalid input** (typo'd name, etc.) → the player is told and can
  retry within whatever time remains; it never crashes the server.
- **Being disconnected grants no immunity** — a disconnected player
  can still be killed or voted out; otherwise "disconnect to survive"
  would be a free exploit.

---

## AI bots

Bots are rule-based, not an LLM call — deliberately, for a hackathon
demo:

- Zero risk of an API failure or latency spike crashing a live match
- Fully deterministic and unit-testable ("Mafia bots never vote a
  teammate" is a one-line assertion, not a hope)
- The whole game runs fully offline, no external dependency at all

Bots weight their discussion targets and votes toward whoever has been
named most in the discussion log (`accusation_count` in
`game_state.py`), so they "follow the room" instead of voting
uniformly at random, and Mafia-aligned bots (including a Detective
bot's occasional confident read) never implicate a teammate. Discussion
lines fire after a randomized delay per bot so they don't all speak
instantly/simultaneously.

---

## Testing

```bash
# Pure rules-engine unit tests (roles, night resolution, voting, win condition)
python -m unittest tests.test_engine -v

# Full networked game, played start-to-finish over real sockets
python tests/test_integration_headless.py

# Targeted checks for the post-playtest fixes (case-insensitive
# matching, elimination_log, personal eliminated/can't-vote notices,
# and a timing check proving the night phase runs concurrently)
python tests/verify_fixes.py

# Confirms the sender never receives their own chat message echoed back
python tests/verify_no_echo.py

# PTY-based smoke test for the curses client: runs it as a real
# subprocess attached to a pseudo-terminal against a live server,
# feeds it keystrokes across night/discussion/voting phases, and
# checks it starts, draws a real screen, survives input, and shuts
# down cleanly with no traceback
python tests/verify_curses_client.py

# Plays a real match to completion and confirms the client actually
# exits on its own after "Press Enter to close" -- the fix for the
# curses loop spinning forever and never restoring the terminal
python tests/verify_clean_exit.py

# Same exit-on-game-over check for the --no-curses fallback client
python tests/verify_simple_client_exit.py

# Hijack blocked (wrong/no token rejected, correct token accepted) and
# reconnect-restores-the-active-prompt (with a real recomputed
# remaining timeout, not a reset window)
python tests/verify_reconnect.py

# graveyard field present in broadcasts once there's an elimination,
# and the Double Agent's reveal text is correctly formatted
python tests/verify_graveyard_and_double_agent.py

# Play-again "yes" path: a human who stays gets a genuinely fresh
# second match (new role_assigned, reshuffled roles, previously-dead
# bots correctly resurrected), not stuck or disconnected
python tests/verify_play_again.py
```

All of the above are included in this repo and pass as of this commit.
`test_engine.py` covers the rules in isolation (23 tests: role-pool
assignment constraints, Saboteur-always-human, Double Agent's
true-vs-apparent alignment, Doctor self-save, sabotage nullification,
tie handling, both win conditions). `test_integration_headless.py`
spins up a real `GameServer`, connects a real scripted socket client
alongside bots, and asserts the match reaches `game_over` with a
correct final reveal — proving the networking/threading/timeout
plumbing works end-to-end, not just the logic in isolation.

---

## Round timers -- customizable at lobby creation

Discussion/vote/night-action timers no longer have to be edited in
`constants.py` to change. The host sets them when starting the server:

```bash
# Skip the prompt entirely by passing them on the command line:
python run_server.py --discussion-timeout 60 --vote-timeout 20 --night-timeout 20

# Or just leave them out -- you'll be asked interactively before the
# lobby opens (press Enter on any of them to accept the default shown):
python run_server.py
#   Configure round timers for this match (press Enter to accept the default):
#     Discussion phase (seconds) [90]:
#     Voting phase (seconds) [30]:
#     Night action phase (seconds) [25]:
```

`--auto-start` (headless mode) never prompts -- it silently uses
`constants.py`'s defaults for anything not passed explicitly on the
command line, so scripted/CI usage is unaffected. Under the hood,
`GameServer` now takes these as constructor parameters (falling back
to the constants only when not given), rather than the timers being
fixed module-level globals -- this is also what makes per-server
configuration possible in the first place.

## Play again

After a match ends, every connected human is asked "Play again?
(yes/no)" with its own countdown. Anyone who says no, doesn't answer
in time, or is disconnected gets replaced by a freshly-named bot for
the next match -- the total player count stays exactly the same as
the match that just ended, so `min_players` stays satisfied
automatically without any extra bookkeeping. Bots always continue
(reset to a clean state, roles reshuffled) since they have no opinion
to ask. If nobody wants to continue, the server ends normally -- the
existing "connection dropped, press Enter to close" flow handles that
case without needing anything new.

One real bug found and fixed while building this: declining play-again
closes that player's connection server-side so their client gets a
clean "connection closed" exit rather than sitting there indefinitely.
The first attempt called `socket.close()` directly, but that player's
own reader thread is still concurrently blocked inside `recv()` on the
exact same socket at that moment -- a plain `close()` from a different
thread doesn't reliably wake that up or guarantee the peer sees a
clean EOF. Fixed with `socket.shutdown(SHUT_RDWR)` before `close()`,
the standard fix for closing a socket out from under a thread that's
concurrently reading it. Caught by a direct raw-socket test that
bypassed the curses/PTY layer specifically to isolate this from
higher-level noise -- confirmed the peer saw a hang before the fix and
a clean EOF after.

## Lobby screen

Before the match starts, connecting players see a proper title screen
instead of a bare log: a hand-built block-letter "TERMINAL MAFIA"
banner, a tagline, a double-line separator, a bordered frame, and a
live "Players connected: X / Y" list that updates as people join --
something the game genuinely didn't have before (connected players
previously had zero visibility into who else had joined pre-game; the
server only ever printed that to its own console). Curses-only, same
scope as the other visual touches; the plain-text fallback client
keeps its existing simple "Connected as X. Waiting..." line.

## Reconnect security

Reconnecting used to be matched by name alone -- which meant anyone
who knew (or guessed) a disconnected player's name could reconnect
*as* them and immediately inherit their role, private info, and vote.
Fixed with a per-player secret token: issued once on first join, saved
locally by the client (a small `.mafia_rejoin_<name>.token` file next
to wherever you run it from), and required to match on any later
reconnect. A mismatch is rejected with a clear error instead of
silently taking over the slot. Run the client from the same folder
each time so it can find its saved token.

Separately, a reconnect now also restores whatever you should
currently be seeing -- if you drop mid-vote or mid-night-action and
reconnect within the timeout window, you get the exact prompt back
(with a freshly recomputed remaining time, not a reset full window)
instead of being stuck with nothing on screen despite still being able
to act in time.

## Graveyard

A compact, running "Graveyard: Alice(VILLAGER), Bob(DETECTIVE)" line
is shown after every elimination and on reconnect, so you don't have
to scroll back through the log to remember who's already out and what
they were.

## Death animation

Doctor saves are handled correctly here: the animation only fires
when `death_announcement`'s `player` field is non-null, and a
successful save (like "no Mafia target chosen") sends `player: null`
-- so a protected player never triggers it, and the client just logs
"No one died last night." (Confirmed by re-reading the single call
site rather than assuming -- there's exactly one, and it's already
correctly guarded.)

When a night kill happens, the curses client plays a short (~1.5s)
in-place ASCII beat before the "X was found dead... they were ROLE!"
line -- each frame redraws over the last (not scrolled), matching how
a real terminal-video plays. It's five hand-built, hard-coded frames
(no image/video conversion, no external asset, no dependency) -- a
small script generated them once with guaranteed column alignment
(every line padded to the same width, gun/victim positions fixed
across frames, only the bullet position and victim pose change), and
the resulting strings are baked into `run_client.py` as
`DEATH_ANIMATION_FRAMES`. Curses-only, same as the live countdown --
the plain-text fallback client keeps its original single-line
announcement.

Preview (frame 1 of 5 -- the bullet travels further right across
frames 2-3, then frame 4 is the impact/flash, frame 5 is the victim
down):
```
 .--.                              O
( oo )>  *                        /|\
 `--'                             / \
```

## Fixes -- latest round (reconnect security, graveyard, double agent, bot variety)

- **Reconnect hijack.** See "Reconnect security" above.
- **Reconnect didn't restore the active prompt.** See "Reconnect
  security" above.
- **The plain-text fallback client (`--no-curses` / stock Windows
  Python) never exited after the game ended.** The curses client had
  already been fixed for this, but the fallback client's own input
  loop had the identical bug -- nothing set an exit condition on
  `game_over` or on the connection dropping, so it ran forever until
  manually Ctrl+C'd. Same fix applied there: "Press Enter to close" on
  both cases, and a "Goodbye!" confirmation once it actually exits.
- **The Double Agent's row in the final "reveal" table showed the raw
  `DOUBLE_AGENT` enum value** instead of the friendly "DOUBLE AGENT
  (secretly Mafia-aligned)" text used everywhere else (death
  announcements, the match summary). Fixed for consistency. (We
  couldn't find a structural reason a Double Agent would miss the
  game-over broadcast entirely -- the broadcast logic doesn't filter
  by role anywhere -- so if that still happens, it's worth re-testing
  now that the reconnect-prompt fix above is in, since a reconnect
  around when the game ended is a more likely explanation.)
- **More bot dialogue variety.** Expanded the discussion line banks
  roughly 3-4x (4→14 generic lines, 4→14 suspicion lines, 3→12 Mafia
  deflection lines) so bots repeat themselves far less over a longer
  match.

## Fixes -- earlier round (curses UI rewrite, chat double-echo, death animation, terminal not restoring)

- **Curses client wasn't exiting after the game ended.** The main
  loop's exit flag (`self.running`) was only ever set to `False` by
  Ctrl+C -- nothing set it on `game_over` or on the connection closing
  (which happens naturally once the server process exits after the
  match ends). So the loop just kept spinning forever, `run()` never
  returned, `curses.wrapper` never got to call `endwin()`, and the
  terminal was never handed back to normal mode -- which is what a
  screen full of stale/overlapping content after a match actually was:
  a terminal stuck in raw curses mode with nothing left updating it
  properly. Fixed with an explicit "Press Enter to close" prompt on
  both `game_over` and on the connection dropping: the player gets a
  moment to read the final summary, and pressing Enter is what now
  actually ends the loop, restores the terminal, and prints a plain
  "Terminal restored. Goodbye!" confirmation once it's back to normal.
  Verified with a dedicated PTY test (`verify_clean_exit.py`) that
  plays a real match to completion and asserts the process exits on
  its own after Enter, with no SIGTERM needed.

Found and fixed during hands-on playtesting:

- **Terminal UI rewritten around `curses`.** The earlier fix (a
  thread-safe `safe_print` that cleared and redrew the input line) was
  a mitigation, not a real fix -- it reduced corruption but couldn't
  eliminate it, because Python's `input()` hands control to GNU
  Readline, which keeps its own internal model of the screen; writing
  to stdout from a background thread while `input()` is active happens
  "behind Readline's back," so its next redraw is based on stale state.
  This showed up worst during the long discussion window, where typing
  a full sentence gave a countdown tick or an incoming chat line many
  chances to land mid-keystroke.

  The real fix: the client (`run_client.py`) now uses `curses` for a
  proper split-screen layout -- a scrolling message log on top, a
  pinned input line at the bottom, a live countdown status bar.
  Curses fully owns the terminal and redraws deterministically every
  frame from a single thread (the network listener thread only ever
  pushes parsed messages onto a queue; it never touches the screen
  directly), so there's no longer two independent things fighting over
  the same line. This also means the countdown can now be a genuine
  live per-second tick instead of the earlier checkpoint-only
  compromise, since a live tick can no longer corrupt anything.

  `curses` ships in the standard library on Linux and Mac, so this
  doesn't cost the "zero external dependencies" pitch there. Stock
  Windows Python doesn't include `curses`, so the client detects that
  at import time and falls back automatically to the previous
  line-based UI (fully playable, just without the split-screen layout
  and live countdown) -- installing `windows-curses` on Windows gets
  the full experience there too. `--no-curses` forces the fallback
  manually if ever needed for a demo machine.

- **Chat appeared twice for the sender.** Root cause: the server
  broadcast every chat message to *all* connected players, including
  whoever sent it -- so you'd see your own line once from your
  terminal's normal typing echo, and a second time when the server's
  broadcast came back to you. Fixed by excluding the sender from their
  own chat broadcast (`_handle_chat` now broadcasts to everyone
  *except* the player who sent it); the client now logs its own sent
  message locally instead, so it still shows exactly once.

## Fixes -- first round (timer/countdown, game-over summary, can't-vote notice, night-phase concurrency, case-insensitive matching)

- **Countdown timer.** Every timed prompt (discussion / vote / night
  action / sabotage decision) now shows a live countdown on the
  client, with reminders at the halfway point and in the final
  seconds. It's checkpoint-based (not a literal per-second redraw) --
  see the text-overlap fix below for why that's the deliberate,
  correct call here, not a shortcut.
- **Full game-over summary to everyone.** `game_over` now includes
  `rounds_played` and a round-by-round `elimination_log` (who died,
  which phase, their revealed role), broadcast to every connected
  player -- dead or alive -- not just a bare final role list.
- **Eliminated players are told, clearly, that they can't vote.** A
  player who's just died gets an unmistakable first-person notice
  distinct from the public death announcement ("You were eliminated
  -- you can no longer vote or act, but you'll keep seeing what
  happens"), and if the match continues, they get an explicit
  reminder at the start of the next vote instead of silence. They're
  also blocked from sending chat during discussion once dead (with an
  explanation), closing a related gap where eliminated players could
  otherwise keep talking as if still alive.
- **The "Doctor/killer" lag.** Root cause: Detective, Mafia, Doctor,
  and Saboteur were being prompted **sequentially** -- the Doctor's
  prompt didn't even appear until Detective *and* Mafia had both
  finished responding, so it could look hung or laggy for up to 2-3x
  `NIGHT_ACTION_TIMEOUT` before Doctor saw anything. None of these
  roles need to see another's choice before acting, so they're now
  all prompted and collected **concurrently** (same pattern already
  used for voting) -- the whole night phase now takes roughly one
  timeout window, not a sum of four. Verified via `tests/verify_fixes.py`,
  which times the phase directly.

  A second, related bug found while root-causing this: target names
  were matched with **exact-case string equality**, so typing `bob`
  for a player named `Bob` was silently rejected as "invalid input" --
  easy to hit live and easy to mistake for the game being broken.
  Fixed with case-insensitive, whitespace-trimmed matching everywhere
  a target/vote/sabotage choice is parsed.
- **Text overlap between input and output.** Root cause: the listener
  thread (printing incoming chat/server messages) and the main thread
  (blocked in `input()` reading what you're typing) were writing to
  the terminal with no coordination, so an incoming message could
  visually clobber a line you were mid-typing. Fixed with a
  thread-safe `safe_print()` used everywhere, which -- on Unix/Mac
  terminals with GNU readline (the default for `input()` there) --
  clears the current line, prints the incoming message, then redraws
  whatever you'd already typed so you can keep going without losing
  your place. This is also *why* the countdown is checkpoint-based
  rather than a per-second live tick: a tick fighting for the same
  line every second would reintroduce exactly this problem instead of
  fixing it. On Windows (no `readline` by default), it falls back to
  plain sequential printing -- the overlap risk is a bit higher there,
  but nothing crashes or corrupts input.

## Known design decisions worth knowing about (not bugs)

- **Tie vote → no elimination**, not a random tiebreak. Simplicity
  over drama, on purpose.
- **Disconnected players are alive-but-idle**, never auto-eliminated
  (see Disconnect handling above).
- **Doctor has no cooldown** and can protect themselves every night —
  standard Mafia balance; without self-protect, a suspected Doctor is
  a guaranteed kill.
- **At 4 players there's no Doctor**, so Mafia has strong early odds
  with no protective counter-play — this is expected game balance at
  the minimum player count, not an engine bug (confirmed by direct
  testing; a 4-player game strongly favors Mafia since there's no
  Doctor to counter the night kill).
- **If the sole Mafia player dies, a surviving Double Agent takes over
  the night kill** so the Mafia team is never permanently silenced.

---

## Deliverables checklist (per problem statement)

- [x] Supports 4+ players with hidden, role-based information
- [x] Structured game loop: role assignment → night/day phases →
      elimination → win condition
- [x] Fully playable via terminal input/output, no GUI
- [x] Graceful handling of disconnects and invalid input mid-game
- [x] Clear, unambiguous win condition for each side
- [x] AI bot players to fill a lobby (stretch goal)
- [x] Additional special roles beyond the basic two sides: Detective,
      Doctor, Saboteur, Double Agent (stretch goal)
- [ ] Spectator mode for eliminated players (not implemented — see
      "Possible next steps" below)
- [ ] Match history / replay (discussion log is already tracked in
      `game_state.discussion_log`, but no replay viewer yet)

## Possible next steps if time remains

- Spectator mode: eliminated players already stay connected (Option A
  disconnect policy keeps them "in" the server) — they just need a
  `phase`/`chat` broadcast subscription without action prompts.
- Match replay: `discussion_log` and vote tallies are already recorded
  per round; a replay viewer just needs to render what's already there.
