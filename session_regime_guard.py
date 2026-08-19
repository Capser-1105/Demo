"""
session_regime_guard.py
=======================
V3.0 — FULLY ONLINE ADAPTIVE (không giờ cứng, không ngưỡng tuyệt đối cố định).

Nguyên tắc:
  - Mọi quyết định size / block / threshold đều suy ra từ
    tỷ lệ (rolling WR / baseline WR) và (signal WR / baseline).
  - Baseline = EWMA chậm + long window của CHÍNH bot → tự chuẩn hóa.
  - Hour WR và signal WR (BREAK / BAT_RAC / BREAK_SC1) học online từ kết quả thật.
  - Không có DANGER_HOURS, không có TOXIC_ROLL_WR = 0.38 cứng.
  - Chỉ còn "tốc độ học" (window, alpha) — không phải ngưỡng quyết định cược.

Vấn đề gốc đã kiểm định trên CSV:
  H22–H01 WR cao, H02–H05 / H14 WR sụp → edge non-stationary theo giờ.
  BẺ streak=1 ≈ coin-flip. BAT_RAC yếu hơn FOLLOW/BET.
  Confidence vẫn cao khi edge đã mất → miscalibration.

API:
  guard = get_session_regime_guard()
  dec = guard.evaluate(reason, confidence, tier, regime, hour, run_len=...)
  # sau kết quả:
  guard.record(is_win, reason, hour, tier, regime, run_len=...)
"""

from __future__ import annotations

from collections import deque, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import math


# ---------------------------------------------------------------------------
# Chỉ còn tốc độ học / độ nhạy — KHÔNG phải ngưỡng quyết định tuyệt đối
# ---------------------------------------------------------------------------
ROLL_WINDOW = 20          # cửa sổ ngắn phát hiện sụp nhanh
LONG_WINDOW = 90          # baseline dài hạn
SIG_WINDOW = 28           # cửa sổ theo loại tín hiệu
HOUR_WINDOW = 40          # tối đa mẫu giữ cho mỗi giờ
STREAK_WINDOW = 24        # cửa sổ theo bucket streak (sc1 vs sc2+)
MIN_ROLL = 6
MIN_SIG = 10
MIN_HOUR = 4
MIN_STREAK = 8
EWMA_ALPHA_FAST = 0.14
EWMA_ALPHA_SLOW = 0.025

# ---------------------------------------------------------------------------
# FIX (đã kiểm chứng bằng phản chứng trên log thật 19/08): baseline neo bởi
# EWMA_ALPHA_SLOW=0.025 quá ì — khi có 1 đợt nóng ngắn (vd 16/20 lệnh thắng),
# baseline bị kéo lên và mắc kẹt ở đó rất lâu vì alpha quá nhỏ để "quên".
# Hệ quả: hiệu suất bình thường sau đó (55-59%) bị so sánh nhầm với baseline
# ảo cao hơn thực tế → state báo TOXIC/DEGRADED sai, chặn nhầm tín hiệu đang
# thắng. Bằng chứng: 206 lệnh bị guard chặn trong 1 phiên thực ra có WR giả
# định 57.3% nếu được đặt — CAO HƠN cả WR các lệnh guard cho phép đặt (53.1%).
#
# REBASE: thêm 1 cửa sổ trung hạn (MEDIUM_WINDOW) độc lập với EWMA. Nếu mức
# trung hạn lệch khỏi baseline hiện tại theo CÙNG 1 HƯỚNG một cách bền vững
# qua nhiều lần cập nhật liên tiếp (không phải 1 đợt nóng/lạnh ngắn rồi đảo
# lại) — tức là hiệu suất THẬT SỰ đã đổi mức chứ không phải nhiễu ngắn hạn —
# baseline được phép "đuổi kịp" nhanh hơn nhiều so với alpha mặc định.
# ---------------------------------------------------------------------------
MEDIUM_WINDOW = 45          # cửa sổ trung hạn để phát hiện đổi mức bền vững
REBASE_MIN_N = 30           # cần tối thiểu bấy nhiêu mẫu trong cửa sổ trung hạn
REBASE_GAP = 0.05           # độ lệch tối thiểu (so với ewma_slow) mới tính là "lệch thật"
REBASE_STREAK_CAP = 6       # số lần cập nhật liên tiếp cùng hướng tối đa được tính bonus
REBASE_ALPHA_MULT = 1.5     # mỗi bước streak nhân thêm bấy nhiêu lần alpha gốc


