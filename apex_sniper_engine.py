from __future__ import annotations
import math
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

from bridge_pattern_scanner import scan_patterns
from vomm_micro import calculate_vomm, analyze_micro
from diagnostic_models import calculate_rolling_hurst
from ctw_proper import calc_ctw_prior_v2
from market_microstructure_guard import MarketMicrostructureGuard
from bat_rac_online_adapter import get_bat_rac_adapter
from session_regime_guard import get_session_regime_guard


ENABLE_VOMM_OR_RULE = True
ENABLE_REGIME_CONFLICT_ROUTING = False
ENABLE_BRIDGE_AS_5TH_GATE = False

DISABLE_ENTROPY_GATE = True
DISABLE_TRAP_GATE = True
DISABLE_VECTOR_GATE = False

ENABLE_BRIDGE_ENTROPY_OVERRIDE = True

# Microstructure guard: đo lag1/flip realtime
ENABLE_MICROSTRUCTURE_GUARD = True

# Session Regime Guard — học online WR theo giờ / signal / streak, tự điều chỉnh size & block
ENABLE_SESSION_REGIME_GUARD = True

# BẮT RÁC GIỮ NGUYÊN — không tắt cứng.
# Thay bằng Online Adapter: học live FADE vs FOLLOW, chọn mode đang thắng.
ENABLE_BAT_RAC = True
ENABLE_BAT_RAC_ONLINE = True

# BẺ: không tắt cứng. Adaptive theo micro + session regime guard
ENABLE_BREAK = True
BREAK_MIN_N_ACTIVE = 3

ENTROPY_WINDOW = 13
BRIDGE_OVERRIDE_MIN_COVERAGE = 5.0
LONG_BRIDGE_LOG_THRESHOLD = 10

# Soft baselines (chỉ dùng khi guard chưa có đủ mẫu; sau đó guard override)
CHOPPY_MIN_CONF = 0.58
MIN_N_ACTIVE_FOR_HIGH = 1
RUN1_MIN_CONF = 0.58
POST_BREAK_EXTRA_THRESH = 0.02
CHOPPY_EXTRA_THRESH = 0.01


def calc_momentum(totals: List[float]) -> str:
    if len(totals) < 3:
        return "SIDEWAY"
    s1, s2, s3 = totals[-3], totals[-2], totals[-1]
    if s3 > s2 and s2 >= s1:
        return "UP (TAI)"
    if s3 < s2 and s2 <= s1:
        return "DOWN (XIU)"
    return "SIDEWAY"


def calc_regime(binary_seq: List[str]) -> str:
    """Multi-scale regime: kết hợp cửa sổ 6 + 10 + 16."""
    if len(binary_seq) < 6:
        return "UNKNOWN"

    def _trans_rate(w: List[str]) -> float:
        if len(w) < 2:
            return 0.5
        return sum(1 for i in range(len(w) - 1) if w[i] != w[i + 1]) / (len(w) - 1)

    r6 = _trans_rate(binary_seq[-6:])
    r10 = _trans_rate(binary_seq[-10:]) if len(binary_seq) >= 10 else r6
    r16 = _trans_rate(binary_seq[-16:]) if len(binary_seq) >= 16 else r10
    rate = 0.45 * r6 + 0.35 * r10 + 0.20 * r16
    if rate > 0.62:
        return "ALTERNATING (NHAY)"
    if rate < 0.38:
        return "CLUSTERING (BET)"
    return "CHOPPY (RAC)"


def _current_run_length(binary_seq: List[str]) -> int:
    if not binary_seq:
        return 0
    last = binary_seq[-1]
    n = 0
    for x in reversed(binary_seq):
        if x == last:
            n += 1
        else:
            break
    return n


def calc_adaptive_entropy(binary_seq: List[str], regime: str) -> Dict[str, float]:
    if not binary_seq:
        return {"value": 0.0, "threshold": 0.95}
    t = sum(1 for x in binary_seq if x == "T")
    p_t = t / len(binary_seq)
    p_x = 1.0 - p_t
    ent = 0.0
    if p_t > 0:
        ent -= p_t * math.log2(p_t)
    if p_x > 0:
        ent -= p_x * math.log2(p_x)

    threshold = 0.95
    if regime == "CLUSTERING (BET)":
        threshold = 0.90
    if regime == "ALTERNATING (NHAY)":
        threshold = 0.99
    return {"value": ent, "threshold": threshold}


