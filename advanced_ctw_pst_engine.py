
from __future__ import annotations
import math
from typing import Dict, List, Tuple, Optional, Literal, Any


DEFAULT_MAX_DEPTH = 5
DEFAULT_KN_DISCOUNT = 0.75



DEFAULT_JS_PRUNE_THRESHOLD = 0.01



SmoothingMethod = Literal["kt", "kneser_ney"]






def _kl_bernoulli(p: float, q: float) -> float:
    """KL(p || q) cho 2 phân phối Bernoulli, an toàn ở biên 0/1."""
    eps = 1e-12
    p = min(max(p, eps), 1 - eps)
    q = min(max(q, eps), 1 - eps)
    return p * math.log(p / q) + (1 - p) * math.log((1 - p) / (1 - q))


def jensen_shannon_divergence(p: float, q: float) -> float:
    """
    JS(p,q) = 0.5*KL(p||m) + 0.5*KL(q||m), m=(p+q)/2 — đối xứng, bị chặn
    trong [0, ln2] nats. Dùng để so sánh phân phối P(T) giữa 1 nút và nút
    cha của nó (cùng 1 tham số p vì alphabet nhị phân T/X).
    """
    m = (p + q) / 2.0
    return 0.5 * _kl_bernoulli(p, m) + 0.5 * _kl_bernoulli(q, m)






