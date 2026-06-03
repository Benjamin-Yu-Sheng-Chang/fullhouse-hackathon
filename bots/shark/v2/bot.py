"""Bot C: The Shark — tight preflop, position-aware, value bets."""
import json
import os
import random

BOT_NAME = "Shark v2 GTO Preflop"

STRONG_HANDS = {
    ("A", "A"), ("K", "K"), ("Q", "Q"), ("J", "J"), ("T", "T"),
    ("A", "K"), ("A", "Q"), ("A", "J"), ("K", "Q"),
}


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

def hand_strength(cards):
    ranks = tuple(sorted([c[0] for c in cards], reverse=True))
    suited = cards[0][1] == cards[1][1]
    if ranks in STRONG_HANDS:
        return "strong"
    if ranks[0] in "AKQJT" or suited:
        return "medium"
    return "weak"

def decide(state):
    table_preflop_action = _gto_table_preflop_action(state)
    if table_preflop_action:
        return table_preflop_action
    street  = state["street"]
    owed    = state["amount_owed"]
    pot     = state["pot"]
    stack   = state["your_stack"]
    seat    = state["seat_to_act"]
    n       = len(state["players"])
    # Late position = closer to dealer button = more info
    position = seat / max(n - 1, 1)  # 0 = early, 1 = late

    # Preflop: tight hand selection
    if street == "preflop":
        strength = hand_strength(state["your_cards"])

        if strength == "strong":
            raise_to = min(state["min_raise_to"] * 3, stack + state["your_bet_this_street"])
            return {"action": "raise", "amount": raise_to}

        if strength == "medium" and position > 0.5:
            if owed < pot * 0.2:
                return {"action": "call"}

        if state["can_check"]:
            return {"action": "check"}

        return {"action": "fold"}

    # Postflop: position-based value betting
    if state["can_check"]:
        if position > 0.6 and random.random() < 0.4:
            bet = min(int(pot * 0.6), stack + state["your_bet_this_street"])
            bet = max(bet, state["min_raise_to"])
            return {"action": "raise", "amount": bet}
        return {"action": "check"}

    # Calling threshold tightens in early position
    threshold = 0.25 if position > 0.5 else 0.15
    if pot > 0 and owed / pot <= threshold:
        return {"action": "call"}

    return {"action": "fold"}
