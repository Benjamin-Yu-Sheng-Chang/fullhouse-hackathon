"""Abstract Pressure v5: v1 pressure with expensive-call discipline."""
import random
from collections import Counter

import eval7

BOT_NAME = "Abstract Pressure v5"
BIG_BLIND = 100
RANKS = "23456789TJQKA"
RANK_VALUE = {rank: value for value, rank in enumerate(RANKS, 2)}

CONFIG = {
    "equity_iterations": 75,
    "open_early": 61,
    "open_middle": 54,
    "open_late": 45,
    "continue_early": 68,
    "continue_late": 57,
    "value_equity": 0.56,
    "raise_equity": 0.68,
    "call_margin": 0.06,
    "multiway_margin": 0.045,
    "semi_bluff_frequency": 0.34,
    "thin_value_pair": True,
    "pressure_frequency": 0.28,
    "river_extra_margin": 0.05,
    "large_bet_extra_margin": 0.08,
    "danger_extra_margin": 0.025,
}


def _cards(cards):
    return [eval7.Card(card) for card in cards]


def _live_opponents(state):
    me = state["seat_to_act"]
    return sum(1 for p in state["players"] if p["seat"] != me and not p["is_folded"] and p["state"] != "busted")


def _position_factor(state):
    return state["seat_to_act"] / max(1, len(state["players"]) - 1)


def _position_label(state):
    pos = _position_factor(state)
    if pos >= 0.67:
        return "late"
    if pos >= 0.34:
        return "middle"
    return "early"


def _table_profile(state):
    recent = state.get("match_action_log", [])[-300:]
    count = max(1, len(recent))
    raises = sum(1 for a in recent if a.get("action") in ("raise", "all_in"))
    folds = sum(1 for a in recent if a.get("action") == "fold")
    calls = sum(1 for a in recent if a.get("action") == "call")
    raise_rate = raises / count
    fold_rate = folds / count
    call_rate = calls / count
    if raise_rate > 0.25:
        label = "aggressive"
    elif fold_rate > 0.34:
        label = "foldy"
    elif call_rate > 0.35:
        label = "sticky"
    else:
        label = "balanced"
    return {"raise_rate": raise_rate, "fold_rate": fold_rate, "call_rate": call_rate, "label": label}


def _preflop_features(cards):
    high, low = sorted((RANK_VALUE[c[0]] for c in cards), reverse=True)
    suited = cards[0][1] == cards[1][1]
    pair = high == low
    gap = high - low
    return high, low, suited, pair, gap


def _preflop_score(cards):
    high, low, suited, pair, gap = _preflop_features(cards)
    if pair:
        return 58 + high * 3
    broadway = int(high >= 10) + int(low >= 10)
    wheel = high == 14 and low <= 5
    return high * 3 + low * 1.45 + broadway * 3 + (8 if high == 14 else 0) + (5 if suited else 0) + (3 if gap <= 1 or wheel else 0) - max(0, gap - 2) * 2


def _straight_info(values):
    vals = set(values)
    if 14 in vals:
        vals.add(1)
    made = False
    draw = False
    for start in range(1, 11):
        window = set(range(start, start + 5))
        hit = len(vals & window)
        made = made or hit >= 5
        draw = draw or hit >= 4
    return made, draw


def _texture(state):
    board = state["community_cards"]
    hole = state["your_cards"]
    all_cards = board + hole
    rank_counts = Counter(c[0] for c in all_cards)
    board_rank_counts = Counter(c[0] for c in board)
    suit_counts = Counter(c[1] for c in all_cards)
    board_suits = Counter(c[1] for c in board)
    values = [RANK_VALUE[c[0]] for c in all_cards]
    board_values = [RANK_VALUE[c[0]] for c in board]
    made_straight, straight_draw = _straight_info(values)
    board_straight, board_straight_draw = _straight_info(board_values) if board else (False, False)
    flush_count = max(suit_counts.values()) if suit_counts else 0
    board_flush_count = max(board_suits.values()) if board_suits else 0
    pair_ranks = [RANK_VALUE[r] for r, count in rank_counts.items() if count >= 2 and any(c[0] == r for c in hole)]
    trips = any(count >= 3 and any(c[0] == r for c in hole) for r, count in rank_counts.items())
    two_pair = len(pair_ranks) >= 2
    pair_rank = max(pair_ranks) if pair_ranks else 0
    board_high = max(board_values) if board_values else 0
    overpair = len(hole) == 2 and hole[0][0] == hole[1][0] and RANK_VALUE[hole[0][0]] > board_high
    top_pair = pair_rank and board_high and pair_rank == board_high
    made_flush = flush_count >= 5 and any(suit_counts[c[1]] >= 5 for c in hole)
    flush_draw = len(board) >= 3 and flush_count >= 4 and any(suit_counts[c[1]] >= 4 for c in hole)
    board_danger = board_flush_count >= 4 or board_straight or board_straight_draw

    if made_flush or made_straight or trips or two_pair:
        made = "strong"
    elif overpair or top_pair or pair_rank >= 10:
        made = "medium"
    elif pair_rank:
        made = "weak_pair"
    else:
        made = "air"
    wet = flush_draw or straight_draw or board_danger
    return {
        "made": made,
        "pair_rank": pair_rank,
        "overpair": overpair,
        "top_pair": bool(top_pair),
        "flush_draw": flush_draw,
        "straight_draw": straight_draw and not made_straight,
        "made_flush": made_flush,
        "made_straight": made_straight,
        "board_danger": board_danger,
        "wet": wet,
        "paired_board": any(count >= 2 for count in board_rank_counts.values()),
    }