class CTW_PST_Model:
    """
    Cấu trúc cây Trie cho Probabilistic Suffix Tree (PST), mỗi nút lưu
    (c0, c1) = (đếm X, đếm T) tại context đó, kết hợp:
      - Pe (xác suất ước lượng tại nút): chọn 1 trong 2 phương pháp qua
        `smoothing` ("kt" = Krichevsky-Trofimov như ctw_proper.py hiện tại,
        "kneser_ney" = chiết khấu tuyệt đối + continuation, mục 2.3 báo cáo).
      - Pw (CTW blending đệ quy): P_w(s) = (P_e(s) + P_w(s0)*P_w(s1)) / 2,
        ĐÚNG công thức Willems-Shtarkov-Tjalkens (1995), giống ctw_proper.py
        — đây KHÔNG phải điểm khác biệt, chỉ là tái cài đặt tương thích để
        module này độc lập, dễ so sánh song song.
      - _prune_tree(): cắt các nhánh sâu mà JS-divergence so với nút cha
        dưới ngưỡng — tiết kiệm tính toán khi max_depth lớn (ít quan trọng
        ở max_depth=5 hiện tại, nhưng cần có nếu sau này tăng depth).

    KHÔNG giữ state giữa các lần gọi trên history khác nhau — build lại từ
    đầu mỗi lần (giống ctw_proper.ProperCTW, để tương thích pipeline hiện
    tại: engine gọi lại trên toàn bộ deque mỗi vòng).
    """

    def __init__(self, max_depth: int = DEFAULT_MAX_DEPTH,
                 smoothing: SmoothingMethod = "kt",
                 kn_discount: float = DEFAULT_KN_DISCOUNT):
        self.max_depth = max_depth
        self.smoothing = smoothing
        self.kn_discount = kn_discount

        self.tree: Dict[str, Tuple[int, int, float, float]] = {}



        self._pruned: set = set()



    def _log_pe_kt(self, c0: int, c1: int, symbol: str) -> float:
        """Krichevsky-Trofimov (Laplace 1/2) — giống ctw_proper.py."""
        total = c0 + c1
        cb = c1 if symbol == "T" else c0
        return math.log((cb + 0.5) / (total + 1.0))

    def _kneser_ney_smoothing(self, context: str, symbol: str,
                               counts_by_ctx: Dict[str, Tuple[int, int]]) -> float:
        """
        [MỤC 2.3 + 5.1 BÁO CÁO] Kneser-Ney chiết khấu tuyệt đối, áp dụng cho
        alphabet nhị phân (T/X). KHÔNG BAO GIỜ trả về xác suất 0 — khi gặp
        context mới (count=0 ở mọi nơi), lùi dần (backoff) về context ngắn
        hơn, tới base case context rỗng dùng Laplace toàn cục.

        Công thức (Chen & Goodman 1999, rút gọn cho 2 symbol):
            P_KN(s|ctx) = max(c(ctx,s) - D, 0) / N(ctx)
                          + bow(ctx) * P_KN(s|ctx[1:])
            bow(ctx) = D * |{s': c(ctx,s') > 0}| / N(ctx)
        Base case (ctx rỗng): Laplace toàn cục (c_s+0.5)/(N+1).

        Trả về xác suất P(symbol | context) — KHÔNG phải log, vì công thức
        backoff là CỘNG (không phải tích), không cần log-space ở đây (số
        lượng phép cộng nhỏ = max_depth, không underflow).
        """
        D = self.kn_discount

        def recurse(ctx: str) -> float:
            c0, c1 = counts_by_ctx.get(ctx, (0, 0))
            n = c0 + c1
            if ctx == "":

                cs = c1 if symbol == "T" else c0
                return (cs + 0.5) / (n + 1.0)
            if n == 0:


                return recurse(ctx[1:])
            cs = c1 if symbol == "T" else c0
            discounted = max(cs - D, 0.0) / n
            n_distinct = (1 if c0 > 0 else 0) + (1 if c1 > 0 else 0)
            bow = (D * n_distinct) / n
            return discounted + bow * recurse(ctx[1:])

        return recurse(context)



    def _node(self, ctx: str) -> Tuple[int, int, float, float]:
        return self.tree.get(ctx, (0, 0, 0.0, 0.0))

    def _process_symbol(self, history: List[str], t: int, mutate: bool,
                         counts_by_ctx: Optional[Dict[str, Tuple[int, int]]] = None
                         ) -> Dict[int, float]:
        D = min(self.max_depth, t)
        symbol = history[t]
        contexts = {d: "".join(history[t - d:t]) for d in range(D + 1)}

        new_pe: Dict[int, Tuple[int, int, float]] = {}
        for d in range(D, -1, -1):
            ctx = contexts[d]
            c0, c1, log_pe, _ = self._node(ctx)
            if self.smoothing == "kneser_ney" and counts_by_ctx is not None:
                p_target = self._kneser_ney_smoothing(ctx, symbol, counts_by_ctx)
                log_pe_n = math.log(max(p_target, 1e-12))
            else:
                log_pe_n = self._log_pe_kt(c0, c1, symbol)
            c0n, c1n = c0, c1
            if symbol == "T":
                c1n += 1
            else:
                c0n += 1
            new_pe[d] = (c0n, c1n, log_pe_n)

        log_pw_at: Dict[int, float] = {}
        for d in range(D, -1, -1):
            c0n, c1n, log_pe_n = new_pe[d]
            if d == D or (t - d - 1) < 0 or contexts[d] in self._pruned:
                log_pw = log_pe_n
            else:
                extending_symbol = history[t - d - 1]
                other_symbol = "X" if extending_symbol == "T" else "T"
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

    def _build_counts_by_ctx(self, history: List[str]) -> Dict[str, Tuple[int, int]]:
        """Đếm (c0,c1) cho MỌI context độ dài 0..max_depth xuất hiện trong
        history — cần cho Kneser-Ney (đếm count thật, không phải log-space
        tích lũy như KT)."""
        counts: Dict[str, List[int]] = {}
        n = len(history)
        for t in range(n):
            symbol = history[t]
            D = min(self.max_depth, t)
            for d in range(D + 1):
                ctx = "".join(history[t - d:t])
                if ctx not in counts:
                    counts[ctx] = [0, 0]
                if symbol == "T":
                    counts[ctx][1] += 1
                else:
                    counts[ctx][0] += 1
        return {k: (v[0], v[1]) for k, v in counts.items()}

    def fit(self, history: List[str]) -> None:
        counts_by_ctx = self._build_counts_by_ctx(history) if self.smoothing == "kneser_ney" else None
        for t in range(len(history)):
            self._process_symbol(history, t, mutate=True, counts_by_ctx=counts_by_ctx)

    def predict_proba(self, sequence: List[str]) -> Dict[str, float]:
        """
        [TÊN HÀM THEO ĐẶC TẢ MỤC 5.1] Dự đoán P(T)/P(X) cho ký tự TIẾP THEO
        sau `sequence`. Build lại cây từ đầu mỗi lần gọi (xem docstring
        class) rồi mô phỏng 1 bước giả định cho cả 2 ứng viên T/X.
        """
        if len(sequence) == 0:
            return {"pT": 0.5, "pX": 0.5}
        self.tree = {}
        self._pruned = set()
        self.fit(sequence)
        if self.smoothing == "kneser_ney":
            self._prune_tree()

        t = len(sequence)
        counts_by_ctx = self._build_counts_by_ctx(sequence + ["T"]) if self.smoothing == "kneser_ney" else None
        log_pw_T = self._process_symbol(sequence + ["T"], t, mutate=False, counts_by_ctx=counts_by_ctx)[0]
        counts_by_ctx_x = self._build_counts_by_ctx(sequence + ["X"]) if self.smoothing == "kneser_ney" else None
        log_pw_X = self._process_symbol(sequence + ["X"], t, mutate=False, counts_by_ctx=counts_by_ctx_x)[0]
        m = max(log_pw_T, log_pw_X)
        eT, eX = math.exp(log_pw_T - m), math.exp(log_pw_X - m)
        pT = eT / (eT + eX)
        return {"pT": pT, "pX": 1.0 - pT}



    def _prune_tree(self, jensen_shannon_threshold: float = DEFAULT_JS_PRUNE_THRESHOLD) -> int:
        """
        [TÊN HÀM THEO ĐẶC TẢ MỤC 5.1] So sánh P(T|ctx) của mỗi nút với
        P(T|ctx[1:]) (nút cha — context NGẮN hơn 1 ký tự). Nếu JS-divergence
        giữa 2 phân phối Bernoulli này < threshold, đánh dấu ctx là "pruned"
        — predict_proba() sẽ KHÔNG mở rộng cây qua context đó nữa (coi Pw
        tại context cha là đủ, không cần lưu/dùng context dài hơn).

        Trả về số nút bị cắt (để log/debug).
        """
        self._pruned = set()
        n_pruned = 0
        for ctx, (c0, c1, log_pe, log_pw) in list(self.tree.items()):
            if len(ctx) == 0:
                continue
            parent_ctx = ctx[1:]
            parent = self.tree.get(parent_ctx)
            if parent is None:
                continue
            p_total = c0 + c1
            if p_total == 0:
                continue
            p_this = c1 / p_total
            pc0, pc1 = parent[0], parent[1]
            p_total_parent = pc0 + pc1
            if p_total_parent == 0:
                continue
            p_parent = pc1 / p_total_parent
            jsd = jensen_shannon_divergence(p_this, p_parent)
            if jsd < jensen_shannon_threshold:
                self._pruned.add(ctx)
                n_pruned += 1
        return n_pruned






