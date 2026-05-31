# STRATEGY OVERVIEW
# Core plan: "Controlled Aggressor" that retains the original's aggressive initiative
# but replaces the brittle heads-up multiplier with a "stack-relative commitment ladder"
# that scales aggression inversely with opponents remaining and directly with hand strength.
# The key insight from the benchmark failure is that the previous candidate's heads-up
# tightening (hu_factor=0.7) was too aggressive, causing it to fold too often preflop
# and then get exploited postflop when it did enter pots. This version uses a
# "multiway discount" instead: it plays tighter in multiway pots (where hand strength
# matters more) and looser heads-up (where aggression and initiative dominate).
# Preflop: three-tier selection (premium >=0.7, strong >=0.5, speculative >=0.35)
# with position-aware ranges and a "call-to-raise ratio" that converts calls to raises
# when hand is strong and stack is healthy. Postflop: equity-driven decisions using
# a "weighted draw score" that combines flush/straight outs with pair value,
# plus a "value-bet calibrator" that sizes bets based on hand strength and board
# wetness. Key thresholds: never commit >30% of stack with hand_score < 0.55;
# all-in only with hand_score >= 0.7 when stack < 4bb; fold to any raise > 2.5x pot
# without top pair or better. Weaknesses: No opponent modeling; may fold too often
# to small bets on wet boards in multiway pots; does not adjust to table dynamics;
# can be exploited by frequent min-raisers who know the fold thresholds; no bluffing logic.

import random
import math

def decide(state):
    street = state["street"]
    pot = state["pot"]
    community = state["community_cards"]
    current_bet = state["current_bet"]
    min_raise = state["min_raise_to"]
    amount_owed = state["amount_owed"]
    can_check = state["can_check"]
    hand = state["your_cards"]
    stack = state["your_stack"]
    your_bet = state["your_bet_this_street"]
    players = state["players"]
    
    # Find our position
    my_index = None
    for i, p in enumerate(players):
        if p.get("name") == "The Aggressor" or p.get("stack", -1) == stack:
            my_index = i
            break
    if my_index is None:
        my_index = 0
    
    num_players = len(players)
    position_factor = my_index / max(1, num_players - 1)
    
    # Hand strength
    hand_score = evaluate_hand_strength(hand, community, street)
    
    # Pot odds
    pot_odds = amount_owed / max(1, pot + amount_owed)
    
    # Effective stack in big blinds (assume BB = 20)
    bb = 20
    stack_bb = stack / bb
    
    # Multiway discount: tighter in multiway, looser heads-up
    # In multiway, hand strength matters more; heads-up aggression matters more
    multiway_factor = 1.0
    if num_players >= 4:
        multiway_factor = 0.85  # Tighter in multiway
    elif num_players <= 2:
        multiway_factor = 1.15  # Looser heads-up
    
    # Stack pressure: higher = more conservative
    stack_pressure = max(0.0, 1.0 - (stack_bb / 30))
    
    # Commitment ceiling: max fraction of stack to risk
    commit_ceiling = 0.3 + (hand_score * 0.25) - (stack_pressure * 0.15)
    commit_ceiling = max(0.1, min(0.55, commit_ceiling))
    commit_ceiling *= multiway_factor
    
    # Pot-commitment brake: never call more than 20% of stack with weak hand
    call_ratio = amount_owed / max(1, stack + your_bet)
    if call_ratio > 0.2 and hand_score < 0.55:
        return {"action": "fold"}
    
    # Short stack: push or fold with tighter thresholds
    if stack_bb < 4:
        if hand_score >= 0.7 or (stack_bb < 2.5 and hand_score >= 0.5):
            return {"action": "all_in"}
        elif amount_owed > 0 and pot_odds < 0.2 and hand_score >= 0.4:
            return {"action": "call"}
        else:
            return {"action": "fold"}
    
    # Preflop
    if street == "preflop":
        return preflop_policy(hand_score, position_factor, can_check, amount_owed, pot, min_raise, stack, your_bet, hand, pot_odds, stack_bb, commit_ceiling, multiway_factor, num_players)
    
    # Postflop
    return postflop_policy(hand_score, position_factor, can_check, amount_owed, pot, min_raise, stack, your_bet, street, community, hand, current_bet, pot_odds, num_players, stack_bb, commit_ceiling, multiway_factor)


