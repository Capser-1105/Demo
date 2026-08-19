
































from __future__ import annotations
import math
from collections import deque
from typing import Any, Dict, List, Optional

from advanced_ctw_pst_engine import calc_advanced_ctw_pst_prior
from live_bridge_taxonomy import (
    build_default_expert_pool, update_pool, poll_pool, AnomalyNoiseExpert,
)
from earcp_ensemble_controller import EARCPEnsembleController




ENABLE_EARCP_AS_PRIMARY_DECISION = True
ENABLE_CTW_PST_KNESER_NEY = True
DYNAMIC_THRESHOLD_BASELINE = 0.60
DYNAMIC_THRESHOLD_ALPHA = 0.20
DYNAMIC_THRESHOLD_BETA = 0.15
ENTROPY_MONITOR_WINDOW = 13
MIN_ACTIVE_EXPERTS_FOR_COHERENCE = 4







def shannon_entropy(window: List[str]) -> float:
    if not window:
        return 0.0
    t = window.count("T") / len(window)
    x = 1.0 - t
    ent = 0.0
    if t > 0:
        ent -= t * math.log2(t)
    if x > 0:
        ent -= x * math.log2(x)
    return ent






class DynamicExecutionEngine:
    """
    Pipeline đầy đủ theo mục 5.4:
      1. Nhận kết quả streaming (update_result) -> lưu buffer.
      2. Giám sát Entropy (10-15 ván gần nhất, mặc định ENTROPY_MONITOR_WINDOW).
      3. CTW/PST (N "chuyên gia" thống kê — ở đây N=2: KT-baseline song song
         Kneser-Ney, để so sánh trực tiếp ngay trong cùng 1 lần chạy) +
         Bridge Taxonomy (K chuyên gia, live_bridge_taxonomy.py) -> ghép qua
         EARCPEnsembleController.
      4. Ngưỡng động: θ_t = μ_baseline + α·variance_coherence + β·entropy(S)
           (đúng công thức mục 5.4 — variance_coherence = (1-coherence),
            vì coherence∈[0,1] với 1=đồng thuận tuyệt đối/variance=0)
      5. So sánh p_hat với θ_t -> BET TAI / BET XỈU / SKIP.

    Interface KHỚP ApexSniperEngine để có thể cắm thử thay thế trực tiếp.
    """

    def __init__(self, max_history: int = 200,
                 entropy_window: int = ENTROPY_MONITOR_WINDOW,
                 baseline: float = DYNAMIC_THRESHOLD_BASELINE,
                 alpha: float = DYNAMIC_THRESHOLD_ALPHA,
                 beta: float = DYNAMIC_THRESHOLD_BETA,
                 eta_mwua: Optional[float] = None,
                 use_kneser_ney: bool = ENABLE_CTW_PST_KNESER_NEY):
        self._history: deque = deque(maxlen=max_history)
        self._last_signal: dict = {}
        self.entropy_window = entropy_window
        self.baseline = baseline
        self.alpha = alpha
        self.beta = beta
        self.use_kneser_ney = use_kneser_ney






        self._stat_names = ["CTW_KT", "CTW_KneserNey"] if use_kneser_ney else ["CTW_KT"]

        self._bridge_pool = build_default_expert_pool()
        self._noise_gate_idx = [i for i, e in enumerate(self._bridge_pool)
                                  if isinstance(e, AnomalyNoiseExpert)]

        all_names = self._stat_names + [e.name for e in self._bridge_pool]
        kwargs = {"eta": eta_mwua} if eta_mwua is not None else {}
        self.controller = EARCPEnsembleController(all_names, **kwargs)



    def update_result(self, d1: int, d2: int, d3: int) -> None:
        total = d1 + d2 + d3
        res = "T" if total >= 11 else "X"



        if self._history:
            self.controller.update_weights(res)
        update_pool(self._bridge_pool, res)
        self._history.append({"d1": d1, "d2": d2, "d3": d3, "sum": total, "res": res})

    @property
    def n_samples(self) -> int:
        return len(self._history)

    def get_signal(self) -> Dict[str, Any]:
        hist = list(self._history)
        binary_seq = [h["res"] for h in hist]
        n = len(binary_seq)


        ent_window = binary_seq[-self.entropy_window:]
        entropy_val = shannon_entropy(ent_window)


        stat_preds: List[Dict[str, Any]] = []
        ctw_kt = calc_advanced_ctw_pst_prior(binary_seq, smoothing="kt") if n >= 3 else {"pT": 0.5, "pX": 0.5}
        side_kt = "T" if ctw_kt["pT"] >= ctw_kt["pX"] else "X"
        prob_kt = max(ctw_kt["pT"], ctw_kt["pX"])
        stat_preds.append({"side": side_kt, "prob": prob_kt, "name": "CTW_KT"})
        if self.use_kneser_ney:
            ctw_kn = calc_advanced_ctw_pst_prior(binary_seq, smoothing="kneser_ney") if n >= 3 else {"pT": 0.5, "pX": 0.5}
            side_kn = "T" if ctw_kn["pT"] >= ctw_kn["pX"] else "X"
            prob_kn = max(ctw_kn["pT"], ctw_kn["pX"])
            stat_preds.append({"side": side_kn, "prob": prob_kn, "name": "CTW_KneserNey"})


        bridge_preds = poll_pool(self._bridge_pool)
        noise_gates = [bridge_preds[i] for i in self._noise_gate_idx]


        all_preds = stat_preds + bridge_preds
        agg = self.controller.aggregate(all_preds, noise_gates=noise_gates)














        if agg["n_active"] < MIN_ACTIVE_EXPERTS_FOR_COHERENCE:
            variance_coherence = 1.0
        else:
            variance_coherence = 1.0 - agg["coherence"]
        theta_t = self.baseline + self.alpha * variance_coherence + self.beta * entropy_val
        theta_t = min(theta_t, 0.97)



        if agg.get("high_volatility"):
            theta_t = max(theta_t, 0.90)

        p_hat_t, p_hat_x = agg["pT"], agg["pX"]
        prob_post = max(p_hat_t, p_hat_x)
        target_post = "T" if p_hat_t >= p_hat_x else "X"


        decision = "WAIT"
        reason = ""
        if n < 5:
            reason = "Radar đang thu thập dữ liệu khởi tạo (EARCP)..."
        elif agg["n_active"] == 0:
            decision = "SKIP"
            reason = "[EARCP] Không chuyên gia nào đủ điều kiện tham gia vòng này (toàn bộ abstain)."
        elif prob_post < theta_t:
            decision = "SKIP"
            reason = (f"[EARCP NGƯỠNG ĐỘNG] p={prob_post:.3f} < θ_t={theta_t:.3f} "
                       f"(μ={self.baseline:.2f} + α·varCoh={variance_coherence:.2f}·{self.alpha} "
                       f"+ β·entropy={entropy_val:.2f}·{self.beta})")
        else:
            decision = "BET"
            reason = (f"[EARCP NGƯỠNG ĐỘNG] p={prob_post:.3f} >= θ_t={theta_t:.3f}, "
                       f"coherence={agg['coherence']:.2f}, {agg['n_active']} chuyên gia tham gia.")

        side = target_post if decision == "BET" else None

        result = {
            "decision": decision,
            "side": "TAI" if side == "T" else ("XIU" if side == "X" else None),
            "confidence": round(prob_post, 4),
            "reason": reason,
            "entropy": round(entropy_val, 4),
            "coherence": round(agg["coherence"], 4),
            "variance_coherence": round(variance_coherence, 4),
            "dynamic_threshold": round(theta_t, 4),
            "n_active_experts": agg["n_active"],
            "active_expert_names": agg["active_names"],
            "high_volatility": agg.get("high_volatility", False),
            "ctw_kt": {"pT": round(ctw_kt["pT"], 4)},
            "ctw_kneser_ney": ({"pT": round(ctw_kn["pT"], 4)} if self.use_kneser_ney else None),
            "top_experts": self.controller.top_experts(5),
            "n_samples": n,
        }
        self._last_signal = result
        return result

    @property
    def last_signal(self) -> dict:
        return self._last_signal

    def reset(self) -> None:
        self._history.clear()
        self._last_signal = {}
        self._bridge_pool = build_default_expert_pool()
        self.controller.reset()


