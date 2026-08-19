"""
fade_follow_advisor.py  —  V80 (fully adaptive, không số tĩnh từ audit)

TRIẾT LÝ:
  Sàn/game này thay đổi liên tục — bất kỳ con số nào rút ra từ 1 lần audit
  (kể cả audit rất kỹ) đều có thể lỗi thời vài ngày sau. Bản V80 KHÔNG còn
  bất kỳ hằng số WR/EV nào lấy từ lịch sử cố định (không "All_rp WR56.3%",
  không "BR baseline 45.7%", không rule sc==3/gap15-25 cứng...). Toàn bộ
  quyết định giờ chỉ dựa trên 1 công cụ duy nhất — ƯỚC LƯỢNG PHÂN CẤP
  (hierarchical shrinkage) — tự tính lại mỗi lần từ chính dữ liệu đang có
  trong live memory tại thời điểm gọi, không hard-code ngưỡng WR nào.

Ý TƯỞNG:
  Mỗi tín hiệu có 1 "địa chỉ" context (origin|regime|conf|run|gap|crowd).
  Với 1 context bất kỳ, ta không bao giờ đợi "đủ mẫu" rồi mới quyết định
  (cách cũ: <8 mẫu -> bỏ qua, dùng số tĩnh) — thay vào đó LUÔN LUÔN cho ra
  1 ước lượng liên tục bằng cách "lùi dần" (backoff) qua 3 tầng, càng ít
  mẫu càng nghiêng về tầng thô hơn, không bao giờ cứng nhắc:

      P(ctx chính xác) --backoff--> P(origin+regime) --backoff--> P(toàn cục)

  Công thức backoff kiểu Bayes/Kneser-Ney (đã dùng chỗ khác trong hệ thống,
  advanced_ctw_pst_engine.py, nên đây là kỹ thuật nhất quán, không phải số
  bịa): mỗi tầng đóng góp theo (n / (n + K)), phần còn lại nhường cho tầng
  thô hơn. K chỉ là hằng số làm mượt (tốc độ tin dữ liệu), KHÔNG phải một
  WR/EV baseline — khác bản chất với các con số cũ.

  Quyết định action/size là 1 HÀM LIÊN TỤC của (điểm ước lượng p, tổng độ
  tin cậy hiệu dụng) — không có bậc thang IF/ELIF cứng theo gap/sc/regime
  nữa. Khi p càng lệch xa 0.5 VÀ đủ mẫu để tin, hệ thống càng dám đảo side
  (FADE) hoặc tự tin theo (FOLLOW full size); khi chưa đủ bằng chứng, hệ
  thống chỉ co giãn size liên tục quanh 1.0, không đảo side.

  `tier` (LOW/MID/HIGH/MAX) vẫn được dùng làm hệ số nhân size cuối — nhưng
  hệ số này giờ cũng tự học (EWMA tier WR / global WR), không phải bảng
  tra cứu tĩnh.

Toàn bộ hệ thống tự tái hiệu chỉnh theo thời gian thực — nếu game đổi tính
chất giữa chừng, live memory (cửa sổ trượt RECENT_WINDOW) sẽ tự "quên" dữ
liệu cũ và ngả theo dữ liệu mới trong vài chục ván, không cần deploy lại.
"""

from __future__ import annotations

import os
import csv
import json
import math
import re
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Deque, Tuple
from collections import deque, defaultdict
from datetime import datetime


LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fade_follow_log.csv")
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffa_live_state.json")

# ---------------------------------------------------------------------------
# Hằng số "tốc độ học" / làm mượt — KHÔNG phải WR/EV baseline. Đổi các số
# này chỉ làm hệ thống phản ứng nhanh/chậm hơn với dữ liệu mới, không mã
# hoá bất kỳ giả định nào về việc BR/BTH/NAT "vốn dĩ" tốt hay xấu.
# ---------------------------------------------------------------------------
RECENT_WINDOW = 80          # cửa sổ trượt mỗi context (càng nhỏ càng "quên" nhanh regime cũ)
K_CTX = 5.0                 # pseudo-count backoff: context chính xác -> origin+regime
K_ORIGIN = 8.0               # pseudo-count backoff: origin+regime -> toàn cục
K_GLOBAL = 6.0               # pseudo-count backoff: toàn cục -> trung lập 0.5 (khi mới khởi động)