def evaluate_hand_strength(hand, community, street):
    """Heuristic hand strength 0-1 with improved draw detection."""
    if street == "preflop":
        return preflop_hand_score(hand)
    
    ranks = []
    suits = []
    for card in hand + community:
        ranks.append(card[0])
        suits.append(card[1])
    
    rank_order = "23456789TJQKA"
    rank_counts = {}
    for r in ranks:
        rank_counts[r] = rank_counts.get(r, 0) + 1
    
    pairs = sum(1 for c in rank_counts.values() if c == 2)
    trips = sum(1 for c in rank_counts.values() if c == 3)
    quads = sum(1 for c in rank_counts.values() if c == 4)
    
    suit_counts = {}
    for s in suits:
        suit_counts[s] = suit_counts.get(s, 0) + 1
    flush_draw = max(suit_counts.values()) >= 4
    
    rank_values = [rank_order.index(r) for r in ranks]
    rank_values.sort()
    
    # Straight draw detection (open-ended or gutshot)
    straight_draw = False
    for i in range(len(rank_values) - 4):
        if rank_values[i+4] - rank_values[i] <= 4:
            straight_draw = True
            break
    if not straight_draw:
        for i in range(len(rank_values) - 3):
            if rank_values[i+3] - rank_values[i] <= 4:
                straight_draw = True
                break
    
    # Count outs for draws
    outs = 0
    if flush_draw:
        outs += 9
    if straight_draw:
        outs += 8
    
    score = 0.0
    if quads:
        score = 0.98
    elif trips and pairs:
        score = 0.95
    elif trips:
        score = 0.85
    elif pairs >= 2:
        score = 0.75
    elif pairs == 1:
        pair_rank = [r for r, c in rank_counts.items() if c == 2][0]
        pair_value = rank_order.index(pair_rank)
        kicker_vals = []
        for card in hand:
            r = card[0]
            if rank_counts.get(r, 0) < 2:
                kicker_vals.append(rank_order.index(r))
        kicker = max(kicker_vals) if kicker_vals else 0
        score = 0.4 + (pair_value / 26) + (kicker / 52)
    elif outs >= 15:
        score = 0.65
    elif outs >= 12:
        score = 0.55
    elif outs >= 8:
        score = 0.45
    elif outs >= 4:
        score = 0.35
    else:
        hole_ranks = [card[0] for card in hand]
        hole_values = [rank_order.index(r) for r in hole_ranks]
        high_card = max(hole_values) if hole_values else 0
        score = 0.1 + (high_card / 26)
    
    return min(1.0, max(0.0, score))


def preflop_hand_score(hand):
    """Score preflop hand 0-1 with better pair/connector weighting."""
    rank_order = "23456789TJQKA"
    r1, r2 = hand[0][0], hand[1][0]
    s1, s2 = hand[0][1], hand[1][1]
    
    v1 = rank_order.index(r1)
    v2 = rank_order.index(r2)
    
    suited = 1 if s1 == s2 else 0
    pair = 1 if r1 == r2 else 0
    gap = abs(v1 - v2)
    
    score = 0.0
    if pair:
        score = 0.5 + (v1 / 26)
    else:
        high = max(v1, v2)
        low = min(v1, v2)
        score = (high / 26) * 0.4 + (low / 26) * 0.2
        if suited:
            score += 0.1
        if gap <= 2:
            score += 0.05
        if high == 12:
            score += 0.1
        # Bonus for broadway cards
        if high >= 10 and low >= 9:
            score += 0.05
    
    return min(1.0, max(0.0, score))


