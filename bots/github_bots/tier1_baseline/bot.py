"""
Fullhouse Hackathon — equity-driven hero bot (self-contained).

This is the tournament submission. The sandbox loads exactly ONE file, `bot.py`,
by path, with no `mybot` package alongside it — so everything is inlined here:

  * a thin eval7 hand-eval layer (string cards in, int score out),
  * a deadline-bounded Monte-Carlo equity engine (hero vs N random opponents),
  * a static 169-entry preflop policy driven by the Chen starting-hand score
    (NO Monte Carlo preflop — instant and deterministic), and
  * an EV / pot-odds postflop policy that haircuts the (optimistic) vs-random
    equity before comparing it to the price.

Allowed libs only: `eval7` + Python stdlib (`random`, `time`, `itertools`).
No file I/O, no banned imports, single-threaded. Hard limits: 2 s / decision,
0.5 CPU, 768 MB — we spend well under all three.

Decision contract: `decide(state) -> {"action": ...}`. For a raise, `"amount"`
is the TOTAL to raise to; the engine snaps sub-minimum raises up to
`min_raise_to` and converts over-stack raises to all-in. A crash silently
auto-folds, so `decide()` wraps everything in a defensive try/except that takes
the free check when available and folds otherwise.

Known trap (see repo memory): vs-random equity OVERESTIMATES hero equity because
real opponents fold trash, so a villain still in the pot holds a stronger-than-
random hand. We correct for it with a per-opponent haircut before trusting the
number against pot odds.
"""

from __future__ import annotations

import random
import time

import eval7

BOT_NAME = "Equity-EV"
BOT_AVATAR = "robot_1"

# --------------------------------------------------------------------------- #
# Card primitives (inlined from mybot.hand_eval)
# --------------------------------------------------------------------------- #
RANKS = "23456789TJQKA"
SUITS = "shdc"
FULL_DECK = [r + s for r in RANKS for s in SUITS]
RANK_VAL = {r: i + 2 for i, r in enumerate(RANKS)}  # 2..14

# Single shared RNG: drives both the MC sampler and the bounded bluff frequency.
_RNG = random.Random()

# Time budget for the postflop Monte-Carlo loop (wall clock). Leaves a huge
# margin under the 2 s/decision cap even at 0.5 CPU; the loop returns the
# estimate so far if it hits the deadline, so we always answer in time.
BUDGET_S = 0.30
POSTFLOP_ITERS = 8000  # ±~1.1% at 95%; deadline caps it for many-opponent boards


# --------------------------------------------------------------------------- #
# Monte-Carlo equity engine (inlined + specialised from mybot.equity)
# --------------------------------------------------------------------------- #
def _equity_vs_random(hole, board, n_opp, iters, deadline):
    """Hero equity in [0, 1] vs ``n_opp`` uniformly-random opponents.

    Completes the board each iteration and credits hero ``1`` for a sole win or
    ``1/k`` for a k-way chop at the top (tie = fractional pot, matching the
    published-equity convention). ``deadline`` (a ``time.monotonic()`` value)
    hard-stops the loop and returns the estimate so far. NOTE: vs-random is
    optimistic — the caller haircuts it before trusting it (see module docstring).
    """
    known = set(hole) | set(board)
    deck = [eval7.Card(c) for c in FULL_DECK if c not in known]
    hero_cards = [eval7.Card(c) for c in hole]
    board_cards = [eval7.Card(c) for c in board]
    need_board = 5 - len(board)
    draw = need_board + 2 * n_opp
    if draw > len(deck) or n_opp <= 0:
        return 0.5  # nonsensical request — neutral, never crash

    sample = _RNG.sample
    evaluate = eval7.evaluate
    monotonic = time.monotonic

    eq_sum = 0.0
    completed = 0
    for i in range(iters):
        # Check the clock every 1024 iters (monotonic() isn't free) and never on
        # i==0, so at least one iteration always completes.
        if deadline is not None and i and (i & 0x3FF) == 0 and monotonic() >= deadline:
            break
        drawn = sample(deck, draw)
        runout = board_cards + drawn[:need_board]
        rest = drawn[need_board:]
        hero_score = evaluate(hero_cards + runout)
        v_scores = [evaluate(rest[2 * v: 2 * v + 2] + runout) for v in range(n_opp)]
        best_v = max(v_scores)
        if hero_score > best_v:
            eq_sum += 1.0
        elif hero_score == best_v:
            k = 1 + sum(1 for s in v_scores if s == hero_score)
            eq_sum += 1.0 / k
        completed += 1

    return eq_sum / completed if completed else 0.5


# --------------------------------------------------------------------------- #
# Preflop: static Chen-score policy (no Monte Carlo)
# --------------------------------------------------------------------------- #
# Chen high-card points; pairs use 2x this (floored at 5). Ten-and-below are
# value/2, but J/Q/K/A get the canonical bumped scores.
_CHEN_HI = {14: 10.0, 13: 8.0, 12: 7.0, 11: 6.0}
for _v in range(2, 11):
    _CHEN_HI[_v] = _v / 2.0