FLIP_THRESH = 0.90          # |p-0.5|*sqrt(n_eff) cần vượt mức này mới được ĐẢO side
SIZE_SLOPE = 2.6             # độ dốc co giãn size theo (p-0.5)
SIZE_FLOOR = 0.28            # size tối thiểu khi tín hiệu đang xấu rõ nhưng chưa đủ để đảo
SIZE_CEIL = 1.0               # không bao giờ tự tăng size vượt mức Kelly gốc ở đây

TIER_EWMA_ALPHA = 0.06        # tốc độ học hệ số tier (so với global WR)


def _reg_label(regime: str) -> str:
    return "CHOP" if "CHOPPY" in (regime or "") else (
        "ALT" if "ALT" in (regime or "") else (
            "CLUST" if "CLUSTER" in (regime or "") else "OTHER"
        )
    )


def _conf_bin(c: float) -> str:
    if c < 0.62:
        return "c<62"
    if c < 0.68:
        return "c62-68"
    if c < 0.75:
        return "c68-75"
    if c < 0.80:
        return "c75-80"
    return "c>=80"


def _run_bin(sc: int) -> str:
    if sc <= 1:
        return "run1"
    if sc <= 2:
        return "run2"
    if sc <= 3:
        return "run3"
    return "run4+"


def _gap_bin(gap: Optional[float]) -> str:
    if gap is None or (isinstance(gap, float) and math.isnan(gap)):
        return "g?"
    a = abs(float(gap))
    if a < 3:
        return "g0-3"
    if a < 8:
        return "g3-8"
    if a < 15:
        return "g8-15"
    if a < 25:
        return "g15-25"
    if a < 50:
        return "g25-50"
    return "g50+"


def _vol_dir(side: str, vol_lead: Optional[str]) -> str:
    if not vol_lead or vol_lead not in ("TAI", "XIU"):
        return "unk"
    if side in ("TAI", "XIU"):
        return "with" if side == vol_lead else "against"
    return "unk"


def _crowd_dir(side: str, imb: Optional[float], thresh: float = 0.08) -> str:
    if imb is None or (isinstance(imb, float) and math.isnan(imb)):
        return "unk"
    if side == "TAI" and imb > thresh:
        return "with"
    if side == "XIU" and imb < -thresh:
        return "with"
    if side == "TAI" and imb < -thresh:
        return "against"
    if side == "XIU" and imb > thresh:
        return "against"
    return "neutral"


def _flip(side: str) -> str:
    return "XIU" if side == "TAI" else "TAI"


@dataclass
class FadeFollowRecommendation:
    action: str
    original_side: str
    recommended_side: str
    confidence_note: str
    evidence_key: Optional[str] = None
    size_multiplier: float = 1.0
    meta: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# LIVE MEMORY — 3 tầng dữ liệu, không tầng nào chứa số tĩnh, tất cả đều là
