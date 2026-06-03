"""
Fullhouse Hackathon — equity-driven hero bot (self-contained, Tier 2).

The sandbox loads exactly ONE file, `bot.py`, by path, with no `mybot` package
alongside it — so everything is inlined here:

  * a thin eval7 hand-eval layer (string cards in, int score out),
  * a deadline-bounded Monte-Carlo equity engine (hero vs N opponents, either
    uniformly random OR drawn from per-opponent ranges — see USE_RANGE_SAMPLING),
  * a static 169-entry preflop policy driven by the Chen starting-hand score
    (NO Monte Carlo preflop — instant and deterministic),
  * an EV / pot-odds postflop policy, and
  * **Tier 2: a per-match opponent model** built from `state["match_action_log"]`
    that classifies each villain (station / maniac / nit / tag) and uses that to
    (a) replace the Tier-1 static equity haircut with a range-aware one and
    (b) size value bets, gate bluffs by fold equity, and trap maniacs — all to
    grow `E[final_stack − 10_000]` (the qualifier's ranking metric).

The Tier-2 model is gated behind `USE_RANGE_MODEL`. When it is False, or when an
opponent has too little data (`< MIN_SAMPLE` decisions) / is unknown, every code
path collapses to the exact Tier-1 numbers — so a neutral field, the first ~20
hands of every match, and a one-line flag flip all reproduce the shipped Tier-1
behaviour. This is the revert lever from the plan (§3.2 / Gate 2).

Allowed libs only: `eval7` + Python stdlib. No file I/O, no banned imports,
single-threaded. Hard limits: 2 s / decision, 0.5 CPU, 768 MB — we spend well
under all three. A crash silently auto-folds, so `decide()` wraps everything in a
defensive try/except that takes the free check when available and folds otherwise.

Known trap (see repo memory): vs-random equity OVERESTIMATES hero equity because
real opponents fold trash. Tier 1 corrected it with a fixed haircut; Tier 2
replaces that with a data-driven, opponent-specific correction.
"""

from __future__ import annotations

import random
import time

import eval7

BOT_NAME = "Equity-EV"
BOT_AVATAR = "robot_1"

# --------------------------------------------------------------------------- #
# Tier-2 master switches
# --------------------------------------------------------------------------- #
# USE_RANGE_MODEL: master switch for the whole opponent model. False => exact
# Tier-1 behaviour (the one-line revert from the plan's Gate 2).
USE_RANGE_MODEL = True
# USE_RANGE_SAMPLING: draw opponents' hole cards from their modelled ranges in
# the MC loop (plan §3.2 "preferred"). False => vs-random MC + a model-driven
# haircut (cheaper, lower variance). The haircut path is the gated default; the
# sampling path is an opt-in refinement validated separately.
USE_RANGE_SAMPLING = False
# Below this many logged decisions an opponent is treated as unknown (neutral
# prior == Tier-1). Never swing hard off 3 hands (plan §3.1).
MIN_SAMPLE = 16
# USE_STAGE_B: Stage-B cEV posture corrections layered on the Tier-2 model
# (PLAN_maximize_chip_delta §B + leak L1/L3):
#   (a) MULTIWAY fold-equity stabs — generalise the heads-up-only semi-bluff so we
#       collect fold equity against a folding field in multiway pots too. Gated by
#       a multiway-aware fold equity `fe` = PRODUCT of per-opponent fold
#       probabilities, so any station / maniac / unknown still in the pot drives
#       `fe -> 0` and the stab never fires (self-protecting, not over-fit).
#   (b) PASSIVE-FIELD value sizing — when every classified in-pot opponent is
#       low-aggression (a pot-odds caller, not a raiser), cap value sizing at ~1/2
#       pot and value thinner so we stop folding callers off our value bets.
# False => exact current Tier-2 behaviour (one-flag revert).
USE_STAGE_B = True
# USE_STAGE_C: widen late-position preflop steals HARD against a field classified
# as folders (PLAN_maximize_chip_delta §C / leak L2). The Tier-2 steal mechanism
# fires but only widens ~0.5 Chen pts (cap 1.0) — far too timid vs a field that
# folds ~80% preflop. Stage C uses a clean preflop-fold signal and a position-
# scaled, larger widen, still gated on EVERY live opponent being a classified
# folder (no station / maniac / unknown). False => Tier-2 steal magnitude.
USE_STAGE_C = True

# --------------------------------------------------------------------------- #
# Card primitives (inlined from mybot.hand_eval)
# --------------------------------------------------------------------------- #
RANKS = "23456789TJQKA"
SUITS = "shdc"
FULL_DECK = [r + s for r in RANKS for s in SUITS]
RANK_VAL = {r: i + 2 for i, r in enumerate(RANKS)}  # 2..14

