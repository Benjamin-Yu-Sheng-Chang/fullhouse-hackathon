"""
FullHouse Hackathon 2026 — Advanced NL Hold'em Bot v2

Improvements over baseline:
  • GTO preflop hand ranges by position (6-max)
  • Hand category evaluation: made hand type + draw strength via eval7
  • Board texture: wet/dry/paired/connected — adjusts c-bet freq & sizing
  • Position-aware postflop strategy (IP value-bet, OOP check-call)
  • Opponent aggression detection + adaptive strategy
  • Monte Carlo equity for marginal/calling decisions
"""
import eval7
import random

BB = 100
_RANKS = '23456789TJQKA'
_RANK_IDX = {r: i for i, r in enumerate(_RANKS)}
_DECK = [eval7.Card(r + s) for r in _RANKS for s in 'shdc']

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: PREFLOP HAND RANGES  (6-max GTO approximations)
# ══════════════════════════════════════════════════════════════════════════════

def _canonical(hole):
    """['Ah','Kd'] → 'AKo'  |  ['Ah','As'] → 'AA'"""
    r1, r2 = hole[0][0], hole[1][0]
    s1, s2 = hole[0][1], hole[1][1]
    if _RANK_IDX[r1] < _RANK_IDX[r2]:
        r1, r2, s1, s2 = r2, r1, s2, s1
    if r1 == r2:
        return r1 + r2
    return r1 + r2 + ('s' if s1 == s2 else 'o')

# Opening ranges — how wide each position opens first-in
_OPEN = {
    'BTN': frozenset({
        'AA','KK','QQ','JJ','TT','99','88','77','66','55','44','33','22',
        'AKs','AQs','AJs','ATs','A9s','A8s','A7s','A6s','A5s','A4s','A3s','A2s',
        'KQs','KJs','KTs','K9s','K8s','K7s','K6s',
        'QJs','QTs','Q9s','Q8s',
        'JTs','J9s','J8s',
        'T9s','T8s',
        '98s','97s','96s',
        '87s','86s','85s',
        '76s','75s',
        '65s','64s','54s',
        'AKo','AQo','AJo','ATo','A9o','A8o','A7o','A6o',
        'KQo','KJo','KTo','K9o',
        'QJo','QTo',
        'JTo',
    }),
    'CO': frozenset({
        'AA','KK','QQ','JJ','TT','99','88','77','66','55','44',
        'AKs','AQs','AJs','ATs','A9s','A8s','A7s','A6s','A5s',
        'KQs','KJs','KTs','K9s','K8s',
        'QJs','QTs','Q9s',
        'JTs','J9s',
        'T9s','T8s',
        '98s','97s',
        '87s','86s',
        '76s','75s','65s',
        'AKo','AQo','AJo','ATo','A9o',
        'KQo','KJo','KTo',
        'QJo','QTo','JTo',
    }),
    'HJ': frozenset({
        'AA','KK','QQ','JJ','TT','99','88','77','66','55',
        'AKs','AQs','AJs','ATs','A9s','A8s',
        'KQs','KJs','KTs','K9s',
        'QJs','QTs',
        'JTs','J9s',
        'T9s','T8s',
        '98s','97s',
        '87s','76s',
        'AKo','AQo','AJo','ATo',
        'KQo','KJo',
        'QJo','JTo',
    }),
    'UTG': frozenset({
        'AA','KK','QQ','JJ','TT','99','88','77',
        'AKs','AQs','AJs','ATs',
        'KQs','KJs',
        'QJs','JTs',
        'AKo','AQo','AJo',
        'KQo',
    }),
    'SB': frozenset({
        'AA','KK','QQ','JJ','TT','99','88','77','66','55','44',
        'AKs','AQs','AJs','ATs','A9s','A8s','A7s','A6s','A5s','A4s',
        'KQs','KJs','KTs','K9s','K8s',
        'QJs','QTs','Q9s',
        'JTs','J9s',
        'T9s','T8s',
        '98s','97s',
        '87s','86s',
        '76s','65s',
        'AKo','AQo','AJo','ATo','A9o','A8o',
        'KQo','KJo','KTo',
        'QJo','JTo',
    }),
}
_OPEN['UTG+1'] = _OPEN['HJ']  # alias

