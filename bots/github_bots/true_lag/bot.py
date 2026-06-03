"""Diverse-pool villain: TRUE LAG (loose-aggressive, thinking).

The reference field's only aggressor is a pure maniac (raises 70% at random, never
folds) — trivially trapped. A real LAG is the harder generalization test: it opens
wide and applies pressure (c-bets, barrels, 3-bet bluffs) BUT folds when it's beat
and value-raises when it's ahead. If the hero's added aggression (Stages B/C) is
over-cooked, a LAG punishes it (check-raises, floats, 3-bets light); a bot that
only trapped the maniac will bleed here. Self-contained, distinct code: rule-based
betting with eval7 hand categories + a simple draw detector and randomised
frequencies. Not a hero mirror (no Monte-Carlo, no opponent model).
"""
import random

import eval7

BOT_NAME = "True LAG"

_RANK = {r: i for i, r in enumerate("23456789TJQKA", start=2)}
_CATS = ("High Card", "Pair", "Two Pair", "Trips", "Straight",
         "Flush", "Full House", "Quads", "Straight Flush")


def _made(hole, board):
    """Return (category_index 0..8, is_overpair_or_tptk_plus)."""
    cards = [eval7.Card(c) for c in hole + board]
    ci = _CATS.index(eval7.handtype(eval7.evaluate(cards)))
    return ci


def _draws(hole, board):
    """(flush_draw, straight_draw) booleans using hole+board."""
    cards = hole + board
    suits = {}
    for c in cards:
        suits[c[1]] = suits.get(c[1], 0) + 1
    flush_draw = any(n == 4 for n in suits.values())
    ranks = sorted({_RANK[c[0]] for c in cards})
    if 14 in ranks:
        ranks = sorted(set(ranks) | {1})  # wheel
    straight_draw = False
    for lo in range(1, 11):
        window = [r for r in ranks if lo <= r < lo + 5]
        if len(window) == 4:
            straight_draw = True
            break
    return flush_draw, straight_draw


def _pf_tier(hole):
    """0 trash .. 4 premium."""
    r = sorted((_RANK[c[0]] for c in hole), reverse=True)
    suited = hole[0][1] == hole[1][1]
    gap = r[0] - r[1]
    if r[0] == r[1]:
        if r[0] >= 11:
            return 4          # JJ+
        if r[0] >= 8:
            return 3          # 88-TT
        return 2              # small pairs
    if r[0] == 14 and r[1] >= 12:
        return 4              # AK, AQ
    if r[0] >= 13 and r[1] >= 11:
        return 3              # KQ, KJ, AJ-ish
    if suited and gap <= 2 and r[1] >= 6:
        return 2              # suited connectors / gappers
    if r[0] >= 12 or (suited and r[0] >= 10):
        return 1              # weak broadway / suited high
    if suited and gap <= 1:
        return 1              # low suited connector
    return 0


def decide(state):
    owed = state["amount_owed"]
    pot = max(1, state["pot"])
    can_check = state["can_check"]
    hole = state["your_cards"]
    board = state.get("community_cards") or []
    stack = state["your_stack"]
    my_bet = state.get("your_bet_this_street", 0)
    min_raise_to = state.get("min_raise_to", 0)
    current_bet = state.get("current_bet", 0)
    bb = 100
    seat = state["seat_to_act"]
    n = len(state["players"])
    late = (seat / max(n - 1, 1)) > 0.5

    def raise_to(frac):
        return {"action": "raise",
                "amount": min(int(min_raise_to + frac * pot), stack + my_bet)}

    # ---- Preflop ---------------------------------------------------------
    if state["street"] == "preflop":
        tier = _pf_tier(hole)
        raised = current_bet > bb
        if not raised:
            # open wide, wider in position
            if tier >= 4:
                return raise_to(1.0)
            if tier >= (1 if late else 2):
                return raise_to(0.8)
            if can_check:
                return {"action": "check"}
            return {"action": "fold"}
        # facing a raise: 3-bet premiums (+ occasional bluff), call playable, fold trash
        if tier >= 4 or (tier <= 1 and random.random() < 0.12):
            return raise_to(2.2)
        if tier >= 2 and owed <= pot * 0.6:
            return {"action": "call"}
        if can_check:
            return {"action": "check"}
        return {"action": "fold"}

    # ---- Postflop --------------------------------------------------------
    cat = _made(hole, board)
    fd, sd = _draws(hole, board)
    has_draw = fd or sd
    strong = cat >= 2          # two pair or better
    decent = cat == 1          # a pair

    if can_check:
        # bet for value when strong; semi-bluff draws; stab air sometimes (esp. late)
        if strong:
            return raise_to(0.7)
        if has_draw and random.random() < 0.6:
            return raise_to(0.6)
        if decent and random.random() < 0.5:
            return raise_to(0.45)
        if random.random() < (0.45 if late else 0.25):
            return raise_to(0.55)   # pure stab
        return {"action": "check"}

    # facing a bet
    price = owed / (pot + owed)
    if cat >= 3:                                   # trips+ — raise for value
        return raise_to(1.0)
    if cat == 2:                                   # two pair — usually raise
        return raise_to(0.8) if random.random() < 0.6 else {"action": "call"}
    if has_draw:                                   # semi-bluff raise or call
        if random.random() < 0.35 and owed < stack:
            return raise_to(0.9)
        return {"action": "call"} if price <= 0.4 else {"action": "fold"}
    if decent:                                     # a pair — call a fair price
        return {"action": "call"} if price <= 0.33 else {"action": "fold"}
    # air — fold (a true LAG gives up when it has nothing), rare bluff-raise
    if random.random() < 0.06 and owed < stack and owed <= pot * 0.5:
        return raise_to(1.0)
    return {"action": "fold"}