def calc_advanced_ctw_pst_prior(
    binary_seq: List[str],
    max_depth: int = DEFAULT_MAX_DEPTH,
    smoothing: SmoothingMethod = "kneser_ney",
) -> Dict[str, Any]:
    """
    Wrapper tiện dùng — TRẢ VỀ CÙNG SHAPE {"pT","pX","best_order"} như
    calc_ctw_prior_v2() trong ctw_proper.py, để có thể cắm thay thế 1-dòng
    trong dynamic_execution_engine.py khi muốn A/B-test, KHÔNG cắm vào
    apex_sniper_engine.py hiện tại (xem cảnh báo đầu file).
    """
    n = len(binary_seq)
    if n < 3:
        return {"pT": 0.5, "pX": 0.5, "best_order": 0}
    model = CTW_PST_Model(max_depth=max_depth, smoothing=smoothing)
    pred = model.predict_proba(binary_seq)
    best_order = min(max_depth, n - 1)
    return {"pT": pred["pT"], "pX": pred["pX"], "best_order": best_order,
            "n_pruned": len(model._pruned)}






if __name__ == "__main__":
    import random
    from ctw_proper import calc_ctw_prior_v2

    print("=" * 90)
    print("TEST 1 — DỮ LIỆU NGẪU NHIÊN THUẦN (kiểm tra KHÔNG lệch hướng giả,")
    print("         giống bài học CTS bị lock-on bug trong diagnostic_models.py)")
    print("=" * 90)
    random.seed(42)
    for label, smoothing in (("KT (baseline, = ctw_proper.py)", "kt"),
                              ("Kneser-Ney (mới)", "kneser_ney")):
        deviations = []
        for trial in range(20):
            seq = [random.choice("TX") for _ in range(120)]
            n_extreme = 0
            for i in range(30, len(seq)):
                window = seq[:i]
                if smoothing == "kt":
                    pred = calc_ctw_prior_v2(window, max_depth=5)
                else:
                    pred = calc_advanced_ctw_pst_prior(window, max_depth=5, smoothing="kneser_ney")
                if abs(pred["pT"] - 0.5) > 0.15:
                    n_extreme += 1
            deviations.append(n_extreme / (len(seq) - 30))
        avg_dev = sum(deviations) / len(deviations)
        print(f"  {label:35s} tỷ lệ |pT-0.5|>0.15 trên random = {avg_dev*100:5.2f}% "
              f"(qua {len(deviations)} trial, mỗi trial 90 điểm)")
    print("  -> Nếu Kneser-Ney lệch RÕ RÀNG cao hơn KT ở đây, đó là dấu hiệu")
    print("     lock-on bug giống CTS — KHÔNG nên dùng cho tới khi sửa.\n")

    print("=" * 90)
    print("TEST 2 — DỮ LIỆU THẬT (340 phiên, 3 session) — so KT vs Kneser-Ney")
    print("=" * 90)
    try:
        from load_data import load_by_session, to_dice, to_side
        from stats_utils import fmt_result

        sessions = load_by_session()
        BURN_IN = 20

        def score(smoothing):
            w = n = 0
            for fn, rows in sessions:
                binary_seq = []
                for r in rows:
                    a = to_side(r["actual"])
                    if a is None:
                        continue
                    binary_seq.append(a)
                for i in range(BURN_IN, len(binary_seq)):
                    window = binary_seq[:i]
                    if smoothing == "kt":
                        pred = calc_ctw_prior_v2(window, max_depth=5)
                    else:
                        pred = calc_advanced_ctw_pst_prior(window, max_depth=5, smoothing=smoothing)
                    side = "T" if pred["pT"] >= pred["pX"] else "X"
                    n += 1
                    if side == binary_seq[i]:
                        w += 1
            return w, n

        w1, n1 = score("kt")
        print(fmt_result("CTW chuẩn (KT estimator, = ctw_proper.py hiện tại)", w1, n1))
        w2, n2 = score("kneser_ney")
        print(fmt_result("CTW + Kneser-Ney (mới, file này)", w2, n2))
        print("\n  -> So sánh 2 dòng trên: nếu Kneser-Ney KHÔNG vượt KT có ý nghĩa")
        print("     thống kê (p<0.05 VÀ cùng chiều cải thiện), KHÔNG có lý do bật")
        print("     ENABLE_ADVANCED_CTW_PST cho quyết định cược thật.")
    except Exception as e:
        print(f"  [Bỏ qua test trên data thật — thiếu file/lỗi: {e}]")

    print("\n=== Self-test PASSED (không crash) — xem số liệu phía trên trước khi tin) ===")
