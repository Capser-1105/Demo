"""
BAT_RAC Online Direction Adapter
================================
Không dùng rule lag1 cố định. Học liên tục từ kết quả thật:

  - mode FADE  = lật đám đông (BẮT RÁC gốc)
  - mode FOLLOW = bám đám đông (tức "→BÁM")

Mỗi lần có kết quả, cập nhật rolling stats. Khi cần quyết định:
  chọn mode có WR ước lượng cao hơn (Laplace/EWMA, không số cứng tuyệt đối).

Session 17/08 18h: rule lag1→BÁM cho WR 33% (n=15, −3.7M).
Adapter này tự đảo lại về FADE khi FOLLOW đang thua.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Tuple


@dataclass
class _Bucket:
    wins: float = 0.0
    n: float = 0.0

    def update(self, is_win: bool, w: float = 1.0) -> None:
        self.n += w
        if is_win:
            self.wins += w

    def wr(self, prior_w: float = 2.0, prior_p: float = 0.5) -> float:
        # Laplace / Beta(prior) shrinkage — không hard-code ngưỡng WR
        return (self.wins + prior_w * prior_p) / (self.n + prior_w)


class BatRacOnlineAdapter:
    """
    Online selector: FADE vs FOLLOW cho tín hiệu BẮT RÁC.
    - recent: cửa sổ trượt ngắn (nhạy regime shift)
    - ewma: hệ số quên liên tục
    - global: tổng thể session
    Quyết định = argmax WR ước lượng của 2 mode.
    """

    def __init__(self, window: int = 24, ewma_alpha: float = 0.12):
        self.window = window
        self.alpha = ewma_alpha
        self._recent: Deque[Tuple[str, bool]] = deque(maxlen=window)
        self._ewma: Dict[str, _Bucket] = {
            "FADE": _Bucket(),
            "FOLLOW": _Bucket(),
        }
        self._global: Dict[str, _Bucket] = {
            "FADE": _Bucket(),
            "FOLLOW": _Bucket(),
        }
        # Lệnh đang chờ kết quả: (mode, target_side "T"/"X")
        self._pending: Optional[Tuple[str, str]] = None

    # ------------------------------------------------------------------ API
    def choose(self, crowd_side: str, run_len: int = 0) -> Tuple[str, str, str]:
        """
        crowd_side: hướng đám đông đang đu ("T" hoặc "X")
        run_len: độ dài streak hiện tại (nếu >=5 → ưu tiên FADE vì bệt dài hay gãy)
        Returns: (mode, target_side, note)
        """
        fade_wr = self._score("FADE")
        follow_wr = self._score("FOLLOW")
        n_fo = self._global["FOLLOW"].n
        n_fa = self._global["FADE"].n

        # FOLLOW chỉ khi:
        #  1) follow_wr vượt fade rõ (≥ +0.08)
        #  2) đã có ≥ 4 sample FOLLOW (tránh cold-start chọn nhầm)
        #  3) streak không quá dài (run_len < 5) — bệt dài hay gãy
        margin = 0.08
        can_follow = (
            follow_wr > fade_wr + margin
            and n_fo >= 4
            and run_len < 5
        )

        if can_follow:
            mode = "FOLLOW"
            target = crowd_side
        else:
            mode = "FADE"
            target = "X" if crowd_side == "T" else "T"

        note = (
            f"fadeWR={fade_wr:.2f} followWR={follow_wr:.2f} "
            f"nF={n_fa:.0f} nFo={n_fo:.0f} sc={run_len}"
        )
        self._pending = (mode, target)
        return mode, target, note

    def record_outcome(self, actual: str, is_win: Optional[bool] = None) -> None:
        """Gọi sau mỗi ván có cược BẮT RÁC (kể cả khi pending từ choose)."""
        if self._pending is None:
            return
        mode, target = self._pending
        self._pending = None
        if is_win is None:
            is_win = (str(actual).upper()[:1] == str(target).upper()[:1])
        self._push(mode, bool(is_win))

    def record_explicit(self, mode: str, is_win: bool) -> None:
        """Ghi nhận khi biết rõ mode đã dùng (từ reason log)."""
        mode = "FOLLOW" if "FOLLOW" in mode.upper() or "BÁM" in mode else "FADE"
        self._push(mode, bool(is_win))
        self._pending = None

    def bootstrap_from_rows(self, rows) -> int:
        """
        rows: iterable of (mode, is_win) hoặc dict có reason + is_win.
        Bootstrap nhanh từ CSV gần nhất để không cold-start.
        """
        n = 0
        for r in rows:
            if isinstance(r, (tuple, list)):
                mode, w = r[0], r[1]
            else:
                reason = str(r.get("reason", ""))
                if "BẮT RÁC" not in reason:
                    continue
                mode = "FOLLOW" if "BÁM" in reason or "→BÁM" in reason else "FADE"
                w = str(r.get("is_win", "")).lower() in ("true", "1", "1.0")
            self._push(mode, bool(w))
            n += 1
        return n

    def snapshot(self) -> Dict:
        return {
            "fade_wr": round(self._score("FADE"), 3),
            "follow_wr": round(self._score("FOLLOW"), 3),
            "fade_n": self._global["FADE"].n,
            "follow_n": self._global["FOLLOW"].n,
            "recent": list(self._recent)[-8:],
        }

    # ------------------------------------------------------------------ internal
    def _push(self, mode: str, is_win: bool) -> None:
        mode = "FOLLOW" if mode.upper().startswith("FO") else "FADE"
        self._recent.append((mode, is_win))
        self._global[mode].update(is_win)
        # EWMA: decay cả hai, cộng outcome vào mode thắng
        a = self.alpha
        for m, b in self._ewma.items():
            b.n *= (1 - a)
            b.wins *= (1 - a)
        self._ewma[mode].n += a
        if is_win:
            self._ewma[mode].wins += a

    def _score(self, mode: str) -> float:
        """Kết hợp recent + ewma + global. FOLLOW bị prior thấp hơn khi sample mỏng."""
        rec = [w for m, w in self._recent if m == mode]
        if rec:
            recent_wr = (sum(rec) + 1.0) / (len(rec) + 2.0)
            recent_n = len(rec)
        else:
            recent_wr, recent_n = 0.5, 0

        # FOLLOW: prior bi quan hơn (0.42) khi ít data — session 18h BAM = 33%
        # FADE: prior 0.52 (lịch sử anti-persist tốt hơn)
        prior_p = 0.42 if mode == "FOLLOW" else 0.52
        ewma_wr = self._ewma[mode].wr(prior_w=2.0, prior_p=prior_p)
        glob_wr = self._global[mode].wr(prior_w=4.0, prior_p=prior_p)
        glob_n = self._global[mode].n

        w_rec = min(1.0, recent_n / 5.0) * 0.45
        w_ew = 0.35
        w_gl = 0.20 + 0.10 * min(1.0, glob_n / 20.0)
        s = w_rec + w_ew + w_gl
        return (w_rec * recent_wr + w_ew * ewma_wr + w_gl * glob_wr) / s


_ADAPTER: Optional[BatRacOnlineAdapter] = None


def get_bat_rac_adapter() -> BatRacOnlineAdapter:
    global _ADAPTER
    if _ADAPTER is None:
        _ADAPTER = BatRacOnlineAdapter()
    return _ADAPTER
