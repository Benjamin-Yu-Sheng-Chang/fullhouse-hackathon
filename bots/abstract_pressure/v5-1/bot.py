"""Abstract Pressure v5-1: v5 pressure with Tier-2 style opponent reads."""
import random
from collections import Counter

import eval7

BOT_NAME = "Abstract Pressure v5-1"
BIG_BLIND = 100
RANKS = "23456789TJQKA"
RANK_VALUE = {rank: value for value, rank in enumerate(RANKS, 2)}

CONFIG = {
    "equity_iterations": 75,
    "open_early": 59,
    "open_middle": 51,
    "open_late": 41,
    "continue_early": 63,
    "continue_late": 51,
    "value_equity": 0.54,
    "raise_equity": 0.66,
    "call_margin": 0.035,
    "multiway_margin": 0.030,
    "semi_bluff_frequency": 0.30,
    "thin_value_pair": True,
    "pressure_frequency": 0.28,
    "river_extra_margin": 0.035,
    "large_bet_extra_margin": 0.060,
    "danger_extra_margin": 0.020,
    "model_min_actions": 16,
}


def _cards(cards):
    return [eval7.Card(card) for card in cards]


def _live_opponents(state):
    me = state["seat_to_act"]
    return sum(1 for p in state["players"] if p["seat"] != me and not p["is_folded"] and p["state"] != "busted")


def _hero_bot_id(state):
    seat = state["seat_to_act"]
    for player in state["players"]:
        if player["seat"] == seat:
            return player.get("bot_id")
    return None


def _live_opp_ids(state):
    seat = state["seat_to_act"]
    ids = []
    for player in state["players"]:
        if player["seat"] == seat or player.get("is_folded") or player.get("state") == "busted":
            continue
        bot_id = player.get("bot_id")
        if bot_id:
            ids.append(bot_id)
    return ids


def _opponent_archetype(actions, vpip, aggression, fold_freq, allin_freq):
    if actions < CONFIG["model_min_actions"]:
        return "unknown"
    if vpip <= 0.24 and fold_freq >= 0.55:
        return "nit"
    if allin_freq >= 0.10 or (vpip >= 0.40 and aggression >= 2.0) or (vpip >= 0.55 and aggression >= 1.3):
        return "maniac"
    if aggression <= 0.5 and vpip >= 0.25:
        return "station"
    return "tag"


def _opponent_model(state):
    hero = _hero_bot_id(state)
    acc = {}
    seen_hand = set()
    for entry in state.get("match_action_log", [])[-300:]:
        bot_id = entry.get("bot_id")
        if not bot_id or bot_id == hero:
            continue
        action = entry.get("action")
        stats = acc.setdefault(bot_id, {"hands": 0, "vpip": 0, "pfr": 0, "decisions": 0, "raises": 0, "calls": 0, "folds": 0, "allins": 0})
        hand_key = (entry.get("hand_num"), bot_id)
        if hand_key not in seen_hand:
            seen_hand.add(hand_key)
            stats["hands"] += 1
            if action in ("call", "raise", "all_in"):
                stats["vpip"] += 1
            if action in ("raise", "all_in"):
                stats["pfr"] += 1
        stats["decisions"] += 1
        if action == "raise":
            stats["raises"] += 1
        elif action == "call":
            stats["calls"] += 1
        elif action == "fold":
            stats["folds"] += 1
        elif action == "all_in":
            stats["allins"] += 1

    model = {}
    for bot_id, stats in acc.items():
        hands = max(1, stats["hands"])
        decisions = max(1, stats["decisions"])
        vpip = stats["vpip"] / hands
        aggression = (stats["raises"] + stats["allins"]) / max(1, stats["calls"])
        fold_freq = stats["folds"] / decisions
        allin_freq = stats["allins"] / decisions
        model[bot_id] = {
            "vpip": vpip,
            "pfr": stats["pfr"] / hands,
            "aggression": aggression,
            "fold_freq": fold_freq,
            "allin_freq": allin_freq,
            "archetype": _opponent_archetype(stats["decisions"], vpip, aggression, fold_freq, allin_freq),
        }
    return model


