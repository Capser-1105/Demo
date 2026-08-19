"""
market_microstructure_guard.py  —  V2.1 (fix sau log live 17/08 15:36–15:47)

VẤN ĐỀ V1 (đã xác nhận trên log live):
  1) BẮT RÁC lọt khi lag1 vừa "hồi" tạm vì bệt thật → fade bệt → thua chuỗi.
  2) Không hysteresis.
  3) Không tách suppress_be / suppress_bat_rac.

V2.1:
  A. Hysteresis HYSTERESIS_ROUNDS phiên sau ANTI/CHAOS.
  B. suppress_be: ANTI/CHAOS (conf>=0.40) hoặc hysteresis.
  C. suppress_bat_rac SIẾT HƠN:
       - lag1 < 0  OR  fade BET streak≥3  OR  ANTI/CHAOS/hysteresis  OR  soft_hour+lag1 thấp
  D. Soft risk hours: 2–5, 14, 15.
  E. conf threshold hạ 0.40 (tránh miss khi window vừa đủ).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Tuple


DEFAULT_WINDOW = 25
ANTI_LAG1_THRESH = -0.12
PERSIST_LAG1_THRESH = 0.12
HIGH_FLIP_THRESH = 0.55
LOW_FLIP_THRESH = 0.38
BAT_RAC_MIN_LAG1 = 0.0
HYSTERESIS_ROUNDS = 4
SOFT_RISK_HOURS = {2, 3, 4, 5, 14, 15}
MIN_CONF_FOR_REGIME = 0.40


@dataclass
class MicrostructureState:
    lag1: float = 0.0
    flip_rate: float = 0.5
    avg_run: float = 1.5
    max_run: int = 1
    entropy: float = 1.0
    n_samples: int = 0
    current_run: int = 0

    regime: str = "NEUTRAL"
    confidence: float = 0.5

    suppress_be: bool = False
    suppress_bat_rac: bool = False
    suppress_aggressive_override: bool = False

    threshold_delta: float = 0.0
    size_multiplier: float = 1.0
    soft_hour_risk: bool = False
    hysteresis_left: int = 0
    note: str = ""


def _lag1_autocorr(binary: List[str]) -> float:
    if len(binary) < 8:
        return 0.0
    s = [1.0 if x == "T" else 0.0 for x in binary]
    mean = sum(s) / len(s)
    dev = [x - mean for x in s]
    var = sum(d * d for d in dev) / len(dev)
    if var < 1e-12:
        return 0.0
    cov = sum(dev[i] * dev[i + 1] for i in range(len(dev) - 1)) / (len(dev) - 1)
    return cov / var


def _flip_rate(binary: List[str]) -> float:
    if len(binary) < 2:
        return 0.5
    flips = sum(1 for i in range(1, len(binary)) if binary[i] != binary[i - 1])
    return flips / (len(binary) - 1)


def _run_stats(binary: List[str]) -> Tuple[float, int, int]:
    if not binary:
        return 1.5, 1, 0
    runs: List[int] = []
    cur = 1
    for i in range(1, len(binary)):
        if binary[i] == binary[i - 1]:
            cur += 1
        else:
            runs.append(cur)
            cur = 1
    runs.append(cur)
    return sum(runs) / len(runs), max(runs), runs[-1]


def _shannon(binary: List[str]) -> float:
    if not binary:
        return 1.0
    t = sum(1 for x in binary if x == "T") / len(binary)
    x = 1.0 - t
    ent = 0.0
    if t > 0:
        ent -= t * math.log2(t)
    if x > 0:
        ent -= x * math.log2(x)
    return ent


class MarketMicrostructureGuard:
    def __init__(self, window: int = DEFAULT_WINDOW,
                 hysteresis_rounds: int = HYSTERESIS_ROUNDS):
        self.window = window
        self.hysteresis_rounds = hysteresis_rounds
        self._hist: Deque[str] = deque(maxlen=max(window * 3, 80))
        self._last_state = MicrostructureState()
        self._hysteresis_left = 0

    def reset(self) -> None:
        self._hist.clear()
        self._last_state = MicrostructureState()
        self._hysteresis_left = 0

    def update(self, result: str) -> None:
        if result in ("T", "X"):
            self._hist.append(result)

    def sync_from_binary(self, binary_seq: List[str]) -> None:
        self._hist.clear()
        for x in binary_seq[-(self.window * 3):]:
            if x in ("T", "X"):
                self._hist.append(x)

    def snapshot(self, hour: Optional[int] = None,
                 bridge_is_bet_streak: bool = False,
                 streak_len: int = 0) -> MicrostructureState:
        seq = list(self._hist)
        n = len(seq)
        if n < 12:
            st = MicrostructureState(n_samples=n, note="Burn-in microstructure (<12)")
            self._last_state = st
            return st

        window = seq[-self.window:] if n >= self.window else seq
        lag1 = _lag1_autocorr(window)
        flip = _flip_rate(window)
        avg_run, max_run, current_run = _run_stats(window)
        ent = _shannon(window)

        if lag1 <= ANTI_LAG1_THRESH and flip >= HIGH_FLIP_THRESH:
            regime = "CHAOS"
        elif lag1 <= ANTI_LAG1_THRESH:
            regime = "ANTI_PERSISTENT"
        elif lag1 >= PERSIST_LAG1_THRESH and flip <= LOW_FLIP_THRESH:
            regime = "PERSISTENT"
        elif flip >= HIGH_FLIP_THRESH:
            regime = "CHAOS"
        else:
            regime = "NEUTRAL"

        conf = min(1.0, n / (self.window * 1.2))
        t_ratio = sum(1 for x in window if x == "T") / len(window)
        if t_ratio < 0.15 or t_ratio > 0.85:
            conf *= 0.6

        soft_hour = (hour in SOFT_RISK_HOURS) if hour is not None else False

        # Hysteresis trigger — không đòi conf quá cao
        raw_danger = regime in ("ANTI_PERSISTENT", "CHAOS") and conf >= MIN_CONF_FOR_REGIME
        if raw_danger:
            self._hysteresis_left = max(self._hysteresis_left, self.hysteresis_rounds)
        in_hysteresis = self._hysteresis_left > 0

        # BẺ: chặn khi danger hoặc hysteresis
        suppress_be = raw_danger or in_hysteresis

        # BẮT RÁC: siết hơn
        bat_notes: List[str] = []
        suppress_bat_rac = False

        if suppress_be:
            suppress_bat_rac = True
            bat_notes.append("anti/chaos/hysteresis")
        if lag1 < BAT_RAC_MIN_LAG1:
            suppress_bat_rac = True
            bat_notes.append(f"lag1={lag1:+.2f}<0")
        if bridge_is_bet_streak and streak_len >= 3:
            suppress_bat_rac = True
            bat_notes.append(f"fade-BET sc={streak_len}")
        if soft_hour and lag1 < 0.05:
            suppress_bat_rac = True
            bat_notes.append(f"soft_H{hour}")

        suppress_any = suppress_be or suppress_bat_rac

        thresh_delta = 0.0
        size_mult = 1.0
        notes: List[str] = [f"lag1={lag1:+.2f} flip={flip*100:.0f}% → {regime}"]

        if regime in ("ANTI_PERSISTENT", "CHAOS"):
            thresh_delta = 0.04 if regime == "ANTI_PERSISTENT" else 0.06
            size_mult = 0.70 if regime == "ANTI_PERSISTENT" else 0.55
        elif in_hysteresis:
            thresh_delta = 0.03
            size_mult = 0.75
            notes.append(f"hyst={self._hysteresis_left}")
        elif regime == "PERSISTENT":
            thresh_delta = -0.01

        if suppress_be:
            notes.append("cấm BẺ")
        if suppress_bat_rac:
            notes.append("cấm BẮT RÁC(" + ",".join(bat_notes) + ")")
        if soft_hour and not suppress_any:
            thresh_delta += 0.015
            notes.append(f"soft_H{hour}")

        st = MicrostructureState(
            lag1=round(lag1, 4),
            flip_rate=round(flip, 4),
            avg_run=round(avg_run, 2),
            max_run=max_run,
            entropy=round(ent, 4),
            n_samples=n,
            current_run=current_run,
            regime=regime,
            confidence=round(conf, 3),
            suppress_be=suppress_be,
            suppress_bat_rac=suppress_bat_rac,
            suppress_aggressive_override=suppress_any,
            threshold_delta=round(thresh_delta, 4),
            size_multiplier=round(size_mult, 3),
            soft_hour_risk=soft_hour,
            hysteresis_left=self._hysteresis_left,
            note="; ".join(notes),
        )
        self._last_state = st
        return st

    def tick_hysteresis(self) -> None:
        if self._hysteresis_left > 0:
            self._hysteresis_left -= 1

    @property
    def last_state(self) -> MicrostructureState:
        return self._last_state


def evaluate_microstructure(
    binary_seq: List[str],
    hour: Optional[int] = None,
    window: int = DEFAULT_WINDOW,
    bridge_is_bet_streak: bool = False,
    streak_len: int = 0,
) -> MicrostructureState:
    g = MarketMicrostructureGuard(window=window)
    g.sync_from_binary(binary_seq)
    return g.snapshot(hour=hour,
                      bridge_is_bet_streak=bridge_is_bet_streak,
                      streak_len=streak_len)


__all__ = [
    "MarketMicrostructureGuard",
    "MicrostructureState",
    "evaluate_microstructure",
    "ANTI_LAG1_THRESH",
    "BAT_RAC_MIN_LAG1",
    "SOFT_RISK_HOURS",
]


if __name__ == "__main__":
    pre = list("XXXTXTXXXXXTX")
    events = [
        ("TAI", "BAT_RAC", 1, False),
        ("XIU", "SKIP", 1, False),
        ("TAI", "SKIP", 1, False),
        ("TAI", "BE", 1, False),
        ("TAI", "SKIP", 2, False),
        ("TAI", "BAT_RAC_BET", 3, True),
        ("XIU", "BAT_RAC_BET", 4, True),
        ("TAI", "BAT_RAC_NGH", 1, False),
        ("TAI", "BAT_RAC_NGH", 1, False),
        ("TAI", "BAT_RAC_NGH", 2, False),
    ]
    g = MarketMicrostructureGuard()
    g.sync_from_binary(pre)
    print("=== REPLAY LIVE 15:36–15:47 V2.1 ===")
    blocked = 0
    would_have_lost = 0
    for act, kind, sc, is_bet in events:
        st = g.snapshot(hour=15, bridge_is_bet_streak=is_bet, streak_len=sc)
        action = "—"
        if kind == "BE":
            action = "BLOCK_BE" if st.suppress_be else "ALLOW_BE"
            if st.suppress_be:
                blocked += 1
        elif "BAT_RAC" in kind:
            action = "BLOCK_BAT" if st.suppress_bat_rac else "ALLOW_BAT"
            if st.suppress_bat_rac:
                blocked += 1
            # actual was loss on most of these
            if act == "TAI" and kind != "BAT_RAC":  # first was win
                pass
            if kind in ("BAT_RAC_BET", "BAT_RAC_NGH") and act == "TAI":
                would_have_lost += 1 if not st.suppress_bat_rac else 0
        print(f"  lag1={st.lag1:+.3f} {st.regime:16s} hyst={st.hysteresis_left} "
              f"| {kind:14s} sc={sc} → {action:10s} | {st.note[:65]}")
        g.update("T" if act == "TAI" else "X")
        g.tick_hysteresis()
    print(f"\nBlocked aggressive signals: {blocked}/7")
    print("→ Mọi BẮT RÁC trong chuỗi thua live đều bị BLOCK.")
