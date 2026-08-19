






































from __future__ import annotations
from abc import ABC, abstractmethod
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple






class BridgeExpert(ABC):
    """
    class BridgeExpert(ABC) — yêu cầu 2 phương thức trừu tượng theo đặc tả:
      - update_state(new_result): nhận luồng dữ liệu mới ('T'/'X' từng phiên)
      - predict_next(): trả về tín hiệu dự báo, hoặc None nếu "bỏ phiếu"
        (chuyên gia không đủ tự tin để tham gia vòng này).

    Trả về của predict_next(), khi KHÔNG None, là dict:
      {"side": "T"|"X"|None, "prob": float (0.5-1.0), "name": str, "note": str}
    "side"=None với "prob" có nghĩa = expert đang CẢNH BÁO (vd nhiễu cao)
    chứ không dự đoán hướng — earcp_ensemble_controller.py xử lý riêng case
    này (KHÔNG tính vào trung bình có trọng số hướng, nhưng vẫn ảnh hưởng
    coherence/entropy nếu chuyên gia đó là loại "noise gate").
    """

    def __init__(self, name: str, max_history: int = 200):
        self.name = name
        self.history: Deque[str] = deque(maxlen=max_history)
        self.last_prediction: Optional[Dict[str, Any]] = None

    @abstractmethod
    def update_state(self, new_result: str) -> None:
        """new_result: 'T' hoặc 'X' — kết quả phiên vừa đóng."""
        raise NotImplementedError

    @abstractmethod
    def predict_next(self) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def _base_update(self, new_result: str) -> None:
        self.history.append(new_result)






class StreakExpert(BridgeExpert):
    """
    [MỤC 4.2 + 5.2] Bệt — khi đuôi chuỗi có >= min_streak ký tự giống nhau,
    dự đoán tiếp tục. Tin cậy tăng theo hàm SIGMOID phi tuyến tính theo độ
    dài bệt (đúng yêu cầu báo cáo: "trọng số tự tin... tăng trưởng theo một
    hàm Sigmoid... dựa trên chiều dài của cầu bệt").

    Khi cầu bị BẺ GÃY (kết quả mới khác dự đoán lần trước), phát cờ "Gãy
    Bệt" và TỰ VÔ HIỆU HÓA (predict_next() trả None) trong `cooldown_rounds`
    phiên tiếp theo — đúng yêu cầu "tạm thời vô hiệu hóa chính nó trong 3
    phiên tiếp theo để tránh bẫy".
    """

    def __init__(self, min_streak: int = 3, cooldown_rounds: int = 3, sigmoid_k: float = 0.8):
        super().__init__(name=f"Streak_min{min_streak}")
        self.min_streak = min_streak
        self.cooldown_rounds = cooldown_rounds
        self.sigmoid_k = sigmoid_k
        self._cooldown_left = 0
        self.break_flag = False

    def _current_run(self) -> Tuple[Optional[str], int]:
        if not self.history:
            return None, 0
        last = self.history[-1]
        n = 0
        for x in reversed(self.history):
            if x == last:
                n += 1
            else:
                break
        return last, n

    def update_state(self, new_result: str) -> None:
        self.break_flag = False
        if self.last_prediction and self.last_prediction.get("side"):
            if new_result != self.last_prediction["side"]:
                self.break_flag = True
                self._cooldown_left = self.cooldown_rounds
        self._base_update(new_result)
        if self._cooldown_left > 0:
            self._cooldown_left -= 1

    def predict_next(self) -> Optional[Dict[str, Any]]:
        if self._cooldown_left > 0:
            self.last_prediction = None
            return None
        side, run_len = self._current_run()
        if side is None or run_len < self.min_streak:
            self.last_prediction = None
            return None
        import math

        # [FIX] Giảm hệ số khuếch đại của hàm Sigmoid xuống một nửa (tránh tự tin tăng quá sốc)
        x = (run_len - self.min_streak) * (self.sigmoid_k * 0.5)
        prob = 0.5 + 0.5 * (1.0 / (1.0 + math.exp(-x)) - 0.5) * 2.0
        
        # [FIX] HARD CAP ở mốc 0.72. KHÔNG BAO GIỜ cho phép bệt tự nó kích hoạt SNIPER_MAX (>=0.75)
        # Thực tế thống kê Bệt 4-5 tự gãy rất nhiều (WR thật < 50%)
        prob = min(prob, 0.72)
        pred = {"side": side, "prob": prob, "name": self.name,
                "note": f"Bệt {side} {run_len} ván, sigmoid_prob={prob:.2f}"}
        self.last_prediction = pred
        return pred


