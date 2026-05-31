#!/usr/bin/env python3
"""Train a compact abstract regret table and export it as NPZ.

This is intentionally a small, portable trainer. It is not full no-limit
multiway CFR; it is an abstract sampled regret trainer that learns policies
for bucketed Fullhouse game states. The output format is designed for the
runtime bot: string keys plus action probabilities in a `.npz`.
"""

from __future__ import annotations

import argparse
import json
import os
import random
from dataclasses import dataclass, field

import eval7
import numpy as np

from abstraction import ACTION_NAMES, abstract_state_key, legal_action_mask


RANKS = "23456789TJQKA"
SUITS = "cdhs"
DECK = [r + s for r in RANKS for s in SUITS]
STREETS = ("preflop", "flop", "turn", "river")


@dataclass
class RegretTable:
    keys: list[str] = field(default_factory=list)
    index: dict[str, int] = field(default_factory=dict)
    regrets: list[np.ndarray] = field(default_factory=list)
    strategy_sum: list[np.ndarray] = field(default_factory=list)
    visits: list[int] = field(default_factory=list)

    def row(self, key: str) -> int:
        if key not in self.index:
            self.index[key] = len(self.keys)
            self.keys.append(key)
            self.regrets.append(np.zeros(len(ACTION_NAMES), dtype=np.float64))
            self.strategy_sum.append(np.zeros(len(ACTION_NAMES), dtype=np.float64))
            self.visits.append(0)
        return self.index[key]

    def strategy(self, idx: int, mask: np.ndarray, epsilon: float) -> np.ndarray:
        positive = np.maximum(self.regrets[idx], 0.0) * mask
        total = positive.sum()
        if total <= 0:
            probs = mask / mask.sum()
        else:
            probs = positive / total
        if epsilon > 0:
            probs = (1.0 - epsilon) * probs + epsilon * (mask / mask.sum())
        return probs

    def update(self, key: str, utilities: np.ndarray, mask: np.ndarray, epsilon: float) -> None:
        idx = self.row(key)
        probs = self.strategy(idx, mask, epsilon)
        node_value = float(np.dot(probs, utilities))
        regret_delta = (utilities - node_value) * mask
        self.regrets[idx] += regret_delta
        self.strategy_sum[idx] += probs
        self.visits[idx] += 1

    def average_strategy_array(self) -> np.ndarray:
        out = np.zeros((len(self.keys), len(ACTION_NAMES)), dtype=np.float32)
        for i, total in enumerate(self.strategy_sum):
            weight = total.sum()
            if weight <= 0:
                out[i] = np.ones(len(ACTION_NAMES), dtype=np.float32) / len(ACTION_NAMES)
            else:
                out[i] = (total / weight).astype(np.float32)
        return out


def make_deck_without(cards: list[str]) -> list[str]:
    known = set(cards)
    return [c for c in DECK if c not in known]


def draw_cards(rng: random.Random, deck: list[str], count: int) -> list[str]:
    return rng.sample(deck, count)


def sample_state(rng: random.Random) -> dict:
    n_players = rng.randint(2, 6)
    street = rng.choices(STREETS, weights=[0.45, 0.25, 0.17, 0.13], k=1)[0]
    board_count = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}[street]

    cards = draw_cards(rng, DECK, 2 + board_count)
    your_cards = cards[:2]
    board = cards[2:]

    pot = rng.choice([150, 250, 400, 700, 1200, 2200, 4000, 7000])
    stack = rng.choice([800, 1500, 3000, 6000, 10000, 14000])
    current_bet = rng.choice([0, 100, 200, 500, 1000, 2000])
    owed = 0 if rng.random() < 0.42 else min(stack, rng.choice([100, 200, 400, 800, 1500, 3000]))
    can_check = owed == 0
    your_bet = rng.choice([0, 50, 100, 300, 700])
    min_raise = max(current_bet + 100, owed + your_bet + 100)

    players = []
    for seat in range(n_players):
        players.append(
            {
                "seat": seat,
                "bot_id": f"train_{seat}",
                "stack": rng.choice([800, 2500, 6000, 10000]),
                "state": "active",
                "is_folded": False,
                "is_all_in": False,
                "bet_this_street": current_bet if seat != 0 and owed else 0,
                "hole_cards": None,
            }
        )

    log = []
    for _ in range(rng.randint(0, 5)):
        log.append(
            {
                "seat": rng.randrange(n_players),
                "action": rng.choice(["check", "call", "raise", "fold"]),
                "amount": rng.choice([0, 100, 300, 700, 1500]),
            }
        )

    seat = rng.randrange(n_players)
    return {
        "type": "action_request",
        "hand_id": "train",
        "street": street,
        "seat_to_act": seat,
        "pot": pot,
        "community_cards": board,
        "current_bet": current_bet,
        "min_raise_to": min_raise,
        "amount_owed": owed,
        "can_check": can_check,
        "your_cards": your_cards,
        "your_stack": stack,
        "your_bet_this_street": your_bet,
        "players": players,
        "action_log": log,
    }


