"""Abstract CFR bot.

Submit this as a directory/zip with:
  bot.py
  data/cfr_strategy.npz
"""

import os
import random
from collections import Counter

import eval7
import numpy as np


BOT_NAME = "Abstract CFR"

RANKS = "23456789TJQKA"
RANK_VALUE = {rank: i for i, rank in enumerate(RANKS)}
STREETS = {"preflop": 0, "flop": 1, "turn": 2, "river": 3}
ACTION_NAMES = ("fold", "check_call", "raise_half", "raise_pot", "all_in")
DATA_DIR = os.environ.get("BOT_DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
STRATEGY_PATH = os.path.join(DATA_DIR, "cfr_strategy.npz")


def _load_strategy():
    try:
        bundle = np.load(STRATEGY_PATH, allow_pickle=False)
        keys = [str(k) for k in bundle["keys"]]
        strategy = bundle["strategy"].astype(np.float64)
        return {key: strategy[i] for i, key in enumerate(keys)}
    except Exception:
        return {}


STRATEGY = _load_strategy()


def _card_value(card):
    return RANK_VALUE[card[0]]


def _preflop_bucket(cards):
    a, b = sorted(cards, key=_card_value, reverse=True)
    ra, rb = _card_value(a), _card_value(b)
    if ra == rb:
        if ra >= RANK_VALUE["Q"]:
            return "pair_premium"
        if ra >= RANK_VALUE["8"]:
            return "pair_medium"
        return "pair_low"

    suited = "s" if a[1] == b[1] else "o"
    gap = abs(ra - rb)
    if ra == RANK_VALUE["A"] and rb <= RANK_VALUE["5"]:
        gap = min(gap, RANK_VALUE["5"] - rb)
    conn = "conn" if gap <= 1 else "gap" if gap <= 3 else "wide"

    if ra >= RANK_VALUE["T"] and rb >= RANK_VALUE["T"]:
        tier = "broadway"
    elif ra == RANK_VALUE["A"]:
        tier = "ace"
    elif ra == RANK_VALUE["K"]:
        tier = "king"
    elif ra >= RANK_VALUE["9"]:
        tier = "medium"
    else:
        tier = "low"
    return tier + "_" + suited + "_" + conn


def _made_hand_bucket(cards, board):
    if not board:
        return _preflop_bucket(cards)

    hand_value = eval7.evaluate([eval7.Card(c) for c in cards + board])
    hand_type = hand_value >> 24
    ranks = sorted((_card_value(c) for c in cards), reverse=True)

    if hand_type >= 8:
        return "straight_flush"
    if hand_type >= 7:
        return "quads"
    if hand_type >= 6:
        return "full_house"
    if hand_type >= 5:
        return "flush"
    if hand_type >= 4:
        return "straight"
    if hand_type >= 3:
        return "trips"
    if hand_type >= 2:
        return "two_pair"
    if hand_type >= 1:
        high = max(ranks)
        if high >= RANK_VALUE["Q"]:
            return "pair_high"
        if high >= RANK_VALUE["8"]:
            return "pair_mid"
        return "pair_low"
    if ranks[0] >= RANK_VALUE["A"]:
        return "high_ace"
    if ranks[0] >= RANK_VALUE["Q"]:
        return "high_broadway"
    if ranks[0] >= RANK_VALUE["8"]:
        return "high_mid"
    return "high_low"


def _board_texture(board):
    if not board:
        return "empty"

    values = sorted({_card_value(c) for c in board})
    counts = Counter(c[0] for c in board)
    suits = Counter(c[1] for c in board)
    highest = max(values)

    high = "hi" if highest >= RANK_VALUE["Q"] else "mid" if highest >= RANK_VALUE["8"] else "low"
    paired = "trips" if max(counts.values()) >= 3 else "paired" if max(counts.values()) == 2 else "unpaired"
    flushy = "fourflush" if max(suits.values()) >= 4 else "flushy" if max(suits.values()) == 3 else "rainbow"

    connected = "dry"
    if len(values) >= 2:
        gaps = [b - a for a, b in zip(values, values[1:])]
        if any(g <= 1 for g in gaps):
            connected = "connected"
        elif any(g <= 2 for g in gaps):
            connected = "semi"
    return high + "_" + paired + "_" + flushy + "_" + connected


def _position_bucket(state):
    players = state.get("players", [])
    n = max(len(players), 1)
    seat = int(state.get("seat_to_act", 0))
    ratio = seat / max(n - 1, 1)
    if ratio >= 0.67:
        return "late"
    if ratio >= 0.34:
        return "middle"
    return "early"


def _pressure_bucket(state):
    pot = max(int(state.get("pot", 0)), 1)
    owed = int(state.get("amount_owed", 0))
    stack = max(int(state.get("your_stack", 0)), 1)
    spr = stack / pot
    if owed == 0:
        pressure = "free"
    elif owed / pot <= 0.25:
        pressure = "cheap"
    elif owed / pot <= 0.75:
        pressure = "priced"
    else:
        pressure = "expensive"

    if spr <= 1.5:
        depth = "short"
    elif spr <= 5:
        depth = "medium"
    else:
        depth = "deep"
    return pressure + "_" + depth


def _history_bucket(state):
    log = state.get("action_log", [])[-6:]
    aggressive = sum(1 for item in log if item.get("action") in {"raise", "all_in"})
    passive = sum(1 for item in log if item.get("action") in {"call", "check"})
    if aggressive >= 2:
        return "multi_raise"
    if aggressive == 1:
        return "raised"
    if passive >= 2:
        return "passive"
    return "quiet"


def _abstract_key(state):
    cards = list(state.get("your_cards", []))
    board = list(state.get("community_cards", []))
    return "|".join(
        [
            str(STREETS.get(state.get("street", "preflop"), 0)),
            _made_hand_bucket(cards, board),
            _board_texture(board),
            _position_bucket(state),
            _pressure_bucket(state),
            _history_bucket(state),
            str(min(len(state.get("players", [])), 9)),
        ]
    )


def _legal_mask(state):
    owed = int(state.get("amount_owed", 0))
    stack = int(state.get("your_stack", 0))
    min_raise = int(state.get("min_raise_to", 0))
    current = int(state.get("your_bet_this_street", 0))
    max_total = stack + current
    can_raise = stack > owed and max_total >= min_raise
    return np.array([owed > 0, True, can_raise, can_raise, stack > 0], dtype=np.float64)


def _action_to_response(action_index, state):
    name = ACTION_NAMES[action_index]
    if name == "fold":
        return {"action": "fold"}
    if name == "check_call":
        return {"action": "check"} if state.get("can_check", False) else {"action": "call"}
    if name == "all_in":
        return {"action": "all_in"}

    pot = max(int(state.get("pot", 0)), 1)
    stack = int(state.get("your_stack", 0))
    current = int(state.get("your_bet_this_street", 0))
    min_raise = int(state.get("min_raise_to", 0))
    max_total = stack + current
    frac = 0.5 if name == "raise_half" else 1.0
    amount = max(min_raise, current + int(pot * frac))
    amount = min(amount, max_total)
    if amount < min_raise:
        return {"action": "check"} if state.get("can_check", False) else {"action": "call"}
    return {"action": "raise", "amount": amount}


def _fallback(state):
    cards = state.get("your_cards", [])
    owed = int(state.get("amount_owed", 0))
    pot = max(int(state.get("pot", 0)), 1)
    stack = int(state.get("your_stack", 0))
    ranks = [c[0] for c in cards]

    if len(ranks) == 2 and ranks[0] == ranks[1] and ranks[0] in "AKQJT":
        current = int(state.get("your_bet_this_street", 0))
        max_total = stack + current
        min_raise = int(state.get("min_raise_to", 0))
        if max_total >= min_raise and stack > int(state.get("amount_owed", 0)):
            amount = min(max_total, max(min_raise, pot * 2))
            return {"action": "raise", "amount": amount}
        return {"action": "call"} if not state.get("can_check", False) else {"action": "check"}
    if state.get("can_check", False):
        return {"action": "check"}
    if owed <= pot * 0.25:
        return {"action": "call"}
    return {"action": "fold"}


def decide(state):
    if state.get("type") == "warmup":
        return {"action": "check"}

    key = _abstract_key(state)
    probs = STRATEGY.get(key)
    if probs is None:
        return _fallback(state)

    mask = _legal_mask(state)
    probs = probs * mask
    total = float(probs.sum())
    if total <= 0:
        return _fallback(state)
    probs = probs / total
    action_index = int(np.random.choice(len(ACTION_NAMES), p=probs))
    return _action_to_response(action_index, state)
