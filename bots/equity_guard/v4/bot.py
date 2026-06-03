"""Equity Guard v2: equity-aware value betting with position steals."""
import json
import os
import random

import eval7

BOT_NAME = "Equity Guard v4 GTO Preflop"
RANK_VALUE = {rank: value for value, rank in enumerate("23456789TJQKA", 2)}
BIG_BLIND = 100



# ---------------------------------------------------------------------------
# GTO preflop table hook for 6-max tables. Postflop remains this bot's own
# strategy; this only overrides preflop spots covered by data/preflop_ranges.json.
# ---------------------------------------------------------------------------
GTO_RANKS = "23456789TJQKA"
GTO_RANK_VALUE = {rank: value for value, rank in enumerate(GTO_RANKS, 2)}
GTO_DATA_DIR = os.environ.get("BOT_DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
GTO_PREFLOP_PATH = os.path.join(GTO_DATA_DIR, "preflop_ranges.json")


def _gto_load_preflop_table():
    try:
        with open(GTO_PREFLOP_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {"rfi": {}, "vs_rfi": {}, "vs_limp": {}}


GTO_PREFLOP_TABLE = _gto_load_preflop_table()


def _gto_preflop_features(cards):
    high, low = sorted((GTO_RANK_VALUE[card[0]] for card in cards), reverse=True)
    suited = cards[0][1] == cards[1][1]
    pair = high == low
    gap = high - low
    return high, low, suited, pair, gap


def _gto_hand_code(cards):
    high, low, suited, pair, _gap = _gto_preflop_features(cards)
    hi = GTO_RANKS[high - 2]
    lo = GTO_RANKS[low - 2]
    if pair:
        return hi + lo
    return hi + lo + ("s" if suited else "o")


def _gto_rank_index(rank):
    return GTO_RANKS.index(rank)


def _gto_all_hand_codes():
    hands = set()
    for i in range(len(GTO_RANKS) - 1, -1, -1):
        for j in range(i, -1, -1):
            if i == j:
                hands.add(GTO_RANKS[i] + GTO_RANKS[j])
            else:
                hands.add(GTO_RANKS[i] + GTO_RANKS[j] + "s")
                hands.add(GTO_RANKS[i] + GTO_RANKS[j] + "o")
    return hands


def _gto_expand_pair(token):
    if "-" in token:
        start, end = token.split("-")
        hi = _gto_rank_index(start[0])
        lo = _gto_rank_index(end[0])
    else:
        hi = _gto_rank_index(token[0])
        lo = hi
    if token.endswith("+"):
        lo = hi
        hi = len(GTO_RANKS) - 1
    lo, hi = min(lo, hi), max(lo, hi)
    return {GTO_RANKS[i] + GTO_RANKS[i] for i in range(lo, hi + 1)}


def _gto_expand_non_pair(token):
    suitedness = token[-1]
    body = token[:-1]
    high = body[0]
    low = body[1]
    high_idx = _gto_rank_index(high)
    low_idx = _gto_rank_index(low)
    if token.endswith("+"):
        suitedness = token[-2]
        high = token[0]
        low = token[1]
        high_idx = _gto_rank_index(high)
        low_idx = _gto_rank_index(low)
        return {high + GTO_RANKS[i] + suitedness for i in range(low_idx, high_idx)}
    if "-" in token:
        start, end = token.split("-")
        suitedness = start[-1]
        high = start[0]
        start_low = _gto_rank_index(start[1])
        end_low = _gto_rank_index(end[1])
        start_low, end_low = min(start_low, end_low), max(start_low, end_low)
        return {high + GTO_RANKS[i] + suitedness for i in range(start_low, end_low + 1)}
    return {token}


def _gto_expand_range(tokens):
    hands = set()
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if token == "ALL":
            hands.update(_gto_all_hand_codes())
        elif len(token) >= 2 and token[0] == token[1]:
            hands.update(_gto_expand_pair(token))
        else:
            hands.update(_gto_expand_non_pair(token))
    return hands


def _gto_position_label(state):
    pos = state["seat_to_act"] / max(1, len(state.get("players", [])) - 1)
    if pos >= 0.67:
        return "late"
    if pos >= 0.34:
        return "middle"
    return "early"


def _gto_position_from_blinds(state):
    players = state.get("players", [])
    n = len(players)
    if n < 2:
        return "BTN"
    sb = bb = None
    for item in state.get("action_log", [])[:4]:
        if item.get("action") == "small_blind":
            sb = item.get("seat")
        elif item.get("action") == "big_blind":
            bb = item.get("seat")
    if sb is None or bb is None:
        return _gto_position_label(state).upper()
    offsets = {
        (bb + 1) % n: "LJ",
        (bb + 2) % n: "HJ",
        (bb + 3) % n: "CO",
        (bb + 4) % n: "BTN",
        sb: "SB",
        bb: "BB",
    }
    return offsets.get(state["seat_to_act"], _gto_position_label(state).upper())


def _gto_first_rfi_position(state):
    players = state.get("players", [])
    n = len(players)
    if n < 2:
        return None
    sb = bb = None
    for item in state.get("action_log", [])[:4]:
        if item.get("action") == "small_blind":
            sb = item.get("seat")
        elif item.get("action") == "big_blind":
            bb = item.get("seat")
    if sb is None or bb is None:
        return None
    seat_to_pos = {
        (bb + 1) % n: "LJ",
        (bb + 2) % n: "HJ",
        (bb + 3) % n: "CO",
        (bb + 4) % n: "BTN",
        sb: "SB",
        bb: "BB",
    }
    for item in state.get("action_log", [])[2:]:
        if item.get("action") in ("raise", "all_in"):
            return seat_to_pos.get(item.get("seat"))
        if item.get("action") == "call" and item.get("seat") == sb:
            return "SB"
    return None


def _gto_raise_to_bb(state, bb_amount):
    cap = state["your_stack"] + state["your_bet_this_street"]
    amount = int(bb_amount * BIG_BLIND)
    return min(max(state["min_raise_to"], amount), cap)


def _gto_frequency_action(spot, code):
    for item in spot.get("frequencies", []):
        if code not in _gto_expand_range(item.get("hands", [])):
            continue
        roll = random.random()
        cumulative = 0.0
        for action in ("raise", "call", "check", "fold"):
            cumulative += float(item.get(action, 0.0))
            if roll <= cumulative:
                return action
        return "fold"
    return None


def _gto_table_action_from_name(state, action_name, spot, position):
    if action_name == "raise":
        if state["current_bet"] <= BIG_BLIND:
            return {"action": "raise", "amount": _gto_raise_to_bb(state, spot.get("raise_size_bb", 2.5))}
        multiplier = spot.get("raise_multiplier", 3.5 if position not in ("SB", "BB") else 4.0)
        amount = int(state["current_bet"] * multiplier)
        return {"action": "raise", "amount": min(state["your_stack"] + state["your_bet_this_street"], max(state["min_raise_to"], amount))}
    if action_name == "call":
        if state["amount_owed"]:
            return {"action": "call"}
        if state["can_check"]:
            return {"action": "check"}
    if action_name == "check" and state["can_check"]:
        return {"action": "check"}
    if state["can_check"]:
        return {"action": "check"}
    return {"action": "fold"}


def _gto_table_preflop_action(state):
    if len(state.get("players", [])) != 6 or state.get("street") != "preflop":
        return None
    code = _gto_hand_code(state["your_cards"])
    position = _gto_position_from_blinds(state)
    opener = _gto_first_rfi_position(state)

    if opener is None and state["current_bet"] <= BIG_BLIND:
        spot = GTO_PREFLOP_TABLE.get("rfi", {}).get(position)
        if not spot:
            return None
        action_name = _gto_frequency_action(spot, code)
        if action_name:
            return _gto_table_action_from_name(state, action_name, spot, position)
        if code in _gto_expand_range(spot.get("raise", [])):
            return {"action": "raise", "amount": _gto_raise_to_bb(state, spot.get("raise_size_bb", 2.5))}
        if state["amount_owed"] and code in _gto_expand_range(spot.get("call", [])):
            return {"action": "call"}
        if state["can_check"]:
            return {"action": "check"}
        return {"action": "fold"}

    if opener == "SB" and position == "BB" and state["can_check"]:
        spot = GTO_PREFLOP_TABLE.get("vs_limp", {}).get("BB_vs_SB")
        if not spot:
            return None
        action_name = _gto_frequency_action(spot, code)
        if action_name:
            return _gto_table_action_from_name(state, action_name, spot, position)
        return {"action": "check"}

    if opener and opener != position and state["amount_owed"]:
        spot = GTO_PREFLOP_TABLE.get("vs_rfi", {}).get(position + "_vs_" + opener)
        if not spot:
            return None
        action_name = _gto_frequency_action(spot, code)
        if action_name:
            if action_name == "call" and state["amount_owed"] > state["your_stack"] * 0.28:
                return {"action": "fold"}
            return _gto_table_action_from_name(state, action_name, spot, position)
        if code in _gto_expand_range(spot.get("raise", [])):
            multiplier = spot.get("raise_multiplier", 3.5 if position not in ("SB", "BB") else 4.0)
            amount = int(state["current_bet"] * multiplier)
            return {"action": "raise", "amount": min(state["your_stack"] + state["your_bet_this_street"], max(state["min_raise_to"], amount))}
        if code in _gto_expand_range(spot.get("call", [])) and state["amount_owed"] <= state["your_stack"] * 0.28:
            return {"action": "call"}
        return {"action": "fold"}
    return None

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
    table_preflop_action = _gto_table_preflop_action(state)
    if table_preflop_action:
        return table_preflop_action
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
