"""Diverse-pool villain: BIG-BET CALLING STATION.

Stresses the hero's "vs station" logic the saturated reference field can't: the
ref pot-odds bots (mathematician / ref_bot_2) FOLD to bets > ~1/2 pot, so they are
really folders, not stations. This bot is a *true* station — it calls down very
wide, even to big bets, and almost never folds or raises. The correct counter is
thin+big value and NEVER bluffing; a bot that bluffs this loses, a bot that
value-bets thin and large prints. Self-contained, distinct code (rule-based +
eval7 hand category), not a hero mirror.
"""
import eval7

BOT_NAME = "Calling Station"

_RANK = {r: i for i, r in enumerate("23456789TJQKA", start=2)}


def _cat(hole, board):
    """Coarse made-hand category 0..8 (high card .. straight flush) via eval7."""
    cards = [eval7.Card(c) for c in hole + board]
    return eval7.handtype(eval7.evaluate(cards))


def _has_pair_or_better(hole, board):
    cat = _cat(hole, board)
    return cat not in ("High Card",)


def decide(state):
    owed = state["amount_owed"]
    pot = state["pot"]
    can_check = state["can_check"]
    hole = state["your_cards"]
    board = state.get("community_cards") or []
    stack = state["your_stack"]
    my_bet = state.get("your_bet_this_street", 0)
    min_raise_to = state.get("min_raise_to", 0)

    ranks = sorted((_RANK[c[0]] for c in hole), reverse=True)
    premium = ranks[0] == 14 and ranks[1] >= 13  # AA/AK-ish — the only raise

    # Preflop: enter almost any pot by calling; only premiums put in a raise.
    if state["street"] == "preflop":
        if premium and ranks[0] == ranks[1]:  # AA: small raise
            return {"action": "raise", "amount": min(min_raise_to + 2 * pot, stack + my_bet)}
        if can_check:
            return {"action": "check"}
        # Call almost anything getting any kind of price (true station).
        if owed <= stack and owed <= pot * 1.5:
            return {"action": "call"}
        return {"action": "call"} if owed < stack else {"action": "fold"}

    # Postflop: passive. Bet small only with two pair+ for value; otherwise check.
    if can_check:
        cat = _cat(hole, board)
        strong = cat not in ("High Card", "Pair")
        if strong:
            return {"action": "raise", "amount": min(min_raise_to + pot // 2, stack + my_bet)}
        return {"action": "check"}

    # Facing a bet: call down extremely wide. Fold only stone-cold air (no pair,
    # no real draw) facing a big bet.
    if owed >= stack:
        # all-in call: need at least a pair
        return {"action": "call"} if _has_pair_or_better(hole, board) else (
            {"action": "fold"} if owed > pot * 0.6 else {"action": "call"})
    if _has_pair_or_better(hole, board):
        return {"action": "call"}
    # air: still call small bets (curiosity), fold only to large bets
    if owed <= pot * 0.5:
        return {"action": "call"}
    return {"action": "fold"}
