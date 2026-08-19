from __future__ import annotations
from collections import deque
from typing import Optional, List

from bankroll import Bankroll
from apex_sniper_engine import get_apex_sniper_engine, ApexSniperEngine
import fade_follow_advisor as ffa

HIST_MIN = 5


class _DummyVol:
    def __init__(self):
        self.gap_pct = 0.0
        self.vol_lead = ""
        self.total = 0.0

    def has_snaps(self):
        return True

    def best_gap(self):
        return self.gap_pct

    def gap_change(self):
        return 0.0

    def late_heavy(self):
        return False

    def best_ud(self):
        return 0.0


class _DummyHist:
    def __init__(self, engine: "ApexSniperLogicEngine"):
        self._engine = engine

    def size(self) -> int:
        return self._engine.sniper_engine.n_samples


class ApexSniperLogicEngine:
    """Logic engine với filter audit 2026-08-16 + rolling WR pause."""

    def __init__(self, initial_balance: float, kpi: float, *,
                 stop_on_kpi: bool = False, state_engine=None):
        self.bankroll = Bankroll(initial_balance, kpi, stop_on_kpi=stop_on_kpi)
        self.sniper_engine: ApexSniperEngine = get_apex_sniper_engine(reset=True)
        # Nạp lịch sử CSV vào FFA memory (nếu có) — không hardcode số
        try:
            import glob, os
            _paths = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "apex_sniper_*.csv")))
            if not _paths:
                _paths = sorted(glob.glob("/home/workdir/attachments/apex_sniper_*.csv"))
            if _paths:
                ffa.bootstrap_from_csv(_paths)
        except Exception:
            pass

        self.vol = _DummyVol()
        self.hist = _DummyHist(self)

        self.conf_thresholds: List[int] = [65, 72, 78]
        self.exploration_rate: float = 0.0
        self.exploration_bet_pct: float = 0.0
        self.decay_false_skip: float = 1.0
        self.skip_streak: int = 0

        self._cur_regime: str = "UNKNOWN"
        self._cur_sq: float = 0.5
        self._streak_snap_cnt: int = 0
        self._streak_snap_side: str = ""
        self._streak_str: str = ""
        self._history_loaded_flag: bool = False
        self._insight_cache: dict = {}

        self.history_totals: deque = deque(maxlen=200)
        self._skip_reasons: deque = deque(maxlen=10)
        self._total_bets: int = 0
        self._total_skip: int = 0
        self.last_decision: dict = {}
        self._pending_side: Optional[str] = None
        self.last_placed_sid: Optional[int] = None
        self.last_placed_bet: dict = {}

        # Rolling WR + auto-pause (P1)
        self.ROLLING_WR_WINDOW: int = 30
        self.PAUSE_WR_THRESHOLD: float = 0.38
        self.PAUSE_ROUNDS: int = 6
        self.MIN_BETS_FOR_PAUSE: int = 30
        self._recent_results: deque = deque(maxlen=self.ROLLING_WR_WINDOW)
        self._bets_since_last_reset: int = 0
        self._auto_reset_count: int = 0
        self._last_reset_reason: str = ""
        self._pause_left: int = 0  # số phiên còn phải SKIP do WR thấp

    def reset_pattern_state(self, reason: str = "") -> None:
        """Đã tắt auto-reset mid-session theo yêu cầu trước."""
        self._last_reset_reason = f"[DISABLED] {reason}" if reason else "[DISABLED]"
        return

    def _check_auto_reset(self) -> bool:
        return False

    def _rolling_wr(self) -> float:
        if len(self._recent_results) < 10:
            return 0.5
        return sum(self._recent_results) / len(self._recent_results)

    def _update_pause_state(self) -> None:
        """Không pause cứng. FFA + engine tự hiểu context."""
        return

    def _load_history_items(self, items) -> None:
        for it in items:
            try:
                if isinstance(it, dict):
                    if "d1" in it and "d2" in it and "d3" in it:
                        d1, d2, d3 = int(it["d1"]), int(it["d2"]), int(it["d3"])
                        self.history_totals.append(d1 + d2 + d3)
                        self.sniper_engine.update_result(d1, d2, d3)
                        continue
                    dice_str = str(it.get("dice", "?"))
                    if dice_str and dice_str != "?" and "-" in dice_str:
                        try:
                            parts = [int(x) for x in dice_str.split("-")]
                            if len(parts) == 3 and all(1 <= p <= 6 for p in parts):
                                d1, d2, d3 = parts
                                self.history_totals.append(d1 + d2 + d3)
                                self.sniper_engine.update_result(d1, d2, d3)
                                continue
                        except (ValueError, TypeError):
                            pass
                    result = it.get("result")
                    if result in ("TAI", "XIU"):
                        total = int(it.get("total") or 0)
                        if total <= 0:
                            total = 14 if result == "TAI" else 7
                        self.history_totals.append(total)
                        fake_total = total
                        fd1, fd2 = 3, 3
                        fd3 = max(1, min(6, fake_total - fd1 - fd2))
                        self.sniper_engine.update_result(fd1, fd2, fd3)
                elif isinstance(it, (list, tuple)) and len(it) >= 3:
                    d1, d2, d3 = int(it[0]), int(it[1]), int(it[2])
                    self.history_totals.append(d1 + d2 + d3)
                    self.sniper_engine.update_result(d1, d2, d3)
            except Exception:
                continue
        self._history_loaded_flag = True
        self._insight_cache["history_loaded"] = True

    def mark_bet_placed(self, sid: Optional[int] = None, dec: Optional[dict] = None) -> None:
        """Ghi nhận lệnh đã thực sự được đặt (AUTO click hoặc MANUAL confirm).
        Được gọi từ main_apex.py với (sid, dec) — dec là decision dict đã lock.
        Giữ tương thích ngược: dec là optional nên vẫn gọi được mark_bet_placed(sid).
        """
        self.last_placed_sid = sid
        if isinstance(dec, dict):
            self.last_placed_bet = dict(dec)
            # đồng bộ lại last_decision để UI/insight luôn phản ánh lệnh vừa chốt
            self.last_decision = dict(dec)

    def record_false_skip(self) -> None:
        self.skip_streak += 1

    def analyze(self, sid: int, tai: float, xiu: float, tai_u: int, xiu_u: int,
                rmt: float, estimated_volume: bool = False) -> dict:
        total_vol = tai + xiu
        self.vol.total = total_vol
        self.vol.gap_pct = abs(tai - xiu) / total_vol * 100.0 if total_vol > 0 else 0.0
        self.vol.vol_lead = "TAI" if tai >= xiu else "XIU"

        sig = self.sniper_engine.get_signal()
        self._cur_regime = sig.get("regime", "UNKNOWN")
        self._cur_sq = 1.0 - sig.get("entropy", 0.5)

        decision = sig["decision"]
        side = sig["side"]

        # --- Pause do rolling WR thấp ---
        if self._pause_left > 0:
            self._pause_left -= 1
            self.skip_streak += 1
            return {
                "side": None, "amount": 0, "tier": "SKIP", "confidence": 0,
                "reason": (
                    f"[AUTO-PAUSE] Còn {self._pause_left} phiên "
                    f"(rolling WR thấp). {self._last_reset_reason}"
                ),
                "allow_execution": False,
                "entropy": sig.get("entropy", 0.5), "probability": 0.5,
                "score": 0, "signal": "SKIP",
                "regime": self._cur_regime,
                "is_drift": sig.get("is_drift"),
                "is_trap_palindrome": sig.get("is_trap_palindrome"),
                "bridge_scan": sig.get("bridge_scan"),
                "hurst_value": sig.get("hurst_value"),
                "long_bridge_active": sig.get("long_bridge_active"),
                "long_bridge_covered": sig.get("long_bridge_covered"),
                "long_bridge_predicted": sig.get("long_bridge_predicted"),
                "earcp_decision": sig.get("earcp_decision"),
                "earcp_side": sig.get("earcp_side"),
                "earcp_confidence": sig.get("earcp_confidence"),
                "earcp_coherence": sig.get("earcp_coherence"),
                "earcp_n_active": sig.get("earcp_n_active"),
                "earcp_high_vol": sig.get("earcp_high_vol"),
                "run_len": sig.get("run_len"),
                "dynamic_threshold": sig.get("dynamic_threshold"),
            }

        if decision == "BET" and side:
            tier_map = {
                "MID": "SNIPER_MID", "HIGH": "SNIPER_HIGH",
                "MAX": "SNIPER_MAX", "LOW": "SNIPER_LOW"
            }
            tier = tier_map.get(sig["tier"], "NORMAL")

            # Hạ tier nếu n_act thấp (double-check)
            earcp_active = sig.get("earcp_n_active", 0)
            if earcp_active <= 2 and tier in ("SNIPER_MAX", "SNIPER_HIGH"):
                tier = "SNIPER_MID"
                sig["reason"] = (
                    f"[HẠ TIER ÉP] n_act={earcp_active}≤2 → MID. "
                ) + sig.get("reason", "")

            confidence_pct = int(round(sig["confidence"] * 100))
            streak_side_now, streak_cnt_now = self._current_streak()
            bridge_status_now = (
                (sig.get("bridge_scan") or {}).get("best") or {}
            ).get("status")
            reason_txt = sig.get("reason") or ""

            # V70: thay vì 1 cờ boolean khóa cứng, phân loại nguồn gốc tín hiệu
            # (signal_origin) để fade_follow_advisor coi đây là MỘT CHIỀU
            # context được live-memory học/hiệu chỉnh, không phải vùng cấm.
            if "BẺ TỬ HUYỆT" in reason_txt:
                signal_origin = "BTH"
            elif "BẮT RÁC" in reason_txt:
                signal_origin = "BR"
            else:
                signal_origin = "NAT"
            already_faded = signal_origin != "NAT"  # giữ để log/tương thích ngược

            # crowd imbalance từ vol nếu có
            _imb = None
            try:
                if self.vol.gap_pct and self.vol.vol_lead:
                    # xấp xỉ: gap dương nghiêng TAI
                    g = float(self.vol.gap_pct) / 100.0
                    _imb = g if self.vol.vol_lead == "TAI" else -g
            except Exception:
                _imb = None
            ffa_rec = ffa.evaluate(
                regime=self._cur_regime,
                tier=tier,
                confidence=sig["confidence"],
                streak_cnt=streak_cnt_now,
                bridge_status=bridge_status_now,
                original_side=side,
                crowd_imbalance=_imb,
                earcp_n_active=int(sig.get("earcp_n_active") or 0),
                earcp_side=sig.get("earcp_side"),
                signal_origin=signal_origin,
                system_loss_streak=self.bankroll.loss_streak,
                gap_pct=float(self.vol.gap_pct) if self.vol.gap_pct else None,
                vol_lead=self.vol.vol_lead if self.vol.vol_lead else None,
            )

            if ffa_rec.action == "SKIP":
                self.skip_streak += 1
                return {
                    "side": None, "amount": 0, "tier": "SKIP", "confidence": 0,
                    "reason": ffa_rec.confidence_note,
                    "allow_execution": False,
                    "entropy": sig.get("entropy", 0.5), "probability": 0.5,
                    "score": 0, "signal": "SKIP",
                    "regime": self._cur_regime,
                    "ffa_action": "SKIP",
                    "ffa_note": ffa_rec.confidence_note,
                    "earcp_n_active": sig.get("earcp_n_active"),
                    "run_len": sig.get("run_len"),
                    "is_drift": sig.get("is_drift"),
                    "is_trap_palindrome": sig.get("is_trap_palindrome"),
                    "bridge_scan": sig.get("bridge_scan"),
                    "earcp_decision": sig.get("earcp_decision"),
                    "earcp_side": sig.get("earcp_side"),
                    "earcp_confidence": sig.get("earcp_confidence"),
                    "earcp_coherence": sig.get("earcp_coherence"),
                    "earcp_high_vol": sig.get("earcp_high_vol"),
                }
            # V70: BỎ khối khóa kép cũ (từng ép FOLLOW mỗi khi ffa_rec trả về
            # FADE trên lệnh engine tự bẻ/bắt rác). Audit 2026-08-16 cho thấy
            # đúng cơ chế đó đang bị chặn là cơ chế hiệu quả nhất hệ thống
            # (WR 69.2% khi được phép fade, so với 49.3% khi bị khóa cứng theo
            # FOLLOW). fade_follow_advisor.evaluate() giờ tự quyết định dựa
            # trên live-context + origin-prior, không cần chặn lại ở đây nữa.
            final_side = ffa_rec.recommended_side
            size_mult = ffa_rec.size_multiplier

            # Kết hợp FFA size + Session Regime Guard size (học online, không giờ cứng)
            regime_mult = float(sig.get("regime_size_mult", 1.0) or 1.0)
            micro_mult = float(sig.get("micro_size_mult", 1.0) or 1.0)
            combined_mult = size_mult * regime_mult * micro_mult
            combined_mult = max(0.10, min(1.25, combined_mult))

            amount = (
                self.bankroll.calculate_bet(tier=tier, confidence=confidence_pct)
                if hasattr(self.bankroll, "calculate_bet") else 0
            )
            amount = int(amount * combined_mult)
            ffa.log_recommendation(sid, ffa_rec)

            dec = {
                "side": final_side, "amount": amount, "tier": tier,
                "confidence": confidence_pct,
                "reason": sig["reason"], "allow_execution": True,
                "entropy": sig.get("entropy", 0.5),
                "probability": sig["confidence"],
                "score": confidence_pct, "signal": decision,
                "regime": self._cur_regime,
                "is_drift": sig.get("is_drift"),
                "is_trap_palindrome": sig.get("is_trap_palindrome"),
                "ctw_prior": sig.get("ctw_prior"),
                "momentum": sig.get("momentum"),
                "bridge_scan": sig.get("bridge_scan"),
                "hurst_value": sig.get("hurst_value"),
                "long_bridge_active": sig.get("long_bridge_active"),
                "long_bridge_covered": sig.get("long_bridge_covered"),
                "long_bridge_predicted": sig.get("long_bridge_predicted"),
                "earcp_decision": sig.get("earcp_decision"),
                "earcp_side": sig.get("earcp_side"),
                "earcp_confidence": sig.get("earcp_confidence"),
                "earcp_coherence": sig.get("earcp_coherence"),
                "earcp_n_active": sig.get("earcp_n_active"),
                "earcp_high_vol": sig.get("earcp_high_vol"),
                "ffa_action": ffa_rec.action,
                "ffa_original_side": ffa_rec.original_side,
                "ffa_note": ffa_rec.confidence_note,
                "run_len": sig.get("run_len"),
                "dynamic_threshold": sig.get("dynamic_threshold"),
                "regime_state": sig.get("regime_state"),
                "regime_size_mult": sig.get("regime_size_mult"),
                "regime_note": sig.get("regime_note"),
                "micro_lag1": sig.get("micro_lag1"),
                "micro_regime": sig.get("micro_regime"),
                "micro_size_mult": sig.get("micro_size_mult"),
                "combined_size_mult": round(combined_mult, 3),
            }
        else:
            self.skip_streak += 1
            dec = {
                "side": None, "amount": 0, "tier": "SKIP", "confidence": 0,
                "reason": sig["reason"], "allow_execution": False,
                "entropy": sig.get("entropy", 0.5), "probability": 0.5,
                "score": 0, "signal": "SKIP",
                "regime": self._cur_regime,
                "is_drift": sig.get("is_drift"),
                "is_trap_palindrome": sig.get("is_trap_palindrome"),
                "bridge_scan": sig.get("bridge_scan"),
                "hurst_value": sig.get("hurst_value"),
                "long_bridge_active": sig.get("long_bridge_active"),
                "long_bridge_covered": sig.get("long_bridge_covered"),
                "long_bridge_predicted": sig.get("long_bridge_predicted"),
                "earcp_decision": sig.get("earcp_decision"),
                "earcp_side": sig.get("earcp_side"),
                "earcp_confidence": sig.get("earcp_confidence"),
                "earcp_coherence": sig.get("earcp_coherence"),
                "earcp_n_active": sig.get("earcp_n_active"),
                "earcp_high_vol": sig.get("earcp_high_vol"),
                "run_len": sig.get("run_len"),
                "dynamic_threshold": sig.get("dynamic_threshold"),
            }

        self.last_decision = dec
        self._pending_side = dec.get("side") if decision == "BET" else None
        return dec

    def _current_streak(self) -> tuple:
        hist = list(self.history_totals)
        if not hist:
            return ("", 0)
        side = "TAI" if hist[-1] >= 11 else "XIU"
        cnt = 0
        for x in reversed(hist):
            s = "TAI" if x >= 11 else "XIU"
            if s == side:
                cnt += 1
            else:
                break
        return (side, cnt)

    def process_result(self, sid: int, d1: int, d2: int, d3: int,
                       bet_executed: bool = False, mode: str = "MANUAL",
                       override: Optional[dict] = None) -> dict:
        total = d1 + d2 + d3
        actual_side = "TAI" if total >= 11 else "XIU"
        dec = self.last_decision or {}

        if dec.get("ffa_action") in ("FADE", "REDUCE_SIZE", "FOLLOW"):
            try:
                ffa.record_outcome(sid, actual_side)
            except Exception:
                pass

        side = dec.get("side") if bet_executed else None
        tier = dec.get("tier", "SKIP") if bet_executed else "SKIP"
        amount = dec.get("amount", 0) if bet_executed else 0
        confidence = dec.get("confidence", 0) if bet_executed else 0

        predicted_side = self._pending_side
        was_correct = (predicted_side == actual_side) if predicted_side else None
        is_win = bool(was_correct) if (bet_executed and predicted_side) else False

        pnl = 0
        auto_reset_triggered = False
        if bet_executed and predicted_side:
            self.bankroll.update(
                is_win=is_win, amount=amount, side=predicted_side, tier=tier
            )
            try:
                ffa.notify_system_result(is_win)
            except Exception:
                pass
            pnl = round(amount * 0.98) if is_win else -amount
            self.skip_streak = 0
            self._skip_reasons.clear()
            self._total_bets += 1
            self._recent_results.append(is_win)
            self._bets_since_last_reset += 1
            self._update_pause_state()
            try:
                auto_reset_triggered = self._check_auto_reset()
            except Exception:
                auto_reset_triggered = False
        else:
            self.skip_streak += 1
            self._total_skip += 1
            reason_txt = dec.get("reason", "")
            if reason_txt:
                self._skip_reasons.append(reason_txt)

        pre_hist = list(self.history_totals)
        pattern_13 = "".join("T" if x >= 11 else "X" for x in pre_hist[-13:])
        pattern_21 = "".join("T" if x >= 11 else "X" for x in pre_hist[-21:])
        streak_side, streak_cnt = self._current_streak()

        self.sniper_engine.update_result(d1, d2, d3)
        sig = self.sniper_engine.last_signal
        self._pending_side = None
        self.history_totals.append(total)

        result_str = (
            "WIN" if (tier != "SKIP" and is_win)
            else ("LOSS" if tier != "SKIP" and bet_executed else "SKIP")
        )

        row = {
            "sid": sid,
            "pattern_pre": pattern_13,
            "streak": f"{streak_cnt}{streak_side[0] if streak_side else ''}",
            "side": side or "—",
            "amount": amount,
            "tier": tier,
            "reason": dec.get("reason", ""),
            "confidence": confidence,
            "dice": f"{d1}-{d2}-{d3}",
            "total": total,
            "actual": actual_side,
            "result": result_str,
            "is_win": is_win,
            "pnl": pnl,
            "predicted_total": "",
            "gap_lock": self.vol.gap_pct,
            "gap_early": 0.0,
            "gap_change": 0.0,
            "gap_pct": self.vol.gap_pct,
            "vol_lead": self.vol.vol_lead,
            "late_heavy": False,
            "vol_stable": True,
            "remaining_s": "",
            "total_vol_m": round(self.vol.total / 1e6, 4) if self.vol.total else 0,
            "tai_pct": 50.0,
            "regime": self._cur_regime,
            "session_quality": (
                "NORMAL" if sig.get("entropy", 0.5) < sig.get("entropy_threshold", 0.95)
                else "POOR"
            ),
            "hist_size": len(self.history_totals),
            "skip_reason": ", ".join(list(self._skip_reasons)[-3:]) if self._skip_reasons else "",
            "history_21": pattern_21,
            "kpi_hit": getattr(self.bankroll, "kpi_stop_signal", False),
            "Trap": sig.get("is_trap_palindrome", False),
            "Rev": sig.get("is_drift", False),
            "M": round(sig.get("bayes_posterior", {}).get("pT", 0.5), 4),
            "Vec": sig.get("momentum", ""),
            "Z": "",
            "pattern_13": pattern_13,
            "pattern_21": pattern_21,
            "pattern_100": "",
            "tai_users": "", "xiu_users": "", "ud_diff": "",
            "crowd_imbalance": "", "smart_money_divergence": "",
        }

        _bs = (dec.get("bridge_scan") or {}).get("best")
        row["bridge_pattern"] = _bs.get("note", "") if _bs else ""
        row["bridge_status"] = _bs.get("status", "") if _bs else ""
        _hv = dec.get("hurst_value")
        row["hurst"] = _hv if _hv is not None else ""
        row["long_bridge_active"] = bool(dec.get("long_bridge_active"))
        row["long_bridge_covered"] = dec.get("long_bridge_covered") or 0
        row["long_bridge_predicted"] = dec.get("long_bridge_predicted") or ""
        row["earcp_decision"] = dec.get("earcp_decision") or ""
        row["earcp_side"] = dec.get("earcp_side") or ""
        row["earcp_confidence"] = (
            dec.get("earcp_confidence")
            if dec.get("earcp_confidence") is not None else ""
        )
        row["earcp_coherence"] = (
            dec.get("earcp_coherence")
            if dec.get("earcp_coherence") is not None else ""
        )
        row["earcp_n_active"] = (
            dec.get("earcp_n_active")
            if dec.get("earcp_n_active") is not None else ""
        )
        row["earcp_high_vol"] = bool(dec.get("earcp_high_vol"))
        row["ffa_action"] = dec.get("ffa_action") or ""
        row["ffa_original_side"] = dec.get("ffa_original_side") or ""
        row["ffa_note"] = dec.get("ffa_note") or ""
        row["auto_reset_triggered"] = auto_reset_triggered
        row["auto_reset_count_total"] = self._auto_reset_count
        row["auto_reset_reason"] = (
            self._last_reset_reason if auto_reset_triggered else ""
        )
        row["run_len"] = dec.get("run_len") or sig.get("run_len") or 0
        row["dynamic_threshold"] = (
            dec.get("dynamic_threshold") or sig.get("dynamic_threshold") or ""
        )
        row["pause_left"] = self._pause_left
        row["rolling_wr"] = round(self._rolling_wr() * 100, 1)

        if hasattr(self.bankroll, "stats"):
            br = self.bankroll.stats()
            _EXCLUDE = {
                "total", "regime", "result", "tier", "side", "amount", "dice",
                "actual", "is_win", "pnl", "reason", "confidence", "streak",
                "gap_lock", "gap_early", "gap_change", "gap_pct",
                "vol_lead", "late_heavy", "vol_stable", "session_quality",
            }
            safe_br = {k: v for k, v in br.items() if k not in _EXCLUDE}
            row.update(safe_br)
            row["cum_profit"] = br.get("cumulative_profit", "")
            row["cum_balance"] = br.get("total_balance", "")
            row["cum_wins"] = br.get("cumulative_wins", "")
            row["cum_losses"] = br.get("cumulative_losses", "")
            row["cum_wr"] = br.get("cumulative_wr", "")
            if "ghost_mode" in br:
                row["ghost"] = br["ghost_mode"]
            row["kpi_floor"] = br.get("floor", "")
            row["kpi_mode"] = br.get("stop_on_kpi_mode", "")

        self.last_decision = {**dec, "tier": tier, "amount": amount, "side": side or "-"}
        return row

    def get_insight(self) -> dict:
        sig = self.sniper_engine.last_signal
        hist = list(self.history_totals)
        streak_side, streak_cnt = self._current_streak()

        last21 = hist[-21:]
        last100 = hist[-100:]
        tai_21 = (
            (sum(1 for x in last21 if x >= 11) / len(last21) * 100.0)
            if last21 else 0.0
        )
        tai_100 = (
            (sum(1 for x in last100 if x >= 11) / len(last100) * 100.0)
            if last100 else 0.0
        )

        seq21 = ["T" if x >= 11 else "X" for x in last21]
        n_blocks = 0
        n_flips = 0
        for i in range(len(seq21)):
            if i == 0 or seq21[i] != seq21[i - 1]:
                n_blocks += 1
            if i > 0 and seq21[i] != seq21[i - 1]:
                n_flips += 1

        total_bets = self._total_bets
        total_skip = self._total_skip
        skip_ratio = (total_skip / max(total_bets + total_skip, 1)) * 100.0

        regime = sig.get("regime", self._cur_regime)
        momentum = sig.get("momentum", "")
        long_bias = (
            "Thiên TÀI" if tai_100 > 55
            else ("Thiên XỈU" if tai_100 < 45 else "Cân bằng")
        )

        return {
            "regime": regime,
            "session_quality": (
                "NORMAL" if sig.get("entropy", 0.5) < sig.get("entropy_threshold", 0.95)
                else "POOR"
            ),
            "history_loaded": self._history_loaded_flag,
            "hist_size": len(hist),
            "skip_streak": self.skip_streak,
            "skip_reasons": list(self._skip_reasons),
            "current_bet": self.last_decision,
            "last_placed_sid": self.last_placed_sid,
            "last_placed_bet": self.last_placed_bet,
            "streak_side": streak_side,
            "streak_cnt": streak_cnt,
            "gap": self.vol.gap_pct,
            "total_vol_m": round(self.vol.total / 1e6, 4) if self.vol.total else 0.0,
            "vol_stable": True,
            "vol_flipped": False,
            "market_status": momentum or "LIVE",
            "blocks": str(n_blocks),
            "pingpong": n_flips,
            "phase": sig.get("pattern_type", regime) if "pattern_type" in sig else regime,
            "tai_21": round(tai_21, 1),
            "tai_100": round(tai_100, 1),
            "long_bias": long_bias,
            "total_bets": total_bets,
            "total_skip": total_skip,
            "skip_ratio": round(skip_ratio, 1),
            "state_quality": round(self._cur_sq, 3),
            "last_reason": sig.get("reason", ""),
            "entropy": sig.get("entropy", 0.5),
            "is_drift": sig.get("is_drift", False),
            "n_samples": sig.get("n_samples", 0),
            "engine": "APEX_SNIPER_V6.5_AUDIT",
            "bridge_note": (
                (sig.get("bridge_scan") or {}).get("best", {}).get("note", "")
                if (sig.get("bridge_scan") or {}).get("best") else ""
            ),
            "rolling_wr": round(self._rolling_wr() * 100, 1),
            "pause_left": self._pause_left,
        }
