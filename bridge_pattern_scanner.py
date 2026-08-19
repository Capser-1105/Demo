
from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple






def rle(seq: List[str]) -> List[Tuple[str, int]]:
    """['T','T','X','X','X','T'] -> [('T',2),('X',3),('T',1)]"""
    if not seq:
        return []
    out = []
    cur, n = seq[0], 1
    for x in seq[1:]:
        if x == cur:
            n += 1
        else:
            out.append((cur, n))
            cur, n = x, 1
    out.append((cur, n))
    return out






def detect_streak(seq: List[str]) -> Optional[Dict[str, Any]]:
    if not seq:
        return None
    runs = rle(seq)
    sym, length = runs[-1]
    if length >= 3:
        return {"name": "BET", "symbol": sym, "status": "CONFIRMED",
                "length": length, "predicted_next": sym,
                "note": f"Bet {sym} dang chay {length} ván liên tiếp"}
    if length == 2:
        return {"name": "BET", "symbol": sym, "status": "FORMING",
                "length": length, "predicted_next": sym,
                "note": f"Mới 2 ván {sym} liên tiếp — có thể đang hình thành bệt"}
    return None







def find_periodic_pattern(seq: List[str], max_period: int = 10) -> Optional[Dict[str, Any]]:
    """
    Tim period p (2..max_period, p=1 da xu ly rieng o detect_streak) nho
    nhat sao cho duoi cua seq tuan theo seq[-1-p]==seq[-1], seq[-2-p]==seq[-2],...
    cang xa cang tot. Uu tien p NHO NHAT tim duoc (don gian/"thuc" hon).

    [FIX] Voi period nho (p=2,3), chi can 2 chu ky (covered=2p ~ 4-6 phan tu)
    la qua de khop TINH CO ngay ca khi do la mot doan con cua mot motif LON
    hon (vd "TTXTXTTXTXTTXTX" period thuc=5, nhung 4 ky tu cuoi "TXTX" tinh
    co cung khop period=2). Them san toi thieu tuyet doi cho `covered` de
    tranh false-positive nay, khong chi dua vao ty le 2p.
    """
    n = len(seq)
    best = None
    for p in range(2, max_period + 1):
        if n < p + 1:
            continue
        L = 0
        i = n - 1
        while i - p >= 0 and seq[i] == seq[i - p]:
            L += 1
            i -= 1
        covered = L + p if L > 0 else 0
        confirm_floor = max(2 * p, 6)
        forming_floor = max(p + 2, 5)
        if covered >= confirm_floor:
            status = "CONFIRMED"
        elif covered >= forming_floor:
            status = "FORMING"
        else:
            continue
        block = seq[-p:]
        predicted_next = seq[n - p]
        cand = {
            "name": "PERIOD", "period": p, "status": status,
            "block": "".join(block), "covered": covered,
            "predicted_next": predicted_next,
            "note": f"Motif '{''.join(block)}' (chu kỳ {p}) đang lặp lại, đã thấy {covered} phiên khớp",
        }
        if best is None:
            best = cand
        elif best["status"] != "CONFIRMED" and status == "CONFIRMED":
            best = cand
        elif best["status"] == status and cand["covered"] > best["covered"]:
            best = cand
        elif best["status"] == status and cand["covered"] == best["covered"] and p < best["period"]:
            best = cand
    return best






_RAMP_TEMPLATES = {
    "1-2-3": [1, 2, 3],
    "3-2-1": [3, 2, 1],
    "1-2-1": [1, 2, 1],
    "2-1-2": [2, 1, 2],
}


def detect_ramp(seq: List[str]) -> Optional[Dict[str, Any]]:
    runs = rle(seq)
    lengths = [r[1] for r in runs]
    if len(lengths) < 2:
        return None
    last_sym = runs[-1][0]
    next_sym = "T" if last_sym == "X" else "X"
    for name, tmpl in _RAMP_TEMPLATES.items():
        k = len(tmpl)
        if len(lengths) >= k and lengths[-k:] == tmpl:
            evidence = sum(tmpl)
            return {"name": f"TANG_GIAM_{name}", "status": "CONFIRMED",
                    "predicted_next": next_sym,
                    "evidence": evidence,
                    "note": f"Khớp đúng mẫu tăng giảm {name} ở {k} đoạn gần nhất"}
        if len(lengths) >= k - 1 and lengths[-(k - 1):] == tmpl[:-1]:
            evidence = sum(tmpl[:-1])
            return {"name": f"TANG_GIAM_{name}", "status": "FORMING",
                    "predicted_next": next_sym,
                    "evidence": evidence,
                    "note": f"Đang khớp {k-1}/{k} đoạn đầu của mẫu {name} — theo dõi đoạn kế tiếp"}
    return None