# BB defend ranges (vs single raise) — keyed by raise size in BBs
_BB_DEFEND = {
    3:  frozenset({  # vs 3BB (wide)
        'AA','KK','QQ','JJ','TT','99','88','77','66','55','44','33','22',
        'AKs','AQs','AJs','ATs','A9s','A8s','A7s','A6s','A5s','A4s','A3s','A2s',
        'KQs','KJs','KTs','K9s','K8s','K7s','K6s','K5s',
        'QJs','QTs','Q9s','Q8s','Q7s',
        'JTs','J9s','J8s','J7s',
        'T9s','T8s','T7s',
        '98s','97s','96s',
        '87s','86s','85s',
        '76s','75s','74s',
        '65s','64s','54s','53s','43s',
        'AKo','AQo','AJo','ATo','A9o','A8o','A7o',
        'KQo','KJo','KTo','K9o',
        'QJo','QTo','Q9o',
        'JTo','J9o','T9o','98o','87o','76o',
    }),
    5:  frozenset({  # vs 4-5BB
        'AA','KK','QQ','JJ','TT','99','88','77','66','55','44',
        'AKs','AQs','AJs','ATs','A9s','A8s','A7s','A6s','A5s',
        'KQs','KJs','KTs','K9s','K8s',
        'QJs','QTs','Q9s',
        'JTs','J9s',
        'T9s','T8s','98s','97s',
        '87s','76s','65s',
        'AKo','AQo','AJo','ATo','A9o',
        'KQo','KJo',
        'QJo','JTo',
    }),
    99: frozenset({  # vs 6BB+ (tight)
        'AA','KK','QQ','JJ','TT','99','88','77','66',
        'AKs','AQs','AJs','ATs',
        'KQs','KJs',
        'QJs','JTs',
        'AKo','AQo','AJo',
        'KQo',
    }),
}

# 3-bet ranges (polarised: value hands + suited-ace bluffs)
_3BET_IP  = frozenset({'AA','KK','QQ','JJ','TT','AKs','AKo','AQs','KQs','A5s','A4s','A3s','76s','65s','54s'})
_3BET_OOP = frozenset({'AA','KK','QQ','JJ','AKs','AKo'})

# Tighter early-position ranges for 7-9 player tables
# Every extra player before action reaches us = opponent who could have a stronger hand
_OPEN_EARLY_LARGE = frozenset({   # UTG / UTG+1 at 7-9 player tables (~8% of hands)
    'AA','KK','QQ','JJ','TT',
    'AKs','AQs','AJs',
    'KQs',
    'AKo','AQo',
})
_OPEN_MID_LARGE = frozenset({     # UTG+2 / UTG+3 at 7-9 player tables (~13%)
    'AA','KK','QQ','JJ','TT','99',
    'AKs','AQs','AJs','ATs',
    'KQs','KJs',
    'QJs','JTs',
    'AKo','AQo','AJo',
    'KQo',
})

def _open_range(pos, n):
    """Return opening range set for given position and table size."""
    if n <= 2:   return _OPEN['BTN']
    if pos == 0: return _OPEN['BTN']           # BTN: always wide
    if pos == n - 1: return _OPEN['CO']        # CO
    if pos == n - 2: return _OPEN['HJ']        # HJ
    if pos == 1: return _OPEN['SB']            # SB
    # Early positions: tighten for large tables
    if n >= 7:
        # pos 3 = first to act (UTG), pos 4 = UTG+1, etc.
        positions_from_utg = pos - 3   # 0=UTG, 1=UTG+1, 2=UTG+2 ...
        if positions_from_utg <= 1:
            return _OPEN_EARLY_LARGE   # Very tight
        else:
            return _OPEN_MID_LARGE    # Moderately tight
    return _OPEN['UTG']  # Standard 6-max UTG

def _bb_defend(cur_bet_bbs):
    if cur_bet_bbs <= 3: return _BB_DEFEND[3]
    if cur_bet_bbs <= 5: return _BB_DEFEND[5]
    return _BB_DEFEND[99]

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: BOARD TEXTURE
# ══════════════════════════════════════════════════════════════════════════════