# Single shared RNG: drives both the MC sampler and the bounded bluff frequency.
_RNG = random.Random()

# Time budget for the postflop Monte-Carlo loop (wall clock). Leaves a huge
# margin under the 2 s/decision cap even at 0.5 CPU; the loop returns the
# estimate so far if it hits the deadline, so we always answer in time.
BUDGET_S = 0.30
POSTFLOP_ITERS = 8000  # ±~1.1% at 95%; deadline caps it for many-opponent boards


# --------------------------------------------------------------------------- #
# Chen starting-hand score (also used to rank combos for range building)
# --------------------------------------------------------------------------- #
# Chen high-card points; pairs use 2x this (floored at 5). Ten-and-below are
# value/2, but J/Q/K/A get the canonical bumped scores.
_CHEN_HI = {14: 10.0, 13: 8.0, 12: 7.0, 11: 6.0}
for _v in range(2, 11):
    _CHEN_HI[_v] = _v / 2.0


def _chen_vals(hi, lo, suited):
    """Chen score from rank values + suitedness (shared by _chen and combo ranking)."""
    if hi == lo:  # pocket pair
        return max(_CHEN_HI[hi] * 2.0, 5.0)
    score = _CHEN_HI[hi]
    if suited:
        score += 2.0
    gap = hi - lo - 1  # cards strictly between the two ranks
    if gap == 1:
        score -= 1.0
    elif gap == 2:
        score -= 2.0
    elif gap == 3:
        score -= 4.0
    elif gap >= 4:
        score -= 5.0
    if gap <= 1 and hi <= 11:  # straight bonus: connectors below Q
        score += 1.0
    return score


def _chen(hole):
    """Bill Chen starting-hand score (≈ -1 for 72o up to 20 for AA)."""
    v1, v2 = RANK_VAL[hole[0][0]], RANK_VAL[hole[1][0]]
    hi, lo = (v1, v2) if v1 >= v2 else (v2, v1)
    return _chen_vals(hi, lo, hole[0][1] == hole[1][1])


# Precompute all 1326 two-card combos sorted by Chen score (strongest first).
# Board-independent, so building a "top-X% range" per decision is just a prefix
# of this list with the dead cards filtered out (plan §3.2). Built once at import.
def _build_sorted_combos():
    combos = []
    for i in range(52):
        ci = FULL_DECK[i]
        vi, si = RANK_VAL[ci[0]], ci[1]
        for j in range(i + 1, 52):
            cj = FULL_DECK[j]
            vj, sj = RANK_VAL[cj[0]], cj[1]
            hi, lo = (vi, vj) if vi >= vj else (vj, vi)
            sc = _chen_vals(hi, lo, si == sj)
            combos.append((sc, ci, cj))
    combos.sort(key=lambda t: -t[0])
    return [(c1, c2) for _, c1, c2 in combos]


_SORTED_COMBOS = _build_sorted_combos()  # list[(card_str, card_str)], strongest first


# --------------------------------------------------------------------------- #
# Opponent model (Tier 2) — built from state["match_action_log"]
# --------------------------------------------------------------------------- #
# match_action_log entries are ONLY {hand_num, seat, bot_id, action, amount}
# (no street/board/pot), rolling over the last 200 actions, reset per match.
# We lead with the robustly-computable §2.1 stats (VPIP/PFR/AF/fold%/all-in%);
# the first logged action of a (hand_num, bot_id) is that bot's preflop decision.
_NEUTRAL = {
    "n": 0, "vpip": 0.32, "pfr": 0.18, "af": 1.2, "fold_freq": 0.45,
    "allin_freq": 0.0, "looseness": 0.32, "archetype": "unknown",
    "pf_fold_freq": 0.45,
}


