"""Trap Steal v1: tightens versus maniacs and steals from folding tables."""
BOT_NAME = "Trap Steal v1"
RANK_VALUE = {rank: value for value, rank in enumerate("23456789TJQKA", 2)}


def _preflop_score(cards):
    high, low = sorted((RANK_VALUE[card[0]] for card in cards), reverse=True)
    suited = cards[0][1] == cards[1][1]
    pair = high == low
    gap = high - low

    if pair:
        return 60 + high * 3

    return high * 3 + low + (6 if suited else 0) - max(0, gap - 1) * 2


def _table_profile(state):
    recent_actions = state.get("match_action_log", [])[-250:]
    action_count = max(1, len(recent_actions))
    raise_count = sum(1 for action in recent_actions if action.get("action") in ("raise", "all_in"))
    fold_count = sum(1 for action in recent_actions if action.get("action") == "fold")
    return raise_count / action_count, fold_count / action_count


def _has_pair(state):
    ranks = [card[0] for card in state["community_cards"] + state["your_cards"]]
    return any(ranks.count(card[0]) >= 2 for card in state["your_cards"])


def decide(state):
    owed = state["amount_owed"]
    pot = max(1, state["pot"])
    total_bet_cap = state["your_stack"] + state["your_bet_this_street"]
    raise_rate, fold_rate = _table_profile(state)
    late_position = state["seat_to_act"] >= len(state["players"]) * 0.55
    score = _preflop_score(state["your_cards"])

    if state["street"] == "preflop":
        trap_threshold = 67 if raise_rate > 0.22 else 62
        steal_threshold = 46 if fold_rate > 0.32 and late_position else 54

        if score >= trap_threshold:
            raise_to = max(state["min_raise_to"], int(pot + owed * (4 if raise_rate > 0.22 else 3)))
            return {"action": "raise", "amount": min(raise_to, total_bet_cap)}
        if owed == 0 and score >= steal_threshold:
            raise_to = max(state["min_raise_to"], int(pot * 1.3))
            return {"action": "raise", "amount": min(raise_to, total_bet_cap)}
        if owed <= pot * 0.16 and score >= steal_threshold + 4:
            return {"action": "call"}
        if state["can_check"]:
            return {"action": "check"}
        return {"action": "fold"}

    has_pair = _has_pair(state)

    if owed == 0:
        if has_pair:
            raise_to = max(state["min_raise_to"], int(pot * 0.75))
            return {"action": "raise", "amount": min(raise_to, total_bet_cap)}
        if late_position and fold_rate > 0.35 and raise_rate < 0.2:
            raise_to = max(state["min_raise_to"], int(pot * 0.45))
            return {"action": "raise", "amount": min(raise_to, total_bet_cap)}
        return {"action": "check"}

    if has_pair and owed <= pot * (0.42 if raise_rate < 0.25 else 0.28):
        return {"action": "call"}
    return {"action": "fold"}