def _board_texture(board):
    if not board:
        return {'wet': False, 'monotone': False, 'flush_draw': False,
                'paired': False, 'connected': False, 'high_card': 0, 'dynamic': False}
    ranks = [_RANK_IDX[c[0]] for c in board]
    suits = [c[1] for c in board]
    sc = {}
    for s in suits:
        sc[s] = sc.get(s, 0) + 1
    max_suit = max(sc.values())
    monotone  = max_suit >= 3
    flush_draw = max_suit == 2
    uniq = sorted(set(ranks))
    connected = len(uniq) >= 3 and (uniq[-1] - uniq[0]) <= 4
    rc = {}
    for r in ranks:
        rc[r] = rc.get(r, 0) + 1
    paired   = any(v >= 2 for v in rc.values())
    high_card = max(ranks)
    dynamic  = (flush_draw and not monotone) or connected
    return {
        'wet':        monotone or dynamic,
        'monotone':   monotone,
        'flush_draw': flush_draw,
        'connected':  connected,
        'paired':     paired,
        'high_card':  high_card,
        'dynamic':    dynamic,
    }

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: HAND CATEGORY  (made hand + draw strength)
# ══════════════════════════════════════════════════════════════════════════════

def _hand_category(hole, board):
    """
    Returns (made_rank, draw_rank).

    made_rank:
      0 Air   1 Weak pair   2 Mid pair   3 TP-weak-kicker
      4 TPTK/Overpair   5 Two pair   6 Set/Trips
      7 Straight   8 Flush   9 Full house+

    draw_rank:
      0 None   1 Backdoor   2 Gutshot   3 OESD   4 Flush draw (or combo)
    """
    if len(board) < 3:
        return (4, 0)
    try:
        my  = [eval7.Card(c) for c in hole]
        brd = [eval7.Card(c) for c in board]
        score = eval7.evaluate(my + brd)
        htype = eval7.handtype(score)

        brd_rnks = sorted([_RANK_IDX[c[0]] for c in board], reverse=True)
        h_rnks   = sorted([_RANK_IDX[c[0]] for c in hole],  reverse=True)

        MADE = {
            'High Card': 0, 'Pair': None,  # Pair refined below
            'Two Pair': 5, 'Trips': 6,
            'Straight': 7, 'Flush': 8,
            'Full House': 9, 'Quads': 9, 'Straight Flush': 9,
        }
        made = MADE.get(htype, 0)

        if htype == 'Pair':
            # Pocket pair?
            if h_rnks[0] == h_rnks[1]:
                if h_rnks[0] > brd_rnks[0]:
                    made = 4  # Overpair
                elif len(brd_rnks) > 1 and h_rnks[0] > brd_rnks[1]:
                    made = 2  # Mid pocket pair
                else:
                    made = 1
            else:
                # Paired a board card
                brd_set = set(brd_rnks)
                matched = next((r for r in h_rnks if r in brd_set), None)
                if matched is None:
                    made = 1
                elif matched == brd_rnks[0]:
                    kicker = max(r for r in h_rnks if r != matched)
                    made = 4 if kicker >= 9 else 3
                elif len(brd_rnks) > 1 and matched == brd_rnks[1]:
                    made = 2
                else:
                    made = 1

        draw = _draw_strength(hole, board)
        return (made, draw)
    except Exception:
        return (0, 0)

def _draw_strength(hole, board):
    """0=none 1=backdoor 2=gutshot 3=OESD 4=flush-draw"""
    if len(board) < 3:
        return 0
    try:
        all_cards = hole + board
        all_r = [_RANK_IDX[c[0]] for c in all_cards]
        all_s = [c[1] for c in all_cards]
        h_s   = [c[1] for c in hole]
        h_r   = [_RANK_IDX[c[0]] for c in hole]
        # Flush draw
        if len(board) <= 4:
            for suit in 'shdc':
                if all_s.count(suit) >= 4 and h_s.count(suit) >= 1:
                    return 4
        # Straight draws
        uniq = sorted(set(all_r))
        n = len(uniq)
        if n >= 4:
            for i in range(n - 3):
                w = uniq[i:i+4]
                if w[-1]-w[0] == 3 and any(r in w for r in h_r): return 3
            for i in range(n - 3):
                w = uniq[i:i+4]
                if w[-1]-w[0] == 4 and any(r in w for r in h_r): return 2
        # Backdoor flush (flop only)
        if len(board) == 3:
            for suit in 'shdc':
                if all_s.count(suit) == 3 and h_s.count(suit) >= 1:
                    return 1
        return 0
    except Exception:
        return 0

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4: MONTE CARLO EQUITY
# ══════════════════════════════════════════════════════════════════════════════

