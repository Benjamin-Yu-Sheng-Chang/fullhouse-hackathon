"""Position Bully v1: opens wider late and attacks passive postflop spots."""
import random

BOT_NAME = "Position Bully v1"
RANK_VALUE = {rank: value for value, rank in enumerate("23456789TJQKA", 2)}


def _preflop_score(cards):
    high, low = sorted((RANK_VALUE[card[0]] for card in cards), reverse=True)
    suited = cards[0][1] == cards[1][1]
    pair = high == low
    gap = high - low

    if pair:
        return 55 + high * 3

    return high * 3 + low + (7 if suited else 0) - max(0, gap - 1) * 3


def _active_opponents(state):
    my_seat = state["seat_to_act"]
    return sum(
        1
        for player in state["players"]
        if player["seat"] != my_seat
        and player["state"] == "active"
        and not player["is_folded"]
    )


def _has_pair_or_better(state):
    board = state["community_cards"]
    hole = state["your_cards"]
    ranks = [card[0] for card in board + hole]
    return any(ranks.count(card[0]) >= 2 for card in hole)


def _has_backdoor_or_flush_draw(state):
    board_suits = [card[1] for card in state["community_cards"]]
    return any(card[1] in board_suits for card in state["your_cards"])


def decide(state):
    owed = state["amount_owed"]
    pot = max(1, state["pot"])
    total_bet_cap = state["your_stack"] + state["your_bet_this_street"]
    late_position = state["seat_to_act"] >= len(state["players"]) * 0.55

    if state["street"] == "preflop":
        score = _preflop_score(state["your_cards"])
        already_raised = state["current_bet"] > 100
        open_threshold = 48 if late_position else 58
        call_threshold = 54 if late_position else 64

        if not already_raised and score >= open_threshold:
            raise_to = max(state["min_raise_to"], int(pot * (1.2 if late_position else 1.6)))
            return {"action": "raise", "amount": min(raise_to, total_bet_cap)}
        if already_raised and score >= call_threshold and owed <= pot * 0.35:
            return {"action": "call"}
        if state["can_check"]:
            return {"action": "check"}
        return {"action": "fold"}

    opponents = _active_opponents(state)
    pair_or_better = _has_pair_or_better(state)
    drawish = _has_backdoor_or_flush_draw(state)
    high_card = max(RANK_VALUE[card[0]] for card in state["your_cards"])

    if owed == 0:
        if opponents <= 2 and late_position and random.random() < (0.55 if pair_or_better or drawish else 0.35):
            raise_to = max(state["min_raise_to"], int(pot * 0.55))
            return {"action": "raise", "amount": min(raise_to, total_bet_cap)}
        if pair_or_better and random.random() < 0.45:
            raise_to = max(state["min_raise_to"], int(pot * 0.7))
            return {"action": "raise", "amount": min(raise_to, total_bet_cap)}
        return {"action": "check"}

    if pair_or_better and owed <= pot * 0.45:
        return {"action": "call"}
    if late_position and opponents <= 2 and high_card >= 13 and owed <= pot * 0.22:
        return {"action": "call"}
    return {"action": "fold"}