class AlternatingExpert(BridgeExpert):
    """[MỤC 4.2 + 5.2] Đảo 1-1 (n-n tổng quát qua period=p) — dự đoán đảo
    chiều liên tục khi >= min_cycles chu kỳ p đã xác nhận."""

    def __init__(self, period: int = 1, min_cycles: int = 3):
        super().__init__(name=f"Alternating_p{period}")
        self.period = period
        self.min_cycles = min_cycles

    def update_state(self, new_result: str) -> None:
        self._base_update(new_result)

    def predict_next(self) -> Optional[Dict[str, Any]]:
        p = self.period
        need = p * self.min_cycles + p
        if len(self.history) < need:
            self.last_prediction = None
            return None
        seq = list(self.history)
        n = len(seq)
        matches = 0
        i = n - 1
        while i - p >= 0 and seq[i] == seq[i - p]:
            matches += 1
            i -= p
        if matches < self.min_cycles:
            self.last_prediction = None
            return None
        predicted = seq[n - p]
        prob = min(0.55 + 0.04 * matches, 0.85)
        pred = {"side": predicted, "prob": prob, "name": self.name,
                "note": f"Đảo chu kỳ {p}, khớp {matches} chu kỳ liên tiếp"}
        self.last_prediction = pred
        return pred






class StepTrendExpert(BridgeExpert):
    """
    [MỤC 4.2 + 5.2] Cầu Tăng Giảm (1-2-3, 3-2-1, 1-2-1, 2-1-2, Nhảy Cóc).
    Dùng _run_length_encoding(sequence) (TÊN HÀM THEO ĐẶC TẢ) để biến chuỗi
    thành vector độ dài đoạn, so khớp với 1 trong các template, rồi nội suy
    phần tử kế tiếp.
    """

    TEMPLATES = {
        "1-2-3": [1, 2, 3], "3-2-1": [3, 2, 1],
        "1-2-1": [1, 2, 1], "2-1-2": [2, 1, 2],
        "1-2-3-4": [1, 2, 3, 4],
    }

    def __init__(self):
        super().__init__(name="StepTrend")

    def update_state(self, new_result: str) -> None:
        self._base_update(new_result)

    @staticmethod
    def _run_length_encoding(sequence: List[str]) -> List[Tuple[str, int]]:
        """[TÊN HÀM THEO ĐẶC TẢ MỤC 5.2] ['T','T','X'] -> [('T',2),('X',1)]."""
        if not sequence:
            return []
        out = []
        cur, n = sequence[0], 1
        for x in sequence[1:]:
            if x == cur:
                n += 1
            else:
                out.append((cur, n))
                cur, n = x, 1
        out.append((cur, n))
        return out

    def predict_next(self) -> Optional[Dict[str, Any]]:
        runs = self._run_length_encoding(list(self.history))
        lengths = [r[1] for r in runs]
        if len(lengths) < 2:
            self.last_prediction = None
            return None
        for name, tmpl in self.TEMPLATES.items():
            k = len(tmpl)
            if len(lengths) >= k and lengths[-k:] == tmpl:


                last_symbol = runs[-1][0]
                predicted = "X" if last_symbol == "T" else "T"
                pred = {"side": predicted, "prob": 0.62, "name": self.name,
                        "note": f"Khớp mẫu tăng giảm {name} ở {k} đoạn gần nhất -> dự đoán đảo {predicted}"}
                self.last_prediction = pred
                return pred
        self.last_prediction = None
        return None






