# STRATEGY OVERVIEW
# Core plan: A "stack-preserving equity" bot that fixes the catastrophic busts by adding
# a strict "survival equity" floor that overrides all other logic when a call would commit
# a significant portion of stack. The key insight from the failed candidate is that the
# multiway penalty was too weak and the postflop aggression thresholds were too loose when
# facing bets from multiple opponents. This revision introduces a "commitment threshold"
# that scales with opponents: the fraction of stack that triggers survival mode decreases
# as opponents increase (0.25 / sqrt(opponents)). Additionally, preflop thresholds now use
# a multiplicative penalty (1.0 + 0.10 * (opponents - 2)) instead of additive, making
# multiway folds much more aggressive. Postflop, a new "bet-size adjusted equity" check
# requires equity to exceed (bet/pot) * 0.8 + 0.3 for any call, preventing thin calls
# that bleed chips. The short-stack policy now uses a dynamic threshold that increases
# with opponents, only shoving premium hands multiway.
#
# Key thresholds:
# - Preflop: open thresholds 0.55/0.40/0.25 multiplied by (1.0 + 0.10*(opponents-2)) when opponents>2
# - Commitment threshold: 0.25 / sqrt(opponents) - fold if call commits more than this unless equity >= 0.80
# - Postflop call equity: must exceed (bet/pot)*0.8 + 0.3 (scales with bet size)
# - Value bet: equity >= 0.75 on dry, >= 0.85 on wet, multiplied by (1.0 + 0.08*(opponents-1))
# - Short stack (< 10 BB): shove only if score >= 0.65 or equity >= 0.70, fold otherwise
# - All-in defense: never call all-in unless equity >= 0.80
# - Bluff: removed entirely to avoid spew
# - Pot odds calls: only when equity exceeds required equity by at least 0.15
#
# Expected weaknesses:
# - May fold too many marginal hands, losing some value
# - Weak against heads-up aggression where opponents exploit tightness
# - No bluffing range, predictable
# - May miss thin value bets on dry boards

import random
import math
import eval7

def decide(state):
    street = state["street"]
    pot = max(1, state["pot"])
    community = state["community_cards"]
    current_bet = state["current_bet"]
    min_raise = state["min_raise_to"]
    owed = state["amount_owed"]
    can_check = state["can_check"]
    hole = state["your_cards"]
    stack = state["your_stack"]
    my_bet = state["your_bet_this_street"]
    players = state["players"]
    
    active_players = sum(1 for p in players if p.get("stack", 0) > 0 or p.get("status") == "active")
    opponents = max(1, active_players - 1)
    
    my_index = None
    for i, p in enumerate(players):
        if p.get("name") == "you" or (p.get("stack") == stack and p.get("bet") == my_bet):
            my_index = i
            break
    if my_index is None:
        my_index = 0
    position_factor = my_index / max(1, len(players) - 1) if len(players) > 1 else 0.5
    
    bb = max(1, pot / 10)
    stack_bb = stack / bb if bb > 0 else 20
    
    if stack_bb < 10:
        return short_stack_policy(hole, community, stack, owed, pot, can_check, opponents, position_factor)
    
    if street == "preflop":
        return preflop_policy(hole, owed, pot, stack, can_check, position_factor, opponents, min_raise, my_bet, players)
    
    return postflop_policy(hole, community, owed, pot, stack, can_check, opponents, position_factor, street, min_raise, my_bet, current_bet)

def hand_score(cards):
    if not cards or len(cards) < 2:
        return 0.0
    
    ranks = "23456789TJQKA"
    r1 = ranks.index(cards[0][0]) if cards[0][0] in ranks else -1
    r2 = ranks.index(cards[1][0]) if cards[1][0] in ranks else -1
    
    if r1 < 0 or r2 < 0:
        return 0.0
    
    suited = cards[0][1] == cards[1][1]
    pair = r1 == r2
    gap = abs(r1 - r2)
    
    high = max(r1, r2)
    low = min(r1, r2)
    base = high / 12.0
    
    if pair:
        base = max(base, 0.5 + high * 0.04)
    
    if suited:
        base += 0.08
    
    if gap == 1:
        base += 0.06
    elif gap == 2:
        base += 0.03
    
    if high == 12:
        base += 0.10
    
    if high >= 8 and low >= 8:
        base += 0.05
    
    return min(1.0, max(0.0, base))