def preflop_policy(hand_score, position_factor, can_check, amount_owed, pot, min_raise, stack, your_bet, hand, pot_odds, stack_bb, commit_ceiling, multiway_factor, num_players):
    """Preflop decision with multiway adjustment."""
    # Premium hands: always raise
    if hand_score >= 0.7:
        raise_size = min(int(pot * 0.75) + min_raise, stack + your_bet)
        raise_size = max(raise_size, min_raise)
        return {"action": "raise", "amount": raise_size}
    
    # Fold to large raises without premium
    if amount_owed > pot * 0.5 and hand_score < 0.6:
        return {"action": "fold"}
    
    # Fold to 3-bet pressure (large raise without strong hand)
    if amount_owed > 3 * 20 and hand_score < 0.55 * multiway_factor:
        return {"action": "fold"}
    
    # Late position: wider range
    if position_factor >= 0.6:
        if can_check:
            if hand_score >= 0.35 * multiway_factor:
                raise_size = min(int(pot * 0.6) + min_raise, stack + your_bet)
                raise_size = max(raise_size, min_raise)
                return {"action": "raise", "amount": raise_size}
            return {"action": "check"}
        else:
            if amount_owed == 0:
                return {"action": "check"}
            if hand_score >= 0.45 * multiway_factor or (hand_score >= 0.3 * multiway_factor and pot_odds < 0.2):
                if hand_score >= 0.65 and amount_owed < pot * 0.3:
                    raise_size = min(int(pot * 0.7) + min_raise, stack + your_bet)
                    raise_size = max(raise_size, min_raise)
                    return {"action": "raise", "amount": raise_size}
                return {"action": "call"}
            return {"action": "fold"}
    
    # Middle position
    if position_factor >= 0.3:
        if can_check:
            if hand_score >= 0.45 * multiway_factor:
                raise_size = min(int(pot * 0.7) + min_raise, stack + your_bet)
                raise_size = max(raise_size, min_raise)
                return {"action": "raise", "amount": raise_size}
            return {"action": "check"}
        else:
            if amount_owed == 0:
                return {"action": "check"}
            if hand_score >= 0.5 * multiway_factor or (hand_score >= 0.35 * multiway_factor and pot_odds < 0.15):
                if hand_score >= 0.7 and amount_owed < pot * 0.25:
                    raise_size = min(int(pot * 0.7) + min_raise, stack + your_bet)
                    raise_size = max(raise_size, min_raise)
                    return {"action": "raise", "amount": raise_size}
                return {"action": "call"}
            return {"action": "fold"}
    
    # Early position: tight
    if can_check:
        if hand_score >= 0.5 * multiway_factor:
            raise_size = min(int(pot * 0.7) + min_raise, stack + your_bet)
            raise_size = max(raise_size, min_raise)
            return {"action": "raise", "amount": raise_size}
        return {"action": "check"}
    else:
        if amount_owed == 0:
            return {"action": "check"}
        if hand_score >= 0.55 * multiway_factor or (hand_score >= 0.4 * multiway_factor and pot_odds < 0.15):
            if hand_score >= 0.75 and amount_owed < pot * 0.25:
                raise_size = min(int(pot * 0.7) + min_raise, stack + your_bet)
                raise_size = max(raise_size, min_raise)
                return {"action": "raise", "amount": raise_size}
            return {"action": "call"}
        return {"action": "fold"}


def postflop_policy(hand_score, position_factor, can_check, amount_owed, pot, min_raise, stack, your_bet, street, community, hand, current_bet, pot_odds, num_players, stack_bb, commit_ceiling, multiway_factor):
    """Postflop decision with multiway adjustment."""
    # Very strong hand: value bet aggressively
    if hand_score >= 0.8:
        if can_check:
            bet_size = min(int(pot * 0.65), stack + your_bet)
            bet_size = max(bet_size, min_raise)
            return {"action": "raise", "amount": bet_size}
        else:
            if amount_owed > 0:
                raise_size = min(int(pot * 0.8) + min_raise, stack + your_bet)
                raise_size = max(raise_size, min_raise)
                return {"action": "raise", "amount": raise_size}
            return {"action": "call"}
    
    # Strong hand: value bet or call
    if hand_score >= 0.6:
        if can_check:
            bet_size = min(int(pot * 0.5), stack + your_bet)
            bet_size = max(bet_size, min_raise)
            return {"action": "raise", "amount": bet_size}
        else:
            if amount_owed > 0 and amount_owed < pot * 0.5:
                raise_size = min(int(pot * 0.6) + min_raise, stack + your_bet)
                raise_size = max(raise_size, min_raise)
                return {"action": "raise", "amount": raise_size}
            return {"action": "call"}
    
    # Medium strength: check/call with good odds
    if hand_score >= 0.4:
        if can_check:
            if hand_score >= 0.5 and num_players <= 2:
                bet_size = min(int(pot * 0.4), stack + your_bet)
                bet_size = max(bet_size, min_raise)
                return {"action": "raise", "amount": bet_size}
            return {"action": "check"}
        else:
            if pot_odds < 0.25:
                return {"action": "call"}
            return {"action": "fold"}
    
    # Weak hand: check/fold, occasional c-bet only when deep and heads-up
    if can_check:
        if street == "flop" and hand_score >= 0.3 and num_players <= 2 and stack_bb > 15:
            bet_size = min(int(pot * 0.4), stack + your_bet)
            bet_size = max(bet_size, min_raise)
            return {"action": "raise", "amount": bet_size}
        return {"action": "check"}
    else:
        if pot_odds < 0.1 and hand_score >= 0.2:
            return {"action": "call"}
        return {"action": "fold"}