def _build_model(state):
    """Return {bot_id: profile} for every non-hero bot seen in the match log.

    Profile keys: n (decisions), vpip, pfr, af, fold_freq, allin_freq,
    looseness (∈[0,1], its VPIP clamped — fraction of hands it shows up with),
    and a coarse archetype. Recomputed each decision (O(≤200)); we never rely on
    module state surviving across calls.
    """
    log = state.get("match_action_log") or []
    hero_id = None
    seat = state.get("seat_to_act")
    for p in state.get("players", []):
        if p.get("seat") == seat:
            hero_id = p.get("bot_id")
            break

    acc = {}          # bot_id -> counters
    seen_hand = set()  # (hand_num, bot_id) -> first action already counted
    for e in log:
        bid = e.get("bot_id")
        if bid is None or bid == hero_id:
            continue
        a = e.get("action")
        c = acc.get(bid)
        if c is None:
            c = acc[bid] = {"hands": 0, "vpip": 0, "pfr": 0, "dec": 0,
                            "raise": 0, "call": 0, "fold": 0, "allin": 0,
                            "pf_fold": 0}
        key = (e.get("hand_num"), bid)
        if key not in seen_hand:  # first action this hand == preflop decision
            seen_hand.add(key)
            c["hands"] += 1
            if a in ("call", "raise", "all_in"):
                c["vpip"] += 1
            if a in ("raise", "all_in"):
                c["pfr"] += 1
            if a == "fold":  # folded preflop — the clean "folds to a steal" signal
                c["pf_fold"] += 1
        c["dec"] += 1
        if a == "raise":
            c["raise"] += 1
        elif a == "call":
            c["call"] += 1
        elif a == "fold":
            c["fold"] += 1
        elif a == "all_in":
            c["allin"] += 1

    model = {}
    for bid, c in acc.items():
        hands = max(c["hands"], 1)
        dec = max(c["dec"], 1)
        vpip = c["vpip"] / hands
        pfr = c["pfr"] / hands
        af = (c["raise"] + c["allin"]) / max(c["call"], 1)
        fold_freq = c["fold"] / dec
        allin_freq = c["allin"] / dec
        p = {
            "n": c["dec"], "vpip": vpip, "pfr": pfr, "af": af,
            "fold_freq": fold_freq, "allin_freq": allin_freq,
            "pf_fold_freq": c["pf_fold"] / hands,
            "looseness": min(0.95, max(0.05, vpip)),
            "archetype": _classify(c["dec"], vpip, af, fold_freq, allin_freq),
        }
        model[bid] = p
    return model


def _classify(n, vpip, af, fold_freq, allin_freq):
    """Coarse bucket driving the exploits. Unknown until MIN_SAMPLE decisions."""
    if n < MIN_SAMPLE:
        return "unknown"
    # Nit FIRST — tight and folds a lot. Checked before maniac so a tight player
    # who only ever 3-bets-or-folds (few calls => spuriously high AF) is read as a
    # nit, not a maniac. AF is unreliable on a small call count.
    if vpip <= 0.24 and fold_freq >= 0.55:
        return "nit"
    # Maniac — must be genuinely LOOSE and aggressive (the looseness gate keeps
    # tight value-raisers out), or a shove-bot (high all-in frequency).
    if allin_freq >= 0.10 or (vpip >= 0.40 and af >= 2.0) or (vpip >= 0.55 and af >= 1.3):
        return "maniac"
    # Station — very passive (almost never raises) but keeps entering / calling.
    if af <= 0.5 and vpip >= 0.25:
        return "station"
    return "tag"


def _profile(model, bot_id):
    return model.get(bot_id) or _NEUTRAL


def _seat_to_bot(state):
    return {p.get("seat"): p.get("bot_id") for p in state.get("players", [])}


def _live_opp_ids(state):
    """bot_ids of opponents still in the hand (not folded, not hero)."""
    seat = state.get("seat_to_act")
    ids = []
    for p in state.get("players", []):
        if p.get("seat") == seat or p.get("is_folded"):
            continue
        ids.append(p.get("bot_id"))
    return ids


def _last_aggressor_id(state, hero_seat):
    """bot_id of the last non-hero player to raise/all-in this hand (the bettor
    whose range matters when we're facing a bet). None if nobody has."""
    s2b = _seat_to_bot(state)
    bid = None
    for a in state.get("action_log", []):
        if a.get("action") in ("raise", "all_in"):
            s = a.get("seat")
            if s != hero_seat:
                bid = s2b.get(s)
    return bid


# --------------------------------------------------------------------------- #
# Monte-Carlo equity engines (inlined + specialised from mybot.equity)
# --------------------------------------------------------------------------- #
def _equity_vs_random(hole, board, n_opp, iters, deadline):
    """Hero equity in [0, 1] vs ``n_opp`` uniformly-random opponents.

    Completes the board each iteration and credits hero ``1`` for a sole win or
    ``1/k`` for a k-way chop at the top. ``deadline`` (a ``time.monotonic()``
    value) hard-stops the loop and returns the estimate so far. NOTE: vs-random
    is optimistic — the caller corrects it (Tier-1 haircut or Tier-2 model).
    """
    known = set(hole) | set(board)
    deck = [eval7.Card(c) for c in FULL_DECK if c not in known]
    hero_cards = [eval7.Card(c) for c in hole]
    board_cards = [eval7.Card(c) for c in board]
    need_board = 5 - len(board)
    draw = need_board + 2 * n_opp
    if draw > len(deck) or n_opp <= 0:
        return 0.5  # nonsensical request — neutral, never crash

    sample = _RNG.sample
    evaluate = eval7.evaluate
    monotonic = time.monotonic

    eq_sum = 0.0
    completed = 0
    for i in range(iters):
        if deadline is not None and i and (i & 0x3FF) == 0 and monotonic() >= deadline:
            break
        drawn = sample(deck, draw)
        runout = board_cards + drawn[:need_board]
        rest = drawn[need_board:]
        hero_score = evaluate(hero_cards + runout)
        v_scores = [evaluate(rest[2 * v: 2 * v + 2] + runout) for v in range(n_opp)]
        best_v = max(v_scores)
        if hero_score > best_v:
            eq_sum += 1.0
        elif hero_score == best_v:
            k = 1 + sum(1 for s in v_scores if s == hero_score)
            eq_sum += 1.0 / k
        completed += 1

    return eq_sum / completed if completed else 0.5


