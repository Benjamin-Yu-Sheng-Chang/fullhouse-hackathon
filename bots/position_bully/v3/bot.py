"""Position Bully v2: steals in position, but slows down multiway and vs raisers."""
import random

BOT_NAME = "Position Bully v3"
RANK_VALUE = {rank: value for value, rank in enumerate("23456789TJQKA", 2)}
BIG_BLIND = 100


def _position(state):
    return state["seat_to_act"] / max(1, len(state["players"]) - 1)


def _preflop_features(cards):
    high, low = sorted((RANK_VALUE[card[0]] for card in cards), reverse=True)
    suited = cards[0][1] == cards[1][1]
    pair = high == low
    gap = high - low
    return high, low, suited, pair, gap


def _preflop_score(cards):
    high, low, suited, pair, gap = _preflop_features(cards)
    if pair:
        return 58 + high * 3
    broadway = int(high >= 10) + int(low >= 10)
    return high * 3 + low + broadway * 3 + (7 if suited else 0) - max(0, gap - 1) * 3


def _active_opponents(state):
    me = state["seat_to_act"]
    return sum(
        1
        for player in state["players"]
        if player["seat"] != me and player["state"] == "active" and not player["is_folded"]
    )


def _street_aggression(state):
    return sum(1 for action in state["action_log"] if action.get("action") in ("raise", "all_in"))


def _has_pair_or_better(state):
    ranks = [card[0] for card in state["your_cards"] + state["community_cards"]]
    return any(ranks.count(card[0]) >= 2 for card in state["your_cards"])


def _has_draw(state):
    board = state["community_cards"]
    if len(board) < 3:
        return False
    suits = [card[1] for card in board + state["your_cards"]]
    flush_draw = any(suits.count(suit) >= 4 for suit in "shdc")
    values = sorted(set(RANK_VALUE[card[0]] for card in board + state["your_cards"]))
    straightish = any(len([v for v in values if start <= v <= start + 4]) >= 4 for start in range(2, 11))
    return flush_draw or straightish


def _board_danger(state):
    board = state["community_cards"]
    if len(board) < 3:
        return False
    suits = [card[1] for card in board]
    values = sorted(set(RANK_VALUE[card[0]] for card in board))
    flushy = any(suits.count(suit) >= 3 for suit in "shdc")
    straighty = any(len([v for v in values if start <= v <= start + 4]) >= 3 for start in range(2, 11))
    return flushy or straighty


def _bet_to(state, pot_fraction):
    cap = state["your_stack"] + state["your_bet_this_street"]
    return min(max(state["min_raise_to"], int(max(100, state["pot"]) * pot_fraction)), cap)


def decide(state):
    owed = state["amount_owed"]
    pot = max(1, state["pot"])
    late = _position(state) >= 0.55
    very_late = _position(state) >= 0.75
    opponents = _active_opponents(state)

    if state["street"] == "preflop":
        score = _preflop_score(state["your_cards"])
        high, low, suited, pair, gap = _preflop_features(state["your_cards"])
        raised = state["current_bet"] > BIG_BLIND
        premium = pair and high >= 10 or high == 14 and low >= 12

        if premium:
            cap = state["your_stack"] + state["your_bet_this_street"]
            size = max(state["min_raise_to"], int(max(state["current_bet"], BIG_BLIND) * 3.5), int(pot * 1.1))
            return {"action": "raise", "amount": min(size, cap)}

        open_threshold = 46 if very_late else (51 if late else 60)
        if opponents >= 4:
            open_threshold += 5
        if not raised and score >= open_threshold:
            return {"action": "raise", "amount": _bet_to(state, 1.05 if late else 1.35)}

        call_threshold = 60 if late else 68
        speculative = pair or (suited and gap <= 2 and high >= 9)
        if raised and score >= call_threshold and owed <= pot * 0.30:
            return {"action": "call"}
        if raised and speculative and owed <= pot * 0.16:
            return {"action": "call"}
        if state["can_check"]:
            return {"action": "check"}
        return {"action": "fold"}

    pair = _has_pair_or_better(state)
    draw = _has_draw(state)
    danger = _board_danger(state)
    aggression = _street_aggression(state)

    if owed == 0:
        if pair:
            return {"action": "raise", "amount": _bet_to(state, 0.65)}
        if late and opponents <= 2 and aggression == 0:
            chance = 0.45 if draw else 0.28
            if random.random() < chance:
                return {"action": "raise", "amount": _bet_to(state, 0.50)}
        return {"action": "check"}

    pair_call_cap = 0.36 if danger else 0.42
    if aggression > 1:
        pair_call_cap = min(pair_call_cap, 0.25)
    if state["street"] == "river" and danger:
        pair_call_cap = min(pair_call_cap, 0.24)
    if pair and owed <= pot * pair_call_cap and owed <= state["your_stack"] * 0.34:
        return {"action": "call"}
    if late and draw and state["street"] != "river" and owed <= pot * 0.22 and owed <= state["your_stack"] * 0.26:
        return {"action": "call"}
    return {"action": "fold"}