# kết quả thật được ghi nhận trong lúc chạy (hoặc bootstrap từ CSV gần nhất
# chỉ để "mồi" chứ không phải nguồn sự thật cố định — cửa sổ trượt sẽ đẩy
# dữ liệu cũ ra dần khi có kết quả mới).
# ---------------------------------------------------------------------------
class _LiveMemory:
    def __init__(self, maxlen: int = RECENT_WINDOW):
        self.maxlen = maxlen
        self.global_results: Deque[bool] = deque(maxlen=maxlen * 4)
        self.by_context: Dict[str, Deque[bool]] = defaultdict(lambda: deque(maxlen=maxlen))
        self.by_origin_regime: Dict[str, Deque[bool]] = defaultdict(lambda: deque(maxlen=maxlen * 2))
        self.by_tier: Dict[str, Deque[bool]] = defaultdict(lambda: deque(maxlen=maxlen))
        self.loss_streak: int = 0
        self.win_streak: int = 0
        self.total_logged: int = 0

    def context_key(self, regime: str, conf_bin: str, run_bin: str,
                    vol_dir: str, gap_bin: str, origin: str = "NAT") -> str:
        reg = _reg_label(regime)
        return f"{reg}|{conf_bin}|{run_bin}|{vol_dir}|{gap_bin}|{origin}"

    @staticmethod
    def origin_regime_key(regime: str, origin: str) -> str:
        return f"{_reg_label(regime)}|{origin}"

    def record(self, ctx: str, origin_regime: str, tier: str, is_win: bool) -> None:
        self.global_results.append(is_win)
        self.by_context[ctx].append(is_win)
        self.by_origin_regime[origin_regime].append(is_win)
        if tier:
            self.by_tier[tier].append(is_win)
        self.total_logged += 1
        if is_win:
            self.win_streak += 1
            self.loss_streak = 0
        else:
            self.loss_streak += 1
            self.win_streak = 0

    def _wr_n(self, buf: Deque[bool]) -> Tuple[float, int]:
        n = len(buf)
        if n == 0:
            return 0.5, 0
        return sum(buf) / n, n

    def wr(self, ctx: Optional[str] = None) -> Tuple[float, int]:
        if ctx is None:
            return self._wr_n(self.global_results)
        return self._wr_n(self.by_context.get(ctx, deque()))

    def wr_origin_regime(self, key: str) -> Tuple[float, int]:
        return self._wr_n(self.by_origin_regime.get(key, deque()))

    def wr_tier(self, tier: str) -> Tuple[float, int]:
        return self._wr_n(self.by_tier.get(tier, deque()))

    # -------------------------------------------------------------- persist
    def to_state(self) -> Dict[str, Any]:
        return {
            "global_results": list(self.global_results),
            "by_context": {k: list(v) for k, v in self.by_context.items()},
            "by_origin_regime": {k: list(v) for k, v in self.by_origin_regime.items()},
            "by_tier": {k: list(v) for k, v in self.by_tier.items()},
            "loss_streak": self.loss_streak,
            "win_streak": self.win_streak,
            "total_logged": self.total_logged,
        }

    def load_state(self, state: Dict[str, Any]) -> None:
        try:
            self.global_results = deque(state.get("global_results", []), maxlen=self.maxlen * 4)
            self.by_context = defaultdict(lambda: deque(maxlen=self.maxlen))
            for k, v in state.get("by_context", {}).items():
                self.by_context[k] = deque(v, maxlen=self.maxlen)
            self.by_origin_regime = defaultdict(lambda: deque(maxlen=self.maxlen * 2))
            for k, v in state.get("by_origin_regime", {}).items():
                self.by_origin_regime[k] = deque(v, maxlen=self.maxlen * 2)
            self.by_tier = defaultdict(lambda: deque(maxlen=self.maxlen))
            for k, v in state.get("by_tier", {}).items():
                self.by_tier[k] = deque(v, maxlen=self.maxlen)
            self.loss_streak = int(state.get("loss_streak", 0))
            self.win_streak = int(state.get("win_streak", 0))
            self.total_logged = int(state.get("total_logged", 0))
        except Exception:
            pass


_memory = _LiveMemory()


def save_state() -> bool:
    """Ghi toàn bộ live memory ra đĩa — KHÔNG phụ thuộc đường dẫn CSV nào,
    luôn nằm cạnh chính file này nên bot restart ở đâu cũng tìm thấy. Gọi
    tự động sau mỗi record_outcome() — không cần chờ CSV export."""
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(_memory.to_state(), f)
        return True
    except Exception:
        return False


def load_state() -> Dict[str, Any]:
    """Nạp live memory đã lưu từ lần chạy trước — chạy 1 lần lúc import
    module. Nếu chưa có file (lần đầu chạy), im lặng bỏ qua, để
    bootstrap_from_csv() (nếu apex_sniper_logic.py gọi) làm việc mồi thay."""
    if not os.path.exists(STATE_PATH):
        return {"loaded": False, "reason": "no state file yet"}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        _memory.load_state(state)
        return {"loaded": True, **get_live_stats()}
    except Exception as e:
        return {"loaded": False, "reason": str(e)}


# ---------------------------------------------------------------------------
# ƯỚC LƯỢNG PHÂN CẤP (hierarchical backoff) — trái tim của V80.
# Không có bước nào trong đây đọc 1 con số cố định từ audit; mọi input đều
# là (wins, n) đọc trực tiếp từ _memory tại đúng thời điểm gọi.
# ---------------------------------------------------------------------------
def _shrink(wins: int, n: int, prior_p: float, k: float) -> float:
    """Backoff 1 tầng: càng nhiều mẫu (n) càng tin dữ liệu thật, càng ít
    mẫu càng nghiêng về prior_p (ước lượng của tầng thô hơn). k = pseudo-count
    (tốc độ tin), không phải WR giả định."""
    return (wins + k * prior_p) / (n + k)


