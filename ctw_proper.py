


















from __future__ import annotations
import math
from typing import Dict, List, Tuple, Optional


def _log_kt_update(c0: int, c1: int, log_pe: float, symbol: str) -> Tuple[int, int, float]:
    """1 bước update KT estimator (Laplace 1/2) tại 1 node, trả về (c0',c1',log_pe')."""
    total = c0 + c1
    cb = c1 if symbol == "T" else c0
    log_pe_new = log_pe + math.log((cb + 0.5) / (total + 1.0))
    if symbol == "T":
        c1 += 1
    else:
        c0 += 1
    return c0, c1, log_pe_new


class ProperCTW:
    """
    CTW nhị phân, độ sâu tối đa `max_depth`. KHÔNG giữ state giữa các lần
    gọi predict() khác nhau trên các history khác nhau — build lại từ đầu
    mỗi lần (giống cách calc_ctw_prior cũ hoạt động, để tương thích kiến
    trúc hiện tại: engine gọi lại trên toàn bộ deque mỗi vòng).

    Context string convention: ký tự CŨ NHẤT ở đầu, MỚI NHẤT ở cuối (đúng
    thứ tự thời gian) — context độ sâu d tại thời điểm t = history[t-d:t].
    """

    def __init__(self, max_depth: int = 5):
        self.max_depth = max_depth

        self.tree: Dict[str, Tuple[int, int, float, float]] = {}

    def _node(self, ctx: str) -> Tuple[int, int, float, float]:
        return self.tree.get(ctx, (0, 0, 0.0, 0.0))

    def _process_symbol(self, history: List[str], t: int, mutate: bool) -> Dict[int, float]:
        """
        Xử lý 1 ký tự tại vị trí t (dùng context từ history[:t]), update
        cây (nếu mutate=True) hoặc CHỈ tính log_pw giả định (mutate=False,
        dùng cho dự đoán — không ghi đè cây thật).
        Trả về dict {depth: log_pw_after_this_update} cho mọi depth đã đụng tới.
        """
        D = min(self.max_depth, t)
        symbol = history[t]
        contexts = {d: "".join(history[t - d:t]) for d in range(D + 1)}


        new_pe: Dict[int, Tuple[int, int, float]] = {}
        for d in range(D, -1, -1):
            ctx = contexts[d]
            c0, c1, log_pe, _ = self._node(ctx)
            c0n, c1n, log_pe_n = _log_kt_update(c0, c1, log_pe, symbol)
            new_pe[d] = (c0n, c1n, log_pe_n)


        log_pw_at: Dict[int, float] = {}
        for d in range(D, -1, -1):
            c0n, c1n, log_pe_n = new_pe[d]
            if d == D or (t - d - 1) < 0:


                log_pw = log_pe_n
            else:
                extending_symbol = history[t - d - 1]
                other_symbol = "X" if extending_symbol == "T" else "T"
                ctx_same = contexts[d + 1]
                ctx_other = other_symbol + contexts[d]
                log_pw_same = log_pw_at[d + 1]
                _, _, _, log_pw_other = self._node(ctx_other)

                m = max(log_pe_n, log_pw_same + log_pw_other)
                log_pw = m + math.log(
                    math.exp(log_pe_n - m) + math.exp(log_pw_same + log_pw_other - m)
                ) - math.log(2.0)
            log_pw_at[d] = log_pw
            if mutate:
                self.tree[contexts[d]] = (c0n, c1n, log_pe_n, log_pw)

        return log_pw_at

    def fit(self, history: List[str]) -> None:
        """Nạp toàn bộ lịch sử thật vào cây (mutate=True)."""
        for t in range(len(history)):
            self._process_symbol(history, t, mutate=True)

    def predict(self, history: List[str]) -> Dict[str, float]:
        """
        Dự đoán P(T)/P(X) cho ký tự TIẾP THEO sau `history`, KHÔNG ghi đè
        cây thật (chỉ mô phỏng 1 bước update giả định cho từng ứng viên).
        """
        if len(history) == 0:
            return {"pT": 0.5, "pX": 0.5}
        t = len(history)
        log_pw_T = self._process_symbol(history + ["T"], t, mutate=False)[0]
        log_pw_X = self._process_symbol(history + ["X"], t, mutate=False)[0]
        m = max(log_pw_T, log_pw_X)
        eT, eX = math.exp(log_pw_T - m), math.exp(log_pw_X - m)
        pT = eT / (eT + eX)
        return {"pT": pT, "pX": 1.0 - pT}


def calc_ctw_prior_v2(binary_seq: List[str], max_depth: int = 5) -> Dict[str, float]:
    """Drop-in thay thế calc_ctw_prior() cũ — cùng chữ ký trả về {"pT","pX","best_order"}."""
    n = len(binary_seq)
    if n < 3:
        return {"pT": 0.5, "pX": 0.5, "best_order": 0}
    ctw = ProperCTW(max_depth=max_depth)
    ctw.fit(binary_seq)
    pred = ctw.predict(binary_seq)


    best_order = 0
    for d in range(1, min(max_depth, n - 1) + 1):
        ctx = "".join(binary_seq[-d:])
        c0, c1, _, _ = ctw._node(ctx)
        if c0 + c1 > 0:
            best_order = d
    return {"pT": pred["pT"], "pX": pred["pX"], "best_order": best_order}