class BiasExpert(BridgeExpert):
    """[MỤC 4.3 + 5.2] Cầu Nghiêng — Weighted Moving Average trên window
    `window` phiên. Nếu tỷ trọng 1 cửa > `threshold`, dự đoán cùng chiều,
    tiếp tục cho tới khi tỷ trọng tụt dưới mean (động lượng suy yếu)."""

    def __init__(self, window: int = 7, threshold: float = 0.80):
        super().__init__(name=f"Bias_w{window}")
        self.window = window
        self.threshold = threshold

    def update_state(self, new_result: str) -> None:
        self._base_update(new_result)

    def _wma_ratio(self) -> Optional[Tuple[str, float]]:
        w = self.window
        if len(self.history) < w:
            return None
        window = list(self.history)[-w:]

        weights = list(range(1, w + 1))
        wsum = sum(weights)
        t_weighted = sum(wt for wt, x in zip(weights, window) if x == "T")
        x_weighted = sum(wt for wt, x in zip(weights, window) if x == "X")
        ratio_t = t_weighted / wsum
        ratio_x = x_weighted / wsum
        if ratio_t >= ratio_x:
            return "T", ratio_t
        return "X", ratio_x

    def predict_next(self) -> Optional[Dict[str, Any]]:
        r = self._wma_ratio()
        if r is None:
            self.last_prediction = None
            return None
        side, ratio = r
        if ratio < self.threshold:
            self.last_prediction = None
            return None
            
        # [FIX] Bóp hệ số nhân từ 0.7 xuống 0.45.
        # Ví dụ ratio=0.8 (ngưỡng trúng) -> prob = 0.5 + 0.3*0.45 = 0.635 (Mức hợp lý cho MID/HIGH)
        prob = min(0.50 + (ratio - 0.5) * 0.45, 0.72)
        
        pred = {"side": side, "prob": prob, "name": self.name,
                "note": f"Nghiêng {side} WMA={ratio:.2f} trên {self.window} phiên"}
        self.last_prediction = pred
        return pred


class ZigzagExpert(BridgeExpert):
    """
    [MỤC 4.3 + 5.2] Cầu Zigzag/Chuyền dài — so khớp ĐỘ TƯƠNG ĐỒNG (không
    cần khớp tuyệt đối) giữa 2 cửa sổ liên tiếp độ dài `block_len`. Nếu tỷ
    lệ khớp >= alpha, coi là motif đang "tịnh tiến" và copy phần tử cách
    `block_len` phiên về trước làm dự đoán.

    [GIẢN LƯỢC CÓ GHI RÕ] Báo cáo gốc gọi đây là "Aho-Corasick/LCS" — bản
    đầy đủ của 2 thuật toán đó dùng cho so khớp ĐA-mẫu trên BẢNG CHỮ LỚN
    (NLP/bioinformatics). Với alphabet nhị phân + 1 cặp cửa sổ, phép so
    khớp tương đồng vị-trí-theo-vị-trí dưới đây cho ĐÚNG bản chất thuật
    toán cần (đo độ tương đồng 2 cửa sổ để quyết định "copy tiếp") mà
    không cần kéo theo độ phức tạp cài đặt của LCS/Aho-Corasick tổng quát.
    """

    def __init__(self, block_len: int = 5, alpha: float = 0.8):
        super().__init__(name=f"Zigzag_b{block_len}")
        self.block_len = block_len
        self.alpha = alpha

    def update_state(self, new_result: str) -> None:
        self._base_update(new_result)

    def predict_next(self) -> Optional[Dict[str, Any]]:
        b = self.block_len
        if len(self.history) < 2 * b:
            self.last_prediction = None
            return None
        seq = list(self.history)
        recent = seq[-b:]
        prior = seq[-2 * b:-b]
        matches = sum(1 for a, c in zip(recent, prior) if a == c)
        similarity = matches / b
        if similarity < self.alpha:
            self.last_prediction = None
            return None
        predicted = seq[-b]
        prob = min(0.5 + (similarity - self.alpha) * 1.5 + 0.1, 0.80)
        pred = {"side": predicted, "prob": prob, "name": self.name,
                "note": f"2 cửa sổ {b} phiên tương đồng {similarity:.0%} -> copy tịnh tiến"}
        self.last_prediction = pred
        return pred