def hierarchical_estimate(ctx: str, origin_regime: str) -> Tuple[float, float]:
    """
    Trả về (p_hat, n_eff):
      p_hat  = ước lượng xác suất thắng đã qua backoff 3 tầng.
      n_eff  = "độ tin cậy hiệu dụng" (xấp xỉ số mẫu thật đã góp phần vào
               p_hat, dùng để quyết định có đủ bằng chứng để ĐẢO side không).
    """
    glob_wr, glob_n = _memory.wr(None)
    # Tầng toàn cục lùi về 0.5 khi hệ thống còn quá mới (glob_n nhỏ)
    p_global = _shrink(int(round(glob_wr * glob_n)), glob_n, 0.5, K_GLOBAL)

    org_wr, org_n = _memory.wr_origin_regime(origin_regime)
    p_origin = _shrink(int(round(org_wr * org_n)), org_n, p_global, K_ORIGIN)

    ctx_wr, ctx_n = _memory.wr(ctx)
    p_hat = _shrink(int(round(ctx_wr * ctx_n)), ctx_n, p_origin, K_CTX)

    # n_eff: gộp có trọng số giảm dần theo tầng — tầng chính xác đóng góp
    # đầy đủ, tầng thô hơn đóng góp một phần (đã "loãng" qua backoff).
    n_eff = ctx_n + 0.35 * org_n + 0.12 * glob_n
    return p_hat, n_eff


# ---------------------------------------------------------------------------
# Hệ số tier — tự học qua EWMA so với global WR, KHÔNG dùng bảng tra tĩnh.
# ---------------------------------------------------------------------------
_tier_ewma: Dict[str, float] = {}


def _tier_factor(tier: Optional[str]) -> float:
    if not tier:
        return 1.0
    twr, tn = _memory.wr_tier(tier)
    gwr, gn = _memory.wr(None)
    if tn < 4 or gn < 4:
        return 1.0
    prev = _tier_ewma.get(tier, 1.0)
    ratio = max(0.5, min(1.3, twr / max(gwr, 0.05)))
    new = TIER_EWMA_ALPHA * ratio + (1 - TIER_EWMA_ALPHA) * prev
    _tier_ewma[tier] = new
    return round(max(0.6, min(1.15, new)), 3)


# ---------------------------------------------------------------------------
# QUYẾT ĐỊNH — hàm liên tục duy nhất, thay toàn bộ chuỗi rule cũ.
# ---------------------------------------------------------------------------
def _decide(p_hat: float, n_eff: float, original_side: str) -> FadeFollowRecommendation:
    dev = p_hat - 0.5
    flip_score = dev * math.sqrt(max(n_eff, 0.0))

    if flip_score <= -FLIP_THRESH:
        new_side = _flip(original_side)
        size = max(SIZE_FLOOR, min(SIZE_CEIL, 0.55 + 1.4 * abs(dev)))
        return FadeFollowRecommendation(
            action="FADE",
            original_side=original_side,
            recommended_side=new_side,
            confidence_note=(
                f"[ADAPTIVE-FADE] p̂={p_hat*100:.1f}% n_eff={n_eff:.1f} "
                f"flip_score={flip_score:+.2f} → đảo, size {size*100:.0f}%."
            ),
            evidence_key="ADAPTIVE_FADE",
            size_multiplier=round(size, 3),
        )

    if flip_score >= FLIP_THRESH:
        size = max(SIZE_FLOOR, min(SIZE_CEIL, 0.7 + 1.2 * dev))
        return FadeFollowRecommendation(
            action="FOLLOW",
            original_side=original_side,
            recommended_side=original_side,
            confidence_note=(
                f"[ADAPTIVE-FOLLOW] p̂={p_hat*100:.1f}% n_eff={n_eff:.1f} "
                f"flip_score={flip_score:+.2f} → giữ, size {size*100:.0f}%."
            ),
            evidence_key="ADAPTIVE_FOLLOW",
            size_multiplier=round(size, 3),
        )

    # Vùng chưa đủ bằng chứng để đảo — chỉ co giãn size liên tục quanh 1.0
    # theo dấu và độ lớn của dev, không đảo side.
    size = max(SIZE_FLOOR, min(SIZE_CEIL, 1.0 + SIZE_SLOPE * dev))
    action = "FOLLOW" if size >= 0.97 else "REDUCE_SIZE"
    return FadeFollowRecommendation(
        action=action,
        original_side=original_side,
        recommended_side=original_side,
        confidence_note=(
            f"[ADAPTIVE-HOLD] p̂={p_hat*100:.1f}% n_eff={n_eff:.1f} "
            f"(chưa đủ bằng chứng để đảo, |flip_score|<{FLIP_THRESH}) → size {size*100:.0f}%."
        ),
        evidence_key="ADAPTIVE_HOLD",
        size_multiplier=round(size, 3),
    )