def estimate_equity(hole, community, iterations=200):
    if not hole or len(hole) < 2:
        return 0.5
    
    try:
        hole_cards = [eval7.Card(c) for c in hole]
        community_cards = [eval7.Card(c) for c in community] if community else []
        
        if len(community_cards) == 5:
            hand = hole_cards + community_cards
            score = eval7.evaluate(hand)
            return 1.0 if score > 0 else 0.5
        
        deck = eval7.Deck()
        for c in hole_cards + community_cards:
            deck.cards.remove(c)
        
        wins = 0
        total = 0
        for _ in range(iterations):
            deck.shuffle()
            num_needed = 5 - len(community_cards)
            opp_hole = deck.deal(2)
            remaining = deck.deal(num_needed)
            
            our_hand = hole_cards + community_cards + remaining
            opp_hand = opp_hole + community_cards + remaining
            
            our_score = eval7.evaluate(our_hand)
            opp_score = eval7.evaluate(opp_hand)
            
            if our_score > opp_score:
                wins += 1
            elif our_score == opp_score:
                wins += 0.5
            total += 1
            
            deck.cards.extend(opp_hole + remaining)
        
        return wins / total if total > 0 else 0.5
    except:
        return 0.5

def is_made_hand(hole, community):
    if not hole or len(hole) < 2:
        return False
    if not community:
        return False
    
    try:
        cards = [eval7.Card(c) for c in hole + community]
        score = eval7.evaluate(cards)
        ranks = [c[0] for c in hole + community]
        hole_ranks = [c[0] for c in hole]
        for r in hole_ranks:
            if ranks.count(r) >= 2:
                return True
        return False
    except:
        return False

def has_draw(hole, community):
    if not hole or len(hole) < 2:
        return False
    if not community or len(community) < 3:
        return False
    
    try:
        all_cards = hole + community
        suits = [c[1] for c in all_cards]
        for s in "shdc":
            if suits.count(s) >= 4:
                return True
        
        ranks = sorted([eval7.Card(c).rank for c in all_cards])
        for i in range(len(ranks) - 3):
            if ranks[i+3] - ranks[i] <= 3 and len(set(ranks[i:i+4])) == 4:
                return True
        return False
    except:
        return False

def is_dry_board(community):
    if not community or len(community) < 3:
        return True
    
    try:
        suits = [c[1] for c in community]
        for s in "shdc":
            if suits.count(s) >= 3:
                return False
        
        ranks = sorted([eval7.Card(c).rank for c in community])
        for i in range(len(ranks) - 1):
            if ranks[i+1] - ranks[i] <= 2:
                return False
        return True
    except:
        return True

def has_overcards(hole, community):
    if not hole or len(hole) < 2:
        return False
    if not community:
        return True
    
    try:
        board_ranks = [eval7.Card(c).rank for c in community]
        hole_ranks = [eval7.Card(c).rank for c in hole]
        max_board = max(board_ranks) if board_ranks else 0
        return max(hole_ranks) > max_board
    except:
        return True

def short_stack_policy(hole, community, stack, owed, pot, can_check, opponents, position_factor):
    if can_check:
        return {"action": "check"}
    
    score = hand_score(hole)
    
    equity = 0.5
    if community:
        equity = estimate_equity(hole, community, 100)
    
    # Dynamic threshold: increase with opponents to avoid multiway shove disasters
    multiway_factor = 1.0 + 0.08 * (opponents - 1)
    adjusted_score_threshold = 0.65 * multiway_factor
    adjusted_equity_threshold = 0.70 * multiway_factor
    
    if score >= adjusted_score_threshold or equity >= adjusted_equity_threshold:
        return {"action": "all_in"}
    
    if owed > 0:
        pot_odds = pot / owed
        if pot_odds >= 6.0 and (score >= 0.50 or equity >= 0.55):
            return {"action": "call"}
    
    return {"action": "fold"}

def preflop_policy(hole, owed, pot, stack, can_check, position_factor, opponents, min_raise, my_bet, players):
    score = hand_score(hole)
    
    # Multiway penalty: multiplicative factor for opponents > 2
    multiway_factor = 1.0
    if opponents > 2:
        multiway_factor = 1.0 + 0.10 * (opponents - 2)
    
    if position_factor < 0.33:
        open_threshold = 0.55 * multiway_factor
        call_threshold = 0.40 * multiway_factor
        raise_threshold = 0.75 * multiway_factor
    elif position_factor < 0.66:
        open_threshold = 0.40 * multiway_factor
        call_threshold = 0.30 * multiway_factor
        raise_threshold = 0.65 * multiway_factor
    else:
        open_threshold = 0.25 * multiway_factor
        call_threshold = 0.20 * multiway_factor
        raise_threshold = 0.55 * multiway_factor
    
    if can_check:
        return {"action": "check"}
    
    if owed == 0:
        if score >= open_threshold:
            raise_size = max(min_raise, int(pot * 0.75))
            if raise_size > stack + my_bet:
                return {"action": "all_in"}
            return {"action": "raise", "amount": min(raise_size, stack + my_bet)}
        return {"action": "check"}
    
    pot_odds = pot / owed if owed > 0 else 999
    
    # Commitment threshold scales with opponents: 0.25 / sqrt(opponents)
    commit_fraction = (owed + my_bet) / (stack + my_bet) if (stack + my_bet) > 0 else 0
    survival_threshold = 0.25 / math.sqrt(opponents)
    
    if score >= raise_threshold:
        raise_size = max(min_raise, int(pot * 1.0))
        if raise_size > stack + my_bet:
            return {"action": "all_in"}
        return {"action": "raise", "amount": min(raise_size, stack + my_bet)}
    
    if score >= call_threshold:
        if commit_fraction > survival_threshold:
            if score >= call_threshold + 0.30:
                return {"action": "call"}
            return {"action": "fold"}
        if pot_odds >= 3.0:
            return {"action": "call"}
    
    return {"action": "fold"}

