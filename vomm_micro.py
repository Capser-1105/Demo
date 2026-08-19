



from typing import Optional, Dict, Any


def calculate_vomm(history_str: str, max_order: int = 5) -> Dict[str, Any]:
    n = len(history_str)
    if n < 3:
        return {"order": 0, "target": None, "prob": 0.0, "context": ""}

    top_k = min(max_order, n - 1)
    for k in range(top_k, 0, -1):
        context = history_str[-k:]
        counts = {"T": 0, "X": 0}
        match_found = False
        for i in range(0, n - k):
            if history_str[i:i + k] == context:
                nxt = history_str[i + k]
                if nxt in ("T", "X"):
                    counts[nxt] += 1
                    match_found = True
        if match_found:
            total = counts["T"] + counts["X"]
            pT = (counts["T"] + 0.5) / (total + 1)
            pX = (counts["X"] + 0.5) / (total + 1)
            if pT > pX:
                return {"order": k, "target": "T", "prob": pT, "context": context}
            if pX > pT:
                return {"order": k, "target": "X", "prob": pX, "context": context}
    return {"order": 0, "target": None, "prob": 0.5, "context": ""}


def analyze_micro(d1: int, d2: int, d3: int) -> Dict[str, Any]:
    dice = [d1, d2, d3]
    bottoms = sum(1 for d in dice if d <= 2)
    tops = sum(1 for d in dice if d >= 5)
    
    # Tư duy mới: Gia tốc (Momentum) - Đáy sinh Đáy, Đỉnh sinh Đỉnh
    if bottoms >= 2:
        return {"pressure": "X", "desc": "Gia Tốc Xỉu (Nén Đáy)"}
    if tops >= 2:
        return {"pressure": "T", "desc": "Gia Tốc Tài (Nén Đỉnh)"}
        
    return {"pressure": None, "desc": "Cân Bằng"}