"""
rhythm_intelligence.py — Lớp đọc nhịp sàn (không cắt rule, chỉ thích ứng).

Mục tiêu:
  Cùng một rule BẺ / BẮT RÁC / FOLLOW có thể mạnh ở session này và gần như
  im ở session khác — vì stickiness / flip-rate / micro-flow khác nhau.

API chính:
  measure_stickiness(seq, pattern_kind, lookback) -> float  [0,1]
  rolling_flip_rate(seq, window) -> float                  [0,1]
  adaptive_break_confidence(stickiness, trap, micro_div, flip_rate) -> float
  adaptive_fade_confidence(stickiness, regime, flip_rate) -> float
  dice_flow_signal(history_dicts) -> {side, prob, note}
  sum_path_signal(totals) -> {side, prob, note}
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 1. Stickiness — pattern có đang "bám" không?
# ---------------------------------------------------------------------------

def rolling_flip_rate(seq: List[str], window: int = 10) -> float:
    """Tỷ lệ đảo chiều trên `window` phiên gần nhất. 0.5 ≈ random, >0.6 ≈ ALT sống."""
    if len(seq) < 2:
        return 0.5
    w = seq[-window:] if len(seq) >= window else seq
    if len(w) < 2:
        return 0.5
    flips = sum(1 for i in range(1, len(w)) if w[i] != w[i - 1])
    return flips / (len(w) - 1)


def measure_period_stickiness(seq: List[str], period: int, lookback: int = 40) -> float:
    """
    Với chu kỳ p: trong lookback phiên, mỗi lần có thể kiểm tra
    seq[i] == seq[i-p] thì đếm hit; stickiness = hit / trials.
    Cao = pattern đang tiếp tục; thấp = pattern đang gãy / giả.
    """
    if period < 1 or len(seq) < period + 2:
        return 0.5
    window = seq[-lookback:] if len(seq) > lookback else seq
    hits = trials = 0
    for i in range(period, len(window)):
        trials += 1
        if window[i] == window[i - period]:
            hits += 1
    if trials < 3:
        return 0.5
    return hits / trials


def measure_streak_stickiness(seq: List[str], lookback: int = 40) -> float:
    """
    Đo 'bền bệt': trong lookback, tỷ lệ phiên tiếp tục cùng phía với phiên trước.
    Cao = CLUSTERING thật; thấp = hay gãy.
    """
    if len(seq) < 3:
        return 0.5
    window = seq[-lookback:] if len(seq) > lookback else seq
    same = sum(1 for i in range(1, len(window)) if window[i] == window[i - 1])
    return same / (len(window) - 1)


def measure_bridge_stickiness(
    seq: List[str],
    bridge: Optional[Dict[str, Any]],
    lookback: int = 40,
) -> float:
    """
    Stickiness tương ứng loại bridge đang CONFIRMED.
    PERIOD → period stickiness; BET → streak stickiness; NGHIENG → lean persistence.
    """
    if not bridge or not seq:
        return 0.5
    name = str(bridge.get("name", ""))
    note = str(bridge.get("note", ""))
    if name == "PERIOD" or "PERIOD" in name or "Motif" in note:
        p = int(bridge.get("period") or 2)
        return measure_period_stickiness(seq, p, lookback=lookback)
    if name == "BET" or "BET" in name or "Bet " in note:
        return measure_streak_stickiness(seq, lookback=lookback)
    if "NGHIENG" in name or "Nghiêng" in note:
        w = min(lookback, len(seq))
        window = seq[-w:]
        t = window.count("T")
        majority = max(t, w - t) / w if w else 0.5
        return majority
    return 1.0 - rolling_flip_rate(seq, window=min(12, len(seq)))


# ---------------------------------------------------------------------------
# 2. Adaptive confidence cho BẺ / BẮT RÁC (không hard-code 0.68/0.64)
# ---------------------------------------------------------------------------

def adaptive_break_confidence(
    stickiness: float,
    is_trap: bool,
    micro_divergent: bool,
    flip_rate: float,
    bridge_len: int = 0,
) -> float:
    """
    Conf của tín hiệu PHÁ cầu (BẺ).

    Logic nhịp:
      - stickiness CAO (pattern đang bám) → phá = nguy hiểm → conf gần 0.5 (abstain-ish)
      - stickiness THẤP (pattern sắp/đang gãy) → phá có cạnh → conf tăng
      - flip_rate CAO (ALT sống, session-1 style) → phá ALT có lý hơn
      - flip_rate THẤP (session-2 low-flip) → phá dễ tự sát

    Trả về conf ∈ [0.50, 0.72] — không bao giờ hard 0.68 cố định.
    """
    raw = 0.50 + 0.35 * (0.55 - stickiness)

    if is_trap:
        raw += 0.03
    if micro_divergent:
        raw += 0.04
    if bridge_len >= 8:
        raw += 0.02
    elif bridge_len > 0 and bridge_len < 4:
        raw -= 0.03

    if flip_rate >= 0.55:
        raw += 0.05
    elif flip_rate <= 0.40:
        raw -= 0.08

    return max(0.50, min(0.72, raw))


def adaptive_fade_confidence(
    stickiness: float,
    flip_rate: float,
    regime: str = "",
) -> float:
    """
    Conf của tín hiệu FADE đám đông (BẮT RÁC) trong CHOPPY.

    Stickiness cao của NGHIENG/BET → đám đông đang đúng chiều → fade yếu.
    Stickiness thấp → nghiêng giả → fade mạnh hơn.
    """
    raw = 0.50 + 0.30 * (0.58 - stickiness)

    if flip_rate <= 0.40:
        raw -= 0.06
    elif flip_rate >= 0.55:
        raw += 0.03

    if "CHOPPY" in (regime or ""):
        raw += 0.01

    return max(0.50, min(0.70, raw))


def should_surface_break(conf: float, min_edge: float = 0.58) -> bool:
    """Chỉ đưa BẺ ra quyết định khi conf vượt ngưỡng — dưới đó coi như abstain."""
    return conf >= min_edge


def should_surface_fade(conf: float, min_edge: float = 0.58) -> bool:
    return conf >= min_edge


# ---------------------------------------------------------------------------
# 3. Dice-flow & Sum-path — khai thác xúc xắc / tổng điểm
# ---------------------------------------------------------------------------

def dice_flow_signal(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Đọc chuỗi micro-state (BOTTOM/TOP/NEUTRAL) từ history có d1,d2,d3.
    - 2+ BOTTOM liên tiếp → bias X (nén đáy kéo dài)
    - 2+ TOP liên tiếp → bias T
    - Gãy nén: BOTTOM rồi total cao (≥14) → tín hiệu đảo sang T
    """
    if len(history) < 3:
        return {"side": None, "prob": 0.5, "note": "dice_flow: insufficient", "name": "DiceFlow"}

    def _micro(h: Dict) -> str:
        d1, d2, d3 = h.get("d1", 0), h.get("d2", 0), h.get("d3", 0)
        bottoms = sum(1 for d in (d1, d2, d3) if d <= 2)
        tops = sum(1 for d in (d1, d2, d3) if d >= 5)
        if bottoms >= 2:
            return "BOTTOM"
        if tops >= 2:
            return "TOP"
        return "NEUTRAL"

    micros = [_micro(h) for h in history[-8:]]
    totals = [h.get("sum", 0) for h in history[-8:]]

    last = micros[-1]
    run = 0
    for m in reversed(micros):
        if m == last and m != "NEUTRAL":
            run += 1
        else:
            break

    side = None
    prob = 0.5
    note = "dice_flow: neutral"

    if last == "BOTTOM" and run >= 2:
        side, prob = "X", min(0.50 + 0.06 * run, 0.68)
        note = f"dice_flow: nén đáy x{run} → bias X"
    elif last == "TOP" and run >= 2:
        side, prob = "T", min(0.50 + 0.06 * run, 0.68)
        note = f"dice_flow: nén đỉnh x{run} → bias T"
    elif len(micros) >= 2 and micros[-2] == "BOTTOM" and totals[-1] >= 14:
        side, prob = "T", 0.60
        note = "dice_flow: gãy nén đáy (total≥14) → T"
    elif len(micros) >= 2 and micros[-2] == "TOP" and totals[-1] <= 7:
        side, prob = "X", 0.60
        note = "dice_flow: gãy nén đỉnh (total≤7) → X"

    return {"side": side, "prob": prob, "note": note, "name": "DiceFlow"}


