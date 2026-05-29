"""Hybrid Mix v1: equity guard core with position pressure and table adaptation."""
import random

import eval7

BOT_NAME = "Hybrid Mix v1"
RANK_VALUE = {rank: value for value, rank in enumerate("23456789TJQKA", 2)}
BIG_BLIND = 100


def _cards(cards):
    return [eval7.Card(card) for card in cards]


def _position(state):
    return state["seat_to_act"] / max(1, len(state["players"]) - 1)


def _profile(state):
    recent = state.get("match_action_log", [])[-300:]
    count = max(1, len(recent))
    raises = sum(1 for action in recent if action.get("action") in ("raise", "all_in"))
    folds = sum(1 for action in recent if action.get("action") == "fold")
    return raises / count, folds / count


def _live_opponents(state):
    me = state["seat_to_act"]
    return sum(1 for p in state["players"] if p["seat"] != me and not p["is_folded"] and p["state"] != "busted")


def _features(cards):
    high, low = sorted((RANK_VALUE[card[0]] for card in cards), reverse=True)
    suited = cards[0][1] == cards[1][1]
    pair = high == low
    gap = high - low
    return high, low, suited, pair, gap


def _preflop_score(cards):
    high, low, suited, pair, gap = _features(cards)
    if pair:
        return 58 + high * 3
    broadway = int(high >= 10) + int(low >= 10)
    return high * 3 + low * 1.5 + broadway * 3 + (8 if high == 14 else 0) + (5 if suited else 0) - max(0, gap - 2) * 2


def _equity(state, opponents, iterations=85):
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
            villain = eval7.evaluate([deck[idx], deck[idx + 1]] + board + runout)
            idx += 2
            if villain > hero:
                lost = True
                break
            if villain == hero:
                tied = True
        if not lost:
            if tied:
                ties += 1
            else:
                wins += 1
    return (wins + 0.5 * ties) / iterations


def _drawish(state):
    board = state["community_cards"]
    if len(board) < 3:
        return False
    suits = [card[1] for card in board + state["your_cards"]]
    flush_draw = any(suits.count(suit) >= 4 for suit in "shdc")
    values = sorted(set(RANK_VALUE[card[0]] for card in board + state["your_cards"]))
    straight_draw = any(len([v for v in values if start <= v <= start + 4]) >= 4 for start in range(2, 11))
    return flush_draw or straight_draw


def _bet_to(state, pot_fraction):
    cap = state["your_stack"] + state["your_bet_this_street"]
    return min(max(state["min_raise_to"], int(max(100, state["pot"]) * pot_fraction)), cap)


def decide(state):
    owed = state["amount_owed"]
    pot = max(1, state["pot"])
    opponents = max(1, _live_opponents(state))
    late = _position(state) >= 0.55
    raise_rate, fold_rate = _profile(state)

    if state["street"] == "preflop":
        score = _preflop_score(state["your_cards"])
        high, low, suited, pair, gap = _features(state["your_cards"])
        premium = pair and high >= 10 or high == 14 and low >= 12 or suited and high >= 13 and low >= 12
        if premium:
            size = max(state["min_raise_to"], int(max(state["current_bet"], BIG_BLIND) * (4.0 if raise_rate > 0.23 else 3.2)), int(pot * 1.0))
            return {"action": "raise", "amount": min(size, state["your_stack"] + state["your_bet_this_street"])}

        open_line = 50 if late else 58
        if fold_rate > 0.32 and late:
            open_line -= 4
        if raise_rate > 0.24:
            open_line += 5
        if opponents >= 5:
            open_line += 3

        if state["current_bet"] <= BIG_BLIND and score >= open_line:
            return {"action": "raise", "amount": _bet_to(state, 1.0 if late else 1.35)}
        if owed and score >= open_line + 8 and owed <= pot * (0.34 if raise_rate < 0.23 else 0.22):
            return {"action": "call"}
        if state["can_check"]:
            return {"action": "check"}
        return {"action": "fold"}

    equity = _equity(state, opponents)
    price = owed / (pot + owed) if owed else 0
    margin = 0.035 + 0.035 * max(0, opponents - 1) + (0.035 if raise_rate > 0.24 else 0)
    draw = _drawish(state)

    if owed == 0:
        if equity > 0.58:
            return {"action": "raise", "amount": _bet_to(state, 0.62)}
        if late and opponents <= 2 and (draw or equity > 0.43) and random.random() < (0.45 if fold_rate > 0.30 else 0.28):
            return {"action": "raise", "amount": _bet_to(state, 0.48)}
        return {"action": "check"}

    if equity > 0.70 and owed <= state["your_stack"] * 0.50:
        return {"action": "raise", "amount": _bet_to(state, 0.85)}
    if draw and late and owed <= pot * 0.20 and random.random() < 0.25:
        return {"action": "raise", "amount": _bet_to(state, 0.65)}
    if equity >= price + margin:
        return {"action": "call"}
    return {"action": "fold"}