def _range_combos(looseness, dead):
    """Top-`looseness` fraction of starting hands (by Chen), with any combo that
    collides with the dead cards removed. Prefix of the precomputed sorted list,
    so this is O(deck). Returns a list of (Card, Card)."""
    avail = [c for c in _SORTED_COMBOS if c[0] not in dead and c[1] not in dead]
    if not avail:
        return []
    k = max(8, int(round(looseness * len(avail))))  # keep a usable minimum
    top = avail[:k]
    return [(eval7.Card(a), eval7.Card(b)) for a, b in top]


def _equity_vs_ranges(hole, board, opp_looseness, iters, deadline):
    """Hero equity vs opponents whose hole cards are drawn from per-opponent
    ranges (plan §3.2 range-restricted sampling). ``opp_looseness`` is a list of
    range fractions, one per live opponent. Falls back to vs-random if a range
    is empty. Higher cost than vs-random, so callers lower ``iters``."""
    n_opp = len(opp_looseness)
    known = set(hole) | set(board)
    deck = [eval7.Card(c) for c in FULL_DECK if c not in known]
    hero_cards = [eval7.Card(c) for c in hole]
    board_cards = [eval7.Card(c) for c in board]
    need_board = 5 - len(board)
    if n_opp <= 0 or need_board + 2 * n_opp > len(deck):
        return 0.5

    ranges = [_range_combos(L, known) for L in opp_looseness]
    if any(not r for r in ranges):
        return _equity_vs_random(hole, board, n_opp, iters, deadline)

    evaluate = eval7.evaluate
    monotonic = time.monotonic
    randrange = _RNG.randrange
    sample = _RNG.sample

    eq_sum = 0.0
    completed = 0
    for i in range(iters):
        if deadline is not None and i and (i & 0x1FF) == 0 and monotonic() >= deadline:
            break
        used = set(hole) | set(board)
        opp_hands = []
        ok = True
        for r in ranges:
            picked = None
            for _ in range(12):  # rejection-sample a non-colliding combo
                a, b = r[randrange(len(r))]
                ka, kb = str(a), str(b)
                if ka not in used and kb not in used:
                    used.add(ka)
                    used.add(kb)
                    picked = (a, b)
                    break
            if picked is None:
                ok = False
                break
            opp_hands.append(picked)
        if not ok:
            continue
        remaining = [eval7.Card(c) for c in FULL_DECK if c not in used]
        runout = board_cards + (sample(remaining, need_board) if need_board else [])
        hero_score = evaluate(hero_cards + runout)
        best_v = -1
        ties = 0
        for a, b in opp_hands:
            vs = evaluate([a, b] + runout)
            if vs > best_v:
                best_v = vs
                ties = 0
            elif vs == best_v:
                ties += 1
        if hero_score > best_v:
            eq_sum += 1.0
        elif hero_score == best_v:
            eq_sum += 1.0 / (2 + ties)
        completed += 1

    return eq_sum / completed if completed else 0.5


# --------------------------------------------------------------------------- #
# Table-reading helpers (position, big blind, live opponents)
# --------------------------------------------------------------------------- #
def _button_seat(state, n):
    """Derive the dealer (button) seat from the blind markers in this hand's
    action_log. Heads-up: dealer == small blind; otherwise SB == dealer+1."""
    sb = bb = None
    for a in state.get("action_log", []):
        act = a.get("action")
        if act == "small_blind":
            sb = a.get("seat")
        elif act == "big_blind":
            bb = a.get("seat")
    if n <= 0:
        return None
    if n == 2:
        if sb is not None:
            return sb
        if bb is not None:
            return (bb - 1) % n
        return None
    if sb is not None:
        return (sb - 1) % n
    if bb is not None:
        return (bb - 2) % n
    return None