def evaluate(
    regime: str,
    tier: str,
    confidence: float,
    streak_cnt: int,
    bridge_status: Optional[str],
    original_side: str,
    *,
    crowd_imbalance: Optional[float] = None,
    earcp_n_active: int = 0,
    earcp_side: Optional[str] = None,
    signal_origin: str = "NAT",
    engine_already_faded: bool = False,
    system_loss_streak: Optional[int] = None,
    gap_pct: Optional[float] = None,
    vol_lead: Optional[str] = None,
) -> FadeFollowRecommendation:
    """Entry point chính. Toàn bộ logic bên trong là 1 công thức liên tục
    (hierarchical shrinkage + flip-score), không rẽ nhánh theo audit cũ."""
    conf = float(confidence or 0.5)
    if conf > 1.5:
        conf = conf / 100.0
    sc = int(streak_cnt or 0)
    gap = abs(float(gap_pct)) if gap_pct is not None else None
    vd = _vol_dir(original_side, vol_lead)
    cd = _crowd_dir(original_side, crowd_imbalance)
    crowd = cd if cd != "unk" else vd

    origin = signal_origin if signal_origin in ("NAT", "BTH", "BR") else (
        "BTH" if engine_already_faded else "NAT"
    )

    ctx = _memory.context_key(regime or "", _conf_bin(conf), _run_bin(sc), crowd, _gap_bin(gap), origin)
    org_key = _memory.origin_regime_key(regime or "", origin)

    p_hat, n_eff = hierarchical_estimate(ctx, org_key)
    rec = _decide(p_hat, n_eff, original_side)
    rec.meta = {
        "ctx": ctx, "origin_regime": org_key, "origin": origin,
        "p_hat": round(p_hat, 4), "n_eff": round(n_eff, 2),
    }

    factor = _tier_factor(tier)
    if factor != 1.0:
        rec.size_multiplier = round(max(SIZE_FLOOR, min(SIZE_CEIL, rec.size_multiplier * factor)), 3)
        rec.meta["tier_factor"] = factor

    return rec


# ---------------------------------------------------------------------------
# Logging / persistence — không đổi về hình dạng file log (tương thích các
# công cụ đọc log đã có), chỉ đổi field ctx phía trong.
# ---------------------------------------------------------------------------
def log_recommendation(sid: int, rec: FadeFollowRecommendation) -> None:
    is_new = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow([
                "datetime", "sid", "action", "original_side", "recommended_side",
                "evidence_key", "size_multiplier", "ctx", "origin_regime", "tier",
                "actual", "rec_is_win",
            ])
        meta = rec.meta or {}
        w.writerow([
            datetime.now().isoformat(), sid, rec.action, rec.original_side,
            rec.recommended_side, rec.evidence_key or "", rec.size_multiplier,
            meta.get("ctx", ""), meta.get("origin_regime", ""), meta.get("tier", ""),
            "", "",
        ])