def detect_palindrome(binary_seq: List[str]) -> bool:
    if len(binary_seq) < 5:
        return False
    s = "".join(binary_seq)
    for length in range(5, min(10, len(s)) + 1):
        sub = s[-length:]
        if sub == sub[::-1]:
            return True
    return False


def detect_drift(binary_seq: List[str]) -> bool:
    if len(binary_seq) < 14:
        return False
    w1 = binary_seq[-7:]
    w0 = binary_seq[-14:-7]
    mean1 = sum(1 for x in w1 if x == "T") / 7.0
    mean0 = sum(1 for x in w0 if x == "T") / 7.0
    return abs(mean1 - mean0) >= 0.5


def get_micro_state(d1: int, d2: int, d3: int) -> str:
    dice = [d1, d2, d3]
    bottoms = sum(1 for d in dice if d <= 2)
    tops = sum(1 for d in dice if d >= 5)
    if bottoms >= 2:
        return "BOTTOM"
    if tops >= 2:
        return "TOP"
    return "NEUTRAL"


def calc_ctw_prior_legacy(binary_seq: List[str]) -> Dict[str, Any]:
    n = len(binary_seq)
    if n < 3:
        return {"pT": 0.5, "pX": 0.5, "best_order": 0}
    history_str = "".join(binary_seq)
    total_weight = 0.0
    weighted_pT = 0.0
    best_order = 0
    max_weight = 0.0
    max_order = min(5, n - 1)
    for order in range(0, max_order + 1):
        context = "" if order == 0 else history_str[-order:]
        match_t = 0
        match_x = 0
        for i in range(n - order - 1, -1, -1):
            if order == 0 or history_str[i:i + order] == context:
                if history_str[i + order] == "T":
                    match_t += 1
                else:
                    match_x += 1
        total_matches = match_t + match_x
        if total_matches > 0:
            p_t_order = (match_t + 0.5) / (total_matches + 1)
            weight = (order + 1) * math.sqrt(total_matches)
            weighted_pT += p_t_order * weight
            total_weight += weight
            if weight > max_weight:
                max_weight = weight
                best_order = order
    final_pT = weighted_pT / total_weight if total_weight > 0 else 0.5
    return {"pT": final_pT, "pX": 1.0 - final_pT, "best_order": best_order}


def calc_ctw_prior(binary_seq: List[str]) -> Dict[str, Any]:
    return calc_ctw_prior_v2(binary_seq, max_depth=5)


def calc_bayesian_vector_posterior(
    binary_seq_with_micro: List[Tuple[str, str]],
    ctw_prior: Dict[str, Any],
    current_micro: str,
    current_momentum: str,
) -> Dict[str, Any]:
    if len(binary_seq_with_micro) < 5:
        return {"pT": ctw_prior["pT"], "pX": ctw_prior["pX"], "is_aligned": False}

    count_t = count_x = like_t = like_x = 0
    for res, micro in binary_seq_with_micro:
        if res == "T":
            count_t += 1
            if micro == current_micro:
                like_t += 1
        else:
            count_x += 1
            if micro == current_micro:
                like_x += 1

    l_t = (like_t + 0.5) / (count_t + 1)
    l_x = (like_x + 0.5) / (count_x + 1)
    marginal = l_t * ctw_prior["pT"] + l_x * ctw_prior["pX"]
    post_t = (l_t * ctw_prior["pT"]) / marginal if marginal > 0 else ctw_prior["pT"]
    post_x = (l_x * ctw_prior["pX"]) / marginal if marginal > 0 else ctw_prior["pX"]

    if current_momentum == "UP (TAI)":
        post_t *= 1.1
        post_x *= 0.9
    if current_momentum == "DOWN (XIU)":
        post_x *= 1.1
        post_t *= 0.9

    total = post_t + post_x
    if total > 0:
        post_t /= total
        post_x /= total

    prior_target = "T" if ctw_prior["pT"] >= ctw_prior["pX"] else "X"
    post_target = "T" if post_t >= post_x else "X"
    is_aligned = (prior_target == post_target) and (
        (post_target == "T" and current_momentum != "DOWN (XIU)") or
        (post_target == "X" and current_momentum != "UP (TAI)")
    )
    return {"pT": post_t, "pX": post_x, "is_aligned": is_aligned, "target": post_target}