def _position(state, n):
    """Position in [0, 1]: 1.0 = on the button (latest, most info), 0.0 = the
    earliest seat. Falls back to 0.5 if blinds can't be read."""
    if n <= 1:
        return 0.5
    btn = _button_seat(state, n)
    if btn is None:
        return 0.5
    dist = (btn - state["seat_to_act"]) % n  # 0 when hero is the button
    return 1.0 - dist / (n - 1)


def _big_blind(state):
    """Real big-blind size from the action_log (engine constant could change)."""
    bb = 0
    for a in state.get("action_log", []):
        if a.get("action") == "big_blind":
            bb = max(bb, a.get("amount") or 0)
    return bb or 100


def _live_opponents(state):
    """Opponents still in the hand (not folded), excluding hero."""
    seat = state["seat_to_act"]
    cnt = 0
    for p in state.get("players", []):
        if p.get("seat") == seat or p.get("is_folded"):
            continue
        cnt += 1
    return max(cnt, 1)


def _raise(target, min_raise_to, all_in_to):
    """Clamp a raise-to total into [min_raise_to, all_in_to]; shove if it reaches
    our stack. The engine handles the min-snap / all-in conversion too, but we
    keep the action dict honest."""
    target = int(max(min_raise_to, min(int(round(target)), all_in_to)))
    if target >= all_in_to:
        return {"action": "all_in"}
    return {"action": "raise", "amount": target}


# --------------------------------------------------------------------------- #
# Preflop policy
# --------------------------------------------------------------------------- #
def _preflop(state, hole, model):
    chen = _chen(hole)
    n = len(state.get("players", [])) or 2
    pos = _position(state, n)
    bb = _big_blind(state)
    n_opp = _live_opponents(state)

    pot = max(1, state.get("pot", 0))
    owed = state.get("amount_owed", 0)
    can_check = state.get("can_check", owed == 0)
    current_bet = state.get("current_bet", bb)
    min_raise_to = state.get("min_raise_to", current_bet + bb)
    my_bet = state.get("your_bet_this_street", 0)
    my_stack = state.get("your_stack", 0)
    all_in_to = my_stack + my_bet
    eff_bb = all_in_to / bb if bb else 100.0

    # --- Short-stack push/fold (effective <= ~12bb) -----------------------
    if eff_bb <= 12:
        shove_th = 6.5 + 1.5 * (n_opp - 1) - 0.4 * max(0.0, 10.0 - eff_bb)
        if chen >= shove_th:
            return _raise(all_in_to, min_raise_to, all_in_to)  # jam
        if can_check:
            return {"action": "check"}
        return {"action": "fold"}

    raised = current_bet > bb  # someone has put in a real raise beyond the BB

    # --- Unopened (or limped) pot ----------------------------------------
    if not raised:
        open_th = 9.5 - 4.0 * pos  # tight UTG (~9.5) -> wide button (~5.5)
        # Tier-2/Stage-C steal: widen the open vs a field that folds a lot, keep
        # it tight vs stations/maniacs who don't fold to steals. Position-scaled.
        if USE_RANGE_MODEL:
            open_th -= _steal_loosen(state, model, pos)
        if chen >= open_th:
            return _raise(3 * bb, min_raise_to, all_in_to)  # ~3bb open
        if can_check:
            return {"action": "check"}
        if chen >= open_th - 2.5 and owed <= bb:
            return {"action": "call"}
        return {"action": "fold"}

    # --- Facing a raise ---------------------------------------------------
    if chen >= 12.0:  # QQ+ / AKs / JJ — 3-bet for value to ~3x
        return _raise(3 * current_bet, min_raise_to, all_in_to)
    if chen >= 10.0:  # AK / TT / strong broadways — call (snap off shoves too)
        return {"action": "call"}
    pot_odds = owed / (pot + owed) if (pot + owed) > 0 else 1.0
    call_th = 9.0 - 2.5 * pos
    if chen >= call_th and pot_odds <= 0.40:  # playable + a fair price
        return {"action": "call"}
    if can_check:
        return {"action": "check"}
    return {"action": "fold"}


def _steal_loosen(state, model, pos=0.5):
    """How much to lower the open threshold (widen steals), in Chen points.
    Positive only when EVERY live opponent is a classified folder (no station /
    maniac / unknown — any of those could call/raise our steal). `pos` ∈ [0,1] is
    hero's position (1 = button); later position widens harder (lower risk, more
    fold equity behind)."""
    ids = _live_opp_ids(state)
    if not ids:
        return 0.0
    fold = 1.0
    any_known = False
    for bid in ids:
        p = _profile(model, bid)
        if p["archetype"] == "unknown":
            return 0.0  # an unknown could be sticky — don't widen
        any_known = True
        if p["archetype"] in ("station", "maniac"):
            return 0.0  # they don't fold — stealing is -EV
        # Preflop-fold rate is the clean "folds to a steal" signal; the overall
        # fold_freq is diluted by postflop calls (under-reads the folders).
        fold = min(fold, p["pf_fold_freq"])
    if not any_known:
        return 0.0
    if not USE_STAGE_C:
        return max(0.0, min(1.0, (fold - 0.45) * 3.0))  # legacy Tier-2 magnitude
    # Stage C: widen HARD vs a heavy-folding field, scaled by position. A field
    # folding ~80% preflop ⇒ ~2 Chen-pt widen on the button (open range ~top half
    # down to ~top 2/3), tapering to little from early position.
    widen = max(0.0, (fold - 0.45)) * 6.0
    return min(2.5, widen * (0.4 + 0.6 * pos))