def sum_path_signal(totals: List[float], window: int = 10) -> Dict[str, Any]:
    """
    Chuỗi tổng điểm: slope + biên extreme.
    - Slope dương rõ → bias T
    - Slope âm rõ → bias X
    - Nhiều extreme gần đây → tăng conf nhẹ theo hướng mean-revert ngắn
    """
    if len(totals) < 5:
        return {"side": None, "prob": 0.5, "note": "sum_path: insufficient", "name": "SumPath"}

    w = totals[-window:] if len(totals) >= window else totals
    n = len(w)
    x_mean = (n - 1) / 2.0
    y_mean = sum(w) / n
    num = sum((i - x_mean) * (w[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n)) or 1.0
    slope = num / den

    extremes = sum(1 for t in w if t <= 7 or t >= 14)
    extreme_ratio = extremes / n

    side = None
    prob = 0.5
    note = "sum_path: flat"

    if slope > 0.35:
        side, prob = "T", min(0.50 + min(slope, 1.2) * 0.10, 0.66)
        note = f"sum_path: slope=+{slope:.2f} → T"
    elif slope < -0.35:
        side, prob = "X", min(0.50 + min(abs(slope), 1.2) * 0.10, 0.66)
        note = f"sum_path: slope={slope:.2f} → X"

    if extreme_ratio >= 0.45 and side is None and len(w) >= 3:
        last = w[-1]
        if last >= 14:
            side, prob = "X", 0.56
            note = "sum_path: extreme high cluster → mild X"
        elif last <= 7:
            side, prob = "T", 0.56
            note = "sum_path: extreme low cluster → mild T"

    return {
        "side": side, "prob": prob, "note": note, "name": "SumPath",
        "slope": round(slope, 3), "extreme_ratio": round(extreme_ratio, 3),
    }


# ---------------------------------------------------------------------------
# 4. Blend helpers — trộn tín hiệu nhịp vào p_hat EARCP (không ghi đè)
# ---------------------------------------------------------------------------

def blend_signals(
    base_pT: float,
    signals: List[Dict[str, Any]],
    weights: Optional[List[float]] = None,
) -> Tuple[float, List[str]]:
    """
    Trộn các signal {side, prob} vào base_pT bằng trung bình có trọng số nhẹ.
    Signal side=None bị bỏ. Không bao giờ cho một signal chiếm >30% ảnh hưởng.
    """
    active = [
        (s, (weights[i] if weights else 1.0))
        for i, s in enumerate(signals)
        if s.get("side") in ("T", "X") and float(s.get("prob", 0.5)) > 0.52
    ]
    if not active:
        return base_pT, []

    notes = []
    p = base_pT
    for s, w in active:
        conf = float(s["prob"])
        pull = min(0.30, (conf - 0.5) * 1.2) * w
        target = 1.0 if s["side"] == "T" else 0.0
        p = p * (1.0 - pull) + target * pull
        notes.append(s.get("note") or s.get("name") or "sig")
    return max(0.05, min(0.95, p)), notes