def _chen(hole):
    """Bill Chen starting-hand score (≈ -1 for 72o up to 20 for AA)."""
    v1, v2 = RANK_VAL[hole[0][0]], RANK_VAL[hole[1][0]]
    hi, lo = (v1, v2) if v1 >= v2 else (v2, v1)
    suited = hole[0][1] == hole[1][1]

    if hi == lo:  # pocket pair
        return max(_CHEN_HI[hi] * 2.0, 5.0)

    score = _CHEN_HI[hi]
    if suited:
        score += 2.0
    gap = hi - lo - 1  # cards strictly between the two ranks
    if gap == 1:
        score -= 1.0
    elif gap == 2:
        score -= 2.0
    elif gap == 3:
        score -= 4.0
    elif gap >= 4:
        score -= 5.0
    # Straight bonus: 0/1 gap connectors, both below Q.
    if gap <= 1 and hi <= 11:
        score += 1.0
    return score


# --------------------------------------------------------------------------- #
# Table-reading helpers (position, big blind, live opponents)
# --------------------------------------------------------------------------- #
def _button_seat(state, n):
    """Derive the dealer (button) seat from the blind markers in this hand's
    action_log. Heads-up: dealer == small blind; otherwise SB == dealer+1."""
    sb = bb = None
    for a in state.get("action_log", []):
        act = a.get("action")
        if act == "small_blind":
            sb = a.get("seat")
        elif act == "big_blind":
            bb = a.get("seat")
    if n <= 0:
        return None
    if n == 2:
        if sb is not None:
            return sb
        if bb is not None:
            return (bb - 1) % n
        return None
    if sb is not None:
        return (sb - 1) % n
    if bb is not None:
        return (bb - 2) % n
    return None


def _position(state, n):
    """Position in [0, 1]: 1.0 = on the button (latest, most info), 0.0 = the
    earliest seat. Falls back to 0.5 if blinds can't be read."""
    if n <= 1:
        return 0.5
    btn = _button_seat(state, n)
    if btn is None:
        return 0.5
    dist = (btn - state["seat_to_act"]) % n  # 0 when hero is the button
    return 1.0 - dist / (n - 1)


def _big_blind(state):
    """Real big-blind size from the action_log (engine constant could change)."""
    bb = 0
    for a in state.get("action_log", []):
        if a.get("action") == "big_blind":
            bb = max(bb, a.get("amount") or 0)
    return bb or 100


def _live_opponents(state):
    """Opponents still in the hand (not folded), excluding hero."""
    seat = state["seat_to_act"]
    cnt = 0
    for p in state.get("players", []):
        if p.get("seat") == seat or p.get("is_folded"):
            continue
        cnt += 1
    return max(cnt, 1)


def _raise(target, min_raise_to, all_in_to):
    """Clamp a raise-to total into [min_raise_to, all_in_to]; shove if it reaches
    our stack. The engine handles the min-snap / all-in conversion too, but we
    keep the action dict honest."""
    target = int(max(min_raise_to, min(int(round(target)), all_in_to)))
    if target >= all_in_to:
        return {"action": "all_in"}
    return {"action": "raise", "amount": target}


# --------------------------------------------------------------------------- #
# Preflop policy
# --------------------------------------------------------------------------- #
def _preflop(state, hole):
    chen = _chen(hole)
    n = len(state.get("players", [])) or 2
    pos = _position(state, n)
    bb = _big_blind(state)
    n_opp = _live_opponents(state)

    pot = max(1, state.get("pot", 0))
    owed = state.get("amount_owed", 0)
    can_check = state.get("can_check", owed == 0)
    current_bet = state.get("current_bet", bb)
    min_raise_to = state.get("min_raise_to", current_bet + bb)
    my_bet = state.get("your_bet_this_street", 0)
    my_stack = state.get("your_stack", 0)
    all_in_to = my_stack + my_bet
    eff_bb = all_in_to / bb if bb else 100.0

    # --- Short-stack push/fold (effective <= ~12bb) -----------------------
    # The one regime where shove/fold is the equilibrium. Widen as the stack
    # shrinks, tighten with more opponents yet to dodge.
    if eff_bb <= 12:
        shove_th = 6.5 + 1.5 * (n_opp - 1) - 0.4 * max(0.0, 10.0 - eff_bb)
        if chen >= shove_th:
            return _raise(all_in_to, min_raise_to, all_in_to)  # jam
        if can_check:
            return {"action": "check"}
        return {"action": "fold"}

    raised = current_bet > bb  # someone has put in a real raise beyond the BB

    # --- Unopened (or limped) pot ----------------------------------------
    if not raised:
        open_th = 9.5 - 4.0 * pos  # tight UTG (~9.5) -> wide button (~5.5)
        if chen >= open_th:
            return _raise(3 * bb, min_raise_to, all_in_to)  # ~3bb open
        if can_check:
            return {"action": "check"}  # BB option / free flop
        # SB completing or over limps: cheap speculative calls only.
        if chen >= open_th - 2.5 and owed <= bb:
            return {"action": "call"}
        return {"action": "fold"}

    # --- Facing a raise ---------------------------------------------------
    if chen >= 12.0:  # QQ+ / AKs / JJ — 3-bet for value to ~3x
        return _raise(3 * current_bet, min_raise_to, all_in_to)
    if chen >= 10.0:  # AK / TT / strong broadways — call (snap off shoves too)
        return {"action": "call"}
    pot_odds = owed / (pot + owed) if (pot + owed) > 0 else 1.0
    call_th = 9.0 - 2.5 * pos
    if chen >= call_th and pot_odds <= 0.40:  # playable + a fair price
        return {"action": "call"}
    if can_check:
        return {"action": "check"}
    return {"action": "fold"}