# --------------------------------------------------------------------------- #
# Postflop policy (equity / pot-odds, Tier-2 model-aware correction)
# --------------------------------------------------------------------------- #
def _postflop(state, hole, board, deadline, model):
    n_opp = _live_opponents(state)
    s_idx = {"flop": 0, "turn": 1, "river": 2}.get(state.get("street"), 0)
    hero_seat = state["seat_to_act"]

    pot = max(1, state.get("pot", 0))
    owed = state.get("amount_owed", 0)
    can_check = state.get("can_check", owed == 0)
    current_bet = state.get("current_bet", 0)
    min_raise_to = state.get("min_raise_to", current_bet + 100)
    my_bet = state.get("your_bet_this_street", 0)
    my_stack = state.get("your_stack", 0)
    all_in_to = my_stack + my_bet

    # --- Equity estimate ---------------------------------------------------
    live_ids = _live_opp_ids(state)
    if USE_RANGE_MODEL and USE_RANGE_SAMPLING and live_ids:
        looseness = [_profile(model, b)["looseness"] for b in live_ids]
        eq = _equity_vs_ranges(hole, board, looseness, POSTFLOP_ITERS, deadline)
    else:
        eq = _equity_vs_random(hole, board, n_opp, POSTFLOP_ITERS, deadline)

    # --- Correct the (optimistic) vs-random estimate -----------------------
    # Tier-1 baseline: small base haircut + a larger facing-bet penalty that
    # grows with street and bet size. Tier-2 scales BOTH by the opponent model
    # (range-aware), defaulting exactly to Tier-1 when the model is neutral.
    base_hc = min(0.15, 0.03 * n_opp)
    if can_check:
        if USE_RANGE_MODEL and not USE_RANGE_SAMPLING:
            base_hc *= _base_hc_mult(state, model, live_ids)
        eq_adj = eq - base_hc
    else:
        bet_frac = owed / max(1, pot - owed)  # bet size relative to pot-before
        facing_hc = (0.06 + 0.05 * s_idx) * min(bet_frac, 2.0)
        if USE_RANGE_MODEL and not USE_RANGE_SAMPLING:
            base_hc *= _base_hc_mult(state, model, live_ids)
            facing_hc *= _facing_hc_mult(state, model, hero_seat)
        eq_adj = eq - base_hc - facing_hc

    # --- Tier-2 read: exploit knobs (default == Tier-1) --------------------
    ex = _exploits(state, model, live_ids, hero_seat) if USE_RANGE_MODEL else _NEUTRAL_EX

    # --- Checked to us: value-bet / semi-bluff / pot control --------------
    if can_check:
        if eq_adj >= 0.80:  # monsters — bet big (overbet unless a station field)
            return _raise(current_bet + round(pot * ex["overbet_size"]),
                          min_raise_to, all_in_to)
        if eq_adj >= ex["value_th"]:  # made hand worth value
            return _raise(current_bet + round(pot * ex["value_size"]),
                          min_raise_to, all_in_to)
        if USE_STAGE_B:
            # Stage B (L1): multiway fold-equity stab. `ex["fe"]` is the PRODUCT of
            # per-opponent fold probabilities, so this only fires when the whole
            # live field is folders — _bluff_ok then guarantees the chips we win
            # when they all fold beat the chips we risk when called. Works HU and
            # multiway; stab_freq is 0 unless _exploits confirmed a folding field.
            if ex["stab_lo"] <= eq_adj < ex["value_th"]:
                cost = round(pot * ex["stab_size"])
                if _bluff_ok(ex["fe"], pot, cost) and _RNG.random() < ex["stab_freq"]:
                    return _raise(current_bet + cost, min_raise_to, all_in_to)
        else:
            # Legacy Tier-2 semi-bluff: heads-up only, fe-gated flat frequency.
            if n_opp == 1 and ex["lo_sb"] <= eq_adj < ex["value_th"]:
                cost = round(pot * 0.5)
                if _bluff_ok(ex["fe"], pot, cost) and _RNG.random() < ex["sb_freq"]:
                    return _raise(current_bet + cost, min_raise_to, all_in_to)
        return {"action": "check"}

    # --- Facing a bet: EV vs pot odds ------------------------------------
    pot_odds = owed / (pot + owed) if (pot + owed) > 0 else 1.0
    if eq_adj >= ex["raise_th"]:  # strong — raise for value (bigger when crushing)
        frac = 0.9 if eq_adj >= 0.82 else 0.6
        return _raise(current_bet + round((pot + owed) * frac), min_raise_to, all_in_to)
    if eq_adj >= pot_odds + ex["call_cushion"] * n_opp:  # price is right — call
        return {"action": "call"}
    # Occasional heads-up semi-bluff raise — only with fold equity (never vs a
    # maniac / station; more vs a nit).
    if (n_opp == 1 and ex["allow_sb_raise"] and 0.45 <= eq_adj < ex["raise_th"]
            and pot_odds < 0.5 and _bluff_ok(ex["fe"], pot, owed)
            and _RNG.random() < ex["sb_raise_freq"]):
        return _raise(current_bet + round((pot + owed) * 0.8), min_raise_to, all_in_to)
    return {"action": "fold"}