def _equity(hole, board, n_opp, n_sims):
    try:
        my  = [eval7.Card(c) for c in hole]
        brd = [eval7.Card(c) for c in board]
        known = set(hole) | set(board)
        deck  = [c for c in _DECK if str(c) not in known]
        need_brd = 5 - len(brd)
        need_opp = 2 * n_opp
        total    = need_brd + need_opp
        if len(deck) < total:
            return 0.5
        wins = ties = 0
        for _ in range(n_sims):
            draw    = random.sample(deck, total)
            sim_brd = brd + draw[:need_brd]
            my_rank = eval7.evaluate(my + sim_brd)
            best_opp = max(
                eval7.evaluate(draw[need_brd + i*2: need_brd + i*2 + 2] + sim_brd)
                for i in range(n_opp)
            )
            if   my_rank > best_opp: wins += 1
            elif my_rank == best_opp: ties += 1
        return (wins + 0.5 * ties) / n_sims
    except Exception:
        return 0.5

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4b: RANGE-AWARE EQUITY & EXPECTED VALUE
# ══════════════════════════════════════════════════════════════════════════════
#
# Instead of MC against fully random hands, infer each opponent's likely hand
# range from their preflop actions, then sample from that range.
# This gives much more accurate equity — e.g., knowing a UTG raiser has a
# top-15% range means our AJs equity is lower than vs a random hand.

_SUITS = 'shdc'

def _range_to_pairs(range_set: frozenset) -> list:
    """Convert a canonical-hand range set to all specific (c1_str, c2_str) pairs."""
    pairs = []
    for h in range_set:
        if len(h) == 2:                         # Pocket pair e.g. 'AA'
            r = h[0]
            sl = list(_SUITS)
            for i in range(4):
                for j in range(i + 1, 4):
                    pairs.append((r + sl[i], r + sl[j]))
        elif h.endswith('s'):                   # Suited e.g. 'AKs'
            r1, r2 = h[0], h[1]
            for s in _SUITS:
                pairs.append((r1 + s, r2 + s))
        else:                                   # Offsuit e.g. 'AKo'
            r1, r2 = h[0], h[1]
            for s1 in _SUITS:
                for s2 in _SUITS:
                    if s1 != s2:
                        pairs.append((r1 + s1, r2 + s2))
    return pairs

# Pre-convert every range set we use — done once at import, costs ~1 ms
_PAIRS = {name: _range_to_pairs(rng) for name, rng in _OPEN.items()}
_PAIRS['3BET_IP']  = _range_to_pairs(_3BET_IP)
_PAIRS['3BET_OOP'] = _range_to_pairs(_3BET_OOP)
for k, v in _BB_DEFEND.items():
    _PAIRS[f'BB_{k}'] = _range_to_pairs(v)
_PAIRS['EARLY_LARGE'] = _range_to_pairs(_OPEN_EARLY_LARGE)
_PAIRS['MID_LARGE']   = _range_to_pairs(_OPEN_MID_LARGE)