# --------------------------------------------------------------------------- #
# Postflop policy (equity / pot-odds, vs-random + haircut)
# --------------------------------------------------------------------------- #
def _postflop(state, hole, board, deadline):
    n_opp = _live_opponents(state)
    eq = _equity_vs_random(hole, board, n_opp, POSTFLOP_ITERS, deadline)

    pot = max(1, state.get("pot", 0))
    owed = state.get("amount_owed", 0)
    can_check = state.get("can_check", owed == 0)
    current_bet = state.get("current_bet", 0)
    min_raise_to = state.get("min_raise_to", current_bet + 100)
    my_bet = state.get("your_bet_this_street", 0)
    my_stack = state.get("your_stack", 0)
    all_in_to = my_stack + my_bet
    s_idx = {"flop": 0, "turn": 1, "river": 2}.get(state.get("street"), 0)

    # Correct the vs-random overestimate. A small base haircut covers "an
    # opponent still in the pot beats random"; when we're FACING a bet we add a
    # much larger penalty — a bettor's range is condensed and stronger, more so
    # on later streets and for bigger bets (a pot-sized river bet is rarely a
    # bluff vs these fields). Without this, vs-random equity calls off bottom
    # pair to a pot bet. Checked-to spots keep only the small base haircut.
    base_hc = min(0.15, 0.03 * n_opp)
    if can_check:
        eq_adj = eq - base_hc
    else:
        bet_frac = owed / max(1, pot - owed)  # bet size relative to pot-before
        facing_hc = (0.06 + 0.05 * s_idx) * min(bet_frac, 2.0)
        eq_adj = eq - base_hc - facing_hc

    # --- Checked to us: value-bet / semi-bluff / pot control --------------
    if can_check:
        if eq_adj >= 0.80:  # monsters — bet big / overbet to build the pot
            return _raise(current_bet + round(pot * 0.9), min_raise_to, all_in_to)
        if eq_adj >= 0.56:  # made hand worth value — ~2/3 pot
            return _raise(current_bet + round(pot * 0.6), min_raise_to, all_in_to)
        # Bounded heads-up semi-bluff with a hand that still has real equity.
        if n_opp == 1 and 0.42 <= eq_adj < 0.56 and _RNG.random() < 0.33:
            return _raise(current_bet + round(pot * 0.5), min_raise_to, all_in_to)
        return {"action": "check"}

    # --- Facing a bet: EV vs pot odds ------------------------------------
    pot_odds = owed / (pot + owed) if (pot + owed) > 0 else 1.0
    if eq_adj >= 0.68:  # strong — raise for value (bigger when crushing)
        frac = 0.9 if eq_adj >= 0.82 else 0.6
        return _raise(current_bet + round((pot + owed) * frac), min_raise_to, all_in_to)
    if eq_adj >= pot_odds + 0.02 * n_opp:  # price is right (incl. draws) — call
        return {"action": "call"}
    # Occasional heads-up semi-bluff raise with a live drawing hand.
    if n_opp == 1 and 0.45 <= eq_adj < 0.68 and pot_odds < 0.5 and _RNG.random() < 0.15:
        return _raise(current_bet + round((pot + owed) * 0.8), min_raise_to, all_in_to)
    return {"action": "fold"}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _decide(state):
    deadline = time.monotonic() + BUDGET_S
    hole = state["your_cards"]
    board = state.get("community_cards") or []
    preflop = state.get("street", "preflop") == "preflop" or len(board) < 3
    if preflop:
        return _preflop(state, hole)
    return _postflop(state, hole, board, deadline)


def decide(game_state: dict) -> dict:
    """Tournament entry point. Defensive wrapper: on ANY unexpected error take
    the free check if available, otherwise fold — never propagate (an exception
    auto-folds anyway, but checking when free is strictly better)."""
    try:
        return _decide(game_state)
    except Exception:  # pragma: no cover - safety net
        if game_state.get("can_check") or game_state.get("amount_owed", 0) == 0:
            return {"action": "check"}
        return {"action": "fold"}