def detect_bias(seq: List[str]) -> Optional[Dict[str, Any]]:
    best = None
    for w, min_ratio in ((5, 4 / 5), (7, 5 / 7), (10, 7 / 10)):
        if len(seq) < w:
            continue
        window = seq[-w:]
        t = window.count("T")
        ratio = max(t, w - t) / w
        if ratio >= min_ratio:
            side = "T" if t >= w - t else "X"
            cand = {"name": f"NGHIENG_{w}", "status": "CONFIRMED", "side": side,
                    "ratio": round(ratio, 2), "predicted_next": side,
                    "note": f"Nghiêng {side} {max(t,w-t)}/{w} phiên gần nhất"}
            if best is None or w < best.get("_w", 99):
                cand["_w"] = w
                best = cand
    if best:
        best.pop("_w", None)
    return best






def detect_break(seq: List[str]) -> Optional[Dict[str, Any]]:
    if len(seq) < 4:
        return None
    prev_scan = _scan_core(seq[:-1])
    prev_best = prev_scan.get("best")
    if not prev_best or prev_best.get("status") != "CONFIRMED":
        return None
    predicted = prev_best.get("predicted_next") or prev_best.get("side")
    actual = seq[-1]
    if predicted and predicted != actual:
        return {"name": f"GAY_{prev_best['name']}", "status": "EVENT",
                "note": f"Cầu '{prev_best['name']}' vừa bị bẻ ở phiên mới nhất "
                        f"(kỳ vọng {predicted}, thực tế {actual}) — giảm tin cậy, KHÔNG phải tín hiệu cược"}
    return None






_PRIORITY = ["BET", "PERIOD", "NGHIENG_5", "NGHIENG_7", "NGHIENG_10",
             "TANG_GIAM_1-2-3", "TANG_GIAM_3-2-1", "TANG_GIAM_1-2-1", "TANG_GIAM_2-1-2"]


def _evidence(c: Dict[str, Any]) -> int:
    """Luong 'bang chung' (so phan tu duoc giai thich) cua 1 candidate — dung
    de so sanh BET vs PERIOD vs TANG_GIAM cong bang, thay vi uu tien co dinh theo ten."""
    if c["name"] == "BET":
        return c["length"]
    if c["name"] == "PERIOD":
        return c["covered"]
    if c["name"].startswith("TANG_GIAM_"):
        return c.get("evidence", 0)
    return 0


def _scan_core(seq: List[str], max_period: int = 10) -> Dict[str, Any]:
    candidates = []
    for fn in (detect_streak, detect_trap_breakout, lambda s: find_periodic_pattern(s, max_period), detect_ramp, detect_bias):
        r = fn(seq)
        if r:
            candidates.append(r)

    confirmed = [c for c in candidates if c["status"] == "CONFIRMED"]
    forming = [c for c in candidates if c["status"] == "FORMING"]

    

    periodic_names = {"BET", "PERIOD", "TANG_GIAM_1-2-1", "TANG_GIAM_2-1-2"}

    def rank(c):
        if c["name"] in periodic_names:

            if c["name"].startswith("TANG_GIAM_") and c["status"] == "CONFIRMED":
                return (0, -_evidence(c), 0)
            return (0, -_evidence(c), 1)
        try:
            return (1, _PRIORITY.index(c["name"]))
        except ValueError:
            return (2, 0)

    best = None
    if confirmed:
        best = sorted(confirmed, key=rank)[0]
    elif forming:
        best = sorted(forming, key=rank)[0]

    return {"all": candidates, "best": best}

def detect_trap_breakout(seq: List[str]) -> Optional[Dict[str, Any]]:
    """Phát hiện bẫy gãy cầu khuôn (1-1, 2-2) ngay tại thời điểm hiện tại"""
    if len(seq) < 6: return None
    recent = "".join(seq[-6:])
    
    # Bẫy gãy 1-1 (Ví dụ: Đang TXTXT tự nhiên lòi ra TT)
    if "TXTXTT" in recent or "XTXTXX" in recent:
        return {"name": "TRAP_1_1", "status": "EVENT", "note": "Bẫy gãy nhịp 1-1! Nhà cái đang diệt người đu ping-pong."}
        
    # Bẫy gãy 2-2 (Ví dụ: Đang TTXXTT tự nhiên ra TTXXT X -> gãy sang 3-2 hoặc 1-1)
    if "TTXXTTX" in "".join(seq[-7:]) or "XXTTXXT" in "".join(seq[-7:]):
        return {"name": "TRAP_2_2", "status": "EVENT", "note": "Bẫy gãy khuôn 2-2!"}
        
    return None

def scan_patterns(seq: List[str], max_period: int = 10, max_lookback: int = 21) -> Dict[str, Any]:
    """
    Entry point chinh. seq: list ky tu 'T'/'X', phan tu cuoi = moi nhat.
    Chi quet trong toi da `max_lookback` phien gan nhat (theo dung yeu cau:
    luon quet tu phien moi nhat lui ve, gioi han 21 phien).
    """
    window = seq[-max_lookback:] if len(seq) > max_lookback else seq
    core = _scan_core(window, max_period=max_period)
    brk = detect_break(window)

    return {
        "window_used": len(window),
        "all_patterns": core["all"],
        "best": core["best"],
        "break_event": brk,
    }