def _infer_opp_range(alog: list, opp_seat: int, dealer: int, n: int) -> list | None:
    """
    Infer the list of card-pair tuples an opponent is likely to hold,
    based on their preflop actions.
    Returns None when we have no information (treat as random).
    """
    opp_pos = _pos(opp_seat, dealer, n)

    n_raises   = 0
    has_called = False

    for entry in alog:
        if entry.get('seat') != opp_seat:
            continue
        action = entry.get('action', '')
        if action in ('small_blind', 'big_blind'):
            continue
        if action in ('raise', 'all_in'):
            n_raises += 1
        elif action == 'call':
            has_called = True

    if n_raises >= 2:
        # 3-bet or 4-bet → very tight range
        key = '3BET_IP' if (opp_pos == 0 or opp_pos == n - 1) else '3BET_OOP'
        return _PAIRS[key]

    if n_raises == 1:
        # Opened the pot — use their positional opening range
        if n >= 7:
            positions_from_utg = opp_pos - 3
            if 3 <= opp_pos <= n - 3:      # early positions
                key = 'EARLY_LARGE' if positions_from_utg <= 1 else 'MID_LARGE'
            elif opp_pos == n - 1:
                key = 'CO'
            elif opp_pos == 0:
                key = 'BTN'
            elif opp_pos == 1:
                key = 'SB'
            else:
                key = 'EARLY_LARGE'
            return _PAIRS[key]
        # 6-max positions
        pos_key = {0: 'BTN', n-1: 'CO', n-2: 'HJ', 1: 'SB'}.get(opp_pos, 'UTG')
        return _PAIRS.get(pos_key, _PAIRS['UTG'])

    if has_called:
        # Called a raise → wide calling range (use loose BB defend as proxy)
        return _PAIRS['BB_3']

    return None  # No voluntary action yet → random


def _equity_vs_ranges(hole: list, board: list, opp_pair_lists: list, n_sims: int) -> float:
    """
    Monte Carlo equity where each opponent's cards are sampled from their
    inferred range rather than uniformly at random.

    opp_pair_lists: list of (pairs_list | None) per opponent.
                    None means sample uniformly from remaining deck.
    """
    try:
        my  = [eval7.Card(c) for c in hole]
        brd = [eval7.Card(c) for c in board]
        known_strs = set(hole) | set(board)

        need_brd = 5 - len(brd)
        deck_strs = [str(c) for c in _DECK if str(c) not in known_strs]

        wins = ties = 0

        for _ in range(n_sims):
            used = set(known_strs)
            opp_cards = []   # list of [eval7.Card, eval7.Card] per opponent

            for pairs in opp_pair_lists:
                if pairs:
                    # Filter pairs to those not conflicting with already-used cards
                    avail = [(c1, c2) for c1, c2 in pairs
                             if c1 not in used and c2 not in used]
                    if avail:
                        c1s, c2s = random.choice(avail)
                        opp_cards.append([eval7.Card(c1s), eval7.Card(c2s)])
                        used.add(c1s)
                        used.add(c2s)
                        continue
                # Fallback: random from remaining deck
                rem = [c for c in deck_strs if c not in used]
                if len(rem) < 2:
                    break
                c1s, c2s = random.sample(rem, 2)
                opp_cards.append([eval7.Card(c1s), eval7.Card(c2s)])
                used.add(c1s)
                used.add(c2s)

            if not opp_cards:
                continue

            # Deal remaining board cards
            rem_brd = [c for c in deck_strs if c not in used]
            if len(rem_brd) < need_brd:
                continue
            fill = random.sample(rem_brd, need_brd)
            sim_brd = brd + [eval7.Card(c) for c in fill]

            my_rank  = eval7.evaluate(my + sim_brd)
            best_opp = max(eval7.evaluate(oh + sim_brd) for oh in opp_cards)

            if   my_rank > best_opp: wins += 1
            elif my_rank == best_opp: ties += 1

        return (wins + 0.5 * ties) / n_sims if n_sims > 0 else 0.5
    except Exception:
        return 0.5


def _hand_ev(equity: float, pot: int, owed: int) -> float:
    """
    Expected chip value of calling.
    Positive → calling is profitable.
    Formula: equity × total_pot - chips_to_call
    """
    return equity * (pot + owed) - owed


def _raise_ev(equity: float, pot: int, raise_to: int, my_bet: int,
              fold_prob: float) -> float:
    """
    Expected chip value of raising.
    fold_prob: estimated probability opponent(s) fold to our raise.
    When they fold we win the current pot; when they call we go to showdown.
    """
    chips_in = raise_to - my_bet        # new chips we invest
    total_if_called = pot + 2 * chips_in  # approx pot if one opp calls
    ev_when_called = equity * total_if_called - chips_in
    ev_when_folded = pot                 # we win the pot outright
    return fold_prob * ev_when_folded + (1 - fold_prob) * ev_when_called


