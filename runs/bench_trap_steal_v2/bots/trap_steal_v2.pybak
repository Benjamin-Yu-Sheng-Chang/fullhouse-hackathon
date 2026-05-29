"""Trap Steal v2: table-adaptive preflop plus equity-aware postflop traps."""
import random

import eval7

BOT_NAME = "Trap Steal v2"
RANK_VALUE = {rank: value for value, rank in enumerate("23456789TJQKA", 2)}


def _cards(cards):
    return [eval7.Card(card) for card in cards]


def _table_profile(state):
    recent = state.get("match_action_log", [])[-300:]
    count = max(1, len(recent))
    raises = sum(1 for action in recent if action.get("action") in ("raise", "all_in"))
    folds = sum(1 for action in recent if action.get("action") == "fold")
    return raises / count, folds / count


def _position(state):
    return state["seat_to_act"] / max(1, len(state["players"]) - 1)


def _live_opponents(state):
    me = state["seat_to_act"]
    return sum(1 for p in state["players"] if p["seat"] != me and not p["is_folded"] and p["state"] != "busted")


def _score(cards):
    high, low = sorted((RANK_VALUE[card[0]] for card in cards), reverse=True)
    suited = cards[0][1] == cards[1][1]
    pair = high == low
    gap = high - low
    if pair:
        return 62 + high * 3
    return high * 3 + low * 1.3 + (8 if high == 14 else 0) + (6 if suited else 0) - max(0, gap - 2) * 2


def _equity(state, opponents, iterations=70):
    hole = _cards(state["your_cards"])
    board = _cards(state["community_cards"])
    used = set(hole + board)
    deck = [card for card in eval7.Deck() if card not in used]
    need = 5 - len(board)
    wins = ties = 0
    for _ in range(iterations):
        random.shuffle(deck)
        runout = deck[:need]
        idx = need
        hero = eval7.evaluate(hole + board + runout)
        tied = False
        lost = False
        for _opp in range(opponents):
            score = eval7.evaluate([deck[idx], deck[idx + 1]] + board + runout)
            idx += 2
            if score > hero:
                lost = True
                break
            if score == hero:
                tied = True
        if not lost:
            wins += 0 if tied else 1
            ties += 1 if tied else 0
    return (wins + 0.5 * ties) / iterations


def _bet_to(state, pot_fraction):
    cap = state["your_stack"] + state["your_bet_this_street"]
    return min(max(state["min_raise_to"], int(max(100, state["pot"]) * pot_fraction)), cap)


def decide(state):
    owed = state["amount_owed"]
    pot = max(1, state["pot"])
    raise_rate, fold_rate = _table_profile(state)
    late = _position(state) >= 0.55
    opponents = max(1, _live_opponents(state))

    if state["street"] == "preflop":
        score = _score(state["your_cards"])
        trap_line = 70 if raise_rate > 0.24 else 64
        steal_line = 47 if late and fold_rate > 0.30 else 56

        if score >= trap_line:
            size = pot + owed * (4.2 if raise_rate > 0.24 else 3.0)
            return {"action": "raise", "amount": min(max(state["min_raise_to"], int(size)), state["your_stack"] + state["your_bet_this_street"])}
        if state["current_bet"] <= 100 and score >= steal_line:
            return {"action": "raise", "amount": _bet_to(state, 1.15 if late else 1.45)}
        if owed and score >= steal_line + 6 and owed <= pot * (0.26 if raise_rate < 0.20 else 0.18):
            return {"action": "call"}
        if state["can_check"]:
            return {"action": "check"}
        return {"action": "fold"}

    equity = _equity(state, opponents)
    price = owed / (pot + owed) if owed else 0
    caution = 0.05 + 0.035 * max(0, opponents - 1) + (0.04 if raise_rate > 0.25 else 0)

    if owed == 0:
        if equity > 0.64:
            return {"action": "raise", "amount": _bet_to(state, 0.72)}
        if late and fold_rate > 0.34 and equity > 0.40 and random.random() < 0.30:
            return {"action": "raise", "amount": _bet_to(state, 0.45)}
        return {"action": "check"}

    if equity > 0.73 and owed <= state["your_stack"] * 0.45:
        return {"action": "raise", "amount": _bet_to(state, 0.95)}
    if equity >= price + caution:
        return {"action": "call"}
    return {"action": "fold"}