@dataclass
class GuardDecision:
    state: str
    allow_bet: bool
    size_multiplier: float
    threshold_bonus: float
    block_aggressive: bool
    reason: str
    hour_prior_wr: float
    roll_wr: float
    baseline_wr: float
    aggressive_wr: float
    meta: Dict[str, Any] = field(default_factory=dict)


def _wilson_low(wins: int, n: int, z: float = 1.0) -> float:
    """Cận dưới Wilson (z=1 ≈ 68% CI) — tránh over-react khi n nhỏ."""
    if n <= 0:
        return 0.5
    p = wins / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, center - half)


class SessionRegimeGuard:
    """
    Guard thích nghi hoàn toàn từ data live.
    - baseline_wr: EWMA chậm + long window → "mức bình thường của chính bot"
    - roll_wr: cửa sổ ngắn → phát hiện lệch khỏi baseline
    - hour_wr[h]: học WR theo giờ
    - sig_wr[sig]: học WR theo loại tín hiệu (BREAK / BAT_RAC / NORMAL / BREAK_SC1)
    Size / block / threshold = hàm liên tục của các tỷ lệ trên.
    """

    def __init__(
        self,
        roll_window: int = ROLL_WINDOW,
        long_window: int = LONG_WINDOW,
        sig_window: int = SIG_WINDOW,
    ):
        self.roll_window = roll_window
        self.long_window = long_window
        self.sig_window = sig_window

        self._roll: deque = deque(maxlen=roll_window)
        self._long: deque = deque(maxlen=long_window)
        self._medium: deque = deque(maxlen=MEDIUM_WINDOW)
        self._rebase_streak: int = 0
        self._rebase_last_dir: int = 0
        self._sig: Dict[str, deque] = {
            "BREAK": deque(maxlen=sig_window),
            "BAT_RAC": deque(maxlen=sig_window),
            "NORMAL": deque(maxlen=sig_window),
            "OTHER": deque(maxlen=sig_window),
            "BREAK_SC1": deque(maxlen=STREAK_WINDOW),
            "BREAK_SC2P": deque(maxlen=STREAK_WINDOW),
        }
        self._hour: Dict[int, deque] = defaultdict(lambda: deque(maxlen=HOUR_WINDOW))

        self._ewma_fast: float = 0.5
        self._ewma_slow: float = 0.5
        self._ewma_init: bool = False

        self._state: str = "NORMAL"
        self._toxic_streak: int = 0
        self._total: int = 0
        self._last: Optional[GuardDecision] = None

    # ------------------------------------------------------------------
    @staticmethod
    def classify_reason(reason: str, run_len: int = 0) -> str:
        r = str(reason or "")
        if "BẺ TỬ HUYỆT" in r or "BẺ " in r or "[BẺ" in r:
            if run_len <= 1:
                return "BREAK_SC1"
            return "BREAK"
        if "BẮT RÁC" in r:
            return "BAT_RAC"
        if "[BET]" in r or "Hướng" in r or "BIAS" in r or "FOLLOW" in r or "BÁM" in r:
            return "NORMAL"
        return "OTHER"

    @staticmethod
    def _mean(buf: deque) -> Tuple[float, int]:
        n = len(buf)
        if n == 0:
            return 0.5, 0
        return sum(buf) / n, n

    def _baseline(self) -> float:
        long_m, long_n = self._mean(self._long)
        if not self._ewma_init:
            return long_m if long_n else 0.5
        if long_n >= 25:
            return 0.55 * self._ewma_slow + 0.45 * long_m
        return self._ewma_slow

    def _rebase_alpha_slow(self) -> float:
        """Tăng tốc EWMA chậm khi phát hiện hiệu suất trung hạn đã đổi mức
        BỀN VỮNG (nhiều lần cập nhật liên tiếp cùng hướng), thay vì chỉ 1
        đợt nóng/lạnh ngắn hạn rồi đảo lại. Đây là fix trực tiếp cho lỗi
        baseline neo sai đã kiểm chứng (xem ghi chú REBASE ở đầu file)."""
        medium_m, medium_n = self._mean(self._medium)
        if medium_n < REBASE_MIN_N:
            self._rebase_streak = 0
            self._rebase_last_dir = 0
            return EWMA_ALPHA_SLOW

        gap = medium_m - self._ewma_slow
        direction = 1 if gap > REBASE_GAP else (-1 if gap < -REBASE_GAP else 0)

        if direction == 0:
            self._rebase_streak = 0
            self._rebase_last_dir = 0
            return EWMA_ALPHA_SLOW

        if direction == self._rebase_last_dir:
            self._rebase_streak = min(REBASE_STREAK_CAP, self._rebase_streak + 1)
        else:
            self._rebase_streak = 1
        self._rebase_last_dir = direction

        return EWMA_ALPHA_SLOW * (1.0 + self._rebase_streak * REBASE_ALPHA_MULT)

    def _hour_wr(self, hour: int) -> Tuple[float, int]:
        return self._mean(self._hour[hour])

    def _sig_wr(self, sig: str) -> Tuple[float, int]:
        return self._mean(self._sig.get(sig, deque()))

    def _agg_wr(self) -> Tuple[float, int]:
        buf = list(self._sig["BREAK"]) + list(self._sig["BAT_RAC"]) + list(self._sig["BREAK_SC1"])
        if not buf:
            return 0.5, 0
        tail = buf[-self.sig_window:]
        return sum(tail) / len(tail), len(tail)

    # ------------------------------------------------------------------
    def record(
        self,
        is_win: bool,
        reason: str = "",
        hour: Optional[int] = None,
        tier: str = "",
        regime: str = "",
        run_len: int = 0,
    ) -> None:
        w = 1.0 if is_win else 0.0
        self._roll.append(w)
        self._long.append(w)
        self._medium.append(w)

        sig = self.classify_reason(reason, run_len=run_len)
        self._sig[sig].append(w)
        # cũng ghi vào bucket BREAK tổng nếu là BREAK_SC1 / BREAK
        if sig == "BREAK_SC1":
            self._sig["BREAK"].append(w)
        elif sig == "BREAK":
            self._sig["BREAK_SC2P"].append(w)

        if hour is None:
            hour = datetime.now().hour
        self._hour[int(hour)].append(w)
        self._total += 1

        if not self._ewma_init:
            self._ewma_fast = w
            self._ewma_slow = w
            self._ewma_init = True
        else:
            self._ewma_fast = EWMA_ALPHA_FAST * w + (1.0 - EWMA_ALPHA_FAST) * self._ewma_fast
            eff_alpha_slow = self._rebase_alpha_slow()
            self._ewma_slow = eff_alpha_slow * w + (1.0 - eff_alpha_slow) * self._ewma_slow

        self._refresh_state()

    def _refresh_state(self) -> str:
        roll_m, roll_n = self._mean(self._roll)
        base = self._baseline()
        if roll_n < MIN_ROLL:
            self._state = "NORMAL"
            self._toxic_streak = 0
            return self._state

        ratio = roll_m / max(base, 0.05)

        # Ngưỡng tương đối — scale theo chính baseline của bot
        # (không phải số tuyệt đối từ audit)
        if ratio < 0.76:
            self._state = "TOXIC"
            self._toxic_streak += 1
        elif ratio < 0.90:
            if self._state == "TOXIC":
                self._state = "RECOVERING"
            else:
                self._state = "DEGRADED"
            self._toxic_streak = 0
        elif ratio >= 1.03:
            self._state = "NORMAL"
            self._toxic_streak = 0
        else:
            if self._state == "TOXIC":
                self._state = "RECOVERING"
            elif self._state not in ("DEGRADED", "RECOVERING"):
                self._state = "NORMAL"
            self._toxic_streak = 0
        return self._state

    # ------------------------------------------------------------------
    def _adaptive_size(
        self,
        roll_m: float,
        base: float,
        conf: float,
        hour_wr: float,
        hour_n: int,
        sig_wr: float,
        sig_n: int,
        is_aggressive: bool,
        sc1_wr: float,
        sc1_n: int,
        run_len: int,
    ) -> float:
        """
        Size multiplier liên tục ∈ [0.12, 1.18], suy ra 100% từ data live.
        Không có bảng size cứng theo giờ.
        """
        base = max(base, 0.05)
        ratio = roll_m / base
        # Hàm liên tục: ratio thấp → size thấp
        size = max(0.12, min(1.18, ratio ** 1.35))

        # Miscalibration: conf cao hơn realized → phạt size
        if conf > 1.5:
            conf = conf / 100.0
        conf = max(0.45, min(0.95, conf))
        if roll_m + 0.07 < conf and len(self._roll) >= MIN_ROLL:
            gap = conf - roll_m
            size *= max(0.35, 1.0 - gap * 1.35)

        # Hour prior đã học: nếu giờ này đang kém baseline → giảm size
        if hour_n >= MIN_HOUR:
            hour_ratio = hour_wr / base
            if hour_ratio < 0.93:
                size *= max(0.40, hour_ratio ** 0.9)

        # Signal-specific
        if is_aggressive and sig_n >= MIN_SIG:
            sig_ratio = sig_wr / base
            if sig_ratio < 0.96:
                size *= max(0.38, sig_ratio)

        # BREAK streak=1 đặc biệt: học riêng
        if is_aggressive and run_len <= 1 and sc1_n >= MIN_STREAK:
            sc1_ratio = sc1_wr / base
            if sc1_ratio < 0.97:
                size *= max(0.30, sc1_ratio * 0.95)

        # Toxic streak kéo dài → decay thêm
        if self._toxic_streak >= 2:
            decay = max(0.45, 1.0 - 0.09 * (self._toxic_streak - 1))
            size *= decay

        return float(max(0.12, min(1.18, size)))

    def _adaptive_thresh_bonus(self, roll_m: float, base: float, conf: float) -> float:
        base = max(base, 0.05)
        ratio = roll_m / base
        if ratio >= 1.0:
            return 0.0
        deficit = 1.0 - ratio
        bonus = min(0.16, deficit * 0.32)
        if conf > 1.5:
            conf = conf / 100.0
        if conf >= 0.62 and ratio < 0.88:
            bonus = max(bonus, 0.035)
        return round(bonus, 4)

    # ------------------------------------------------------------------
    def evaluate(
        self,
        reason: str = "",
        confidence: float = 0.5,
        tier: str = "SNIPER_MID",
        regime: str = "",
        hour: Optional[int] = None,
        engine_decision: str = "BET",
        run_len: int = 0,
    ) -> GuardDecision:
        if hour is None:
            hour = datetime.now().hour
        hour = int(hour)

        roll_m, roll_n = self._mean(self._roll)
        base = self._baseline()
        hour_wr, hour_n = self._hour_wr(hour)

        sig = self.classify_reason(reason, run_len=run_len)
        is_agg = sig in ("BREAK", "BAT_RAC", "BREAK_SC1")
        # Lấy WR của đúng bucket
        if sig == "BREAK_SC1":
            sig_wr, sig_n = self._sig_wr("BREAK_SC1")
            # fallback nếu sc1 chưa đủ mẫu → dùng BREAK tổng
            if sig_n < MIN_STREAK:
                sig_wr, sig_n = self._sig_wr("BREAK")
        else:
            sig_wr, sig_n = self._sig_wr(sig) if is_agg else self._sig_wr("NORMAL")

        sc1_wr, sc1_n = self._sig_wr("BREAK_SC1")
        agg_wr, agg_n = self._agg_wr()

        if roll_n >= MIN_ROLL:
            state = self._refresh_state()
        else:
            state = "NORMAL"

        # Early warning từ hour prior đã học (chỉ khi roll cũng yếu)
        if hour_n >= MIN_HOUR and state == "NORMAL" and roll_n >= MIN_ROLL:
            if hour_wr < base * 0.88 and roll_m < base * 0.96:
                state = "DEGRADED"

        conf = float(confidence)
        if conf > 1.5:
            conf = conf / 100.0

        size = self._adaptive_size(
            roll_m, base, conf, hour_wr, hour_n, sig_wr, sig_n,
            is_agg, sc1_wr, sc1_n, run_len,
        )
        thresh_bonus = self._adaptive_thresh_bonus(roll_m, base, conf)

        allow = True
        block_agg = False
        notes = []

        # ---- Block aggressive chỉ khi evidence mạnh từ data live ----
        if is_agg:
            # 1) TOXIC toàn cục → chặn aggressive
            if state == "TOXIC" and sig_n >= MIN_SIG:
                block_agg = True
                allow = False
                notes.append(
                    f"[ADAPT-TOXIC] {sig} roll={roll_m*100:.0f}% base={base*100:.0f}% → BLOCK"
                )

            # 2) Signal Wilson-low rất kém so với baseline
            if sig_n >= MIN_SIG:
                wins = int(round(sig_wr * sig_n))
                w_low = _wilson_low(wins, sig_n, z=1.05)
                # Ngưỡng tương đối: w_low < baseline * 0.70
                if w_low < base * 0.70 and sig_n >= 14:
                    block_agg = True
                    allow = False
                    notes.append(
                        f"[ADAPT-SIG] {sig} wr={sig_wr*100:.0f}% wilLow={w_low*100:.0f}% "
                        f"n={sig_n} → BLOCK"
                    )

            # 3) BREAK streak=1: nếu sc1 đang coin-flip hoặc kém → chặn / giảm mạnh
            if run_len <= 1 and sc1_n >= MIN_STREAK:
                sc1_wins = int(round(sc1_wr * sc1_n))
                sc1_low = _wilson_low(sc1_wins, sc1_n, z=1.0)
                if sc1_low < base * 0.78:
                    block_agg = True
                    allow = False
                    notes.append(
                        f"[ADAPT-SC1] BẺ streak=1 wr={sc1_wr*100:.0f}% "
                        f"wilLow={sc1_low*100:.0f}% n={sc1_n} → BLOCK (coin-flip)"
                    )
                elif sc1_wr < base * 0.92:
                    # không block cứng, size đã bị giảm trong _adaptive_size
                    notes.append(
                        f"[ADAPT-SC1-SOFT] sc1 wr={sc1_wr*100:.0f}% < base → size đã giảm"
                    )

            # 4) Hour đang rất kém + aggressive → soft block nếu đã có đủ mẫu giờ
            if hour_n >= MIN_HOUR + 2 and not block_agg:
                if hour_wr < base * 0.72:
                    block_agg = True
                    allow = False
                    notes.append(
                        f"[ADAPT-HOUR] h{hour:02d} wr={hour_wr*100:.0f}% "
                        f"(base={base*100:.0f}%) → BLOCK aggressive"
                    )

        if state == "TOXIC":
            notes.append(
                f"[STATE=TOXIC] roll={roll_m*100:.0f}% base={base*100:.0f}% "
                f"ratio={roll_m/max(base,0.05):.2f} toxStreak={self._toxic_streak}"
            )
        elif state == "DEGRADED":
            notes.append(
                f"[STATE=DEGRADED] roll={roll_m*100:.0f}% base={base*100:.0f}% "
                f"ratio={roll_m/max(base,0.05):.2f}"
            )
            if hour_n >= MIN_HOUR:
                notes.append(f"hour{hour:02d} wr={hour_wr*100:.0f}% n={hour_n}")
        elif state == "RECOVERING":
            notes.append(
                f"[STATE=RECOVER] roll={roll_m*100:.0f}% base={base*100:.0f}%"
            )
        else:
            if hour_n >= MIN_HOUR and hour_wr < base * 0.95:
                notes.append(
                    f"[ADAPT-HOUR] h{hour:02d} wr={hour_wr*100:.0f}% "
                    f"(base={base*100:.0f}%) → size đã điều chỉnh"
                )

        notes.append(f"size×{size:.2f} thresh+{thresh_bonus:.3f}")
        reason_txt = " | ".join(notes) if notes else "[ADAPT] NORMAL"

        dec = GuardDecision(
            state=state,
            allow_bet=allow,
            size_multiplier=round(size, 3),
            threshold_bonus=thresh_bonus,
            block_aggressive=block_agg,
            reason=reason_txt,
            hour_prior_wr=round(hour_wr, 3) if hour_n else 0.5,
            roll_wr=round(roll_m, 3) if roll_n else 0.5,
            baseline_wr=round(base, 3),
            aggressive_wr=round(agg_wr, 3) if agg_n else 0.5,
            meta={
                "roll_n": roll_n,
                "hour_n": hour_n,
                "sig": sig,
                "sig_n": sig_n,
                "sig_wr": round(sig_wr, 3) if sig_n else 0.5,
                "sc1_n": sc1_n,
                "sc1_wr": round(sc1_wr, 3) if sc1_n else 0.5,
                "agg_n": agg_n,
                "toxic_streak": self._toxic_streak,
                "total": self._total,
                "ewma_fast": round(self._ewma_fast, 3),
                "ewma_slow": round(self._ewma_slow, 3),
                "ratio": round(roll_m / max(base, 0.05), 3) if roll_n else 1.0,
                "run_len": run_len,
            },
        )
        self._last = dec
        return dec

    # ------------------------------------------------------------------
    def bootstrap_from_rows(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        loaded = 0
        for r in rows:
            tier = str(r.get("tier", ""))
            if tier == "SKIP" or not tier:
                continue
            amount = r.get("amount", 1)
            try:
                if float(amount) <= 0:
                    continue
            except Exception:
                pass
            is_win = str(r.get("is_win", "")).lower() in ("true", "1", "yes")
            reason = str(r.get("reason", ""))
            run_len = 0
            try:
                st = str(r.get("streak", "0"))
                # streak có dạng "1T", "3X", ...
                digits = "".join(c for c in st if c.isdigit())
                run_len = int(digits) if digits else 0
            except Exception:
                run_len = 0
            hour = None
            dt = r.get("datetime") or r.get("time")
            if dt is not None:
                try:
                    if hasattr(dt, "hour"):
                        hour = dt.hour
                    else:
                        s = str(dt)
                        if " " in s and len(s) >= 13:
                            hour = int(s[11:13])
                        else:
                            hour = int(s[:2])
                except Exception:
                    pass
            if hour is None:
                try:
                    hour = int(str(r.get("time", "12"))[:2])
                except Exception:
                    hour = 12
            self.record(
                is_win=is_win,
                reason=reason,
                hour=hour,
                tier=tier,
                regime=str(r.get("regime", "")),
                run_len=run_len,
            )
            loaded += 1
        return {
            "bootstrapped": loaded,
            "state": self._state,
            "baseline_wr": round(self._baseline(), 3),
            "roll_wr": round(self._mean(self._roll)[0], 3),
            "total": self._total,
        }

    def bootstrap_from_csv(self, paths: List[str]) -> Dict[str, Any]:
        try:
            import pandas as pd
        except ImportError:
            return {"bootstrapped": 0, "error": "no pandas"}
        rows: List[Dict[str, Any]] = []
        for p in paths:
            try:
                df = pd.read_csv(p, low_memory=False)
                rows.extend(df.to_dict("records"))
            except Exception:
                continue
        return self.bootstrap_from_rows(rows)

    def stats(self) -> Dict[str, Any]:
        roll_m, roll_n = self._mean(self._roll)
        base = self._baseline()
        agg_wr, agg_n = self._agg_wr()
        by_sig = {}
        for k, buf in self._sig.items():
            m, n = self._mean(buf)
            by_sig[k] = {"wr": round(m, 3), "n": n}
        by_hour = {}
        for h, buf in sorted(self._hour.items()):
            m, n = self._mean(buf)
            if n > 0:
                by_hour[h] = {"wr": round(m, 3), "n": n}
        return {
            "state": self._state,
            "roll_wr": round(roll_m, 3),
            "roll_n": roll_n,
            "baseline_wr": round(base, 3),
            "ewma_fast": round(self._ewma_fast, 3),
            "ewma_slow": round(self._ewma_slow, 3),
            "ratio": round(roll_m / max(base, 0.05), 3) if roll_n else 1.0,
            "aggressive_wr": round(agg_wr, 3),
            "aggressive_n": agg_n,
            "by_sig": by_sig,
            "by_hour": by_hour,
            "toxic_streak": self._toxic_streak,
            "total": self._total,
            "last": (
                {
                    "state": self._last.state,
                    "allow": self._last.allow_bet,
                    "size": self._last.size_multiplier,
                    "reason": self._last.reason,
                }
                if self._last else None
            ),
        }

    def reset(self) -> None:
        self._roll.clear()
        self._long.clear()
        self._medium.clear()
        self._rebase_streak = 0
        self._rebase_last_dir = 0
        for buf in self._sig.values():
            buf.clear()
        self._hour.clear()
        self._ewma_fast = 0.5
        self._ewma_slow = 0.5
        self._ewma_init = False
        self._state = "NORMAL"
        self._toxic_streak = 0
        self._total = 0
        self._last = None


# ---------------------------------------------------------------------------
_guard_instance: Optional[SessionRegimeGuard] = None


def get_session_regime_guard(reset: bool = False) -> SessionRegimeGuard:
    global _guard_instance
    if _guard_instance is None or reset:
        _guard_instance = SessionRegimeGuard()
    return _guard_instance


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import glob
    import os

    paths = sorted(glob.glob("/home/workdir/attachments/apex_sniper_*.csv"))
    if not paths:
        paths = sorted(glob.glob("apex_sniper_*.csv"))
    print("CSV files:", len(paths))

    g = SessionRegimeGuard()
    info = g.bootstrap_from_csv(paths)
    print("Bootstrap:", info)
    print("Stats:", g.stats())

    # Walk-forward nhanh: giả lập evaluate theo từng giờ
    try:
        import pandas as pd
        dfs = [pd.read_csv(p, low_memory=False) for p in paths]
        all_df = pd.concat(dfs, ignore_index=True)
        bets = all_df[all_df["amount"] > 0].copy()
        bets["hour"] = bets["time"].astype(str).str[:2].astype(int)
        print("\n=== Hour prior đã học ===")
        st = g.stats()
        for h, v in sorted(st["by_hour"].items()):
            print(f"  H{h:02d}: WR={v['wr']*100:.1f}% n={v['n']}")
        print("\n=== Signal prior ===")
        for k, v in st["by_sig"].items():
            print(f"  {k}: WR={v['wr']*100:.1f}% n={v['n']}")
    except Exception as e:
        print("walk err:", e)