def _model_profile(model, bot_id):
    return model.get(bot_id, {"vpip": 0.32, "pfr": 0.18, "aggression": 1.2, "fold_freq": 0.45, "allin_freq": 0.0, "archetype": "unknown"})


def _last_aggressor_id(state):
    hero_seat = state["seat_to_act"]
    for action in reversed(state.get("action_log", [])):
        if action.get("seat") == hero_seat:
            continue
        if action.get("action") in ("raise", "all_in"):
            return action.get("bot_id")
    return None


def _live_archetypes(state, model):
    return [_model_profile(model, bot_id)["archetype"] for bot_id in _live_opp_ids(state)]


def _fold_equity(state, model):
    ids = _live_opp_ids(state)
    if not ids:
        return 0.45
    fold_equity = 1.0
    for bot_id in ids:
        profile = _model_profile(model, bot_id)
        archetype = profile["archetype"]
        if archetype == "station":
            fold_equity = min(fold_equity, 0.05)
        elif archetype == "maniac":
            fold_equity = min(fold_equity, 0.15)
        elif archetype == "unknown":
            fold_equity = min(fold_equity, 0.45)
        else:
            fold_equity = min(fold_equity, max(0.0, min(0.90, profile["fold_freq"])))
    return fold_equity


def _steal_bonus(state, model):
    ids = _live_opp_ids(state)
    if not ids:
        return 0
    fold_freq = 1.0
    for bot_id in ids:
        profile = _model_profile(model, bot_id)
        if profile["archetype"] in ("unknown", "station", "maniac"):
            return 0
        fold_freq = min(fold_freq, profile["fold_freq"])
    return int(max(0, min(5, round((fold_freq - 0.45) * 12))))


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
    model = features["model"]
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
    if pos == "late":
        open_line -= _steal_bonus(state, model)
    if opponents >= 5:
        open_line += 3

    if premium:
        return {"action": "raise", "amount": min(max(state["min_raise_to"], int(max(pot, state["current_bet"]) * 1.2)), cap)}
    if unopened and score >= open_line:
        return {"action": "raise", "amount": _bet_to(state, 1.05 if pos == "late" else 1.35)}
    speculative = pair or suited and gap <= 3 and high >= 9
    broadwayish = high >= 12 and low >= 9 or high == 14 and low >= 5
    if owed and score >= continue_line and owed <= pot * 0.38:
        return {"action": "call"}
    if owed and score >= continue_line - 9 and owed <= pot * 0.18:
        return {"action": "call"}
    if owed and speculative and owed <= pot * 0.24:
        return {"action": "call"}
    if owed and broadwayish and owed <= pot * 0.18:
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
    archetypes = features["archetypes"]
    bettor = features["bettor_archetype"]
    fold_equity = features["fold_equity"]
    pos = features["position"]
    price = owed / (pot + owed) if owed else 0
    station_field = "station" in archetypes
    loose_field = any(archetype in ("station", "maniac") for archetype in archetypes)
    tight_field = bool(archetypes) and all(archetype in ("nit", "tag") for archetype in archetypes)
    margin = CONFIG["call_margin"] + CONFIG["multiway_margin"] * max(0, opponents - 1)
    if texture["wet"] and opponents >= 3:
        margin += 0.025
    if profile["label"] == "aggressive":
        margin += 0.035
    if bettor == "maniac":
        margin -= 0.050
    elif bettor == "station":
        margin -= 0.020
    elif bettor == "nit":
        margin += 0.045
    if texture["board_danger"] and texture["made"] != "strong":
        margin += CONFIG["danger_extra_margin"]
    if state["street"] == "river" and texture["made"] != "strong":
        margin += CONFIG["river_extra_margin"]
    if owed > state["your_stack"] * 0.34:
        margin += CONFIG["large_bet_extra_margin"]
    if owed and owed <= pot * 0.24 and state["street"] != "river":
        margin -= 0.030

    if owed == 0:
        value_line = CONFIG["value_equity"]
        value_fraction = 0.75 if not texture["board_danger"] else 0.58
        if station_field:
            value_line -= 0.08
            value_fraction = 0.50
        elif loose_field:
            value_line -= 0.05
            value_fraction = 0.58
        elif tight_field:
            value_line += 0.02
        if texture["made"] == "strong" or equity >= value_line:
            return {"action": "raise", "amount": _bet_to(state, value_fraction)}
        if CONFIG["thin_value_pair"] and pos == "late" and texture["made"] in ("medium", "weak_pair") and opponents <= 2 and equity >= value_line - 0.10:
            return {"action": "raise", "amount": _bet_to(state, 0.45 if not station_field else 0.38)}
        semi_bluff_frequency = CONFIG["semi_bluff_frequency"]
        if loose_field:
            semi_bluff_frequency = 0.0
        elif tight_field:
            semi_bluff_frequency = min(0.42, semi_bluff_frequency + 0.12)
        if pos == "late" and opponents <= 2 and (texture["flush_draw"] or texture["straight_draw"]) and fold_equity * pot > (1 - fold_equity) * max(1, int(pot * 0.52)) and random.random() < semi_bluff_frequency:
            return {"action": "raise", "amount": _bet_to(state, 0.52)}
        pressure_frequency = CONFIG["pressure_frequency"]
        if loose_field:
            pressure_frequency = 0.0
        elif tight_field:
            pressure_frequency = min(0.40, pressure_frequency + 0.10)
        if pos == "late" and profile["label"] == "foldy" and equity > 0.40 and fold_equity * pot > (1 - fold_equity) * max(1, int(pot * 0.42)) and random.random() < pressure_frequency:
            return {"action": "raise", "amount": _bet_to(state, 0.42)}
        return {"action": "check"}

    if texture["made"] == "strong" and owed <= state["your_stack"] * 0.45:
        return {"action": "raise", "amount": _bet_to(state, 0.95)}
    raise_line = CONFIG["raise_equity"]
    if station_field:
        raise_line -= 0.07
    elif loose_field:
        raise_line -= 0.04
    if bettor == "maniac":
        raise_line += 0.08
    elif bettor == "nit":
        raise_line += 0.03
    if equity >= raise_line and owed <= state["your_stack"] * 0.35:
        return {"action": "raise", "amount": _bet_to(state, 0.78)}
    draw_raise_frequency = CONFIG["semi_bluff_frequency"]
    if loose_field:
        draw_raise_frequency = 0.0
    elif tight_field:
        draw_raise_frequency = min(0.40, draw_raise_frequency + 0.10)
    if (texture["flush_draw"] or texture["straight_draw"]) and pos == "late" and owed <= pot * 0.20 and fold_equity * pot > (1 - fold_equity) * max(1, owed) and random.random() < draw_raise_frequency:
        return {"action": "raise", "amount": _bet_to(state, 0.58)}
    if owed <= pot * 0.24 and state["street"] != "river" and texture["made"] in ("weak_pair", "medium"):
        return {"action": "call"}
    if owed <= pot * 0.18 and state["street"] != "river" and (texture["flush_draw"] or texture["straight_draw"]):
        return {"action": "call"}
    if equity >= price + margin:
        return {"action": "call"}
    return {"action": "fold"}


def decide(state):
    if state.get("type") == "warmup":
        return {"action": "check"}
    opponents = max(1, _live_opponents(state))
    model = _opponent_model(state)
    features = {
        "opponents": opponents,
        "position": _position_label(state),
        "profile": _table_profile(state),
        "model": model,
        "score": _preflop_score(state["your_cards"]),
        "hand": _preflop_features(state["your_cards"]),
    }
    if state["street"] == "preflop":
        return _preflop(state, features)
    features["texture"] = _texture(state)
    features["equity"] = _equity(state, opponents)
    features["archetypes"] = _live_archetypes(state, model)
    features["bettor_archetype"] = _model_profile(model, _last_aggressor_id(state))["archetype"]
    features["fold_equity"] = _fold_equity(state, model)
    return _postflop(state, features)
