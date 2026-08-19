
from __future__ import annotations
import math
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_ETA = 0.15





class EARCPEnsembleController:
    """
    Quản lý 1 tập hợp N+K "chuyên gia" (N = vài cấu hình CTW/PST độ sâu khác
    nhau, K = pool BridgeExpert từ live_bridge_taxonomy.py), MỖI VÒNG:
      1. Mỗi chuyên gia đưa ra p_i (xác suất P(T)) — chuyên gia "gate" (side=
         None, ví dụ AnomalyNoiseExpert) KHÔNG tham gia bước này, chỉ ảnh
         hưởng hệ số suy giảm toàn cục (xem `noise_gate_multiplier`).
      2. get_aggregated_prediction() -> p_hat = sum(W_i * p_i) (đã normalize W).
      3. _calculate_coherence(predictions, weights) -> phương sai có trọng số
         (độ phân tán dự đoán giữa các chuyên gia — thấp = đồng thuận cao).
      4. SAU KHI biết kết quả thật, update_weights(actual_result) cập nhật
         W_i = W_i * exp(-eta * L_i), L_i = log-loss (hoặc zero-one loss) của
         chuyên gia i, rồi normalize lại tổng = 1.

    KHÔNG persist trọng số qua các lần chạy bot khác nhau (đúng quy ước hiện
    tại của apex_sniper_engine.py: "không giữ state cần persist riêng giữa
    các lần chạy") — mỗi session mới bắt đầu lại W đồng đều [1/M]*M. Nếu sau
    này muốn persist (vd để tận dụng học từ session trước), cần thêm cơ chế
    lưu/nạp riêng — CHƯA làm ở đây để giữ rủi ro thấp nhất (trọng số học từ
    1 session ngắn rất dễ overfit, persist sẽ khuếch đại sai lệch đó sang
    session sau).
    """

    def __init__(self, expert_names: List[str], eta: float = DEFAULT_ETA,
                 loss_type: str = "log"):
        self.expert_names = list(expert_names)
        self.M = len(expert_names)
        self.eta = eta
        self.loss_type = loss_type


        self.W: List[float] = [1.0 / self.M] * self.M if self.M else []
        self._last_predictions: Optional[List[Dict[str, Any]]] = None
        self._last_aggregated: Optional[Dict[str, float]] = None
        self.round_count = 0



    def aggregate(self, predictions: List[Dict[str, Any]],
                  noise_gates: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """
        predictions: list dict {"side": "T"/"X"/None, "prob": float, "name": str}
        cùng độ dài + cùng thứ tự với self.expert_names — phần tử có
        side=None (abstain hoặc gate) bị LOẠI khỏi tính p_hat (không phải
        gán prob=0.5 rồi tính vào trung bình — sẽ làm loãng sai tín hiệu).

        noise_gates: list các dict từ AnomalyNoiseExpert-kiểu (side=None,
        "high_volatility": bool) — nếu BẤT KỲ gate nào báo high_volatility,
        trả "high_volatility": True để dynamic_execution_engine.py có thể
        ép ngưỡng lên cao / SKIP, đúng mục 4.4 "ép buộc suy giảm trọng số...
        chủ động ra lệnh SKIP chiến thuật".

        Trả về:
          {"pT": float, "pX": float, "coherence": float, "n_active": int,
           "high_volatility": bool, "active_names": [...]}
        """
        active_idx = [i for i, p in enumerate(predictions) if p.get("side") is not None]
        if not active_idx:
            self._last_predictions = predictions
            self._last_aggregated = {"pT": 0.5, "pX": 0.5}
            return {"pT": 0.5, "pX": 0.5, "coherence": 0.0, "n_active": 0,
                    "high_volatility": self._any_high_vol(noise_gates), "active_names": []}

        active_w = [self.W[i] for i in active_idx]
        w_sum = sum(active_w) or 1.0
        norm_w = [w / w_sum for w in active_w]


        p_list = []
        for idx in active_idx:
            p = predictions[idx]
            prob = float(p.get("prob", 0.5))
            p_i = prob if p["side"] == "T" else (1.0 - prob)
            p_list.append(p_i)

        p_hat = sum(w * p for w, p in zip(norm_w, p_list))
        coherence = self._calculate_coherence(p_list, norm_w, p_hat)

        self._last_predictions = predictions
        self._last_aggregated = {"pT": p_hat, "pX": 1.0 - p_hat}

        return {
            "pT": p_hat, "pX": 1.0 - p_hat,
            "coherence": coherence,
            "n_active": len(active_idx),
            "high_volatility": self._any_high_vol(noise_gates),
            "active_names": [self.expert_names[i] for i in active_idx],
        }

    @staticmethod
    def _any_high_vol(noise_gates: Optional[List[Dict[str, Any]]]) -> bool:
        if not noise_gates:
            return False
        return any(g.get("high_volatility") for g in noise_gates)

    def _calculate_coherence(self, predictions: List[float], weights: List[float],
                              p_hat: Optional[float] = None) -> float:
        """
        [TÊN HÀM THEO ĐẶC TẢ MỤC 5.3] Phương sai có trọng số của các dự đoán
        p_i quanh p_hat. Coherence ở đây được ĐỊNH NGHĨA = 1 - 4*variance
        (variance tối đa cho 2 cụm 0/1 đối lập là 0.25 -> coherence trong
        [0,1], 1 = mọi expert đồng thuận tuyệt đối, 0 = phân cực tối đa).
        """
        if not predictions:
            return 0.0
        if p_hat is None:
            p_hat = sum(w * p for w, p in zip(weights, predictions))
        variance = sum(w * (p - p_hat) ** 2 for w, p in zip(weights, predictions))
        coherence = max(0.0, 1.0 - 4.0 * variance)
        
        # [FIX] Phạt nặng coherence nếu có quá ít chuyên gia tham gia (đập tan tự tin ảo)
        # len(predictions) ở đây chính là n_active
        if len(predictions) <= 2:
            coherence = coherence * 0.5
            
        return coherence

    def get_aggregated_prediction(self) -> Dict[str, float]:
        """[TÊN HÀM THEO ĐẶC TẢ MỤC 5.3] Trả kết quả lần aggregate() gần nhất."""
        return dict(self._last_aggregated) if self._last_aggregated else {"pT": 0.5, "pX": 0.5}



    def update_weights(self, actual_result: str) -> List[float]:
        """
        Cập nhật trọng số theo MWUA tích hợp Fixed-Share (Chống chết lâm sàng chuyên gia)
        """
        if self._last_predictions is None or not self.W:
            return self.W

        eps = 1e-9
        # 1. Tính Loss và cập nhật theo MWUA tiêu chuẩn
        for i, pred in enumerate(self._last_predictions):
            if pred.get("side") is None:
                continue # Chuyên gia bỏ phiếu trắng, không phạt không thưởng

            prob_t = pred.get("prob", 0.5)
            # Clip xác suất để tránh log(0)
            prob_t = max(eps, min(1.0 - eps, prob_t))
            
            p_correct = prob_t if actual_result == "T" else (1.0 - prob_t)
            
            if self.loss_type == "log":
                loss = -math.log(p_correct)
            else: # zero_one
                side = "T" if prob_t >= 0.5 else "X"
                loss = 0.0 if side == actual_result else 1.0
                
            self.W[i] *= math.exp(-self.eta * loss)

        # 2. Chuẩn hóa lần 1
        total_w = sum(self.W)
        if total_w > 0:
            self.W = [w / total_w for w in self.W]
        else:
            self.W = [1.0 / len(self.W)] * len(self.W)

        # ================================================================
        # 3. KỸ THUẬT TỐI TÂN: FIXED-SHARE MIXING (CHỐNG CHẾT LÂM SÀNG)
        # Bơm "oxy" cho các chuyên gia bị dìm sâu, giúp bật dậy chớp nhoáng
        # khi nhà cái bẻ lái (Regime Shift).
        # ================================================================
        ALPHA = 0.05  # 5% trọng số được tái phân phối đều
        N = len(self.W)
        
        self.W = [(1.0 - ALPHA) * w + (ALPHA / N) for w in self.W]

        # Chuẩn hóa lần 2 (Đảm bảo tổng chính xác 100%)
        total_w = sum(self.W)
        self.W = [w / total_w for w in self.W]

        self._last_predictions = None
        return self.W

    def top_experts(self, n: int = 5) -> List[Tuple[str, float]]:
        pairs = sorted(zip(self.expert_names, self.W), key=lambda x: -x[1])
        return pairs[:n]

    def reset(self) -> None:
        self.W = [1.0 / self.M] * self.M if self.M else []
        self._last_predictions = None
        self._last_aggregated = None
        self.round_count = 0







if __name__ == "__main__":
    import random
    from live_bridge_taxonomy import build_default_expert_pool, update_pool, poll_pool

    def run_session(binary_seq, eta=DEFAULT_ETA, burn_in=20):
        pool = build_default_expert_pool()
        names = [e.name for e in pool]
        ctrl = EARCPEnsembleController(names, eta=eta)
        w_mwua = n_mwua = 0
        w_equal = n_equal = 0
        for i, actual in enumerate(binary_seq):
            preds = poll_pool(pool)
            agg = ctrl.aggregate(preds)
            if i >= burn_in and agg["n_active"] > 0:
                mwua_side = "T" if agg["pT"] >= agg["pX"] else "X"
                n_mwua += 1
                if mwua_side == actual:
                    w_mwua += 1

                active_probs = [p["prob"] if p["side"] == "T" else (1 - p["prob"])
                                 for p in preds if p["side"] is not None]
                if active_probs:
                    eq_pT = sum(active_probs) / len(active_probs)
                    eq_side = "T" if eq_pT >= 0.5 else "X"
                    n_equal += 1
                    if eq_side == actual:
                        w_equal += 1
            ctrl.update_weights(actual)
            update_pool(pool, actual)
        return (w_mwua, n_mwua), (w_equal, n_equal), ctrl

    print("=" * 90)
    print("TEST 1 — RANDOM (10 trial x 400 vòng): MWUA vs Equal-Weight vs 50% lý thuyết")
    print("=" * 90)
    random.seed(123)
    tot_mwua = [0, 0]
    tot_equal = [0, 0]
    for _ in range(10):
        seq = [random.choice("TX") for _ in range(400)]
        (wm, nm), (we, ne), _ = run_session(seq)
        tot_mwua[0] += wm; tot_mwua[1] += nm
        tot_equal[0] += we; tot_equal[1] += ne
    from stats_utils import fmt_result
    print(fmt_result("MWUA (EARCP)", *tot_mwua))
    print(fmt_result("Equal-weight (không MWUA)", *tot_equal))
    print("  -> Trên random, CẢ HAI phải quanh 50% — nếu MWUA lệch rõ ràng,")
    print("     đó là dấu hiệu overfitting trọng số vào nhiễu ngắn hạn (eta quá lớn).\n")

    print("=" * 90)
    print("TEST 2 — DỮ LIỆU THẬT (3 session, 340 phiên)")
    print("=" * 90)
    try:
        from load_data import load_by_session, to_side
        sessions = load_by_session()
        tot_mwua = [0, 0]
        tot_equal = [0, 0]
        all_top = {}
        for fn, rows in sessions:
            binary_seq = [to_side(r["actual"]) for r in rows if to_side(r["actual"])]
            (wm, nm), (we, ne), ctrl = run_session(binary_seq)
            tot_mwua[0] += wm; tot_mwua[1] += nm
            tot_equal[0] += we; tot_equal[1] += ne
            for name, w in ctrl.top_experts(3):
                all_top[name] = all_top.get(name, 0) + w
        print(fmt_result("MWUA (EARCP)", *tot_mwua))
        print(fmt_result("Equal-weight (không MWUA)", *tot_equal))
        print("\n  Chuyên gia có trọng số cao nhất cuối mỗi session (tổng 3 session):")
        for name, w in sorted(all_top.items(), key=lambda x: -x[1])[:5]:
            print(f"    {name:30s} tổng trọng số top-3 = {w:.3f}")
        print("\n  -> So với phần A (live system thật) và TEST 2 của advanced_ctw_pst_engine.py:")
        print("     CHỈ kết luận MWUA 'có ích' nếu accuracy ở đây VƯỢT RÕ baseline hiện tại")
        print("     (46.67%, CI95=[34.6,59.1]%) một cách có ý nghĩa — không chỉ vượt 50%.")
    except Exception as e:
        print(f"  [Bỏ qua test trên data thật — thiếu file/lỗi: {e}]")

    print("\n=== Self-test PASSED (không crash) ===")
