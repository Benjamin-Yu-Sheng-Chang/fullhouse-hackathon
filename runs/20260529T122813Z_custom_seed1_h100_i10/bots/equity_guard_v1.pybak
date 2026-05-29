"""Equity Guard v1: Monte-Carlo pot odds with conservative value raises."""
import random

import eval7

BOT_NAME = "Equity Guard v1"
RANK_VALUE = {rank: value for value, rank in enumerate("23456789TJQKA", 2)}


def _eval_cards(cards):
    return [eval7.Card(card) for card in cards]


def _live_opponents(state):
    my_seat = state["seat_to_act"]
    return sum(
        1
        for player in state["players"]
        if player["seat"] != my_seat
        and not player["is_folded"]
        and player["state"] != "busted"
    )


def _preflop_score(cards):
    ranks = sorted((RANK_VALUE[card[0]] for card in cards), reverse=True)
    suited = cards[0][1] == cards[1][1]
    pair = ranks[0] == ranks[1]
    gap = ranks[0] - ranks[1]

    if pair:
        return 42 + ranks[0] * 2

    return ranks[0] * 2 + ranks[1] + (4 if suited else 0) - max(0, gap - 1) * 2


def _monte_carlo_equity(hole_cards, board_cards, opponent_count, iterations=90):
    used = set(hole_cards + board_cards)
    deck = [card for card in eval7.Deck() if card not in used]
    board_cards_needed = 5 - len(board_cards)
    wins = 0
    ties = 0

    for _ in range(iterations):
        random.shuffle(deck)
        runout = deck[:board_cards_needed]
        index = board_cards_needed
        hero_score = eval7.evaluate(hole_cards + board_cards + runout)
        tied = False
        lost = False

        for _opponent in range(opponent_count):
            opponent_hole = [deck[index], deck[index + 1]]
            index += 2
            opponent_score = eval7.evaluate(opponent_hole + board_cards + runout)
            if opponent_score > hero_score:
                lost = True
                break
            if opponent_score == hero_score:
                tied = True

        if not lost:
            if tied:
                ties += 1
            else:
                wins += 1

    return (wins + 0.5 * ties) / iterations


def decide(state):
    owed = state["amount_owed"]
    pot = max(1, state["pot"])
    stack = state["your_stack"]
    total_bet_cap = stack + state["your_bet_this_street"]
    opponent_count = max(1, _live_opponents(state))

    if state["street"] == "preflop":
        score = _preflop_score(state["your_cards"])
        premium = score >= 62
        playable = score >= (46 if opponent_count <= 2 else 50)
        cheap = owed <= max(80, pot * 0.18)

        if premium:
            raise_to = max(state["min_raise_to"], int(pot + owed * 3))
            return {"action": "raise", "amount": min(raise_to, total_bet_cap)}
        if playable and (state["can_check"] or cheap):
            return {"action": "call"} if owed else {"action": "check"}
        if state["can_check"]:
            return {"action": "check"}
        return {"action": "fold"}

    equity = _monte_carlo_equity(
        _eval_cards(state["your_cards"]),
        _eval_cards(state["community_cards"]),
        opponent_count,
    )
    pot_price = owed / (pot + owed) if owed else 0.0
    multiway_margin = 0.05 + 0.03 * max(0, opponent_count - 1)

    if owed == 0:
        if equity > 0.62:
            raise_to = max(state["min_raise_to"], int(pot * 0.65))
            return {"action": "raise", "amount": min(raise_to, total_bet_cap)}
        return {"action": "check"}

    if equity > 0.74 and owed < stack * 0.55:
        raise_to = max(state["min_raise_to"], int(pot + owed * 2.5))
        return {"action": "raise", "amount": min(raise_to, total_bet_cap)}
    if equity >= pot_price + multiway_margin:
        return {"action": "call"}
    return {"action": "fold"}
