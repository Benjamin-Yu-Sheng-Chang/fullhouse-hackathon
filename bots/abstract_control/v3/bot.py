"""
STRATEGY OVERVIEW
Abstract Control v3 keeps v2's stack/profile discipline and adds a small
package of missing upside tools from the value, pressure, trap-steal, and
position-bully families: very-late steal widening, dry-board thin value,
cheap draw pressure, and stronger value extraction when the table profile is
sticky or aggressive. It still avoids postflop all-ins unless short-stacked.
"""
import random
from collections import Counter

import eval7

BOT_NAME = "Abstract Control v3"
BIG_BLIND = 100
RANKS = "23456789TJQKA"
RANK_VALUE = {rank: value for value, rank in enumerate(RANKS, 2)}

CONFIG = {
    "equity_iterations": 105,
    "open_late": 54,
    "open_middle": 60,
    "open_early": 66,
    "continue_late": 64,
    "continue_early": 72,
    "premium_pair_rank": 10,
    "value_equity": 0.63,
    "raise_equity": 0.77,
    "call_margin": 0.055,
    "multiway_margin": 0.035,
    "danger_margin": 0.035,
    "steal_adjustment": 4,
    "aggro_tighten": 5,
    "short_stack_bb": 18,
    "short_stack_jam_score": 76,
    "short_stack_call_score": 72,
    "value_bet_fraction": 0.58,
    "danger_value_fraction": 0.44,
    "raise_bet_fraction": 0.74,
    "pressure_bet_fraction": 0.42,
    "late_pressure_frequency": 0.08,
    "draw_pressure_frequency": 0.08,
    "thin_value_equity": 0.50,
    "thin_value_frequency": 0.24,
    "very_late_steal_adjustment": 5,
    "sticky_value_bonus": 0.08,
    "cheap_draw_raise_frequency": 0.08,
    "heads_up_air_pressure_frequency": 0.06,
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


def _very_late(state):
    return _position_factor(state) >= 0.78


def _table_profile(state):
    recent = state.get("match_action_log", [])[-300:]
    count = max(1, len(recent))
    raises = sum(1 for action in recent if action.get("action") in ("raise", "all_in"))
    folds = sum(1 for action in recent if action.get("action") == "fold")
    calls = sum(1 for action in recent if action.get("action") == "call")
    raise_rate = raises / count
    fold_rate = folds / count
    call_rate = calls / count
    if raise_rate > 0.24:
        label = "aggressive"
    elif fold_rate > 0.34:
        label = "foldy"
    elif call_rate > 0.34:
        label = "sticky"
    else:
        label = "balanced"
    return {"raise_rate": raise_rate, "fold_rate": fold_rate, "call_rate": call_rate, "label": label}


def _hand_features(cards):
    high, low = sorted((RANK_VALUE[card[0]] for card in cards), reverse=True)
    suited = cards[0][1] == cards[1][1]
    pair = high == low
    gap = high - low
    return high, low, suited, pair, gap


def _preflop_score(cards):
    high, low, suited, pair, gap = _hand_features(cards)
    if pair:
        return 59 + high * 3
    broadway = int(high >= 10) + int(low >= 10)
    wheel = high == 14 and low <= 5
    connector = 4 if gap <= 1 or wheel else 2 if gap == 2 else 0
    return high * 3 + low * 1.45 + broadway * 3 + (8 if high == 14 else 0) + (5 if suited else 0) + connector - max(0, gap - 2) * 2


def _straight_info(values):
    vals = set(values)
    if 14 in vals:
        vals.add(1)
    made = False
    draw = False
    for start in range(1, 11):
        hit = len(vals & set(range(start, start + 5)))
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
    board_suit_counts = Counter(c[1] for c in board)
    values = [RANK_VALUE[c[0]] for c in all_cards]
    board_values = [RANK_VALUE[c[0]] for c in board]

    made_straight, straightish = _straight_info(values)
    board_straight, board_straightish = _straight_info(board_values) if board else (False, False)
    flush_count = max(suit_counts.values()) if suit_counts else 0
    board_flush_count = max(board_suit_counts.values()) if board_suit_counts else 0

    pair_ranks = [RANK_VALUE[r] for r, count in rank_counts.items() if count >= 2 and any(c[0] == r for c in hole)]
    pair_rank = max(pair_ranks) if pair_ranks else 0
    board_high = max(board_values) if board_values else 0
    trips = any(count >= 3 and any(c[0] == r for c in hole) for r, count in rank_counts.items())
    two_pair = len(pair_ranks) >= 2
    overpair = len(hole) == 2 and hole[0][0] == hole[1][0] and RANK_VALUE[hole[0][0]] > board_high
    top_pair = bool(pair_rank and board_high and pair_rank == board_high)
    made_flush = flush_count >= 5 and any(suit_counts[c[1]] >= 5 for c in hole)
    flush_draw = len(board) >= 3 and flush_count >= 4 and any(suit_counts[c[1]] >= 4 for c in hole)
    straight_draw = straightish and not made_straight
    board_danger = board_flush_count >= 4 or board_straight or board_straightish
    paired_board = any(count >= 2 for count in board_rank_counts.values())

    if made_flush or made_straight or trips or two_pair:
        made = "strong"
    elif overpair or top_pair or pair_rank >= 10:
        made = "medium"
    elif pair_rank:
        made = "weak_pair"
    else:
        made = "air"

    return {
        "made": made,
        "pair_rank": pair_rank,
        "overpair": overpair,
        "top_pair": top_pair,
        "flush_draw": flush_draw,
        "straight_draw": straight_draw,
        "made_flush": made_flush,
        "made_straight": made_straight,
        "board_danger": board_danger,
        "wet": flush_draw or straight_draw or board_danger,
        "paired_board": paired_board,
    }


def _stack_mode(state):
    bb = state["your_stack"] / BIG_BLIND
    if bb <= CONFIG["short_stack_bb"]:
        mode = "short"
    elif bb >= 80:
        mode = "deep"
    else:
        mode = "normal"
    spr = state["your_stack"] / max(1, state["pot"])
    return {"bb": bb, "mode": mode, "spr": spr}


def _pot_price(state):
    owed = state["amount_owed"]
    pot = max(1, state["pot"])
    price = owed / (pot + owed) if owed else 0.0
    if owed == 0:
        label = "free"
    elif owed <= pot * 0.18:
        label = "cheap"
    elif owed <= pot * 0.45:
        label = "medium"
    else:
        label = "expensive"
    return {"price": price, "label": label}


def _street_aggression(state):
    return sum(1 for action in state["action_log"] if action.get("action") in ("raise", "all_in"))


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


def _bet_to(state, pot_fraction):
    cap = state["your_stack"] + state["your_bet_this_street"]
    raise_to = max(state["min_raise_to"], int(max(BIG_BLIND, state["pot"]) * pot_fraction))
    return min(raise_to, cap)


def _short_stack_policy(state, features):
    owed = state["amount_owed"]
    score = features["hand_score"]
    high, low, suited, pair, _gap = features["hand_features"]
    premium = pair and high >= CONFIG["premium_pair_rank"] or high == 14 and low >= 12 or suited and high >= 13 and low >= 12

    if state["street"] == "preflop" and (premium or score >= CONFIG["short_stack_jam_score"]):
        return {"action": "all_in"}
    if owed and score >= CONFIG["short_stack_call_score"] and owed <= state["your_stack"] * 0.32:
        return {"action": "call"}
    if state["can_check"]:
        return {"action": "check"}
    return {"action": "fold"}


def _preflop_policy(state, features):
    owed = state["amount_owed"]
    pot = max(1, state["pot"])
    score = features["hand_score"]
    high, low, suited, pair, gap = features["hand_features"]
    profile = features["profile"]
    position = features["position"]
    cap = state["your_stack"] + state["your_bet_this_street"]
    unopened = state["current_bet"] <= BIG_BLIND

    premium = pair and high >= CONFIG["premium_pair_rank"]
    premium = premium or high == 14 and low >= 12
    premium = premium or suited and high >= 13 and low >= 12

    if position == "late":
        open_line = CONFIG["open_late"]
        continue_line = CONFIG["continue_late"]
    elif position == "middle":
        open_line = CONFIG["open_middle"]
        continue_line = (CONFIG["continue_late"] + CONFIG["continue_early"]) / 2
    else:
        open_line = CONFIG["open_early"]
        continue_line = CONFIG["continue_early"]

    if profile["label"] == "foldy" and position == "late":
        open_line -= CONFIG["steal_adjustment"]
    if _very_late(state) and profile["label"] != "aggressive" and features["opponents"] <= 3:
        open_line -= CONFIG["very_late_steal_adjustment"]
    if profile["label"] == "aggressive":
        open_line += CONFIG["aggro_tighten"]
        continue_line += CONFIG["aggro_tighten"] * 0.6
    if features["opponents"] >= 5:
        open_line += 3

    if premium:
        multiplier = 3.6 if profile["label"] == "aggressive" else 3.0 if profile["label"] == "sticky" else 2.8
        size = max(state["min_raise_to"], int(max(state["current_bet"], BIG_BLIND) * multiplier), int(pot * 0.9))
        return {"action": "raise", "amount": min(size, cap)}
    if unopened and score >= open_line:
        fraction = 0.95 if position == "late" else 1.25
        return {"action": "raise", "amount": _bet_to(state, fraction)}

    speculative = pair or suited and gap <= 3 and high >= 9
    max_call = 0.30 if profile["label"] != "aggressive" else 0.22
    if owed and score >= continue_line and owed <= pot * max_call:
        return {"action": "call"}
    if owed and speculative and owed <= pot * 0.16:
        return {"action": "call"}
    if state["can_check"]:
        return {"action": "check"}
    return {"action": "fold"}


def _postflop_policy(state, features):
    owed = state["amount_owed"]
    pot = max(1, state["pot"])
    equity = features["equity"]
    profile = features["profile"]
    texture = features["texture"]
    price = features["pot_price"]["price"]
    position = features["position"]
    opponents = features["opponents"]
    stack = features["stack"]
    margin = CONFIG["call_margin"] + CONFIG["multiway_margin"] * max(0, opponents - 1)

    if profile["label"] == "aggressive":
        margin += CONFIG["aggro_tighten"] / 100.0
    if texture["wet"] and opponents >= 3:
        margin += CONFIG["danger_margin"]
    if texture["board_danger"] and texture["made"] != "strong":
        margin += 0.035

    if owed == 0:
        if texture["made"] == "strong":
            fraction = CONFIG["danger_value_fraction"] if texture["board_danger"] else CONFIG["value_bet_fraction"]
            if profile["label"] in ("sticky", "aggressive") and not texture["board_danger"]:
                fraction += CONFIG["sticky_value_bonus"]
            return {"action": "raise", "amount": _bet_to(state, fraction)}
        if equity >= CONFIG["value_equity"] and not (texture["board_danger"] and opponents >= 3):
            return {"action": "raise", "amount": _bet_to(state, 0.44 if texture["board_danger"] else 0.56)}
        thin_value_spot = (
            position == "late"
            and opponents <= 2
            and features["street_aggression"] == 0
            and not texture["wet"]
            and texture["made"] == "medium"
            and equity >= CONFIG["thin_value_equity"]
        )
        if thin_value_spot and random.random() < CONFIG["thin_value_frequency"]:
            return {"action": "raise", "amount": _bet_to(state, 0.38)}
        can_pressure = position == "late" and opponents <= 2 and features["street_aggression"] == 0 and stack["spr"] >= 2.2
        if can_pressure and profile["label"] == "foldy" and equity >= 0.43 and random.random() < CONFIG["late_pressure_frequency"]:
            return {"action": "raise", "amount": _bet_to(state, CONFIG["pressure_bet_fraction"])}
        if can_pressure and (texture["flush_draw"] or texture["straight_draw"]) and equity >= 0.40 and random.random() < CONFIG["draw_pressure_frequency"]:
            return {"action": "raise", "amount": _bet_to(state, CONFIG["pressure_bet_fraction"])}
        if can_pressure and opponents == 1 and profile["label"] == "foldy" and equity >= 0.36:
            if random.random() < CONFIG["heads_up_air_pressure_frequency"]:
                return {"action": "raise", "amount": _bet_to(state, 0.36)}
        return {"action": "check"}

    if texture["made"] == "strong" and owed <= state["your_stack"] * 0.40:
        fraction = CONFIG["raise_bet_fraction"] if not texture["board_danger"] else 0.62
        if profile["label"] == "sticky" and not texture["board_danger"]:
            fraction += 0.08
        return {"action": "raise", "amount": _bet_to(state, fraction)}
    if equity >= CONFIG["raise_equity"] and owed <= state["your_stack"] * 0.38 and opponents <= 3 and not texture["board_danger"]:
        return {"action": "raise", "amount": _bet_to(state, 0.74)}
    if (texture["flush_draw"] or texture["straight_draw"]) and position == "late" and opponents <= 2 and owed <= pot * 0.18:
        if random.random() < CONFIG["draw_pressure_frequency"]:
            return {"action": "raise", "amount": _bet_to(state, CONFIG["pressure_bet_fraction"])}
    if (texture["flush_draw"] or texture["straight_draw"]) and position == "late" and opponents == 1 and owed <= pot * 0.12:
        if random.random() < CONFIG["cheap_draw_raise_frequency"]:
            return {"action": "raise", "amount": _bet_to(state, 0.48)}
    if equity >= price + margin:
        return {"action": "call"}
    return {"action": "fold"}


def _extract_features(state):
    opponents = max(1, _live_opponents(state))
    texture = _texture(state) if state["street"] != "preflop" else None
    return {
        "opponents": opponents,
        "position": _position_label(state),
        "position_factor": _position_factor(state),
        "stack": _stack_mode(state),
        "profile": _table_profile(state),
        "texture": texture,
        "hand_score": _preflop_score(state["your_cards"]),
        "hand_features": _hand_features(state["your_cards"]),
        "pot_price": _pot_price(state),
        "street_aggression": _street_aggression(state),
        "equity": None if state["street"] == "preflop" else _equity(state, opponents),
    }


def decide(state):
    if state.get("type") == "warmup":
        return {"action": "check"}
    features = _extract_features(state)
    if features["stack"]["mode"] == "short":
        return _short_stack_policy(state, features)
    if state["street"] == "preflop":
        return _preflop_policy(state, features)
    return _postflop_policy(state, features)