def _equity(state, opponents):
    hole = _cards(state["your_cards"])
    board = _cards(state["community_cards"])
    used = set(hole + board)
    deck = [card for card in eval7.Deck() if card not in used]
    need = 5 - len(board)
    wins = ties = 0
    for _ in range(CONFIG["equity_iterations"]):
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
            wins += 0 if tied else 1
            ties += 1 if tied else 0
    return (wins + 0.5 * ties) / CONFIG["equity_iterations"]


def _bet_to(state, fraction):
    cap = state["your_stack"] + state["your_bet_this_street"]
    amount = max(state["min_raise_to"], int(max(BIG_BLIND, state["pot"]) * fraction))
    return min(amount, cap)


def _preflop(state, features):
    score = features["score"]
    high, low, suited, pair, gap = features["hand"]
    pos = features["position"]
    profile = features["profile"]
    opponents = features["opponents"]
    owed = state["amount_owed"]
    pot = max(1, state["pot"])
    cap = state["your_stack"] + state["your_bet_this_street"]
    unopened = state["current_bet"] <= BIG_BLIND
    premium = pair and high >= 10 or high == 14 and low >= 12 or suited and high >= 13 and low >= 12

    open_line = CONFIG["open_late"] if pos == "late" else CONFIG["open_middle"] if pos == "middle" else CONFIG["open_early"]
    continue_line = CONFIG["continue_late"] if pos == "late" else CONFIG["continue_early"]
    if profile["label"] == "aggressive":
        open_line += 5
        continue_line += 4
    if profile["label"] == "foldy" and pos == "late":
        open_line -= 8
    if opponents >= 5:
        open_line += 3

    if premium:
        return {"action": "raise", "amount": min(max(state["min_raise_to"], int(max(pot, state["current_bet"]) * 1.2)), cap)}
    if unopened and score >= open_line:
        return {"action": "raise", "amount": _bet_to(state, 1.05 if pos == "late" else 1.35)}
    speculative = pair or suited and gap <= 3 and high >= 9
    if owed and score >= continue_line and owed <= pot * 0.30:
        return {"action": "call"}
    if owed and speculative and owed <= pot * 0.16:
        return {"action": "call"}
    if state["can_check"]:
        return {"action": "check"}
    return {"action": "fold"}


def _postflop(state, features):
    owed = state["amount_owed"]
    pot = max(1, state["pot"])
    equity = features["equity"]
    texture = features["texture"]
    opponents = features["opponents"]
    profile = features["profile"]
    pos = features["position"]
    price = owed / (pot + owed) if owed else 0
    margin = CONFIG["call_margin"] + CONFIG["multiway_margin"] * max(0, opponents - 1)
    if texture["wet"] and opponents >= 3:
        margin += 0.025
    if profile["label"] == "aggressive":
        margin += 0.035
    if texture["board_danger"] and texture["made"] != "strong":
        margin += CONFIG["danger_extra_margin"]
    if state["street"] == "river" and texture["made"] != "strong":
        margin += CONFIG["river_extra_margin"]
    if owed > state["your_stack"] * 0.34:
        margin += CONFIG["large_bet_extra_margin"]

    if owed == 0:
        if texture["made"] == "strong" or equity >= CONFIG["value_equity"]:
            return {"action": "raise", "amount": _bet_to(state, 0.75 if not texture["board_danger"] else 0.58)}
        if CONFIG["thin_value_pair"] and pos == "late" and texture["made"] == "medium" and opponents <= 2:
            return {"action": "raise", "amount": _bet_to(state, 0.45)}
        if pos == "late" and opponents <= 2 and (texture["flush_draw"] or texture["straight_draw"]) and random.random() < CONFIG["semi_bluff_frequency"]:
            return {"action": "raise", "amount": _bet_to(state, 0.52)}
        if pos == "late" and profile["label"] == "foldy" and equity > 0.42 and random.random() < CONFIG["pressure_frequency"]:
            return {"action": "raise", "amount": _bet_to(state, 0.42)}
        return {"action": "check"}

    if texture["made"] == "strong" and owed <= state["your_stack"] * 0.45:
        return {"action": "raise", "amount": _bet_to(state, 0.95)}
    if equity >= CONFIG["raise_equity"] and owed <= state["your_stack"] * 0.35:
        return {"action": "raise", "amount": _bet_to(state, 0.78)}
    if (texture["flush_draw"] or texture["straight_draw"]) and pos == "late" and owed <= pot * 0.20 and random.random() < CONFIG["semi_bluff_frequency"]:
        return {"action": "raise", "amount": _bet_to(state, 0.58)}
    if equity >= price + margin:
        return {"action": "call"}
    return {"action": "fold"}


def decide(state):
    if state.get("type") == "warmup":
        return {"action": "check"}
    opponents = max(1, _live_opponents(state))
    features = {
        "opponents": opponents,
        "position": _position_label(state),
        "profile": _table_profile(state),
        "score": _preflop_score(state["your_cards"]),
        "hand": _preflop_features(state["your_cards"]),
    }
    if state["street"] == "preflop":
        return _preflop(state, features)
    features["texture"] = _texture(state)
    features["equity"] = _equity(state, opponents)
    return _postflop(state, features)
