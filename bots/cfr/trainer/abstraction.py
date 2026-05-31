"""Shared abstractions for the CFR trainer.

The submitted bot keeps a small copy of these routines inline so it can be
uploaded as a single `bot.py` plus `data/`.
"""

from __future__ import annotations

from collections import Counter

import eval7


RANKS = "23456789TJQKA"
RANK_VALUE = {rank: i for i, rank in enumerate(RANKS)}
SUITS = "cdhs"
STREETS = {"preflop": 0, "flop": 1, "turn": 2, "river": 3}
ACTION_NAMES = ("fold", "check_call", "raise_half", "raise_pot", "all_in")


def rank(card: str) -> str:
    return card[0]


def suit(card: str) -> str:
    return card[1]


def card_value(card: str) -> int:
    return RANK_VALUE[rank(card)]


def preflop_bucket(cards: list[str]) -> str:
    a, b = sorted(cards, key=card_value, reverse=True)
    ra, rb = card_value(a), card_value(b)
    if ra == rb:
        if ra >= RANK_VALUE["Q"]:
            return "pair_premium"
        if ra >= RANK_VALUE["8"]:
            return "pair_medium"
        return "pair_low"

    suited = "s" if suit(a) == suit(b) else "o"
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
    return f"{tier}_{suited}_{conn}"


def made_hand_bucket(cards: list[str], board: list[str]) -> str:
    if not board:
        return preflop_bucket(cards)

    hand_value = eval7.evaluate([eval7.Card(c) for c in cards + board])
    # eval7 packs hand class into the high bits. These thresholds are stable
    # enough for bucketing even though exact values are evaluator internals.
    hand_type = hand_value >> 24
    ranks = sorted((card_value(c) for c in cards), reverse=True)

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


def board_texture(board: list[str]) -> str:
    if not board:
        return "empty"

    values = sorted({card_value(c) for c in board})
    counts = Counter(rank(c) for c in board)
    suits = Counter(suit(c) for c in board)
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

    return f"{high}_{paired}_{flushy}_{connected}"


def position_bucket(state: dict) -> str:
    players = state.get("players", [])
    n = max(len(players), 1)
    seat = int(state.get("seat_to_act", 0))
    ratio = seat / max(n - 1, 1)
    if ratio >= 0.67:
        return "late"
    if ratio >= 0.34:
        return "middle"
    return "early"


def pressure_bucket(state: dict) -> str:
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
    return f"{pressure}_{depth}"


def action_history_bucket(state: dict) -> str:
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


def abstract_state_key(state: dict) -> str:
    cards = list(state.get("your_cards", []))
    board = list(state.get("community_cards", []))
    street = state.get("street", "preflop")
    return "|".join(
        [
            str(STREETS.get(street, 0)),
            made_hand_bucket(cards, board),
            board_texture(board),
            position_bucket(state),
            pressure_bucket(state),
            action_history_bucket(state),
            str(min(len(state.get("players", [])), 9)),
        ]
    )


def legal_action_mask(state: dict) -> list[bool]:
    owed = int(state.get("amount_owed", 0))
    can_check = bool(state.get("can_check", owed == 0))
    stack = int(state.get("your_stack", 0))
    min_raise = int(state.get("min_raise_to", 0))
    current = int(state.get("your_bet_this_street", 0))
    max_total = stack + current
    can_raise = stack > owed and max_total >= min_raise
    return [
        owed > 0,
        True,
        can_raise,
        can_raise,
        stack > 0,
    ]


def action_to_response(action_index: int, state: dict) -> dict:
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
        return {"action": "call"} if not state.get("can_check", False) else {"action": "check"}
    return {"action": "raise", "amount": amount}