def record_outcome(sid: int, actual_side: str, tier: str = "") -> None:
    if not os.path.exists(LOG_PATH):
        return
    rows = list(csv.DictReader(open(LOG_PATH, encoding="utf-8-sig")))
    changed = False
    for r in rows:
        if str(r.get("sid")) == str(sid) and not r.get("actual"):
            r["actual"] = actual_side
            is_win = r.get("recommended_side") == actual_side
            r["rec_is_win"] = "True" if is_win else "False"
            _memory.record(
                r.get("ctx") or "",
                r.get("origin_regime") or "",
                r.get("tier") or tier,
                is_win,
            )
            changed = True
            break
    if changed:
        fieldnames = [
            "datetime", "sid", "action", "original_side", "recommended_side",
            "evidence_key", "size_multiplier", "ctx", "origin_regime", "tier",
            "actual", "rec_is_win",
        ]
        with open(LOG_PATH, "w", newline="", encoding="utf-8-sig") as f:
            wtr = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            wtr.writeheader()
            wtr.writerows(rows)
        save_state()


def notify_system_result(is_win: bool) -> None:
    if is_win:
        _memory.win_streak += 1
        _memory.loss_streak = 0
    else:
        _memory.loss_streak += 1
        _memory.win_streak = 0


def get_live_stats() -> Dict[str, Any]:
    glob_wr, glob_n = _memory.wr(None)
    return {
        "global_wr": round(glob_wr, 4),
        "global_n": glob_n,
        "loss_streak": _memory.loss_streak,
        "win_streak": _memory.win_streak,
        "contexts_tracked": len(_memory.by_context),
        "force_skip": False,
    }


def bootstrap_from_csv(csv_paths: List[str]) -> Dict[str, Any]:
    """Chỉ dùng để MỒI live memory lúc khởi động (tránh cold-start hoàn
    toàn) — không phải nguồn sự thật cố định. Cửa sổ trượt (RECENT_WINDOW)
    sẽ tự đẩy dần dữ liệu này ra khi có kết quả mới thật trong phiên."""
    try:
        import pandas as pd
    except ImportError:
        return {"bootstrapped": 0}
    loaded = 0
    for p in csv_paths:
        if not os.path.exists(p):
            continue
        try:
            df = pd.read_csv(p, low_memory=False)
        except Exception:
            continue
        if "tier" not in df.columns:
            continue
        bets = df[df["tier"].astype(str) != "SKIP"]
        for _, row in bets.iterrows():
            try:
                side = str(row.get("side", ""))
                actual = str(row.get("actual", ""))
                if side not in ("TAI", "XIU") or actual not in ("TAI", "XIU"):
                    continue
                regime = str(row.get("regime", ""))
                conf = float(row.get("confidence", 50))
                if conf > 1.5:
                    conf = conf / 100.0
                sc_raw = str(row.get("streak", "1"))
                m = re.match(r"(\d+)", sc_raw)
                sc = int(m.group(1)) if m else 1
                gap = row.get("gap_pct", None)
                try:
                    gap = float(gap) if gap is not None and str(gap) not in ("", "nan") else None
                except Exception:
                    gap = None
                vol_lead = str(row.get("vol_lead", "") or "")
                vd = _vol_dir(side, vol_lead if vol_lead in ("TAI", "XIU") else None)
                reason_str = str(row.get("reason", "") or "")
                if "BẺ TỬ HUYỆT" in reason_str:
                    origin = "BTH"
                elif "BẮT RÁC" in reason_str:
                    origin = "BR"
                else:
                    origin = "NAT"
                tier = str(row.get("tier", "") or "")
                ctx = _memory.context_key(regime, _conf_bin(conf), _run_bin(sc), vd, _gap_bin(gap), origin)
                org_key = _memory.origin_regime_key(regime, origin)
                _memory.record(ctx, org_key, tier, side == actual)
                loaded += 1
            except Exception:
                continue
    _memory.loss_streak = 0
    _memory.win_streak = 0
    save_state()
    return {"bootstrapped": loaded, **get_live_stats()}


# ---------------------------------------------------------------------------
# Tự nạp state đã lưu ngay khi module được import (không chỉ khi chạy trực
# tiếp) — đây là lý do chính khiến bot KHÔNG còn "khởi động lạnh" mỗi lần
# restart nữa, bất kể apex_sniper_logic.py có tìm thấy CSV để bootstrap hay
# không. An toàn tuyệt đối: nếu chưa từng có file state (lần chạy đầu tiên),
# hàm chỉ trả "loaded: False" và mọi thứ hoạt động như trước (khởi động từ
# 0, giống hệt hành vi cũ), không có gì bị phá vỡ.
# ---------------------------------------------------------------------------
_auto_load_result: Dict[str, Any] = load_state()