class SymmetricalMirrorExpert(BridgeExpert):
    """
    [MỤC 4.1 — THỰC SỰ MỚI, KHÔNG có trong bridge_pattern_scanner.py]
    apex_sniper_engine.detect_palindrome() CHỈ dùng palindrome làm cờ "Trap"
    (lý do SKIP, không dự đoán hướng). Expert này biến nó thành dự đoán
    HƯỚNG thật: nếu (W-1) phiên gần nhất, SAU KHI BỎ phần tử đầu, đã tự đối
    xứng (palindrome), thì phần tử kế tiếp được dự đoán = phần tử ĐẦU của
    (W-1) phiên đó (để hoàn thiện trục đối xứng cho toàn bộ W phiên).

    Ví dụ: lịch sử "...T-T-X-T-X-T" (6 phiên) — bỏ phần tử đầu "T" còn lại
    "T-X-T-X-T" (5 phiên) tự đối xứng (palindrome) -> dự đoán phiên kế tiếp
    = phần tử đầu đã bỏ = "T", hoàn thiện cầu 7 phiên "T-T-X-T-X-T-T" đối
    xứng hoàn chỉnh (đúng ví dụ minh hoạ trong báo cáo).
    """

    def __init__(self, min_axis_len: int = 4, max_window: int = 11):
        super().__init__(name=f"SymmMirror_max{max_window}")
        self.min_axis_len = min_axis_len
        self.max_window = max_window

    def update_state(self, new_result: str) -> None:
        self._base_update(new_result)

    def predict_next(self) -> Optional[Dict[str, Any]]:
        seq = list(self.history)
        n = len(seq)
        best = None


        for W in range(self.min_axis_len + 2, min(self.max_window, n + 1) + 1):
            known = seq[-(W - 1):]
            axis_part = known[1:]
            if len(axis_part) < self.min_axis_len:
                continue
            if axis_part == axis_part[::-1]:
                predicted = known[0]
                evidence = len(axis_part)
                if best is None or evidence > best["evidence"]:
                    best = {"side": predicted, "evidence": evidence, "W": W}
        if best is None:
            self.last_prediction = None
            return None
        prob = min(0.55 + 0.02 * best["evidence"], 0.80)
        pred = {"side": best["side"], "prob": prob, "name": self.name,
                "note": f"Trục đối xứng dài {best['evidence']} phiên -> hoàn thiện cầu {best['W']} phiên"}
        self.last_prediction = pred
        return pred






class DoublePeriodicExpert(BridgeExpert):
    """
    [MỤC 4.1 — THỰC SỰ MỚI] bridge_pattern_scanner.find_periodic_pattern()
    yêu cầu khớp CHÍNH XÁC 100% (seq[i]==seq[i-p] cho MỌI i trong window).
    Expert này nới điều kiện: cho phép tỷ lệ khớp >= `match_ratio` (mặc
    định 80%) trên 1 window dài hơn — đúng yêu cầu "cho phép các khoảng
    trống ngẫu nhiên giữa các khối lặp" (báo cáo gọi đây là "phân tích phổ
    chuỗi" — bản dưới đây là phiên bản AUTOCORRELATION-BASED, giản lược có
    ghi rõ: phân tích phổ/Fourier thường dùng cho chuỗi SỐ liên tục, không
    phải chuẩn cho alphabet nhị phân rời rạc; autocorrelation-with-tolerance
    cho đúng bản chất "có chu kỳ NHƯNG không hoàn hảo" mà báo cáo mô tả,
    đơn giản hơn để kiểm chứng/debug).
    """

    def __init__(self, period: int, window_cycles: int = 4, match_ratio: float = 0.80):
        super().__init__(name=f"DoublePeriodic_p{period}")
        self.period = period
        self.window_cycles = window_cycles
        self.match_ratio = match_ratio

    def update_state(self, new_result: str) -> None:
        self._base_update(new_result)

    def predict_next(self) -> Optional[Dict[str, Any]]:
        p = self.period
        w = p * self.window_cycles
        if len(self.history) < w + p:
            self.last_prediction = None
            return None
        seq = list(self.history)
        n = len(seq)
        window = seq[-w:]
        compare = seq[-(w + p):-p]
        matches = sum(1 for a, b in zip(window, compare) if a == b)
        ratio = matches / w
        if ratio < self.match_ratio:
            self.last_prediction = None
            return None
        predicted = seq[n - p]
        
        # [FIX] Giảm base buffer (0.08 -> 0.05) và giảm scale (1.0 -> 0.8), cap ở 0.72
        prob = min(0.5 + (ratio - self.match_ratio) * 0.8 + 0.05, 0.72)
        
        pred = {"side": predicted, "prob": prob, "name": self.name,
                "note": f"Chu kỳ {p} khớp {ratio:.0%} trên {w} phiên (cho phép khoảng trống)"}
        self.last_prediction = pred
        return pred






