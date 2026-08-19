

















from __future__ import annotations
import math
from typing import List, Dict, Any


def calculate_rolling_hurst(binary_seq: List[str], window_size: int = 50) -> float:
    """
    Hurst exponent qua R/S analysis trên cửa sổ trượt window_size phần tử
    cuối của binary_seq (chuyển T/X -> +1/-1).
    H ~ 0.5  -> random walk (không có cấu trúc)
    H > 0.55 -> trend-persistent
    H < 0.45 -> mean-reverting
    """
    if len(binary_seq) < window_size:
        return 0.5
    window = binary_seq[-window_size:]
    sub_seq = [1.0 if x == "T" else -1.0 for x in window]

    mean_val = sum(sub_seq) / len(sub_seq)
    deviations = [x - mean_val for x in sub_seq]

    cum_dev = []
    curr = 0.0
    for d in deviations:
        curr += d
        cum_dev.append(curr)

    r_range = max(cum_dev) - min(cum_dev)
    std_dev = math.sqrt(sum(d * d for d in deviations) / len(sub_seq))

    if std_dev == 0 or r_range == 0:
        return 0.5

    rs_value = r_range / std_dev
    hurst = math.log(rs_value) / math.log(window_size)
    return max(0.0, min(1.0, hurst))


class ContextTreeSwitching:
    """[ĐÃ KIỂM ĐỊNH VÀ TẮT — xem ghi chú đầu file]

    Test trên dữ liệu RANDOM 50/50 thuần (nhiều seed khác nhau) cho thấy
    class này KHÔNG hội tụ về ~50%, mà "khóa" vào 1 hướng dựa theo dao động
    sớm trong dữ liệu (84-89% lệch về 1 phía dù input hoàn toàn không có
    cấu trúc). Nguyên nhân: các order khác nhau dùng context LỒNG NHAU
    (order=k là suffix của order=k+1) nên các "phiếu" không độc lập như
    công thức trộn alpha giả định — fix đúng cần CTW recursive-doubling
    chuẩn (P_w(s) = (P_e(s) + P_w(s0)*P_w(s1))/2), không phải patch nhanh.

    QUAN TRỌNG HƠN: hàm `calc_ctw_prior()` ĐÃ CÓ SẴN trong
    apex_sniper_engine.py (đang dùng để ra quyết định cược thật) cũng thể
    hiện ĐÚNG kiểu lỗi này khi test cùng phương pháp (xem BACKTEST_REPORT.md
    bản cập nhật) — do cùng nguyên nhân (trộn theo order mà không tính tới
    sự tương quan giữa các context lồng nhau). Class này được GIỮ LẠI
    nguyên trạng (không xóa) để làm tài liệu tham khảo khi sửa calc_ctw_prior,
    nhưng KHÔNG được gọi ở bất kỳ đâu trong pipeline live nữa.
    """

    def __init__(self, max_depth: int = 5):
        self.max_depth = max_depth

    def predict_probability(self, binary_seq: List[str]) -> float:
        history_str = "".join(binary_seq)
        n = len(history_str)
        if n == 0:
            return 0.5

        def node_counts(context: str):
            ac = bc = 0
            k = len(context)
            for i in range(0, n - k):
                if history_str[i:i + k] == context:
                    nxt = history_str[i + k]
                    if nxt == "T":
                        bc += 1
                    else:
                        ac += 1
            return ac, bc

        def get_weighted_prob(order: int) -> float:
            order = min(order, self.max_depth, n - 1) if n > 0 else 0
            context = history_str[-order:] if order > 0 else ""
            ac, bc = node_counts(context)
            p_kt = (bc + 0.5) / (ac + bc + 1.0)

            if order <= 0:
                return p_kt

            alpha = 1.0 / (ac + bc + 2.0)
            p_parent = get_weighted_prob(order - 1)
            return alpha * p_kt + (1.0 - alpha) * p_parent

        start_order = min(self.max_depth, n - 1) if n > 0 else 0
        return get_weighted_prob(start_order)

    def predict(self, binary_seq: List[str]):
        p = self.predict_probability(binary_seq)
        target = "T" if p >= 0.5 else "X"
        prob = p if p >= 0.5 else 1.0 - p
        return target, prob
