from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import math


ENABLE_WR_SCALED_SIZING = False
WR_SCALING_MIN_SAMPLES = 30


class Bankroll:
    """
    V8.1 — Flat fraction + damping mạnh hơn theo loss streak (audit 2026-08-16).
    Mục tiêu: giảm DD khi chuỗi thua 3-9 xuất hiện thường xuyên.
    """

    HOUSE_EDGE = 0.02
    STOP_LOSS_PCT = 0.50

    KELLY_BASE = 1.0

    TIER_KELLY_MULTIPLIERS = {
        "SNIPER_MAX": 1.0,
        "SNIPER_HIGH": 1.0,
        "SNIPER": 0.9,
        "SNIPER_MID": 0.8,
        "SNIPER_LOW": 0.6,
        "NORMAL": 0.7,
    }

    # Kích hoạt ghost sớm hơn (audit: chuỗi 6-9 thua phổ biến)
    GHOST_TRIGGER = 5
    HARD_STOP_TRIGGER = 7
    EMERGENCY_STOP = 8
    COOLDOWN_MIN = 5

    def __init__(self, initial: float, kpi: float, *,
                 rolling_kpi: bool = True, reset_on_kpi: bool = True,
                 stop_on_kpi: bool = False, state_engine: Optional[Any] = None):

        self.initial_seed = float(initial)
        self.initial = float(initial)
        self.kpi_step = float(kpi)
        self.balance = float(initial)
        self.peak = float(initial)

        self.kpi_hits = 0
        self.kpi_next_target = self.initial + self.kpi_step
        self._kpi_log: List[dict] = []
        self._kpi_just_hit = False
        self.kpi_stop_signal = False
        self._stop_on_kpi = bool(stop_on_kpi)
        self._state_engine = state_engine
        self.locked_profit = 0.0
        self._overshoot_carry = 0.0

        self.wins = 0
        self.losses = 0
        self.win_streak = 0
        self.loss_streak = 0
        self.total_profit = 0.0
        self.current_dd = 0.0
        self.max_dd = 0.0

        self.cumulative_wins = 0
        self.cumulative_losses = 0
        self.cumulative_profit = 0.0
        self.cumulative_max_dd = 0.0

        self.ghost_mode = False
        self.stop_loss_hit = False
        self.emergency = False
        self.cooldown_until: Optional[datetime] = None

        self.volume_lock_multiplier = 1.0

        self._hard_cap_base = min(
            max(int(self.initial_seed * 0.50), 50_000),
            4_000_000
        )
        self._hard_cap = self._hard_cap_base

        self._recent_results = deque(maxlen=50)
        # Không seed synthetic nữa — bắt đầu trung tính
        self._synthetic_wr_seed_remaining = 0
        self._rolling_wr = 0.5

        self.tier_caps = {
            "SNIPER_MAX": self._hard_cap,
            "SNIPER_HIGH": int(self._hard_cap * 0.85),
            "SNIPER": int(self._hard_cap * 0.75),
            "SNIPER_MID": int(self._hard_cap * 0.55),
            "SNIPER_LOW": int(self._hard_cap * 0.35),
            "NORMAL": int(self._hard_cap * 0.35),
            "SAFE": int(self._hard_cap * 0.35),
        }
        self.dynamic_cap = self._hard_cap

        self.tier_results: Dict[str, deque] = {
            t: deque(maxlen=60) for t in list(self.tier_caps.keys())
        }
        self._recent = deque(maxlen=20)
        self._tai_results = deque(maxlen=100)
        self._xiu_results = deque(maxlen=100)
        self._regime_buf = deque(maxlen=30)

        self._sh_consec_loss = 0
        self._sh_damp_factor = 1.0
        self._consecutive_reversal_losses = 0
        self._reversal_damp_factor = 1.0

        self.rolling_kpi = bool(rolling_kpi)
        self.reset_on_kpi = bool(reset_on_kpi)
        self._regime = "MIXED"
        self._recent_sessions: deque = deque(maxlen=10)

    def _calculate_kelly_fraction(self, win_rate: float) -> float:
        if win_rate <= 0.5:
            return 0.0
        kelly_full = max(0.0, (2.0 * win_rate - 1.0))
        kelly_half = kelly_full * 0.5
        return max(0.0, min(kelly_half, 0.10))

    def _wr_scaling_factor(self) -> float:
        if not ENABLE_WR_SCALED_SIZING:
            return 1.0
        if len(self._recent_results) < WR_SCALING_MIN_SAMPLES:
            return 1.0
        wr = self._rolling_wr
        if wr <= 0.50:
            return 0.15
        if wr >= 0.55:
            return 1.0
        return 0.15 + (wr - 0.50) / 0.05 * (1.0 - 0.15)

    def _calculate_rolling_wr(self) -> float:
        if len(self._recent_results) == 0:
            return 0.5
        return sum(self._recent_results) / len(self._recent_results)

    def _update_rolling_wr(self, is_win: bool):
        self._recent_results.append(1 if is_win else 0)
        self._rolling_wr = self._calculate_rolling_wr()
        if self._synthetic_wr_seed_remaining > 0:
            self._synthetic_wr_seed_remaining -= 1

        if self._rolling_wr > 0.58:
            wr_bonus = min(0.06, (self._rolling_wr - 0.58) * 0.6)
            self._hard_cap = int(self._hard_cap_base * (1.0 + wr_bonus))
        else:
            self._hard_cap = self._hard_cap_base

    @property
    def kpi_reached(self) -> bool:
        return self.balance >= (self.initial + self.kpi_step)

    def _register_kpi_hit(self):
        self.kpi_hits += 1
        self._kpi_just_hit = True
        target = self.initial + self.kpi_step
        overshoot = max(0.0, self.balance - target)
        self._overshoot_carry = overshoot
        self.locked_profit += self.kpi_step
        self._kpi_log.append({
            "hit": self.kpi_hits,
            "at_bal": round(self.balance),
            "locked": round(self.locked_profit),
            "overshoot": round(overshoot),
            "cumulative": round(self.cumulative_profit),
            "time": datetime.now().strftime("%H:%M:%S"),
        })
        if self._stop_on_kpi:
            self.kpi_stop_signal = True

    def reset_cycle(self):
        if self._stop_on_kpi:
            return
        overshoot = self._overshoot_carry
        self._overshoot_carry = 0.0
        new_start = float(self.initial_seed) + overshoot
        self.initial = new_start
        self.balance = new_start
        self.peak = new_start
        self.kpi_next_target = new_start + self.kpi_step
        self.total_profit = 0.0
        self.current_dd = 0.0
        self.max_dd = 0.0
        self.win_streak = 0
        self.loss_streak = 0
        self.ghost_mode = False
        self.cooldown_until = None
        self.volume_lock_multiplier = 1.0
        self.stop_loss_hit = False
        self.emergency = False
        self.wins = 0
        self.losses = 0
        self._sh_consec_loss = 0
        self._sh_damp_factor = 1.0
        self._consecutive_reversal_losses = 0
        self._reversal_damp_factor = 1.0

    def _check_kpi_and_maybe_reset(self) -> bool:
        if self.kpi_reached:
            self._register_kpi_hit()
            if self.rolling_kpi and self.reset_on_kpi and not self._stop_on_kpi:
                self.reset_cycle()
            return True
        return False

    def consume_kpi_just_hit(self) -> bool:
        f = self._kpi_just_hit
        self._kpi_just_hit = False
        return f

    def _check_limits(self):
        if self.emergency:
            return
        if self.kpi_stop_signal:
            return
        floor = self.initial * (1.0 - self.STOP_LOSS_PCT)
        if self.balance <= floor:
            self.stop_loss_hit = True
            self.emergency = True

    def is_stopped(self) -> bool:
        self._check_limits()
        return self.emergency or self.kpi_stop_signal

    def in_cooldown(self) -> Tuple[bool, int]:
        if self.emergency or self.stop_loss_hit or self.kpi_stop_signal:
            return True, 999
        if self.ghost_mode and self.cooldown_until:
            if datetime.now() >= self.cooldown_until:
                self.ghost_mode = False
                self.loss_streak = 0
                self.cooldown_until = None
                return False, 0
        if self.cooldown_until and datetime.now() < self.cooldown_until:
            rem = int((self.cooldown_until - datetime.now()).total_seconds())
            return True, rem
        return False, 0

    @property
    def risk_state(self) -> str:
        if self.emergency:
            return "EMERGENCY"
        if self.ghost_mode:
            return "GHOST"
        if self.cooldown_until and datetime.now() < self.cooldown_until:
            return "COOLDOWN"
        if self.current_dd > 0.35:
            return "HIGH_DD"
        if self.current_dd > 0.20:
            return "MODERATE_DD"
        return "NORMAL"

    def stop_reason(self) -> str:
        if self.stop_loss_hit:
            return "STOP_LOSS"
        if self.kpi_stop_signal:
            return "KPI_STOP"
        if self.emergency:
            return "EMERGENCY"
        return ""

    def calculate_bet(
        self,
        tier: str,
        confidence: int = 50,
        signal_type: str = "CONTINUATION",
        session_quality: str = "GOOD",
        volatility: float = 0.3,
        learning_decay: float = 1.0,
        crowd_imbalance: float = 0.0,
        smart_money_divergence: float = 0.0,
    ) -> int:
        if self.is_stopped():
            return 0

        is_micro = self.balance < 500_000

        if is_micro:
            FRACTIONS = {
                "SNIPER_MAX": 0.08,
                "SNIPER_HIGH": 0.06,
                "SNIPER_MID": 0.04,
                "SNIPER_LOW": 0.025,
                "NORMAL": 0.02,
                "SNIPER_MIN": 0.015,
                "WEAK_ACTION": 0.015,
            }
            MIN_BET = 1_000
            ROUND_TO = 1_000
        else:
            # Giảm nhẹ so với bản cũ (audit: HIGH/MAX vẫn thua nhiều)
            FRACTIONS = {
                # V8.2 — thô hơn ~1.7x (user request 2026-08-16)
                "SNIPER_MAX": 0.028,
                "SNIPER_HIGH": 0.020,
                "SNIPER_MID": 0.014,
                "SNIPER_LOW": 0.009,
                "NORMAL": 0.007,
                "SNIPER_MIN": 0.004,
                "WEAK_ACTION": 0.004,
            }
            MIN_BET = 10_000
            ROUND_TO = 5_000

        base_frac = FRACTIONS.get(tier, 0.008 if not is_micro else 0.04)

        conf_factor = 0.80 + (min(confidence, 85) / 100.0) * 0.30
        base_frac *= conf_factor

        # DD protection
        if self.current_dd > 0.30:
            base_frac *= 0.25
        elif self.current_dd > 0.20:
            base_frac *= 0.45
        elif self.current_dd > 0.10:
            base_frac *= 0.65

        # Loss-streak damping (cân bằng: vẫn bảo vệ nhưng không quá nhát)
        if self.loss_streak >= 6:
            base_frac *= 0.35
        elif self.loss_streak >= 5:
            base_frac *= 0.45
        elif self.loss_streak >= 4:
            base_frac *= 0.55
        elif self.loss_streak >= 3:
            base_frac *= 0.70
        elif self.loss_streak >= 2:
            base_frac *= 0.85
        elif self.loss_streak >= 1:
            base_frac *= 0.95

        if crowd_imbalance > 0.30:
            base_frac *= max(0.65, 1.0 - crowd_imbalance * 0.35)

        base_frac *= self._wr_scaling_factor()
        base_frac *= self.volume_lock_multiplier

        if tier == "SNIPER_HIGH":
            base_frac *= self._sh_damp_factor
        if signal_type == "REVERSAL":
            base_frac *= self._reversal_damp_factor

        # Ghost mode: size rất nhỏ
        if self.ghost_mode:
            base_frac *= 0.40

        bet_size = int(self.balance * base_frac)
        abs_cap = int(self.balance * (0.06 if not is_micro else 0.30))

        if abs_cap < MIN_BET:
            bet_size = abs_cap
        else:
            bet_size = min(bet_size, abs_cap)
            bet_size = max(bet_size, MIN_BET)

        bet_size = (bet_size // ROUND_TO) * ROUND_TO
        if bet_size < MIN_BET and abs_cap >= MIN_BET:
            bet_size = MIN_BET
        elif bet_size <= 0 and abs_cap > 0:
            bet_size = abs_cap

        return int(bet_size)

    def update(
        self,
        is_win: bool,
        amount: int,
        side: str = "",
        tier: str = "NORMAL",
        signal_type: str = "CONTINUATION",
    ) -> None:
        self._update_rolling_wr(is_win)

        if signal_type == "REVERSAL":
            if not is_win:
                self._consecutive_reversal_losses += 1
            else:
                self._consecutive_reversal_losses = 0
                self._reversal_damp_factor = 1.0

        if self._consecutive_reversal_losses > 0:
            self._reversal_damp_factor = max(
                0.35, 1.0 - (0.22 * self._consecutive_reversal_losses)
            )

        if amount <= 0:
            self._check_kpi_and_maybe_reset()
            return

        if is_win:
            profit = int(amount * (1.0 - self.HOUSE_EDGE))
            self.balance += profit
            self.total_profit += profit
            self.cumulative_profit += profit
            self.cumulative_wins += 1
            self.wins += 1
            self.win_streak += 1
            self.loss_streak = 0
            if self.balance > self.peak:
                self.peak = self.balance
                self.current_dd = 0.0
            if self.ghost_mode:
                self.ghost_mode = False
                self.volume_lock_multiplier = 1.0
        else:
            self.balance -= amount
            self.total_profit -= amount
            self.cumulative_profit -= amount
            self.cumulative_losses += 1
            self.losses += 1
            self.loss_streak += 1
            self.win_streak = 0
            if self.balance < self.peak:
                self.current_dd = (self.peak - self.balance) / max(self.peak, 1)
                self.max_dd = max(self.max_dd, self.current_dd)
                abs_dd = self.initial_seed - self.balance
                if abs_dd > 0:
                    self.cumulative_max_dd = max(
                        self.cumulative_max_dd, abs_dd / self.initial_seed
                    )

            if self.loss_streak == self.GHOST_TRIGGER:
                self.ghost_mode = True
                self.volume_lock_multiplier = 0.45
            elif self.loss_streak == self.HARD_STOP_TRIGGER:
                self.ghost_mode = False
                self.volume_lock_multiplier = 0.25
            elif self.loss_streak >= self.EMERGENCY_STOP:
                self.emergency = True

            if tier == "SNIPER_HIGH" and not is_win:
                self._sh_consec_loss += 1
            elif is_win:
                self._sh_consec_loss = 0
                self._sh_damp_factor = 1.0

            if self._sh_consec_loss > 0:
                self._sh_damp_factor = max(
                    0.40, 1.0 - (0.18 * self._sh_consec_loss)
                )

        self._check_kpi_and_maybe_reset()

    def tai_wr(self) -> float:
        return (
            (sum(self._tai_results) / len(self._tai_results) * 100)
            if self._tai_results else 0.0
        )

    def xiu_wr(self) -> float:
        return (
            (sum(self._xiu_results) / len(self._xiu_results) * 100)
            if self._xiu_results else 0.0
        )

    def recent_wr(self) -> float:
        return (
            (sum(self._recent) / len(self._recent) * 100)
            if self._recent else 50.0
        )

    def side_bias(self) -> str:
        t = self.tai_wr()
        x = self.xiu_wr()
        n = len(self._tai_results) + len(self._xiu_results)
        if n < 10:
            return "NEUTRAL"
        if t > x + 10:
            return "TAI_BIAS"
        if x > t + 10:
            return "XIU_BIAS"
        return "NEUTRAL"

    def kpi_progress(self) -> float:
        if self.kpi_step <= 0:
            return 0.0
        return min((self.balance - self.initial) / self.kpi_step * 100, 100.0)

    @property
    def total_balance(self) -> float:
        return self.initial_seed + self.locked_profit + (self.balance - self.initial)

    def set_regime(self, regime: str):
        self._regime = regime or "MIXED"
        self._regime_buf.append(regime)

    def record_session(self, wins: int, losses: int, session_quality: str = "UNKNOWN"):
        total = wins + losses
        wr = (wins / total * 100) if total else None
        self._recent_sessions.append({
            "time": datetime.now(),
            "wins": int(wins),
            "losses": int(losses),
            "wr": round(wr, 1) if wr is not None else None,
            "quality": session_quality,
        })

    def sync_balance(self, actual_balance: float):
        if actual_balance <= 0:
            return
        self.balance = float(actual_balance)
        if self.balance > self.peak:
            self.peak = self.balance
        self.current_dd = max(0.0, self.peak - self.balance)
        self.max_dd = max(self.max_dd, self.current_dd)
        self.total_profit = self.balance - self.initial

    def snapshot_pre_reset(self, pnl: int) -> dict:
        pre_balance = self.balance + pnl
        pre_profit = pre_balance - self.initial
        pre_wins = self.wins + (1 if pnl > 0 else 0)
        pre_losses = self.losses + (1 if pnl < 0 else 0)
        pre_total = pre_wins + pre_losses
        pre_wr = round(pre_wins / max(pre_total, 1) * 100, 1)
        pre_cum_prof = self.cumulative_profit + pnl
        pre_cum_wins = self.cumulative_wins + (1 if pnl > 0 else 0)
        pre_cum_loss = self.cumulative_losses + (1 if pnl < 0 else 0)
        pre_cum_tot = pre_cum_wins + pre_cum_loss
        pre_cum_wr = round(pre_cum_wins / max(pre_cum_tot, 1) * 100, 1)
        return {
            "balance": pre_balance,
            "profit": pre_profit,
            "profit_pct": round(pre_profit / max(self.initial, 1) * 100, 2),
            "wr": pre_wr,
            "wins": pre_wins,
            "losses": pre_losses,
            "cumulative_profit": pre_cum_prof,
            "cumulative_wins": pre_cum_wins,
            "cumulative_losses": pre_cum_loss,
            "cumulative_wr": pre_cum_wr,
            "total_balance": self.total_balance,
        }

    def stats(self) -> dict:
        total = self.wins + self.losses
        cool, rem = self.in_cooldown()
        profit = self.balance - self.initial
        cum_total = self.cumulative_wins + self.cumulative_losses
        cum_wr = (self.cumulative_wins / cum_total * 100) if cum_total else 0.0

        return {
            "balance": self.balance,
            "profit": profit,
            "profit_pct": profit / max(self.initial, 1) * 100,
            "locked_profit": self.locked_profit,
            "kpi_cycle_start": self.initial,
            "total_balance": self.total_balance,
            "cumulative_profit": self.cumulative_profit,
            "cumulative_pct": self.cumulative_profit / max(self.initial_seed, 1) * 100,
            "cumulative_wins": self.cumulative_wins,
            "cumulative_losses": self.cumulative_losses,
            "cumulative_wr": round(cum_wr, 1),
            "cumulative_max_dd": round(self.cumulative_max_dd * 100, 1),
            "kpi": self.kpi_step,
            "kpi_hits": self.kpi_hits,
            "kpi_next": int(self.kpi_next_target),
            "kpi_pct": round(self.kpi_progress(), 1),
            "kpi_reached": self.kpi_reached,
            "kpi_log": list(self._kpi_log[-5:]),
            "kpi_just_hit": self._kpi_just_hit,
            "kpi_stop_signal": self.kpi_stop_signal,
            "stop_loss_hit": self.stop_loss_hit,
            "take_profit_hit": self.kpi_reached,
            "floor": self.initial,
            "initial_seed": self.initial_seed,
            "wins": self.wins,
            "losses": self.losses,
            "total_bets": total,
            "wr": (self.wins / total * 100) if total else 0.0,
            "win_streak": self.win_streak,
            "loss_streak": self.loss_streak,
            "current_dd": round(self.current_dd * 100, 1),
            "max_dd": round(self.max_dd * 100, 1),
            "ghost_mode": self.ghost_mode,
            "emergency": self.emergency,
            "cooldown": cool,
            "cooldown_rem": rem,
            "risk_state": self.risk_state,
            "tai_wr": round(self.tai_wr(), 1),
            "xiu_wr": round(self.xiu_wr(), 1),
            "bias": self.side_bias(),
            "recent_wr": round(self.recent_wr(), 1),
            "stop_reason": self.stop_reason(),
            "rolling_wr": round(self._rolling_wr * 100, 1),
            "kelly_fraction": round(self._calculate_kelly_fraction(self._rolling_wr) * 100, 1),
            "dynamic_hard_cap": self._hard_cap,
            "wr_seed_contamination_remaining": self._synthetic_wr_seed_remaining,
            "bet_sniper_max": self.calculate_bet("SNIPER_MAX", 90),
            "bet_sniper_high": self.calculate_bet("SNIPER_HIGH", 82),
            "bet_sniper": self.calculate_bet("SNIPER", 78),
            "bet_safe": self.calculate_bet("SNIPER_MID", 72),
            "bet_normal": self.calculate_bet("NORMAL", 70),
            "sh_consec_loss": self._sh_consec_loss,
            "sh_damp_factor": round(self._sh_damp_factor, 2),
            "reversal_consec_loss": self._consecutive_reversal_losses,
            "reversal_damp_factor": round(self._reversal_damp_factor, 2),
            "stop_on_kpi_mode": self._stop_on_kpi,
        }

    def bet_sizes_preview(self) -> dict:
        return {
            "SNIPER_MAX": int(self.calculate_bet("SNIPER_MAX", confidence=90)),
            "SNIPER_HIGH": int(self.calculate_bet("SNIPER_HIGH", confidence=82)),
            "SNIPER_MID": int(self.calculate_bet("SNIPER_MID", confidence=72)),
            "NORMAL": int(self.calculate_bet("NORMAL", confidence=70)),
        }