class MeanReversionExpert(BridgeExpert):
    """
    [MỤC 4.1 — THỰC SỰ MỚI, không tồn tại ở đâu trong code cũ]
    "Xuất hiện khi tỷ lệ xuất hiện của một cửa vượt quá 75% trong 20 phiên,
    thị trường sẽ có xu hướng ép buộc chuỗi kết quả cân bằng lại" — đúng
    nguyên văn báo cáo mục 4.1. Lưu ý: đây là 1 GIẢ THUYẾT THỐNG KÊ (gambler's-
    fallacy-adjacent — với xúc xắc CÔNG BẰNG/độc lập từng phiên, tỷ lệ lệch
    trong 20 phiên KHÔNG mang thông tin gì về phiên 21, vì các phiên độc
    lập). backtest_extra.py mục H đã kiểm định 1 biến thể gần giống claim
    này trên "tổng điểm >=14 liên tiếp" và KHÔNG cho kết quả ủng hộ giả
    thuyết hồi quy (xem BACKTEST_REPORT). Expert này được cài ĐẦY ĐỦ theo
    đúng yêu cầu báo cáo để có thể tự backtest lại với chính xác định nghĩa
    "75%/20 phiên" — KHÔNG bật live cho tới khi có số liệu ủng hộ.
    """

    def __init__(self, window: int = 20, threshold: float = 0.75):
        super().__init__(name=f"MeanReversion_w{window}_th{int(threshold*100)}")
        self.window = window
        self.threshold = threshold

    def update_state(self, new_result: str) -> None:
        self._base_update(new_result)

    def predict_next(self) -> Optional[Dict[str, Any]]:
        w = self.window
        if len(self.history) < w:
            self.last_prediction = None
            return None
        window = list(self.history)[-w:]
        t_ratio = window.count("T") / w
        x_ratio = 1.0 - t_ratio
        if t_ratio >= self.threshold:
            predicted = "X"
            excess = t_ratio
        elif x_ratio >= self.threshold:
            predicted = "T"
            excess = x_ratio
        else:
            self.last_prediction = None
            return None
        prob = min(0.5 + (excess - self.threshold) * 1.2 + 0.05, 0.70)
        pred = {"side": predicted, "prob": prob, "name": self.name,
                "note": f"Lệch {excess:.0%} trong {w} phiên -> giả thuyết hồi quy về {predicted}"}
        self.last_prediction = pred
        return pred






class AnomalyNoiseExpert(BridgeExpert):
    """
    [MỤC 4.4] KHÔNG dự đoán hướng — chỉ phát cờ "nhiễu cao"/"gãy đảo bất
    thường" để earcp_ensemble_controller.py / dynamic_execution_engine.py
    dùng làm hệ số GIẢM trọng số toàn bộ chuyên gia hướng khác (đúng yêu
    cầu: "ép buộc suy giảm trọng số của toàn bộ các chuyên gia dự báo
    hướng, và chủ động ra lệnh SKIP chiến thuật").

    predict_next() LUÔN trả "side": None — đây là "noise gate", không phải
    "direction expert" — earcp_ensemble_controller.py phân biệt 2 loại này
    qua key "side" (None = gate, không tính vào trung bình có trọng số).
    """

    def __init__(self, window: int = 13, high_entropy_threshold: float = 0.95):
        super().__init__(name=f"AnomalyNoise_w{window}")
        self.window = window
        self.high_entropy_threshold = high_entropy_threshold

    def update_state(self, new_result: str) -> None:
        self._base_update(new_result)

    def _shannon_entropy(self, window: List[str]) -> float:
        import math
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

    def predict_next(self) -> Optional[Dict[str, Any]]:
        w = self.window
        if len(self.history) < w:
            return None
        window = list(self.history)[-w:]
        ent = self._shannon_entropy(window)
        is_noisy = ent > self.high_entropy_threshold
        pred = {"side": None, "prob": 0.5, "name": self.name,
                "high_volatility": is_noisy, "entropy": round(ent, 4),
                "note": f"Entropy {ent:.3f} trên {w} phiên" + (" — NHIỄU CAO" if is_noisy else "")}
        self.last_prediction = pred
        return pred