if __name__ == "__main__":
    import glob

    print("=== V80 self-test: state đã tự nạp lúc import? ===")
    print("_auto_load_result:", _auto_load_result)

    print("=== V80 self-test: khởi động lạnh (không bootstrap) ===")
    r0 = evaluate("CHOPPY (RAC)", "SNIPER_MID", 0.64, 1, None, "TAI",
                  gap_pct=20.0, vol_lead="XIU", signal_origin="BR")
    assert r0.action == "FOLLOW" and r0.size_multiplier == 1.0, r0
    print("OK lạnh (n_eff=0) -> trung lập, size 1.0:", r0.action, r0.size_multiplier)

    print("\n=== V80 self-test: bơm 15 thua liên tiếp vào đúng 1 context ===")
    ctx_probe = None
    for i in range(15):
        r = evaluate("CHOPPY (RAC)", "SNIPER_HIGH", 0.68, 1, "CONFIRMED", "TAI",
                      gap_pct=20.0, vol_lead="XIU", signal_origin="BTH")
        ctx_probe = r.meta["ctx"]
        org_key = r.meta["origin_regime"]
        _memory.record(ctx_probe, org_key, "SNIPER_HIGH", False)
    r_after = evaluate("CHOPPY (RAC)", "SNIPER_HIGH", 0.68, 1, "CONFIRMED", "TAI",
                        gap_pct=20.0, vol_lead="XIU", signal_origin="BTH")
    assert r_after.action == "FADE", r_after
    print("OK sau 15 thua liên tiếp -> tự đảo FADE:", r_after.action, r_after.confidence_note)

    print("\n=== V80 self-test: bơm 15 thắng liên tiếp vào 1 context khác ===")
    for i in range(15):
        r = evaluate("ALTERNATING (NHAY)", "SNIPER_MID", 0.65, 1, None, "XIU",
                      gap_pct=10.0, vol_lead="TAI", signal_origin="NAT")
        ctx2 = r.meta["ctx"]
        org2 = r.meta["origin_regime"]
        _memory.record(ctx2, org2, "SNIPER_MID", True)
    r_win = evaluate("ALTERNATING (NHAY)", "SNIPER_MID", 0.65, 1, None, "XIU",
                      gap_pct=10.0, vol_lead="TAI", signal_origin="NAT")
    assert r_win.action == "FOLLOW" and r_win.size_multiplier >= 0.95, r_win
    print("OK sau 15 thắng liên tiếp -> FOLLOW size cao:", r_win.action, r_win.size_multiplier)

    print("\n=== V80 self-test: mẫu mỏng (3 thua) KHÔNG đủ để đảo, chỉ giảm size ===")
    for i in range(3):
        r = evaluate("CLUSTERING (BET)", "SNIPER_LOW", 0.60, 2, None, "TAI",
                      gap_pct=15.0, vol_lead="XIU", signal_origin="NAT")
        ctx3 = r.meta["ctx"]; org3 = r.meta["origin_regime"]
        _memory.record(ctx3, org3, "SNIPER_LOW", False)
    r_thin = evaluate("CLUSTERING (BET)", "SNIPER_LOW", 0.60, 2, None, "TAI",
                       gap_pct=15.0, vol_lead="XIU", signal_origin="NAT")
    assert r_thin.action in ("FOLLOW", "REDUCE_SIZE"), r_thin
    assert r_thin.size_multiplier < 1.0, r_thin
    print("OK mẫu mỏng -> chưa đảo, chỉ co size:", r_thin.action, r_thin.size_multiplier)

    print("\n=== V80 self-test: bootstrap từ CSV thật (nếu có) ===")
    paths = sorted(glob.glob("/home/workdir/attachments/apex_sniper_*.csv"))
    if not paths:
        paths = sorted(glob.glob("apex_sniper_*.csv"))
    info = bootstrap_from_csv(paths)
    print("Bootstrap:", info)

    print("\n[OK] V80 self-test PASSED — toàn bộ quyết định đến từ live memory,")
    print("     không có ngưỡng WR/EV nào hard-code từ audit cũ.")
