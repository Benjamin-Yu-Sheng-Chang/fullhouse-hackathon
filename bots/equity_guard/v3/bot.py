"""Equity Guard v2: equity-aware value betting with position steals."""
import random

import eval7

BOT_NAME = "Equity Guard v3"
RANK_VALUE = {rank: value for value, rank in enumerate("23456789TJQKA", 2)}
BIG_BLIND = 100


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


def _position_factor(state):
    return state["seat_to_act"] / max(1, len(state["players"]) - 1)


def _preflop_features(cards):
    ranks = sorted((RANK_VALUE[card[0]] for card in cards), reverse=True)
    suited = cards[0][1] == cards[1][1]
    pair = ranks[0] == ranks[1]
    gap = ranks[0] - ranks[1]
    return ranks[0], ranks[1], suited, pair, gap


def _preflop_score(cards):
    high, low, suited, pair, gap = _preflop_features(cards)
    if pair:
        return 50 + high * 3

    broadway_count = int(high >= 10) + int(low >= 10)
    ace_bonus = 8 if high == 14 else 0
    suited_bonus = 5 if suited else 0
    connector_bonus = 4 if gap <= 1 else (2 if gap == 2 else 0)
    gap_penalty = max(0, gap - 2) * 2

    return high * 3 + low * 1.5 + ace_bonus + broadway_count * 3 + suited_bonus + connector_bonus - gap_penalty


def _preflop_plan(state, opponent_count):
    score = _preflop_score(state["your_cards"])
    high, low, suited, pair, gap = _preflop_features(state["your_cards"])
    owed = state["amount_owed"]
    pot = max(1, state["pot"])
    current_bet = state["current_bet"]
    late = _position_factor(state) >= 0.55
    total_bet_cap = state["your_stack"] + state["your_bet_this_street"]
    unopened_or_limped = current_bet <= BIG_BLIND

    premium = pair and high >= 10
    premium = premium or (high == 14 and low >= 12)
    premium = premium or (suited and high >= 13 and low >= 12)

    open_threshold = 49 if late else 56
    continue_threshold = 58 if late else 64
    if opponent_count <= 2:
        open_threshold -= 4
        continue_threshold -= 3
    if opponent_count >= 5:
        open_threshold += 4

    if premium:
        multiplier = 4.0 if current_bet > BIG_BLIND else 3.0
        raise_to = max(state["min_raise_to"], int(current_bet * multiplier), int(pot * 0.9))
        return {"action": "raise", "amount": min(raise_to, total_bet_cap)}

    if unopened_or_limped and score >= open_threshold:
        size = 3.0 * BIG_BLIND + max(0, opponent_count - 2) * 25
        if late:
            size -= 50
        raise_to = max(state["min_raise_to"], int(size), int(pot * 0.8))
        return {"action": "raise", "amount": min(raise_to, total_bet_cap)}

    speculative = pair or suited and gap <= 3 and high >= 9
    if owed and score >= continue_threshold and owed <= pot * 0.38:
        return {"action": "call"}
    if owed and speculative and owed <= pot * 0.18:
        return {"action": "call"}
    if state["can_check"]:
        return {"action": "check"}
    return {"action": "fold"}


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


def _has_pair_or_better(state):
    ranks = [card[0] for card in state["your_cards"] + state["community_cards"]]
    return any(ranks.count(card[0]) >= 2 for card in state["your_cards"])


def _has_flush_draw(state):
    if len(state["community_cards"]) < 3:
        return False
    suits = [card[1] for card in state["your_cards"] + state["community_cards"]]
    return any(suits.count(suit) >= 4 for suit in "shdc")


def _board_danger(state):
    board = state["community_cards"]
    if len(board) < 3:
        return False
    suits = [card[1] for card in board]
    board_flushy = any(suits.count(suit) >= 3 for suit in "shdc")
    values = sorted(set(RANK_VALUE[card[0]] for card in board))
    board_straighty = any(len([v for v in values if start <= v <= start + 4]) >= 3 for start in range(2, 11))
    return board_flushy or board_straighty


def _bet_size(pot, fraction, state):
    total_bet_cap = state["your_stack"] + state["your_bet_this_street"]
    raise_to = max(state["min_raise_to"], int(pot * fraction))
    return min(raise_to, total_bet_cap)


def decide(state):
    owed = state["amount_owed"]
    pot = max(1, state["pot"])
    stack = state["your_stack"]
    opponent_count = max(1, _live_opponents(state))

    if state["street"] == "preflop":
        return _preflop_plan(state, opponent_count)

    equity = _monte_carlo_equity(
        _eval_cards(state["your_cards"]),
        _eval_cards(state["community_cards"]),
        opponent_count,
    )
    pot_price = owed / (pot + owed) if owed else 0.0
    multiway_margin = 0.03 + 0.035 * max(0, opponent_count - 1)
    late = _position_factor(state) >= 0.55
    pair_or_better = _has_pair_or_better(state)
    flush_draw = _has_flush_draw(state)
    board_danger = _board_danger(state)
    if board_danger and not pair_or_better and owed > pot * 0.45:
        multiway_margin += 0.02
    if state["street"] == "river" and not pair_or_better:
        multiway_margin += 0.05
    if owed > stack * 0.34:
        multiway_margin += 0.08

    if owed == 0:
        if equity > 0.56 or pair_or_better:
            return {"action": "raise", "amount": _bet_size(pot, 0.58, state)}
        if late and opponent_count <= 2 and (equity > 0.42 or flush_draw) and random.random() < 0.45:
            return {"action": "raise", "amount": _bet_size(pot, 0.52, state)}
        if late and opponent_count == 1 and random.random() < 0.22:
            return {"action": "raise", "amount": _bet_size(pot, 0.48, state)}
        return {"action": "check"}

    if equity > 0.68 and owed < stack * 0.55:
        return {"action": "raise", "amount": _bet_size(pot + owed, 0.9, state)}
    if flush_draw and late and owed <= pot * 0.22 and random.random() < 0.35:
        return {"action": "raise", "amount": _bet_size(pot + owed, 0.75, state)}
    if equity >= pot_price + multiway_margin:
        return {"action": "call"}
    return {"action": "fold"}