def _est_fold_prob(opp_pairs: list | None, raise_to: int, pot: int) -> float:
    """
    Rough estimate of how often opponents fold to our raise.
    Stronger (narrower) range → fewer folds.
    Larger raise relative to pot → more folds.
    """
    # Sizing effect: a pot-sized raise folds ~55% of the time vs random opponents
    size_ratio = raise_to / max(pot, 1)
    base_fold = min(0.75, 0.30 + 0.25 * size_ratio)

    if opp_pairs is None:
        return base_fold  # unknown range → use sizing only

    # Narrow range (strong hand) → opponent calls or re-raises more
    range_size = len(opp_pairs)
    # BTN_OPEN has ~900 pairs (wide), 3BET_OOP has ~90 pairs (tight)
    if range_size < 150:     # 3-bet range: very unlikely to fold
        return base_fold * 0.35
    if range_size < 400:     # UTG open range: folds ~50% of the time
        return base_fold * 0.65
    return base_fold         # wide range: folds often to pressure


def _bet_ev(equity: float, pot: int, bet: int, fold_prob: float) -> float:
    """
    Extra EV gained by betting `bet` chips instead of checking.
    Positive → betting is more profitable than checking.

    When opp folds: we win `pot` immediately (no showdown)
    When opp calls: our equity share is equity × (pot + 2*bet), cost is `bet`
    Vs checking:    equity × pot (approximate — opponent also checks back)
    """
    ev_bet   = fold_prob * pot + (1 - fold_prob) * (equity * (pot + 2 * bet) - bet)
    ev_check = equity * pot
    return ev_bet - ev_check


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5: OPPONENT MODELING & POSITION HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _opp_raise_freq(state, my_seat):
    ml = state.get('match_action_log', [])
    m_r = m_a = 0
    for e in ml[-100:]:
        if e.get('seat') == my_seat: continue
        a = e.get('action', '')
        if a in ('small_blind','big_blind','fold'): continue
        m_a += 1
        if a in ('raise','all_in'): m_r += 1
    # Lower threshold from 8→4 so aggression is detected after just a few hands
    mf = (m_r / m_a) if m_a >= 4 else 0.40  # default slightly aggressive until we know better
    hr = sum(1 for e in state.get('action_log',[]) if e.get('action') in ('raise','all_in') and e.get('seat') != my_seat)
    # Stronger per-hand signal: 1 raise → +0.25, 2 raises → +0.45, 3+ → capped at 1.0
    return min(1.0, mf + 0.25 * min(hr, 2))

def _dealer_seat(alog, n):
    for e in alog:
        if e.get('action') == 'small_blind':
            sb = e['seat']
            return sb if n == 2 else (sb - 1 + n) % n
    return 0

def _pos(my_seat, dealer, n):
    return (my_seat - dealer) % n

def _active_opps(players, my_seat):
    return max(1, sum(1 for p in players if not p.get('is_folded', False) and p.get('seat') != my_seat))

def _raise_action(amount, stack, my_bet):
    if amount - my_bet >= stack: return {"action": "all_in"}
    return {"action": "raise", "amount": amount}

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6: MAIN DECIDE
# ══════════════════════════════════════════════════════════════════════════════

