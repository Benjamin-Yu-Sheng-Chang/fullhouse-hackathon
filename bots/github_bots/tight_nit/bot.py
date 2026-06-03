"""Diverse-pool villain: TIGHT NIT (pure over-folder).

Plays only premium hands, folds almost everything else preflop, and folds to any
postflop aggression without a strong made hand — never bluffs, never gets out of
line. Distinct from the reference `shark` (which value-RAISES and floats in
position) and from math/ref_bot_2 (which call ≤1/2-pot bets): this bot's whole
leak is its fold button. The correct counter is relentless stealing and small
c-bets that print fold equity; it confirms the hero's Stage-C steals are aimed at
a real over-folder, not just the refs. Self-contained, distinct code.
"""
import eval7

BOT_NAME = "Tight Nit"

_RANK = {r: i for i, r in enumerate("23456789TJQKA", start=2)}
# Premium opening range (~top 8%): big pairs, AK/AQ, AJs/KQs.
_PREMIUM_PAIRS = {14, 13, 12, 11, 10, 9}  # AA-99


def _preflop_premium(hole):
    r = sorted((_RANK[c[0]] for c in hole), reverse=True)
    suited = hole[0][1] == hole[1][1]
    if r[0] == r[1]:
        return r[0] in _PREMIUM_PAIRS
    if r[0] == 14 and r[1] >= 12:          # AK, AQ
        return True
    if r[0] == 14 and r[1] == 11 and suited:  # AJs
        return True
    if r[0] == 13 and r[1] == 12 and suited:  # KQs
        return True
    return False


def _strong_made(hole, board):
    """Top-pair-good-kicker or better — the only hands it continues with."""
    cat = eval7.handtype(eval7.evaluate([eval7.Card(c) for c in hole + board]))
    if cat not in ("High Card", "Pair"):
        return True  # two pair+ always strong
    if cat == "Pair":
        # is it a pair USING a hole card with a decent kicker (top-pairish)?
        board_ranks = {_RANK[c[0]] for c in board}
        hole_ranks = [_RANK[c[0]] for c in hole]
        if hole_ranks[0] == hole_ranks[1]:            # pocket pair
            return hole_ranks[0] >= max(board_ranks or {0})  # overpair only
        paired = [r for r in hole_ranks if r in board_ranks]
        if paired:
            return max(paired) >= (max(board_ranks) if board_ranks else 0) and max(hole_ranks) >= 12
    return False


def decide(state):
    owed = state["amount_owed"]
    pot = state["pot"]
    can_check = state["can_check"]
    hole = state["your_cards"]
    board = state.get("community_cards") or []
    stack = state["your_stack"]
    my_bet = state.get("your_bet_this_street", 0)
    min_raise_to = state.get("min_raise_to", 0)

    if state["street"] == "preflop":
        if _preflop_premium(hole):
            return {"action": "raise", "amount": min(min_raise_to + pot, stack + my_bet)}
        if can_check:
            return {"action": "check"}
        return {"action": "fold"}

    strong = _strong_made(hole, board)
    if can_check:
        if strong:
            return {"action": "raise", "amount": min(min_raise_to + pot // 2, stack + my_bet)}
        return {"action": "check"}
    # Facing a bet: continue only with a strong made hand, and even then only call
    # (never spews a raise); fold everything else — the pure nit leak.
    if strong and owed <= pot:
        return {"action": "call"}
    return {"action": "fold"}
