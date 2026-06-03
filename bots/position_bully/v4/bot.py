"""Position Bully v2: steals in position, but slows down multiway and vs raisers."""
import json
import os
import random

BOT_NAME = "Position Bully v4 GTO Preflop"
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
    table_preflop_action = _gto_table_preflop_action(state)
    if table_preflop_action:
        return table_preflop_action
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