def postflop_policy(hole, community, owed, pot, stack, can_check, opponents, position_factor, street, min_raise, my_bet, current_bet):
    if not community:
        return {"action": "check"} if can_check else {"action": "fold"}
    
    equity = estimate_equity(hole, community, 300)
    made = is_made_hand(hole, community)
    draw = has_draw(hole, community)
    dry = is_dry_board(community)
    overcards = has_overcards(hole, community)
    
    spr = (stack + my_bet) / pot if pot > 0 else 999
    
    commit_fraction = (owed + my_bet) / (stack + my_bet) if (stack + my_bet) > 0 else 0
    survival_threshold = 0.25 / math.sqrt(opponents)
    
    if owed > 0:
        pot_odds_ratio = pot / owed
    else:
        pot_odds_ratio = 999
    
    required_equity = 1 / (pot_odds_ratio + 1) if pot_odds_ratio > 0 else 0
    
    # Multiway factor for aggression thresholds
    multiway_factor = 1.0 + 0.08 * (opponents - 1)
    
    # Bet-size adjusted equity requirement: must exceed (bet/pot)*0.8 + 0.3
    bet_fraction = owed / pot if pot > 0 else 0
    bet_adjusted_equity = bet_fraction * 0.8 + 0.3
    
    if can_check:
        if equity < 0.45 and not made and not draw:
            return {"action": "check"}
        
        # Value betting with multiway caution
        value_threshold = 0.75 * multiway_factor if dry else 0.85 * multiway_factor
        if equity >= value_threshold and made:
            bet_size = int(pot * 0.66)
            if bet_size > stack + my_bet:
                return {"action": "all_in"}
            return {"action": "raise", "amount": min(bet_size, stack + my_bet)}
        
        if equity >= 0.70 and made and dry:
            bet_size = int(pot * 0.33)
            if bet_size > stack + my_bet:
                return {"action": "all_in"}
            return {"action": "raise", "amount": min(bet_size, stack + my_bet)}
        
        if equity >= 0.75 and draw and spr < 3:
            bet_size = int(pot * 0.5)
            if bet_size > stack + my_bet:
                return {"action": "all_in"}
            return {"action": "raise", "amount": min(bet_size, stack + my_bet)}
        
        if equity < 0.65 and spr > 5 and opponents >= 2:
            return {"action": "check"}
        
        return {"action": "check"}
    
    if owed == 0:
        return {"action": "check"}
    
    # Survival override: fold if committing too much without strong equity
    if commit_fraction > survival_threshold and equity < 0.80:
        return {"action": "fold"}
    
    # Bet-size adjusted equity check for all calls
    if equity < bet_adjusted_equity:
        return {"action": "fold"}
    
    # Multiway pot-control: fold to large bets with marginal equity
    if opponents >= 3 and pot_odds_ratio < 2.0 and equity < 0.60:
        return {"action": "fold"}
    
    if spr < 1.5 and equity >= 0.65:
        return {"action": "all_in"}
    
    # Value raise with multiway caution
    raise_threshold = 0.80 * multiway_factor
    if equity >= raise_threshold and made:
        raise_size = max(min_raise, int(pot * 0.75))
        if raise_size > stack + my_bet:
            return {"action": "all_in"}
        return {"action": "raise", "amount": min(raise_size, stack + my_bet)}
    
    if draw and equity >= 0.65:
        if equity >= 0.75 and pot_odds_ratio >= 3.0:
            raise_size = max(min_raise, int(pot * 0.66))
            if raise_size > stack + my_bet:
                return {"action": "all_in"}
            return {"action": "raise", "amount": min(raise_size, stack + my_bet)}
        if equity >= required_equity + 0.15:
            return {"action": "call"}
    
    if made:
        if equity >= required_equity + 0.15:
            return {"action": "call"}
        if pot_odds_ratio < 3.0 and equity < 0.75:
            return {"action": "fold"}
        return {"action": "call"}
    
    if equity >= required_equity + 0.20 and equity >= 0.55:
        return {"action": "call"}
    
    if position_factor > 0.66 and pot_odds_ratio >= 5.0 and equity >= 0.40:
        return {"action": "call"}
    
    if street == "river" and overcards and pot_odds_ratio >= 3.0 and equity >= 0.35:
        return {"action": "call"}
    
    return {"action": "fold"}
