"""Balanced TAG benchmark villain (Tier-3 diagnostic — NOT a submission).

A solid, *balanced, non-adaptive* tight-aggressive bot. It does **no** opponent
modelling: its ranges and sizings are fixed and position-aware, seeded from
standard RFI / 3-bet / defence heuristics. It stands in for the "online GTO
non-exploitative opponent" we can't reach over the network — the point is a
villain that neither punts (so our Tier-2 exploits don't print free chips the
way they do vs the loose reference bots) nor is grossly exploitable itself.

Self-contained on purpose (the sandbox loads one file): it carries its own copy
of the eval7 equity loop + Chen score, like the hero. It is intentionally
*different* code from the hero so a hero-vs-this duel is a real out-of-sample
generalisation check, not a mirror match.

Design:
  * Preflop  — position-aware RFI by Chen score, polarised 3-bets, odds+equity
    BB defence, short-stack push/fold. Fixed frequencies, no adaptation.
  * Postflop — equity vs random (deadline-bounded MC) minus a *realistic* range
    haircut; value-bet / balanced semi-bluff at fixed sizings; call strictly on
    pot odds vs equity; give up when behind. Balanced bluff frequency so its
    bets aren't pure-exploitable in either direction.
"""

from __future__ import annotations

import random
import time

import eval7

BOT_NAME = "Balanced-TAG"
BOT_AVATAR = "robot_2"

RANKS = "23456789TJQKA"
SUITS = "shdc"
FULL_DECK = [r + s for r in RANKS for s in SUITS]
RANK_VAL = {r: i + 2 for i, r in enumerate(RANKS)}

_RNG = random.Random(0xBA1A)  # fixed seed => reproducible, still balanced
BUDGET_S = 0.25
ITERS = 6000


# --------------------------------------------------------------------------- #
# Equity vs random (inlined)
# --------------------------------------------------------------------------- #
def _equity_vs_random(hole, board, n_opp, iters, deadline):
    known = set(hole) | set(board)
    deck = [eval7.Card(c) for c in FULL_DECK if c not in known]
    hero_cards = [eval7.Card(c) for c in hole]
    board_cards = [eval7.Card(c) for c in board]
    need = 5 - len(board)
    draw = need + 2 * n_opp
    if draw > len(deck) or n_opp <= 0:
        return 0.5
    sample = _RNG.sample
    ev = eval7.evaluate
    mono = time.monotonic
    s = 0.0
    done = 0
    for i in range(iters):
        if deadline is not None and i and (i & 0x3FF) == 0 and mono() >= deadline:
            break
        d = sample(deck, draw)
        run = board_cards + d[:need]
        rest = d[need:]
        hs = ev(hero_cards + run)
        vs = [ev(rest[2 * v:2 * v + 2] + run) for v in range(n_opp)]
        bv = max(vs)
        if hs > bv:
            s += 1.0
        elif hs == bv:
            s += 1.0 / (1 + sum(1 for x in vs if x == hs))
        done += 1
    return s / done if done else 0.5


# --------------------------------------------------------------------------- #
# Chen score + table helpers
# --------------------------------------------------------------------------- #
_CHEN_HI = {14: 10.0, 13: 8.0, 12: 7.0, 11: 6.0}
for _v in range(2, 11):
    _CHEN_HI[_v] = _v / 2.0


def _chen(hole):
    v1, v2 = RANK_VAL[hole[0][0]], RANK_VAL[hole[1][0]]
    hi, lo = (v1, v2) if v1 >= v2 else (v2, v1)
    suited = hole[0][1] == hole[1][1]
    if hi == lo:
        return max(_CHEN_HI[hi] * 2.0, 5.0)
    sc = _CHEN_HI[hi]
    if suited:
        sc += 2.0
    gap = hi - lo - 1
    if gap == 1:
        sc -= 1.0
    elif gap == 2:
        sc -= 2.0
    elif gap == 3:
        sc -= 4.0
    elif gap >= 4:
        sc -= 5.0
    if gap <= 1 and hi <= 11:
        sc += 1.0
    return sc


def _button_seat(state, n):
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
    if n <= 1:
        return 0.5
    btn = _button_seat(state, n)
    if btn is None:
        return 0.5
    dist = (btn - state["seat_to_act"]) % n
    return 1.0 - dist / (n - 1)


def _big_blind(state):
    bb = 0
    for a in state.get("action_log", []):
        if a.get("action") == "big_blind":
            bb = max(bb, a.get("amount") or 0)
    return bb or 100


def _live_opponents(state):
    seat = state["seat_to_act"]
    cnt = 0
    for p in state.get("players", []):
        if p.get("seat") == seat or p.get("is_folded"):
            continue
        cnt += 1
    return max(cnt, 1)