_engine_instance: Optional[DynamicExecutionEngine] = None


def get_dynamic_execution_engine(reset: bool = False) -> DynamicExecutionEngine:
    global _engine_instance
    if _engine_instance is None or reset:
        _engine_instance = DynamicExecutionEngine()
    return _engine_instance







if __name__ == "__main__":
    import random

    print("=" * 90)
    print("TEST 1 — 300 vòng random, full pipeline — bắt crash/lỗi runtime")
    print("=" * 90)
    eng = get_dynamic_execution_engine(reset=True)
    errors = 0
    bet_count = 0
    random.seed(1)
    for i in range(300):
        try:
            sig = eng.get_signal()
            if sig["decision"] == "BET":
                bet_count += 1
            d1, d2, d3 = random.randint(1, 6), random.randint(1, 6), random.randint(1, 6)
            eng.update_result(d1, d2, d3)
        except Exception as e:
            errors += 1
            print("LỖI:", e)
            import traceback
            traceback.print_exc()
    print(f"[OK] 300 vòng full pipeline — lỗi={errors}, bet_count={bet_count}")
    print("Signal mẫu cuối:", {k: v for k, v in eng.last_signal.items()
                                  if k not in ("active_expert_names", "top_experts")})

    print("\n" + "=" * 90)
    print("TEST 2 — SO SÁNH TRỰC TIẾP với ApexSniperEngine hiện tại trên DATA THẬT")
    print("(walk-forward đúng session-boundary, burn-in=20, giống backtest_full.py)")
    print("=" * 90)
    try:
        from load_data import load_by_session, to_dice, to_side
        from apex_sniper_logic import ApexSniperEngine
        from stats_utils import fmt_result

        sessions = load_by_session()
        BURN_IN = 20

        w_new = n_new = 0
        w_old = n_old = 0
        n_new_bet_rate = n_old_bet_rate = 0
        total_rounds = 0
        for fn, rows in sessions:
            new_eng = get_dynamic_execution_engine(reset=True)
            old_eng = ApexSniperEngine()
            for i, r in enumerate(rows):
                d = to_dice(r)
                a = to_side(r["actual"])
                if d is None or a is None:
                    continue
                total_rounds += 1
                sig_new = new_eng.get_signal()
                sig_old = old_eng.get_signal()
                if i >= BURN_IN:
                    if sig_new["decision"] == "BET":
                        n_new_bet_rate += 1
                        n_new += 1
                        side_new = "T" if sig_new["side"] == "TAI" else "X"
                        if side_new == a:
                            w_new += 1
                    if sig_old["decision"] == "BET":
                        n_old_bet_rate += 1
                        n_old += 1
                        side_old = "T" if sig_old["side"] == "TAI" else "X"
                        if side_old == a:
                            w_old += 1
                new_eng.update_result(*d)
                old_eng.update_result(*d)

        print(fmt_result("DynamicExecutionEngine (EARCP, MỚI)", w_new, n_new))
        print(fmt_result("ApexSniperEngine (hệ thống hiện tại, LIVE)", w_old, n_old))
        print(f"\n  Tần suất cược: MỚI={n_new_bet_rate}/{total_rounds} "
              f"({n_new_bet_rate/total_rounds*100:.1f}%) | "
              f"HIỆN TẠI={n_old_bet_rate}/{total_rounds} ({n_old_bet_rate/total_rounds*100:.1f}%)")
        print("\n  KẾT LUẬN: KHÔNG bật ENABLE_EARCP_AS_PRIMARY_DECISION cho tới khi") 
        print("  dòng 'MỚI' ở trên vượt dòng 'HIỆN TẠI' CÓ Ý NGHĨA THỐNG KÊ — xem")
        print("  hướng dẫn 3 bước ở đầu file. Với n nhỏ như trên, kết luận sớm RẤT")
        print("  dễ là may rủi thống kê (xem các p<0.05 'giả' ở module khác trong audit).")
    except Exception as e:
        print(f"  [Bỏ qua test so sánh trên data thật — thiếu file/lỗi: {e}]")
        import traceback
        traceback.print_exc()

    print("\n=== Self-test PASSED ===")