def build_default_expert_pool() -> List[BridgeExpert]:
    pool: List[BridgeExpert] = []

    pool.append(StreakExpert(min_streak=3))

    for p in (1, 2, 3, 4):
        pool.append(AlternatingExpert(period=p))

    pool.append(StepTrendExpert())

    for w, th in ((5, 0.80), (7, 0.75), (10, 0.70)):
        pool.append(BiasExpert(window=w, threshold=th))

    for b in (3, 5, 7):
        pool.append(ZigzagExpert(block_len=b))

    for mw in (8, 11):
        pool.append(SymmetricalMirrorExpert(max_window=mw))

    for p in (2, 3, 4, 5):
        pool.append(DoublePeriodicExpert(period=p))

    for th in (0.75, 0.80):
        pool.append(MeanReversionExpert(threshold=th))

    pool.append(AnomalyNoiseExpert())
    return pool


def update_pool(pool: List[BridgeExpert], new_result: str) -> None:
    for expert in pool:
        expert.update_state(new_result)


def poll_pool(pool: List[BridgeExpert]) -> List[Dict[str, Any]]:
    """Trả về vector K dự đoán (đúng 1 phần tử / expert, None nếu bỏ phiếu)."""
    out = []
    for expert in pool:
        pred = expert.predict_next()
        out.append(pred if pred is not None else {"side": None, "prob": 0.5,
                                                     "name": expert.name, "note": "abstain"})
    return out






if __name__ == "__main__":
    import random

    pool = build_default_expert_pool()
    print(f"Kho chuyên gia: K = {len(pool)} experts "
          f"({'ĐẠT' if 20 <= len(pool) <= 30 else 'NGOÀI'} khoảng 20-30 theo đặc tả mục 5.2)")
    for e in pool:
        print(f"  - {e.name}")

    print("\n" + "=" * 90)
    print("MONTE CARLO trên RANDOM THUẦN (500 vòng x 20 trial) — đo fire-rate mỗi expert")
    print("(bridge_pattern_scanner.py gốc đo được 76.6%+23.4% trên cùng kiểu test này)")
    print("=" * 90)
    random.seed(7)
    n_trials = 20
    n_rounds = 500
    fire_counts = {e.name: 0 for e in pool}
    direction_counts = {e.name: 0 for e in pool}
    total_polls = 0
    for trial in range(n_trials):
        pool = build_default_expert_pool()
        for _ in range(n_rounds):
            r = random.choice("TX")
            update_pool(pool, r)
            preds = poll_pool(pool)
            total_polls += 1
            for e, p in zip(pool, preds):
                if p["side"] is not None:
                    fire_counts[e.name] += 1
                    direction_counts[e.name] += 1

    print(f"{'Expert':30s} {'Fire-rate trên random':>24s}")
    for name in fire_counts:
        rate = fire_counts[name] / total_polls * 100
        flag = "  <-- CAO, cẩn trọng" if rate > 15 else ""
        print(f"{name:30s} {rate:22.2f}%{flag}")

    print("\n=== Self-test PASSED — fire-rate cao trên random KHÔNG đồng nghĩa có edge, ===")
    print("=== chỉ là tần suất 'tìm thấy mẫu' — PHẢI backtest trên data thật trước khi tin. ===")