# --------------------------------------------------------------------------- #
# Tier-2 exploit derivation (all defaults reproduce Tier-1)
# --------------------------------------------------------------------------- #
# Neutral exploit set == exact Tier-1 numbers (used when USE_RANGE_MODEL is off).
_NEUTRAL_EX = {
    "value_th": 0.56, "value_size": 0.6, "overbet_size": 0.9,
    "raise_th": 0.68, "call_cushion": 0.02,
    "lo_sb": 0.42, "sb_freq": 0.33, "fe": 1.0,
    "allow_sb_raise": True, "sb_raise_freq": 0.15,
    # Stage-B multiway stab knobs. stab_freq == 0.0 in the neutral set, so when
    # Stage B is off (or the field isn't a confirmed folder field) no stab fires.
    "stab_lo": 0.20, "stab_size": 0.5, "stab_freq": 0.0,
}


def _looseness_avg(model, ids):
    if not ids:
        return 0.32
    return sum(_profile(model, b)["looseness"] for b in ids) / len(ids)


def _base_hc_mult(state, model, live_ids):
    """Scale the base haircut by how tight the in-pot field is. Looser field =>
    its still-in range is closer to random => smaller haircut. Neutral
    looseness (0.32) returns ~1.0 (Tier-1). Bounded [0.4, 1.5]."""
    L = _looseness_avg(model, live_ids)
    return max(0.4, min(1.5, 1.30 - 0.95 * L))


def _facing_hc_mult(state, model, hero_seat):
    """Scale the facing-bet penalty by the BETTOR's archetype. A maniac's bets
    are air-heavy (tiny haircut => stop folding to them = trap); a nit's bets are
    real (large haircut). Neutral/unknown => 1.0 (Tier-1)."""
    bid = _last_aggressor_id(state, hero_seat)
    if bid is None:
        return 1.0
    arch = _profile(model, bid)["archetype"]
    return {"maniac": 0.15, "station": 0.55, "nit": 1.40,
            "tag": 1.0, "unknown": 1.0}.get(arch, 1.0)


def _pfold(p):
    """Per-opponent probability of folding to a bet, from its archetype/stats.
    Stations ~never fold; maniacs rarely; unknowns get a neutral prior; everyone
    else uses their measured fold frequency (clamped)."""
    a = p["archetype"]
    if a == "unknown":
        return 0.45
    if a == "station":
        return 0.05
    if a == "maniac":
        return 0.15
    return max(0.0, min(0.9, p["fold_freq"]))