def _raise(target, min_raise_to, all_in_to):
    target = int(max(min_raise_to, min(int(round(target)), all_in_to)))
    if target >= all_in_to:
        return {"action": "all_in"}
    return {"action": "raise", "amount": target}


# --------------------------------------------------------------------------- #
# Preflop — fixed position-aware ranges
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

    if eff_bb <= 12:
        shove_th = 7.0 + 1.5 * (n_opp - 1) - 0.35 * max(0.0, 10.0 - eff_bb)
        if chen >= shove_th:
            return _raise(all_in_to, min_raise_to, all_in_to)
        if can_check:
            return {"action": "check"}
        return {"action": "fold"}

    raised = current_bet > bb
    if not raised:
        open_th = 9.5 - 4.0 * pos  # UTG ~9.5 -> button ~5.5
        if chen >= open_th:
            return _raise(2.5 * bb, min_raise_to, all_in_to)
        if can_check:
            return {"action": "check"}
        return {"action": "fold"}

    # facing a raise: polarised, balanced 3-bet + flat range
    if chen >= 13.0:                     # premium — 3-bet for value
        return _raise(3 * current_bet, min_raise_to, all_in_to)
    if chen >= 10.0:                     # strong — flat
        return {"action": "call"}
    pot_odds = owed / (pot + owed) if (pot + owed) > 0 else 1.0
    call_th = 8.5 - 2.0 * pos
    if chen >= call_th and pot_odds <= 0.33:
        return {"action": "call"}
    if can_check:
        return {"action": "check"}
    return {"action": "fold"}


# --------------------------------------------------------------------------- #
# Postflop — equity vs pot odds, balanced betting (no opponent model)
# --------------------------------------------------------------------------- #
def _postflop(state, hole, board, deadline):
    n_opp = _live_opponents(state)
    n = len(state.get("players", [])) or 2
    pos = _position(state, n)
    eq = _equity_vs_random(hole, board, n_opp, ITERS, deadline)

    pot = max(1, state.get("pot", 0))
    owed = state.get("amount_owed", 0)
    can_check = state.get("can_check", owed == 0)
    current_bet = state.get("current_bet", 0)
    min_raise_to = state.get("min_raise_to", current_bet + 100)
    my_bet = state.get("your_bet_this_street", 0)
    my_stack = state.get("your_stack", 0)
    all_in_to = my_stack + my_bet
    s_idx = {"flop": 0, "turn": 1, "river": 2}.get(state.get("street"), 0)

    # Realistic range haircut (opponents in the pot beat random; bettors more so).
    base_hc = min(0.12, 0.03 * n_opp)
    if can_check:
        eq_adj = eq - base_hc
    else:
        bet_frac = owed / max(1, pot - owed)
        eq_adj = eq - base_hc - (0.05 + 0.04 * s_idx) * min(bet_frac, 2.0)

    if can_check:
        # Value bet strong hands; balanced semi-bluff at a fixed, modest freq.
        if eq_adj >= 0.78:
            return _raise(current_bet + round(pot * 0.75), min_raise_to, all_in_to)
        if eq_adj >= 0.55:
            return _raise(current_bet + round(pot * 0.6), min_raise_to, all_in_to)
        bluff_freq = 0.30 if pos > 0.5 else 0.18
        if 0.30 <= eq_adj < 0.55 and _RNG.random() < bluff_freq:
            return _raise(current_bet + round(pot * 0.5), min_raise_to, all_in_to)
        return {"action": "check"}

    pot_odds = owed / (pot + owed) if (pot + owed) > 0 else 1.0
    if eq_adj >= 0.70:
        frac = 0.8 if eq_adj >= 0.85 else 0.6
        return _raise(current_bet + round((pot + owed) * frac), min_raise_to, all_in_to)
    if eq_adj >= pot_odds + 0.015 * n_opp:
        return {"action": "call"}
    # Small, fixed-frequency balancing semi-bluff raise (keeps bets non-pure).
    if n_opp == 1 and 0.42 <= eq_adj < 0.70 and pot_odds < 0.45 and _RNG.random() < 0.10:
        return _raise(current_bet + round((pot + owed) * 0.8), min_raise_to, all_in_to)
    return {"action": "fold"}


def _decide(state):
    deadline = time.monotonic() + BUDGET_S
    hole = state["your_cards"]
    board = state.get("community_cards") or []
    if state.get("street", "preflop") == "preflop" or len(board) < 3:
        return _preflop(state, hole)
    return _postflop(state, hole, board, deadline)


def decide(game_state: dict) -> dict:
    try:
        return _decide(game_state)
    except Exception:
        if game_state.get("can_check") or game_state.get("amount_owed", 0) == 0:
            return {"action": "check"}
        return {"action": "fold"}