def calc_kelly_tier(prob: float) -> Dict[str, Any]:
    if prob < 0.55:
        return {"tier": "SKIP", "f_star": 0.0, "multiplier": 0.0, "cap": 0.0, "safe_frac": 0.0}

    if prob >= 0.75:
        multiplier, tier, cap = 0.28, "MAX", 0.018
    elif prob >= 0.68:
        multiplier, tier, cap = 0.20, "HIGH", 0.014
    elif prob >= 0.62:
        multiplier, tier, cap = 0.14, "MID", 0.010
    else:
        multiplier, tier, cap = 0.10, "LOW", 0.006

    b = 0.98
    q = 1.0 - prob
    f_star = (b * prob - q) / b
    if f_star <= 0:
        return {"tier": "SKIP", "f_star": f_star, "multiplier": 0.0, "cap": 0.0, "safe_frac": 0.0}

    safe_frac = min(f_star * multiplier, cap)
    return {"tier": tier, "f_star": f_star, "multiplier": multiplier, "cap": cap, "safe_frac": safe_frac}


class ApexSniperEngine:
    def __init__(self, max_history: int = 200):
        self._history: deque = deque(maxlen=max_history)
        self._last_signal: dict = {}
        # Stateful expert pool + EARCP (tránh rebuild mỗi lần get_signal)
        self._bridge_pool = None
        self._earcp_ctrl = None
        self._earcp_fitted_len = 0
        # Microstructure guard — đo lag1/flip realtime
        self._micro_guard = MarketMicrostructureGuard(window=25)
        # Online FADE/FOLLOW selector cho BẮT RÁC (học từ kết quả thật)
        self._bat_rac = get_bat_rac_adapter()
        self._last_bat_rac_mode = None  # "FADE" | "FOLLOW" | None
        # Session Regime Guard — học online, tự điều chỉnh size/block (không giờ cứng)
        self._regime_guard = get_session_regime_guard()
        self._last_regime_dec = None
        self._pending_run_len = 0

    def _ensure_earcp(self, binary_seq: List[str]) -> Dict[str, Any]:
        """Giữ state expert pool giữa các lần gọi, chỉ update phần mới."""
        from live_bridge_taxonomy import (
            build_default_expert_pool, update_pool, poll_pool
        )
        from earcp_ensemble_controller import EARCPEnsembleController

        if self._bridge_pool is None or self._earcp_ctrl is None:
            self._bridge_pool = build_default_expert_pool()
            names = [e.name for e in self._bridge_pool]
            self._earcp_ctrl = EARCPEnsembleController(names, eta=0.12)
            self._earcp_fitted_len = 0

        # Chỉ feed các kết quả chưa học
        for act in binary_seq[self._earcp_fitted_len:]:
            preds = poll_pool(self._bridge_pool)
            self._earcp_ctrl.aggregate(preds)
            self._earcp_ctrl.update_weights(act)
            update_pool(self._bridge_pool, act)
        self._earcp_fitted_len = len(binary_seq)

        final_preds = poll_pool(self._bridge_pool)
        return self._earcp_ctrl.aggregate(final_preds)

    def update_result(self, d1: int, d2: int, d3: int) -> None:
        total = d1 + d2 + d3
        res = "T" if total >= 11 else "X"
        micro = get_micro_state(d1, d2, d3)
        self._history.append({
            "d1": d1, "d2": d2, "d3": d3,
            "sum": total, "res": res, "micro": micro
        })
        # Đồng bộ microstructure guard
        self._micro_guard.update(res)
        # Online BAT_RAC: ghi nhận kết quả mode vừa đánh
        if self._last_bat_rac_mode is not None:
            self._bat_rac.record_outcome(res)
            self._last_bat_rac_mode = None
        # Session Regime Guard: ghi nhận kết quả lệnh vừa đánh (nếu có)
        if ENABLE_SESSION_REGIME_GUARD and self._last_signal:
            last = self._last_signal
            if last.get("decision") == "BET" and last.get("tier") not in (None, "SKIP"):
                side = last.get("side")
                # is_win: so sánh side với res
                is_win = False
                if side in ("TAI", "T") and res == "T":
                    is_win = True
                elif side in ("XIU", "X") and res == "X":
                    is_win = True
                hour = datetime.now().hour
                self._regime_guard.record(
                    is_win=is_win,
                    reason=str(last.get("reason", "")),
                    hour=hour,
                    tier=str(last.get("tier", "")),
                    regime=str(last.get("regime", "")),
                    run_len=int(last.get("run_len", self._pending_run_len) or 0),
                )

    @property
    def n_samples(self) -> int:
        return len(self._history)

    def get_signal(self) -> Dict[str, Any]:
        hist = list(self._history)
        totals = [h["sum"] for h in hist]
        binary_seq = [h["res"] for h in hist]

        regime = calc_regime(binary_seq)
        run_len = _current_run_length(binary_seq)

        if regime == "CLUSTERING (BET)" and run_len >= 4:
            ent_window = binary_seq[-min(run_len, ENTROPY_WINDOW):]
        else:
            ent_window = binary_seq[-ENTROPY_WINDOW:]

        ent = calc_adaptive_entropy(ent_window, regime)
        is_trap = detect_palindrome(binary_seq[-10:])
        is_drift = detect_drift(binary_seq)

        current_micro = hist[-1]["micro"] if hist else "NEUTRAL"
        current_momentum = calc_momentum(totals)
        ctw = calc_ctw_prior(binary_seq)

        hist_lag_slice = hist[-22:]
        bayes_input = [
            (hist_lag_slice[i]["res"], hist_lag_slice[i - 1]["micro"])
            for i in range(1, len(hist_lag_slice))
        ]
        bayes = calc_bayesian_vector_posterior(
            bayes_input, ctw, current_micro, current_momentum
        )

        # EARCP stateful
        earcp_agg = self._ensure_earcp(binary_seq) if len(binary_seq) >= 3 else {
            "pT": 0.5, "pX": 0.5, "coherence": 0.0, "n_active": 0,
            "high_volatility": False, "active_names": []
        }

        prob_post = max(earcp_agg.get("pT", 0.5), earcp_agg.get("pX", 0.5))
        target_post = "T" if earcp_agg.get("pT", 0.5) >= earcp_agg.get("pX", 0.5) else "X"

        bridge_scan = scan_patterns(binary_seq, max_lookback=21)
        best_bridge = bridge_scan.get("best")
        micro_dice = (
            analyze_micro(hist[-1]["d1"], hist[-1]["d2"], hist[-1]["d3"])
            if hist else {"pressure": None, "desc": "Chưa có"}
        )
        micro_pressure = micro_dice.get("pressure")

        # =====================================================================
        # MICROSTRUCTURE GUARD — đo lag1/flip trước khi cho phép BẺ/BẮT RÁC
        # =====================================================================
        self._micro_guard.sync_from_binary(binary_seq)
        try:
            _hour = datetime.now().hour
        except Exception:
            _hour = None
        micro_state = self._micro_guard.snapshot(hour=_hour)
        micro_suppress = (
            ENABLE_MICROSTRUCTURE_GUARD
            and micro_state.suppress_aggressive_override
        )

        decision = "WAIT"
        reason = ""
        is_aggressive_override = False
        n_act = earcp_agg.get("n_active", 0)
        coh = earcp_agg.get("coherence", 0.0)

        # =====================================================================
        # 5. MA TRẬN BẺ CẦU — SIẾT CHẶT (chỉ khi n_act ≥ 3 + micro divergence)
        #     + MICRO-GUARD: cấm khi anti-persist / chaos
        # =====================================================================
        if best_bridge and best_bridge.get("status") == "CONFIRMED":
            bridge_name = best_bridge.get("name", "")
            bridge_pred = best_bridge.get("predicted_next")
            bridge_len = best_bridge.get("length") or best_bridge.get("covered") or 0
            is_micro_divergent = (
                micro_pressure is not None and micro_pressure != bridge_pred
            )
            is_streak_mode = "BET" in str(bridge_name)

            can_break_trap = is_trap and not is_streak_mode and bridge_len >= 6 and run_len >= 2
            can_break_micro = False
            reason_sub = ""

            if is_streak_mode and run_len >= 6:
                start_of_streak = len(binary_seq) - run_len
                pre_context = "".join(
                    binary_seq[max(0, start_of_streak - 6): start_of_streak]
                )
                is_from_khuon = (
                    "TTXX" in pre_context or "XXTT" in pre_context
                    or "TTTXXX" in pre_context
                )
                is_from_pingpong = "TXTXT" in pre_context or "XTXTX" in pre_context
                is_from_bet = "TTTT" in pre_context or "XXXX" in pre_context
                is_from_rac = not (is_from_khuon or is_from_pingpong or is_from_bet)

                if is_from_rac and run_len >= 6 and is_micro_divergent:
                    can_break_micro = True
                    reason_sub = f"Bệt non từ Cầu Rác (run={run_len})."
                elif not is_from_rac and run_len >= 8 and is_micro_divergent:
                    can_break_micro = True
                    reason_sub = f"Cầu Rồng quá căng ({run_len} tay)."

            if (can_break_trap or can_break_micro) and n_act >= BREAK_MIN_N_ACTIVE and ENABLE_BREAK:
                if micro_suppress:
                    # Anti/chaos: không BẺ kiểu fade-trap — chuyển sang FOLLOW bridge
                    decision = "BET"
                    target_post = bridge_pred  # bám cầu thay vì bẻ
                    prob_post = 0.64
                    is_aggressive_override = True
                    reason = (
                        f"[BẺ→BÁM] micro={micro_state.regime} lag1={micro_state.lag1:+.2f} "
                        f"→ không phá {bridge_name}, bám {target_post}"
                    )
                else:
                    decision = "BET"
                    target_post = "T" if bridge_pred == "X" else "X"
                    prob_post = 0.68
                    is_aggressive_override = True
                    if can_break_trap:
                        reason = (
                            f"[BẺ TỬ HUYỆT] Bẫy Đối Xứng. Phá {bridge_name} → {target_post}"
                        )
                    else:
                        reason = (
                            f"[BẺ TỬ HUYỆT] {reason_sub} Nén ngược ({micro_pressure}) → {target_post}"
                        )
            elif can_break_trap or can_break_micro:
                decision = "SKIP"
                reason = (
                    f"[BẺ CHẶN] Có trap/micro nhưng n_act={n_act}<{BREAK_MIN_N_ACTIVE} → SKIP."
                )
                is_aggressive_override = True

        # =====================================================================
        # 5.5 BẮT RÁC — ONLINE ADAPTER (không tắt, không rule lag1 cố định):
        #   Học live WR của FADE (lật đám) vs FOLLOW (bám đám).
        #   Session 18h: rule →BÁM theo lag1 = WR 33% → thay bằng học online.
        # =====================================================================
        if (
            ENABLE_BAT_RAC
            and not is_aggressive_override
            and regime == "CHOPPY (RAC)"
        ):
            if best_bridge and best_bridge.get("status") == "CONFIRMED" and n_act >= 2:
                bridge_name = best_bridge.get("name", "")
                crowd_fomo = best_bridge.get("predicted_next")  # hướng đám đông
                if crowd_fomo not in ("T", "X"):
                    crowd_fomo = target_post  # fallback

                if ENABLE_BAT_RAC_ONLINE:
                    mode, target_post, note = self._bat_rac.choose(crowd_fomo, run_len=run_len)
                    self._last_bat_rac_mode = mode
                    prob_post = 0.64
                    is_aggressive_override = True
                    decision = "BET"
                    if mode == "FOLLOW":
                        reason = (
                            f"[BẮT RÁC→BÁM] online {note} | đu {bridge_name} → bám {target_post}"
                        )
                    else:
                        reason = (
                            f"[BẮT RÁC] online {note} | đu {bridge_name}. Lật → {target_post}"
                        )
                else:
                    # fallback: luôn FADE (gốc)
                    target_post = "T" if crowd_fomo == "X" else "X"
                    self._last_bat_rac_mode = "FADE"
                    self._bat_rac.choose(crowd_fomo)  # sync pending
                    prob_post = 0.64
                    is_aggressive_override = True
                    decision = "BET"
                    reason = (
                        f"[BẮT RÁC] đu {bridge_name}. Lật → {target_post}"
                    )

        # Flag post-long-break
        _post_long_break = False
        if len(binary_seq) >= 6:
            prev_run = _current_run_length(binary_seq[:-1])
            if prev_run >= 4:
                _post_long_break = True

        # -----------------------------------------------------------------
        # LONG-STREAK FADE (cải thiện, không tắt):
        # Session 21h: BET sc=6X / sc≥4 bám cầu → gãy liên tục (WR~20%).
        # Khi run_len≥5 và chưa aggressive-override: đảo sang FADE streak.
        # -----------------------------------------------------------------
        if (
            not is_aggressive_override
            and decision in ("BET", "WAIT", None, "")
            and run_len >= 6
            and best_bridge
            and best_bridge.get("status") == "CONFIRMED"
        ):
            # Streak side = binary_seq[-1]
            streak_side = binary_seq[-1] if binary_seq else target_post
            fade_side = "X" if streak_side == "T" else "T"
            target_post = fade_side
            prob_post = max(prob_post, 0.62)
            decision = "BET"
            is_aggressive_override = True
            reason = (
                f"[FADE-BỆT] sc={run_len}{streak_side} ≥6 → đảo bám cầu, đánh {fade_side}"
            )

        # =====================================================================
        # 6. NGƯỠNG ĐỘNG + HARD FILTERS (audit 2026-08-16)
        #    + threshold_delta từ microstructure guard
        # =====================================================================
        dynamic_thresh = 0.54 + 0.08 * (1.0 - coh) + 0.03 * ent.get("value", 0.0)

        if _post_long_break:
            dynamic_thresh += POST_BREAK_EXTRA_THRESH
        if regime == "CHOPPY (RAC)":
            dynamic_thresh += CHOPPY_EXTRA_THRESH
        if is_trap and regime == "CHOPPY (RAC)" and n_act <= 1:
            dynamic_thresh += 0.05
        if run_len <= 1:
            dynamic_thresh = max(dynamic_thresh, RUN1_MIN_CONF)

        # Microstructure: nâng/hạ nhẹ threshold theo regime thống kê
        if ENABLE_MICROSTRUCTURE_GUARD:
            dynamic_thresh += micro_state.threshold_delta

        dynamic_thresh = min(dynamic_thresh, 0.82)

        # --- QUYẾT ĐỊNH (đã nới: không hard-block CHOPPY/RUN1/n_act) ---
        if len(hist) < 12:
            decision = "WAIT"
            reason = "Burn-in (cần ≥12 phiên)..."
        elif is_aggressive_override:
            pass  # BẺ / BẮT RÁC đã set (hoặc đã bị MICRO-GUARD chặn → SKIP)
        elif decision == "SKIP" and reason.startswith("[MICRO-GUARD]"):
            pass  # giữ SKIP từ guard
        elif prob_post < dynamic_thresh:
            decision = "SKIP"
            reason = (
                f"[NGƯỠNG] p={prob_post*100:.1f}% < θ={dynamic_thresh*100:.1f}% "
                f"(Coh={coh:.2f}, Act={n_act}) → SKIP."
            )
        else:
            decision = "BET"
            reason = (
                f"[BET] p={prob_post*100:.1f}% ≥ θ={dynamic_thresh*100:.1f}%. "
                f"Hướng {target_post}."
            )

        # Kelly + hạ tier khi n_act thấp
        kelly = calc_kelly_tier(prob_post) if decision == "BET" else {"tier": "SKIP"}
        if decision == "BET" and kelly.get("tier") == "SKIP":
            decision = "SKIP"
            reason = f"[KELLY] Không edge dương (p={prob_post*100:.1f}%) → SKIP."

        # P0: n_act < MIN_N_ACTIVE_FOR_HIGH → không cho HIGH/MAX
        if decision == "BET" and n_act < 1:
            if kelly.get("tier") in ("MAX", "HIGH"):
                kelly = dict(kelly)
                kelly["tier"] = "MID"
                reason += f" [HẠ TIER n_act=0→MID]"

        # Aggressive override cũng không được MAX khi n_act < 4
        if decision == "BET" and is_aggressive_override and n_act < 4:
            if kelly.get("tier") == "MAX":
                kelly = dict(kelly)
                kelly["tier"] = "HIGH"
                reason += " [HẠ MAX→HIGH n_act<4]"

        vomm = calculate_vomm("".join(binary_seq), max_order=5)
        bridge_gate_passed = (
            (not best_bridge)
            or best_bridge.get("status") in ("CONFIRMED", "FORMING")
        )

        _tier_order = ["LOW", "MID", "HIGH", "MAX"]
        if decision == "BET" and is_drift and kelly.get("tier") in _tier_order:
            idx = _tier_order.index(kelly["tier"])
            if idx > 0:
                kelly = dict(kelly)
                kelly["tier"] = _tier_order[idx - 1]
                reason += " [DRIFT -1 bậc]"

        side = target_post if decision == "BET" else None
        hurst_value = calculate_rolling_hurst(binary_seq, window_size=50)

        _lb = bridge_scan.get("best")
        long_bridge_covered = (
            (_lb.get("covered") or _lb.get("length") or 0) if _lb else 0
        )
        long_bridge_active = (
            bool(_lb)
            and _lb.get("status") == "CONFIRMED"
            and long_bridge_covered >= LONG_BRIDGE_LOG_THRESHOLD
        )
        long_bridge_predicted = (
            _lb.get("predicted_next") if (_lb and long_bridge_active) else None
        )

        earcp_side_letter = (
            "T" if earcp_agg.get("pT", 0.5) >= earcp_agg.get("pX", 0.5) else "X"
        )
        earcp_out = {
            "earcp_decision": "BET" if n_act > 0 else "ABSTAIN",
            "earcp_side": "TAI" if earcp_side_letter == "T" else "XIU",
            "earcp_confidence": round(max(earcp_agg.get("pT", 0.5), earcp_agg.get("pX", 0.5)), 4),
            "earcp_coherence": round(coh, 4),
            "earcp_n_active": n_act,
            "earcp_high_vol": bool(earcp_agg.get("high_volatility", False)),
        }

        # =====================================================================
        # 7. SESSION REGIME GUARD — thích nghi liên tục (không giờ cứng)
        #    Học WR theo giờ / theo signal / theo streak từ data live.
        #    Tự điều chỉnh size_multiplier, threshold_bonus, block_aggressive.
        # =====================================================================
        regime_size_mult = 1.0
        regime_thresh_bonus = 0.0
        regime_note = ""
        regime_state = "NORMAL"
        if ENABLE_SESSION_REGIME_GUARD:
            hour_now = datetime.now().hour
            gdec = self._regime_guard.evaluate(
                reason=reason,
                confidence=prob_post,
                tier=kelly.get("tier", "MID"),
                regime=regime,
                hour=hour_now,
                engine_decision=decision,
                run_len=run_len,
            )
            self._last_regime_dec = gdec
            self._pending_run_len = run_len
            regime_size_mult = gdec.size_multiplier
            regime_thresh_bonus = gdec.threshold_bonus
            regime_state = gdec.state
            regime_note = gdec.reason

            # Áp dụng threshold bonus
            if decision == "BET" and regime_thresh_bonus > 0:
                dynamic_thresh = min(0.85, dynamic_thresh + regime_thresh_bonus)
                if prob_post < dynamic_thresh and not is_aggressive_override:
                    decision = "SKIP"
                    reason = (
                        f"[ADAPT-THRESH] p={prob_post*100:.1f}% < θ={dynamic_thresh*100:.1f}% "
                        f"(+{regime_thresh_bonus:.3f}) state={regime_state} → SKIP"
                    )
                    side = None
                    kelly = {"tier": "SKIP"}

            # Block aggressive khi guard nói BLOCK
            if is_aggressive_override and gdec.block_aggressive:
                decision = "SKIP"
                reason = f"[ADAPT-BLOCK] {gdec.reason}"
                side = None
                kelly = {"tier": "SKIP"}
                is_aggressive_override = False

            # Size multiplier: kết hợp micro + regime
            # (caller / bankroll sẽ nhân amount với regime_size_mult)
            if decision == "BET" and regime_size_mult < 0.55 and regime_state in ("TOXIC", "DEGRADED"):
                # Hạ tier khi size bị cắt mạnh
                _tier_order = ["LOW", "MID", "HIGH", "MAX"]
                cur = kelly.get("tier", "MID")
                if cur in _tier_order:
                    idx = _tier_order.index(cur)
                    new_idx = max(0, idx - 1)
                    if regime_size_mult < 0.35:
                        new_idx = max(0, idx - 2)
                    if new_idx != idx:
                        kelly = dict(kelly)
                        kelly["tier"] = _tier_order[new_idx]
                        reason += f" [ADAPT-SIZE×{regime_size_mult:.2f} →{kelly['tier']}]"

        result = {
            "decision": decision,
            "side": "TAI" if side == "T" else ("XIU" if side == "X" else None),
            "tier": kelly.get("tier", "SKIP"),
            "confidence": round(prob_post, 4),
            "reason": reason,
            **earcp_out,
            "regime": regime,
            "entropy": round(ent["value"], 4),
            "entropy_threshold": ent["threshold"],
            "is_trap_palindrome": is_trap,
            "is_drift": is_drift,
            "momentum": current_momentum,
            "micro_state": current_micro,
            "ctw_prior": {"pT": round(ctw["pT"], 4), "best_order": ctw["best_order"]},
            "bayes_posterior": {
                "pT": round(bayes["pT"], 4),
                "is_aligned": bayes["is_aligned"],
            },
            "kelly": kelly,
            "n_samples": len(hist),
            "bridge_scan": bridge_scan,
            "bridge_gate_passed": bridge_gate_passed,
            "vomm": vomm,
            "micro_dice": micro_dice,
            "vomm_or_applied": None,
            "hurst_value": round(hurst_value, 4),
            "long_bridge_active": long_bridge_active,
            "long_bridge_covered": long_bridge_covered,
            "long_bridge_predicted": long_bridge_predicted,
            "run_len": run_len,
            "dynamic_threshold": round(dynamic_thresh, 4),
            # Microstructure guard fields (log / UI)
            "micro_lag1": micro_state.lag1,
            "micro_flip_rate": micro_state.flip_rate,
            "micro_regime": micro_state.regime,
            "micro_suppress": micro_suppress,
            "micro_size_mult": micro_state.size_multiplier,
            "micro_note": micro_state.note,
            # Session Regime Guard (adaptive)
            "regime_state": regime_state,
            "regime_size_mult": round(regime_size_mult, 3),
            "regime_thresh_bonus": regime_thresh_bonus,
            "regime_note": regime_note,
        }
        self._last_signal = result
        return result

    @property
    def last_signal(self) -> dict:
        return self._last_signal

    def reset(self) -> None:
        self._history.clear()
        self._last_signal = {}
        self._bridge_pool = None
        self._earcp_ctrl = None
        self._earcp_fitted_len = 0
        self._micro_guard.reset()
        self._last_bat_rac_mode = None
        self._last_regime_dec = None
        self._pending_run_len = 0
        # Không reset regime_guard — giữ kiến thức đã học giữa các session
        # (nếu muốn reset hoàn toàn: get_session_regime_guard(reset=True))


_engine_instance: Optional[ApexSniperEngine] = None


def get_apex_sniper_engine(reset: bool = False) -> ApexSniperEngine:
    global _engine_instance
    if _engine_instance is None or reset:
        _engine_instance = ApexSniperEngine()
    return _engine_instance


__all__ = [
    "ApexSniperEngine", "get_apex_sniper_engine",
    "calc_momentum", "calc_regime", "calc_adaptive_entropy",
    "detect_palindrome", "detect_drift", "get_micro_state",
    "calc_ctw_prior", "calc_bayesian_vector_posterior", "calc_kelly_tier",
]