def _exploits(state, model, live_ids, hero_seat):
    """Build the postflop exploit knobs from the model. Starts from the neutral
    (Tier-1) set and applies bounded, archetype-driven nudges."""
    ex = dict(_NEUTRAL_EX)
    if not live_ids:
        return ex

    archs = [_profile(model, b)["archetype"] for b in live_ids]
    bettor = _last_aggressor_id(state, hero_seat)
    bettor_arch = _profile(model, bettor)["archetype"] if bettor else "unknown"

    loose_field = any(a in ("station", "maniac") for a in archs)
    station_field = any(a == "station" for a in archs)
    all_nit_or_tag = archs and all(a in ("nit", "tag") for a in archs)

    # Fold-equity estimate for our bluffs. A stab only wins the pot if EVERY live
    # opponent folds, so Stage B uses the PRODUCT of per-opponent fold
    # probabilities — it scales down with the number of opponents and collapses to
    # ~0 if any one of them is a station / maniac / unknown (self-protecting in
    # multiway). Legacy Tier-2 used the MIN (overestimates multiway fold equity),
    # which was fine when stabs were heads-up only.
    profs = [_profile(model, b) for b in live_ids]
    if USE_STAGE_B:
        fe = 1.0
        for p in profs:
            fe *= _pfold(p)
    else:
        fe = 1.0
        for p in profs:
            fe = min(fe, _pfold(p))
    ex["fe"] = fe

    # Passive-caller field: every classified in-pot opponent is low-aggression (a
    # pot-odds caller, not a raiser). unknown opponents don't count toward the
    # all()-check (could be sticky). Drives Stage-B thin value sizing below.
    known = [p for p in profs if p["archetype"] != "unknown"]
    passive_field = bool(known) and all(
        p["af"] <= 0.6 and p["allin_freq"] < 0.08 for p in known)

    # vs a loose/station field: value-bet THINNER, and size to their calling cap
    # (~half pot) so we don't fold them out (the reference callers fold to big
    # bets — measured). Bluff ~never (fe gate already kills it, but be explicit).
    if station_field:
        ex["value_th"] = 0.50
        ex["value_size"] = 0.5
        ex["overbet_size"] = 0.5   # don't overbet a pot-odds caller out of the pot
        ex["raise_th"] = 0.62      # raise for value thinner facing their (rare) bets
        ex["sb_freq"] = 0.0
        ex["allow_sb_raise"] = False
    elif loose_field:              # loose but not classic station (e.g. maniac caller)
        ex["value_th"] = 0.52
        ex["raise_th"] = 0.64

    # vs an all-tight (nit/tag) field: value tighter, but we DO get fold equity,
    # so allow more semi-bluffing.
    if all_nit_or_tag:
        ex["value_th"] = 0.58
        ex["sb_freq"] = 0.40
        ex["sb_raise_freq"] = 0.22

    # --- Stage B knobs (L3 thin value + L1 multiway stab) ------------------
    # L3: into a passive pot-odds-caller field, value-bet THINNER and sized to be
    # CALLED (~1/2 pot). Applied after the nit/tag block so it overrides the
    # too-tight 0.58 threshold — these callers are loose postflop even when they
    # fold preflop, so thin payable value out-earns tightening up. Skipped if a
    # loose/station field (already handled) or a maniac caller is in the pot.
    if USE_STAGE_B and passive_field and not loose_field:
        ex["value_th"] = min(ex["value_th"], 0.52)
        ex["value_size"] = min(ex["value_size"], 0.5)
        ex["overbet_size"] = min(ex["overbet_size"], 0.6)

    # L1: turn on the multiway stab unless a loose caller is in the pot (then fold
    # equity is gone). The _bluff_ok(fe, ...) gate in _postflop does the +EV math,
    # so this is just an on/off frequency; fe (product) self-throttles by opponent
    # count, and a station/maniac/unknown in the pot already pushes fe below the
    # _bluff_ok threshold.
    if USE_STAGE_B:
        ex["stab_freq"] = 0.0 if loose_field else 0.55

    # vs a maniac BETTOR: trap. Call down lighter, don't bluff-raise, only
    # value-raise the near-nuts (let them keep barreling into us).
    if bettor_arch == "maniac":
        ex["call_cushion"] = -0.01   # call a touch below pot odds (their range is air)
        ex["raise_th"] = 0.82        # only jam the very top; flat everything else
        ex["allow_sb_raise"] = False
    elif bettor_arch == "nit":
        ex["call_cushion"] = 0.05    # their bets are real — need more to continue
        ex["raise_th"] = 0.72

    return ex


def _bluff_ok(fe, pot, cost):
    """Plan §3.3 fold-equity gate: a bluff is +EV only if the chips we win when
    they fold beat the chips we lose when they call: fe·pot > (1−fe)·cost."""
    return fe * pot > (1.0 - fe) * max(1, cost)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _decide(state):
    deadline = time.monotonic() + BUDGET_S
    hole = state["your_cards"]
    board = state.get("community_cards") or []
    model = _build_model(state) if USE_RANGE_MODEL else {}
    preflop = state.get("street", "preflop") == "preflop" or len(board) < 3
    if preflop:
        return _preflop(state, hole, model)
    return _postflop(state, hole, board, deadline, model)


def decide(game_state: dict) -> dict:
    """Tournament entry point. Defensive wrapper: on ANY unexpected error take
    the free check if available, otherwise fold — never propagate (an exception
    auto-folds anyway, but checking when free is strictly better)."""
    try:
        return _decide(game_state)
    except Exception:  # pragma: no cover - safety net
        if game_state.get("can_check") or game_state.get("amount_owed", 0) == 0:
            return {"action": "check"}
        return {"action": "fold"}