def decide(state):
    if state.get('type') == 'warmup':
        return {"ok": True}
    try:
        hole    = state['your_cards']
        board   = state.get('community_cards', [])
        street  = state.get('street', 'preflop')
        pot     = max(state.get('pot', 100), 1)
        stack   = state.get('your_stack', 1000)
        owed    = state.get('amount_owed', 0)
        can_chk = state.get('can_check', False)
        min_r   = state.get('min_raise_to', 2 * BB)
        cur_bet = state.get('current_bet', 0)
        my_seat = state.get('seat_to_act', 0)
        my_bet  = state.get('your_bet_this_street', 0)
        players = state.get('players', [])
        alog    = state.get('action_log', [])

        if not hole: return {"action": "fold"}

        n      = max(len(players), 2)
        dealer = _dealer_seat(alog, n)
        p      = _pos(my_seat, dealer, n)
        is_btn = (p == 0)
        is_co  = (p == n - 1) and n >= 4
        is_sb  = (p == 1)
        is_bb  = (p == 2) or (n == 2 and p == 1)
        is_late  = is_btn or is_co

        n_opp      = _active_opps(players, my_seat)
        opp_agg    = _opp_raise_freq(state, my_seat)
        aggressive = opp_agg >= 0.50
        spr        = stack / pot
        hand       = _canonical(hole)

        # ── SHORT-STACK PUSH/FOLD ─────────────────────────────────────────────
        if stack < 12 * BB and street == 'preflop':
            # Against a large field, equity vs all opponents is very low even for
            # strong hands. Use min(2, n_opp) to reflect realistic callers.
            eq = _equity(hole, [], min(2, n_opp), 400)
            thresh = 0.52 if stack >= 7 * BB else 0.42
            if eq >= thresh or (owed > 0 and eq >= owed / (pot + owed) + 0.03):
                return {"action": "all_in"}
            return {"action": "check"} if can_chk else {"action": "fold"}

        # ── PREFLOP ───────────────────────────────────────────────────────────
        if street == 'preflop':
            open_to   = max(min_r, 3 * BB)
            three_bet = max(min_r, 3 * cur_bet + BB)

            if can_chk or owed == 0:
                # First in: use position range
                if hand in _open_range(p, n):
                    return _raise_action(open_to, stack, my_bet)
                # BTN steal ~30% of remaining
                if is_btn and random.random() < 0.30:
                    return _raise_action(open_to, stack, my_bet)
                # SB complete with marginal hands
                if is_sb and can_chk and random.random() < 0.25:
                    return {"action": "call"}  # limp
                return {"action": "check"} if can_chk else {"action": "fold"}

            # Facing a raise — find the raiser and compute EV vs their inferred range
            raiser_seat = next(
                (e['seat'] for e in reversed(alog)
                 if e.get('action') in ('raise', 'all_in') and e.get('seat') != my_seat),
                None
            )
            raiser_pairs = (_infer_opp_range(alog, raiser_seat, dealer, n)
                            if raiser_seat is not None else None)
            # Range-aware equity vs the raiser specifically
            eq_vs_raiser = _equity_vs_ranges(hole, [], [raiser_pairs] if raiser_pairs else [None], 400)
            req          = owed / (pot + owed)
            ev_call      = _hand_ev(eq_vs_raiser, pot, owed)

            # Pot-committed: jam or fold based on EV
            if owed >= 0.40 * stack:
                return {"action": "all_in"} if eq_vs_raiser >= 0.50 else {"action": "fold"}

            # 3-bet: only if premium hand, not vs aggressors, and EV of 3-betting > calling
            threebet_range = _3BET_IP if is_late else _3BET_OOP
            if hand in threebet_range and not aggressive and cur_bet <= 6 * BB and spr >= 5:
                fp_3bet   = _est_fold_prob(raiser_pairs, three_bet, pot)
                ev_3bet   = _raise_ev(eq_vs_raiser, pot, three_bet, my_bet, fp_3bet)
                if ev_3bet > ev_call:
                    return _raise_action(three_bet, stack, my_bet)

            # BB defend: already invested, so defend wide.
            # Call if hand is in range OR EV is near-zero (implied odds / info value).
            if is_bb:
                cur_bbs = max(1, round(cur_bet / BB))
                if hand in _bb_defend(cur_bbs):
                    return {"action": "call"}
                # Outside the table range but cheap or near break-even: still call
                if ev_call > -BB * 1.5:
                    return {"action": "call"}
                return {"action": "fold"}

            # Cold-call: range gate + allow small negative EV (implied odds)
            cold_margin = 0.01 * max(0, n - 6)
            call_rng    = _open_range(p, n)
            if hand in call_rng and ev_call > -BB:
                return {"action": "call"}
            if hand in call_rng and eq_vs_raiser >= req + cold_margin:
                return {"action": "call"}
            return {"action": "fold"}

        # ── POSTFLOP ─────────────────────────────────────────────────────────
        tex        = _board_texture(board)
        made, draw = _hand_category(hole, board)
        multiway   = n_opp >= 4
        size_mult  = 1.20 if tex['wet'] else (0.80 if tex['paired'] else 1.00)

        # Build inferred range pairs for every active opponent
        active_opp_seats = [p.get('seat') for p in players
                            if not p.get('is_folded', False) and p.get('seat') != my_seat]
        opp_pair_lists   = [_infer_opp_range(alog, s, dealer, n) for s in active_opp_seats]
        prim_pairs       = opp_pair_lists[0] if opp_pair_lists else None

        # Range-aware equity (fewer sims on the flop; more precision on river)
        sims = {3: 350, 4: 500}.get(len(board), 650)
        eq   = _equity_vs_ranges(hole, board, opp_pair_lists, sims) if opp_pair_lists else 0.5
        req  = owed / (pot + owed) if owed > 0 else 0.0

        # ── EV of every candidate action ────────────────────────────────────
        ev_call = _hand_ev(eq, pot, owed)           # positive → call profitable

        if can_chk:
            # Against aggressors never bet — any bet triggers a massive re-raise
            if aggressive:
                return {"action": "check"}

            # Evaluate several bet fractions; pick the one with the highest EV lift
            best_ev_lift = 0.0      # must beat "just check" (ev_lift > 0)
            best_bet_amt = None

            for frac in [0.33, 0.50, 0.67, 0.90]:
                bet_amt = max(min_r, int(pot * frac * size_mult))
                if bet_amt - my_bet >= stack:
                    continue                         # don't jam via this path

                # Positional / strength gates stop obviously losing bets
                if not is_late and made < 5 and draw < 3:   # OOP: 2-pair+ or strong draw
                    continue
                if multiway and made < 6 and draw < 4:       # 4-way+: set+ or flush draw
                    continue

                fp       = _est_fold_prob(prim_pairs, bet_amt, pot)
                ev_lift  = _bet_ev(eq, pot, bet_amt, fp)
                if ev_lift > best_ev_lift:
                    best_ev_lift = ev_lift
                    best_bet_amt = bet_amt

            # Jam with nuts on low SPR — always +EV
            if made >= 7 and spr < 2.0:
                return {"action": "all_in"}

            if best_bet_amt is not None:
                return _raise_action(best_bet_amt, stack, my_bet)
            return {"action": "check"}

        else:
            # ── Facing a bet ──────────────────────────────────────────────────

            if aggressive:
                if spr < 0.5 and ev_call > 0:
                    return {"action": "all_in"}
                return {"action": "call"} if ev_call > 0 else {"action": "fold"}

            # Check-raise / re-raise with strong hands
            if my_bet == 0 and not multiway:
                raise_to = max(min_r, int((pot + 2 * owed) * 0.82))
                fp_raise = _est_fold_prob(prim_pairs, raise_to, pot)
                ev_raise = _raise_ev(eq, pot, raise_to, my_bet, fp_raise)
                if ev_raise > ev_call and ev_raise > 0 and made >= 5:
                    return _raise_action(raise_to, stack, my_bet)

            if spr < 0.6 and ev_call > 0:
                return {"action": "all_in"}

            # ── Hold position — don't fold without a compelling reason ────────
            #
            # Rule 1: Tiny bets (< 20% pot) are essentially free to call.
            #   Even with 25% equity the EV is positive; implied odds make it better.
            if owed < pot * 0.20:
                return {"action": "call"}

            # Rule 2: River showdown — we can't improve our hand, so just use
            #   raw pot odds. If equity covers the price, call and see the cards.
            if street == 'river':
                return {"action": "call"} if eq >= req else {"action": "fold"}

            # Rule 3: Any made hand (pair or better) is worth calling one bet
            #   on the flop or turn, provided the bet is not pot-sized or larger.
            #   Don't give opponents free fold equity when we have showdown value.
            if made >= 1 and owed <= pot * 0.60:
                return {"action": "call"}

            # Rule 4: Strong draws have implied odds — call even if slightly
            #   below pot-odds equity because hitting pays future streets.
            if draw >= 3 and owed <= pot * 0.55:   # flush draw or OESD
                return {"action": "call"}
            if draw >= 2 and eq >= req - 0.05:     # gutshot with decent equity
                return {"action": "call"}

            # Rule 5: General EV — allow a small negative-EV call for information
            #   and implied odds (roughly one BB of implied value per call).
            if ev_call > -BB:
                return {"action": "call"}

            # Genuinely losing proposition: fold
            return {"action": "fold"}

    except Exception:
        return {"action": "check"} if state.get('can_check', False) else {"action": "fold"}