def estimate_equity(state: dict, rng: random.Random, samples: int) -> float:
    your_cards = list(state["your_cards"])
    board = list(state["community_cards"])
    opponents = max(1, min(len(state.get("players", [])) - 1, 5))
    needed_board = 5 - len(board)
    wins = 0.0
    known = your_cards + board

    for _ in range(samples):
        deck = make_deck_without(known)
        drawn = draw_cards(rng, deck, opponents * 2 + needed_board)
        opp_hands = [drawn[i * 2 : i * 2 + 2] for i in range(opponents)]
        runout = board + drawn[opponents * 2 :]
        our_score = eval7.evaluate([eval7.Card(c) for c in your_cards + runout])
        opp_scores = [eval7.evaluate([eval7.Card(c) for c in hand + runout]) for hand in opp_hands]
        best_opp = max(opp_scores)
        if our_score > best_opp:
            wins += 1.0
        elif our_score == best_opp:
            ties = 1 + sum(1 for score in opp_scores if score == our_score)
            wins += 1.0 / ties

    return wins / max(samples, 1)


def utilities_for_state(state: dict, rng: random.Random, equity_samples: int) -> np.ndarray:
    equity = estimate_equity(state, rng, equity_samples)
    pot = float(max(state.get("pot", 0), 1))
    owed = float(state.get("amount_owed", 0))
    stack = float(max(state.get("your_stack", 0), 1))
    players = max(len(state.get("players", [])), 2)

    pressure = owed / pot if pot else 0.0
    fold_equity_base = max(0.02, min(0.62, 0.18 + 0.10 * pressure + 0.04 * (players - 2)))
    raise_half_cost = min(stack, max(owed, pot * 0.5))
    raise_pot_cost = min(stack, max(owed, pot))

    fold_u = -owed
    call_u = equity * (pot + owed) - (1.0 - equity) * owed
    half_u = fold_equity_base * pot + (1.0 - fold_equity_base) * (
        equity * (pot + raise_half_cost) - (1.0 - equity) * raise_half_cost
    )
    pot_u = min(0.78, fold_equity_base + 0.12) * pot + (1.0 - min(0.78, fold_equity_base + 0.12)) * (
        equity * (pot + raise_pot_cost) - (1.0 - equity) * raise_pot_cost
    )
    allin_fe = min(0.88, fold_equity_base + 0.22)
    allin_u = allin_fe * pot + (1.0 - allin_fe) * (equity * (pot + stack) - (1.0 - equity) * stack)

    if state.get("can_check", False):
        call_u = equity * pot
    return np.array([fold_u, call_u, half_u, pot_u, allin_u], dtype=np.float64)


def save_npz(path: str, table: RegretTable, metadata: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.savez_compressed(
        path,
        keys=np.array(table.keys, dtype=np.str_),
        strategy=table.average_strategy_array(),
        visits=np.array(table.visits, dtype=np.int32),
        action_names=np.array(ACTION_NAMES, dtype=np.str_),
        metadata=np.array(json.dumps(metadata), dtype=np.str_),
    )


def train(args: argparse.Namespace) -> RegretTable:
    rng = random.Random(args.seed)
    np.random.seed(args.seed % (2**32))
    table = RegretTable()

    for i in range(1, args.iterations + 1):
        epsilon = max(args.min_epsilon, args.epsilon * (1.0 - i / max(args.iterations, 1)))
        for _ in range(args.samples):
            state = sample_state(rng)
            key = abstract_state_key(state)
            mask = np.array(legal_action_mask(state), dtype=np.float64)
            utilities = utilities_for_state(state, rng, args.equity_samples)
            table.update(key, utilities, mask, epsilon)

        if args.log_interval and i % args.log_interval == 0:
            print(f"{i:,}/{args.iterations:,} iterations - {len(table.keys):,} infosets")

    return table


def main() -> None:
    parser = argparse.ArgumentParser(description="Train abstract CFR table for bots/cfr/bot.py")
    parser.add_argument("--iterations", "-i", type=int, default=5000)
    parser.add_argument("--samples", "-s", type=int, default=4)
    parser.add_argument("--equity-samples", type=int, default=24)
    parser.add_argument("--epsilon", type=float, default=0.08)
    parser.add_argument("--min-epsilon", type=float, default=0.01)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output", "-o", default="data/cfr_strategy.npz")
    parser.add_argument("--log-interval", type=int, default=500)
    args = parser.parse_args()

    table = train(args)
    metadata = {
        "iterations": args.iterations,
        "samples": args.samples,
        "equity_samples": args.equity_samples,
        "seed": args.seed,
        "infosets": len(table.keys),
        "action_names": ACTION_NAMES,
    }
    save_npz(args.output, table, metadata)
    print(f"saved {len(table.keys):,} infosets to {args.output}")


if __name__ == "__main__":
    main()
