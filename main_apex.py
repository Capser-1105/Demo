
import atexit
import traceback
import json, time, base64, struct, os, threading, re, importlib
from datetime import datetime
from typing import Optional
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from rich.console import Console, Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.text import Text
from rich import box
from apex_sniper_logic import ApexSniperLogicEngine as LogicEngine, HIST_MIN





from timer_manager import get_timer, reset_timer, SESSION_TOTAL_S





from collections import defaultdict
class ReportGenerator:
    """Tạo báo cáo tổng kết khi bot dừng"""
    @staticmethod
    def generate(logic: LogicEngine, csv_writer, mode: str):
        br_stats = logic.bankroll.stats()
        insight = logic.get_insight()
        
        report = {
            "mode": mode,
            "duration": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "initial_balance": br_stats.get("initial_seed"),
            "final_balance": br_stats.get("total_balance"),
            "total_profit": br_stats.get("cumulative_profit"),
            "total_roi": br_stats.get("cumulative_pct"),
            "total_bets": insight.get("total_bets",0),
            "total_skip": insight.get("total_skip",0),
            "win_rate": br_stats.get("cumulative_wr"),
            "kpi_hits": br_stats.get("kpi_hits"),
            "locked_profit": br_stats.get("locked_profit"),
            "max_drawdown": br_stats.get("cumulative_max_dd"),
            "tier_performance": br_stats.get("tier_info", {}),
            "hourly_breakdown": {},
            "regime_breakdown": {},
        }
        

        try:
            import csv
            hourly = defaultdict(lambda: {"wins":0, "losses":0})
            regime_stats = defaultdict(lambda: {"wins":0, "losses":0})
            with open(csv_writer.f.name, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row["result"] == "WIN":
                        hour = int(row["time"].split(":")[0])
                        hourly[hour]["wins"] += 1
                        regime_stats[row["regime"]]["wins"] += 1
                    elif row["result"] == "LOSS":
                        hour = int(row["time"].split(":")[0])
                        hourly[hour]["losses"] += 1
                        regime_stats[row["regime"]]["losses"] += 1
            for hour, data in hourly.items():
                total = data["wins"]+data["losses"]
                wr = data["wins"]/total*100 if total else 0
                report["hourly_breakdown"][hour] = {"wins":data["wins"], "losses":data["losses"], "wr": round(wr,1)}
            for regime, data in regime_stats.items():
                total = data["wins"]+data["losses"]
                wr = data["wins"]/total*100 if total else 0
                report["regime_breakdown"][regime] = {"wins":data["wins"], "losses":data["losses"], "wr": round(wr,1)}
        except Exception as e:
            console.print(f"[yellow]Không thể đọc CSV để thống kê: {e}[/]")
        
        report_file = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        console.print(f"\n[bold cyan]📊 Báo cáo đã lưu: {report_file}[/]")
        return report
console = Console()















class _DisabledDiscordNotifier:
    def __getattr__(self, _name):
        def _noop(*args, **kwargs):
            return None
        return _noop

def get_discord_notifier() -> "_DisabledDiscordNotifier":
    return _DisabledDiscordNotifier()



CONFIG = {
    "TARGET_GAME": "TX_THUONG",
    "URL":       "https://v.hitclub.voting/?a=d4fb11ca555768b08d24038129a9d40c&referrer_domain=hitclubmoi.com",
    "URL_FALLBACKS": [
        "https://web.sun.win/",
        "https://web.sunwin.jobs/?affId=Sunwin",
        "https://www.google.com",
    ],
    "WIN_TITLE": "SUNWIN",
    "WIN_TITLES": ["SUNWIN", "Sunwin", "Sun Win", "Tài Xỉu", "Chrome", "Edge"],
    "COORDS_TX_THUONG": {
        "TAI":     (575, 710),
        "XIU":     (1050, 710),
        "BET_BTN": (792, 963),
        "CHIPS": {
            100_000: (757, 877),
            50_000:  (657, 877),
            10_000:  (556, 876),
            1_000:   (456, 877),
        },
    },
    "COORDS_TX_MD5": {
        "TAI":     (0, 0),
        "XIU":     (0, 0),
        "BET_BTN": (0, 0),
        "CHIPS": {
            100_000: (0, 0),
            50_000:  (0, 0),
            10_000:  (0, 0),
            1_000:   (0, 0),
        },
    },
    "FOCUS_DELAY":  0.08,
    "CLICK_DELAY":  0.01,
    "USE_SCHEDULE": False,


    "USE_CSV_WARMUP": False,


    "CONF_THRESHOLDS": [55, 60, 65],

    "EXPLORATION_RATE": 0.05,

    "EXPLORATION_BET_PCT": 0.25,
}
CONFIG["COORDS"] = (
    CONFIG["COORDS_TX_THUONG"]
    if CONFIG.get("TARGET_GAME") == "TX_THUONG"
    else CONFIG["COORDS_TX_MD5"]
)

SESSION_TOTAL_S = 50
KILL_FILE       = "KILL_SIGNAL"



BET_WINDOW_MAX  = 50
BET_WINDOW_MIN  = 7






EXECUTION_START_S = 15
EXECUTION_END_S = 7


TRADING_SCHEDULE = [
    (0, 0, 24, 0, "00–24h", 75),
]

def in_trading_hours():
    now = datetime.now()
    h, m = now.hour, now.minute
    total_min = h * 60 + m
    for sh, sm, eh, em, label, wr in TRADING_SCHEDULE:
        start = sh * 60 + sm
        end   = (eh * 60 + em) or 24 * 60
        if start <= total_min < end:
            return True, label, wr, end - total_min
    next_label, wait_min = "", 999
    for sh, sm, eh, em, label, wr in TRADING_SCHEDULE:
        start = sh * 60 + sm
        diff  = start - total_min if start > total_min else (24 * 60 - total_min) + start
        if diff < wait_min:
            wait_min, next_label = diff, label
    return False, next_label, 0, wait_min


_pyautogui    = None
_pygetwindow  = None

def get_pyautogui():
    global _pyautogui
    if _pyautogui is None:
        try:
            _pyautogui = importlib.import_module("pyautogui")
        except Exception:
            _pyautogui = None
    return _pyautogui

def get_pygetwindow():
    global _pygetwindow
    if _pygetwindow is None:
        try:
            _pygetwindow = importlib.import_module("pygetwindow")
        except Exception:
            _pygetwindow = None
    return _pygetwindow


class MsgPackDecoder:
    def __init__(self, data: bytes):
        self.data   = memoryview(data)
        self.offset = 0

    def unpack(self):
        if self.offset >= len(self.data):
            return None
        b = self.data[self.offset]
        self.offset += 1
        if b <= 0x7f: return b
        if 0x80 <= b <= 0x8f:
            return {self.unpack(): self.unpack() for _ in range(b & 0x0f)}
        if 0x90 <= b <= 0x9f:
            return [self.unpack() for _ in range(b & 0x0f)]
        if 0xa0 <= b <= 0xbf:
            n = b & 0x1f; s = self.data[self.offset:self.offset + n]; self.offset += n
            return s.tobytes().decode("utf-8", "ignore")
        if b == 0xc0: return None
        if b == 0xc2: return False
        if b == 0xc3: return True
        if b >= 0xe0: return struct.unpack(">b", bytes([b]))[0]
        if b == 0xca:
            v = struct.unpack(">f", self.data[self.offset:self.offset + 4])[0]; self.offset += 4; return v
        if b == 0xcb:
            v = struct.unpack(">d", self.data[self.offset:self.offset + 8])[0]; self.offset += 8; return v
        if b in (0xc4, 0xc5, 0xc6):
            sz = 2 ** (b - 0xc4); nl = 1 if sz == 1 else 2 if sz == 2 else 4
            n  = int.from_bytes(self.data[self.offset:self.offset + nl], "big"); self.offset += nl
            s  = self.data[self.offset:self.offset + n]; self.offset += n; return s.tobytes()
        if b in (0xcc, 0xcd, 0xce, 0xcf):
            sz = 2 ** (b - 0xcc); n = int.from_bytes(self.data[self.offset:self.offset + sz], "big")
            self.offset += sz; return n
        if b in (0xd0, 0xd1, 0xd2, 0xd3):
            sz  = 2 ** (b - 0xd0)
            fmt = ">b" if sz == 1 else ">h" if sz == 2 else ">i" if sz == 4 else ">q"
            n   = struct.unpack(fmt, self.data[self.offset:self.offset + sz])[0]; self.offset += sz; return n
        if b in (0xd9, 0xda, 0xdb):
            sz = 2 ** (b - 0xd9); nl = 1 if sz == 1 else 2 if sz == 2 else 4
            n  = int.from_bytes(self.data[self.offset:self.offset + nl], "big"); self.offset += nl
            s  = self.data[self.offset:self.offset + n]; self.offset += n
            return s.tobytes().decode("utf-8", "ignore")
        if b in (0xdc, 0xdd):
            sz = 2 if b == 0xdc else 4
            n  = int.from_bytes(self.data[self.offset:self.offset + sz], "big")
            self.offset += sz; return [self.unpack() for _ in range(n)]
        if b in (0xde, 0xdf):
            sz = 2 if b == 0xde else 4
            n  = int.from_bytes(self.data[self.offset:self.offset + sz], "big")
            self.offset += sz; return {self.unpack(): self.unpack() for _ in range(n)}
        return None


def decode_payload(raw_string: str):
    s = (raw_string or "").strip()
    if s.startswith("[5,"):
        try:
            import json
            parsed = json.loads(s)
            if isinstance(parsed, list) and len(parsed) >= 2:
                return parsed[1]
        except Exception:
            pass
    if "{" in s:
        try:
            import json
            start_idx = s.index("{")
            end_idx = s.rindex("}") + 1
            return json.loads(s[start_idx:end_idx])
        except:
            pass
    return None

def _to_num(v) -> int:
    try:
        if v is None: return 0
        if isinstance(v, (int, float)): return int(v)
        s = str(v).replace(",", "").replace(" ", "")
        m = re.search(r"-?\d+(\.\d+)?", s)
        return int(float(m.group(0))) if m else 0
    except Exception:
        return 0

def _extract_taixiu_from_1008(data: dict):
    def _to_num(val):
        if val is None: return 0.0
        s = re.sub(r'[^\d.]', '', str(val))
        try: return float(s) if s else 0.0
        except: return 0.0

    # NẾU LÀ DATA CỦA BÀN MD5 (đã bóc tách bs array)
    if "bs" in data:
        tai, xiu = 0.0, 0.0
        for item in data["bs"]:
            if item.get("eid") == 1: tai = float(item.get("v", 0))
            if item.get("eid") == 2: xiu = float(item.get("v", 0))
        return tai, xiu, False

    # NẾU LÀ DATA CỦA TÀI XỈU THƯỜNG
    gi = data.get("gi")
    vol_keys = ["t", "tB", "tb", "sum", "total", "totalBet", "v"]
    tai, xiu = 0.0, 0.0
    
    if isinstance(gi, list) and len(gi) > 0:
        obj = gi[0]
        B = obj.get("B") or obj.get("b") or obj.get("T") or obj.get("t") or {}
        S = obj.get("S") or obj.get("s") or obj.get("X") or obj.get("x") or {}
        def pick(d):
            if isinstance(d, (int, float)): return float(d)
            if not isinstance(d, dict): return 0.0
            for k in vol_keys:
                if k in d: return _to_num(d[k])
            return 0.0
        tai, xiu = pick(B), pick(S)
    
    if tai == 0 and xiu == 0:
        tai = _to_num(data.get("tai") or data.get("T") or data.get("B"))
        xiu = _to_num(data.get("xiu") or data.get("X") or data.get("S"))
    return float(tai), float(xiu), False

def _extract_ud_from_1008(data: dict):
    if "bs" in data:
        tai_u, xiu_u = 0, 0
        for item in data["bs"]:
            if not isinstance(item, dict):
                continue
            if item.get("eid") == 1: tai_u = _to_num(item.get("bc", 0))
            if item.get("eid") == 2: xiu_u = _to_num(item.get("bc", 0))
        return tai_u, xiu_u

    gi = data.get("gi")
    groups = gi if isinstance(gi, list) else [gi]
    groups.append(data)

    def pick_users(side):
        if not isinstance(side, dict):
            return 0
        for key in ("tU", "uT", "tu", "ut", "users", "userCount", "totalUser"):
            if key in side:
                return max(0, _to_num(side.get(key)))
        return 0

    for group in groups:
        if not isinstance(group, dict):
            continue
        tai_side = group.get("B") or group.get("T") or group.get("b") or group.get("t") or {}
        xiu_side = group.get("S") or group.get("X") or group.get("s") or group.get("x") or {}
        tai_u, xiu_u = pick_users(tai_side), pick_users(xiu_side)
        if tai_u + xiu_u > 0:
            return tai_u, xiu_u

    return (
        max(0, _to_num(data.get("tai_u") or data.get("taiUsers") or data.get("tU"))),
        max(0, _to_num(data.get("xiu_u") or data.get("xiuUsers") or data.get("xU"))),
    )

def _extract_result_from_1003(data: dict):
    """Extract kết quả phiên từ CMD 1003 payload."""
    if not data:
        return 0, 0, 0, 0, 0, "XIU"
    
    try:
        sid   = int(data.get("sid") or data.get("sID") or 0)
        

        d1 = _to_num(data.get("d1") or data.get("a") or data.get("dice1") or 0)
        d2 = _to_num(data.get("d2") or data.get("b") or data.get("dice2") or 0)
        d3 = _to_num(data.get("d3") or data.get("c") or data.get("dice3") or 0)
        

        total = _to_num(data.get("total") or data.get("t") or data.get("sum") or 0)
        if total <= 0:
            total = d1 + d2 + d3
        

        if d1 == 0 and d2 == 0 and d3 == 0:
            dices = data.get("dices") or data.get("dice") or data.get("d") or []
            if isinstance(dices, list) and len(dices) >= 3:
                d1 = _to_num(dices[0])
                d2 = _to_num(dices[1])
                d3 = _to_num(dices[2])
                total = d1 + d2 + d3
        
        result = "TAI" if total >= 11 else "XIU"

        console.print(f"[cyan]📊 Extract 1003: sid={sid}, d1-d2-d3={d1}-{d2}-{d3}, total={total}, result={result}[/]")
        return sid, d1, d2, d3, total, result
    except Exception as e:
        console.print(f"[red]❌ Error extracting result from 1003: {e}[/]")
        return 0, 0, 0, 0, 0, "XIU"

def _normalize_htr(htr):
    """Chuẩn hóa dữ liệu history từ các định dạng khác nhau."""
    seq = []
    

    if isinstance(htr, str):
        try:
            for ch in (htr or "").strip():
                if ch.upper() in ("T", "X"):
                    seq.append("TAI" if ch.upper() == "T" else "XIU")
        except Exception as e:
            console.print(f"[red]Lỗi parse string htr: {e}[/]")
        return seq


    if isinstance(htr, list):
        tmp = []
        for idx, item in enumerate(htr):
            try:
                if isinstance(item, dict):

                    r = item.get("result") or item.get("r") or item.get("s") or ""
                    if not r and "d1" in item and "d2" in item and "d3" in item:

                        d1 = _to_num(item.get("d1") or item.get("a") or 0)
                        d2 = _to_num(item.get("d2") or item.get("b") or 0)
                        d3 = _to_num(item.get("d3") or item.get("c") or 0)
                        r = "TAI" if (d1 + d2 + d3) >= 11 else "XIU"
                    
                    if r:
                        sid = int(item.get("sid") or 0)
                        dice = str(item.get("dice", "?"))
                        total = int(item.get("total") or 0)
                        
                        result_val = "TAI" if "T" in str(r).upper() else "XIU"
                        tmp.append({"result": result_val, "sid": sid, "dice": dice, "total": total})
                
                elif isinstance(item, str):

                    if "T" in item.upper():
                        tmp.append({"result": "TAI", "sid": 0, "dice": "?", "total": 0})
                    elif "X" in item.upper():
                        tmp.append({"result": "XIU", "sid": 0, "dice": "?", "total": 0})
                
                elif isinstance(item, (list, tuple)) and len(item) >= 3:

                    d1 = _to_num(item[0])
                    d2 = _to_num(item[1])
                    d3 = _to_num(item[2])
                    sid = _to_num(item[3]) if len(item) > 3 else 0
                    total = d1 + d2 + d3
                    tmp.append({"result": "TAI" if total >= 11 else "XIU", "sid": sid, "dice": f"{d1}-{d2}-{d3}", "total": total})
            except Exception as e:
                console.print(f"[yellow]⚠️ Lỗi parse item {idx}: {e}[/]")
                continue
        
        return tmp


    try:
        s = str(htr or "").strip()
        for ch in s:
            if ch.upper() in ("T", "X"):
                seq.append("TAI" if ch.upper() == "T" else "XIU")
    except Exception:
        pass

    return seq

def _find_htr(obj, depth=0):
    if depth > 5 or not isinstance(obj, (dict, list)):
        return None
    if isinstance(obj, dict):
        for k in ("htr", "hist", "H", "history"):
            if k in obj:
                return obj[k]
        for v in obj.values():
            r = _find_htr(v, depth + 1)
            if r:
                return r
    if isinstance(obj, list):
        for item in obj:
            r = _find_htr(item, depth + 1)
            if r:
                return r
    return None

def _ingest_history(logic: LogicEngine, htr, ui, label: str = ""):
    """Debug history ingestion."""

    console.print(f"[yellow]DEBUG: Nhận htr payload có độ dài {len(str(htr))}[/]")
    
    if not htr:
        console.print("[red]❌ Lỗi: Payload htr trống hoặc None![/]")
        return 0

    try:
        raw_items = _normalize_htr(htr)
    except Exception as e:
        console.print(f"[red]❌ Lỗi normalize htr: {e}[/]")
        return 0

    if not raw_items:
        return 0


    items = []
    try:
        if raw_items and isinstance(raw_items[0], dict):
            items = raw_items
        else:
            for r in raw_items:
                if r in ("TAI", "XIU"):
                    items.append({"result": r, "dice": "?", "total": 0, "sid": 0})
    except Exception as e:
        console.print(f"[red]❌ Lỗi chuẩn hóa items: {e}[/]")
        return 0

    if not items:
        return 0


    try:
        logic._load_history_items(items)
    except Exception as e:
        console.print(f"[red]❌ Lỗi load items into logic: {e}[/]")
        return 0


    try:
        base = list(ui.htr_history or []) if ui.htr_history else []
        
        all_sid_zero = all(int(it.get("sid") or 0) == 0 for it in items)
        if all_sid_zero:

            base.extend(items)
        else:

            seen = {(int(it.get("sid") or 0), it.get("dice"), it.get("total")) for it in base}
            for it in items:
                key = (int(it.get("sid") or 0), it.get("dice"), it.get("total"))
                if key not in seen:
                    base.append(it)
                    seen.add(key)


        if any(int(it.get("sid") or 0) > 0 for it in base):
            base.sort(key=lambda x: int(x.get("sid") or 0))


        base = base[-100:]
        

        ui.set_htr_history(base)


        if label:
            ui.set_status(f"✅ Nạp lịch sử ({len(base)}) — {label}")

        loaded = len(base)
        

        if loaded >= 100:
            logic._history_loaded_flag = True
            logic._insight_cache["history_loaded"] = True
        elif loaded >= HIST_MIN:
            logic._history_loaded_flag = True
            logic._insight_cache["history_loaded"] = True
        else:
            logic._history_loaded_flag = False
            logic._insight_cache["history_loaded"] = False

        return loaded

    except Exception as e:
        console.print(f"[red]❌ Lỗi merge history vào UI: {e}[/]")
        return 0


class AutoBetEngine:
    def __init__(self):
        self.last_result = ""
        self._bet_pending = False

    def _win(self):
        pgw = get_pygetwindow()
        if pgw is None:
            return None
        titles = CONFIG["WIN_TITLES"]
        for t in titles:
            wins = pgw.getWindowsWithTitle(t)
            if wins:
                return wins[0]
        return None

    def _chip_plan(self, amount: int) -> list:
        chips  = sorted(CONFIG["COORDS"]["CHIPS"].keys(), reverse=True)
        plan   = []
        remain = amount
        for c in chips:
            if remain <= 0:
                break
            n = remain // c
            if n > 0:
                plan.append((c, n, CONFIG["COORDS"]["CHIPS"][c]))
                remain -= c * n
        return plan

    def place_bet(self, side: str, amount: int) -> bool:
        if self._bet_pending:
            self.last_result = "Bet already pending - skipping to prevent double-click"
            return False

        pag = get_pyautogui()
        pgw = get_pygetwindow()
        if pag is None or pgw is None:
            self.last_result = "No pyautogui/pygetwindow"
            return False
        win = self._win()
        if win is None:
            self.last_result = "Window not found"
            return False
        try:
            if hasattr(win, "restore") and getattr(win, "isMinimized", False):
                win.restore()
                time.sleep(CONFIG["FOCUS_DELAY"] * 2)
            if hasattr(win, "maximize"):
                try:
                    win.maximize()
                    time.sleep(CONFIG["FOCUS_DELAY"] * 3)
                except Exception:
                    pass
            win.activate()
            time.sleep(CONFIG["FOCUS_DELAY"] * 3)
        except Exception:
            pass

        self._bet_pending = True
        try:
            coord = CONFIG["COORDS"][side]
            pag.moveTo(*coord, duration=0.05)
            pag.click()
            time.sleep(CONFIG["CLICK_DELAY"])
            plan = self._chip_plan(amount)
            for chip_val, count, chip_coord in plan:
                for _ in range(count):
                    pag.moveTo(*chip_coord, duration=0.03)
                    pag.click()
                    time.sleep(CONFIG["CLICK_DELAY"])
            pag.moveTo(*CONFIG["COORDS"]["BET_BTN"], duration=0.05)
            pag.click()
            time.sleep(CONFIG["CLICK_DELAY"])
            time.sleep(0.1)
            self.last_result = "OK"
            return True
        except Exception as e:
            self.last_result = str(e)
            return False
        finally:
            self._bet_pending = False


def _bar(pct: float, width: int = 20, fc: str = "green", ec: str = "grey23") -> str:
    filled = max(0, min(width, int(pct / 100 * width)))
    return f"[{fc}]{'█' * filled}[/][dim]{'░' * (width - filled)}[/]"

def _tier_style(tier: str) -> str:
    return {
        "SNIPER_MAX":  "bold bright_red",
        "SNIPER_HIGH": "bold bright_magenta",
        "SNIPER":      "bold bright_yellow",
        "SAFE":        "bold cyan",
        "NORMAL":      "bold white",
    }.get(tier, "bold white")

def _wr_col(wr: float) -> str:
    if wr >= 65: return "bold bright_green"
    if wr >= 55: return "green"
    if wr >= 48: return "yellow"
    return "red"

def _conf_col(conf: int) -> str:
    if conf >= 85: return "bold bright_green"
    if conf >= 75: return "green"
    if conf >= 68: return "yellow"
    return "dim"

def _streak_char(side: str, cnt: int) -> str:
    if cnt <= 0: return "—"
    ch = "T" if side == "TAI" else "X"
    return f"{cnt}{ch}"

def _regime_style(regime: str) -> str:
    return {
        "TREND":   "bold bright_green",
        "MIXED":   "bold cyan",
        "CHOP":    "bold yellow",
        "CHAOTIC": "bold red",
    }.get(regime, "white")

def _sq_icon(sq: str) -> str:
    return {"GOLDEN": "⭐", "GOOD": "🟢", "BAD": "🔴"}.get(sq, "⚪")


def _format_vnd_compact(value: float, already_million: bool = False) -> str:
    try:
        amount = float(value or 0)
        if already_million:
            amount *= 1_000_000
    except (TypeError, ValueError):
        amount = 0.0
    if amount >= 1_000_000_000:
        return f"{amount / 1_000_000_000:.2f}B"
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"{amount / 1_000:.0f}K"
    return f"{amount:.0f}d"

class Dashboard:
    def __init__(self, mode: str, logic: LogicEngine, beteng: AutoBetEngine):
        self.mode     = mode
        self.logic    = logic
        self.beteng   = beteng
        self.market   = {"sid": 0, "tai": 0, "xiu": 0, "tai_u": 0, "xiu_u": 0, "rmt": 0.0}
        self.status   = "🟢 Đăng nhập và mở bàn Tài Xỉu..."
        self.history: list = []
        self.htr_history: list = []
        self.auto_log: list = []
        self.bet_executed = False
        self.bet_in_window = False
        self.is_rest      = False

    def update_market(self, sid, tai, xiu, rmt, tai_u=0, xiu_u=0):
        self.market = {"sid": sid, "tai": tai, "xiu": xiu, "tai_u": tai_u, "xiu_u": xiu_u,
                       "rmt": rmt, "rmt_at": time.monotonic()}

        timer_mgr = get_timer()

        rmt_ms = int(rmt * 1000) if rmt < 100 else int(rmt)
        timer_mgr.update_market_rmt(sid, rmt_ms)

    def set_status(self, s: str):
        self.status = s

    def add_history(self, row: dict):
        """Thêm một dòng history vào bảng."""
        if not row:
            return
        try:
            self.history.insert(0, row)
            self.history = self.history[:25]
        except Exception as e:
            console.print(f"[red]❌ Error adding history: {e}[/]")

    def add_auto_log(self, s: str):
        self.auto_log.insert(0, s)
        self.auto_log = self.auto_log[:5]

    def set_htr_history(self, items: list):
        """Đặt history từ ingest."""
        if items is None:
            items = []
        self.htr_history = list(items[-100:])

    def append_htr(self, result, dice, total, sid):
        """Thêm một item vào history."""
        if not self.htr_history:
            self.htr_history = []
        self.htr_history.append({"result": result, "dice": dice,
                                  "total": total, "sid": sid})
        if len(self.htr_history) > 100:
            self.htr_history.pop(0)

    def get_pattern_from_htr(self, n: int = 21) -> str:
        """Generate pattern từ htr_history để đảm bảo sync với LỊCH SỬ SÀN."""
        if not self.htr_history:
            return ""
        return "".join(["T" if item.get("result") == "TAI" else "X" 
                         for item in self.htr_history[-n:]])


    def generate(self) -> Layout:
        ins      = self.logic.get_insight()
        br       = self.logic.bankroll.stats()
        kpi_stop = br.get("kpi_stop_signal", False)


        if br.get("emergency"):
            hdr_bg, border_main = "dark_red",    "red"
        elif kpi_stop:
            hdr_bg, border_main = "dark_green",  "bright_green"
        elif br.get("ghost_mode") or br.get("cooldown"):
            hdr_bg, border_main = "dark_orange3", "yellow"
        else:
            hdr_bg, border_main = "dark_blue",   "cyan"


        layout = Layout()
        layout.split(
            Layout(name="header",  size=3),
            Layout(name="row1",    size=17),
            Layout(name="signal",  size=4),
            Layout(name="table",   ratio=3),
            Layout(name="htr",     size=5),
            Layout(name="footer",  size=3),
        )
        layout["row1"].split_row(
            Layout(name="radar",    ratio=4),
            Layout(name="analysis", ratio=4),
            Layout(name="wallet",   ratio=3),
        )




        mode_tag   = "🤖 AUTO" if self.mode == "AUTO" else "👤 MANUAL"
        sid_tag    = f"SID #{self.market['sid']}"
        ts         = datetime.now().strftime("%H:%M:%S")
        total_bets = ins.get("total_bets", 0)
        total_skip = ins.get("total_skip", 0)
        skip_ratio = ins.get("skip_ratio", 0.0)
        try:
            skip_ratio = float(skip_ratio)
        except (TypeError, ValueError):
            skip_ratio = 0.0
        skip_ratio_text = f" ({skip_ratio:.0%})" if skip_ratio >= 0 else ""
        hist_size  = ins.get("hist_size", 0)
        cum_w      = br.get("cumulative_wins", 0)
        cum_l      = br.get("cumulative_losses", 0)
        wr_live    = round(cum_w / max(cum_w + cum_l, 1) * 100, 1)
        skip_str   = ins.get("skip_streak", 0)

        if br.get("emergency"):
            state_tag = f" ⛔ DỪNG: {br.get('stop_reason','')[:40]}"
        elif kpi_stop:
            state_tag = f" 🔒 KPI #{br.get('kpi_hits',0)} +{br.get('locked_profit',0):,.0f}đ đã khóa"
        elif br.get("ghost_mode"):
            state_tag = f" 👻 GHOST ({br.get('loss_streak',0)} thua liên)"
        elif br.get("cooldown"):
            m   = br.get("cooldown_rem", 0) // 60
            s2  = br.get("cooldown_rem", 0) % 60
            state_tag = f" ❄️ COOLDOWN {m:02d}:{s2:02d}"
        else:
            now_h = datetime.now().hour
            hour_warn = f" ⚠️H{now_h}?" if now_h in {3,4,6,21} else f" ✅H{now_h}"
            state_tag = (f" 🟢 V5.3 SC>=2  {hist_size}hist "
                         f"{total_bets}bet/{total_bets+total_skip}ph "
                         f"WR:{wr_live:.0f}%  skip:{skip_str}{skip_ratio_text}{hour_warn}")

        layout["header"].update(Panel(Text(
            f" {mode_tag} | {state_tag} | {sid_tag} | {ts} ",
            justify="center", style=f"bold white on {hdr_bg}"), padding=0))




        tai    = self.market["tai"]
        xiu    = self.market["xiu"]
        tot    = max(tai + xiu, 1)
        t_pct  = tai / tot * 100
        x_pct  = 100 - t_pct
        gap    = ins.get("gap", 0.0)
        regime = ins.get("regime", "MIXED")
        sq     = ins.get("session_quality", "GOOD")


        timer_mgr = get_timer()
        rmt, rmt_col, timer_b = timer_mgr.get_timer_display()
        vol_b   = _bar(t_pct, 26, "green", "red")

        st_side = ins.get("streak_side")
        st_cnt  = ins.get("streak_cnt", 0)
        st_char = _streak_char(st_side, st_cnt)
        mkt_s   = ins.get("market_status", "?")
        blk_s   = ins.get("blocks", "")
        pp      = ins.get("pingpong", 0)
        rg_s    = _regime_style(regime)
        sq_ic   = _sq_icon(sq)

        layout["radar"].update(Panel(Group(
            Text.from_markup(f" ⏱  {timer_b} [{rmt_col}]{rmt:.0f}s[/]"),
            Text(""),
            Text.from_markup(f" 🟢 TÀI [{_wr_col(t_pct)}]{t_pct:5.1f}%[/]  {tai:>14,}đ"),
            Text.from_markup(f" 🔴 XỈU [{_wr_col(x_pct)}]{x_pct:5.1f}%[/]  {xiu:>14,}đ"),
            Text.from_markup(f"  {vol_b}"),
            Text(""),
            Text.from_markup(
                f" 💰 Vol:[white]{_format_vnd_compact(ins.get('total_vol_m', 0), already_million=True):>7s}[/]  "
                f"GAP:[yellow]{gap:.1f}%[/]  "
                f"{'✅Stable' if ins.get('vol_stable') else '⚠️Flip' if ins.get('vol_flipped') else '—'}"
            ),
            Text.from_markup(
                f" [{rg_s}]◈ {regime}[/]  {sq_ic}[dim]{sq}[/]  "
                f"Phase:[dim]{ins.get('phase', '—')}[/]"
            ),
            Text(""),
            Text.from_markup(
                f" 🎯 Cầu:[bold yellow]{st_char:>4s}[/]  "
                f"PP:[cyan]{pp}[/]  "
                f"Blk:[dim]{blk_s or '—'}[/]"
            ),
            Text.from_markup(
                f" 📈 21ph:TÀI={ins.get('tai_21', 0):.0f}%  "
                f"100ph:TÀI={ins.get('tai_100', 0):.0f}%  "
                f"[dim]{ins.get('long_bias', '')}[/]"
            ),
        ), title="📡 RADAR  •  🎯 V5.3: SC>=2 ONLY | SC1 BLOCKED", border_style="cyan"))




        hist_ok = ins.get("history_loaded", False)
        bet     = ins.get("current_bet")
        skips   = ins.get("skip_reasons", [])

        pm      = self.get_pattern_from_htr(21)
        pm_markup = "".join(
            "[bold green]T[/]" if c == "T" else "[bold red]X[/]" for c in pm
        )

        if kpi_stop:
            ana      = Group(
                Text(""),
                Text(f" 🎯 KPI ĐẠT! +{br.get('profit', 0):,.0f}đ",
                     style="bold bright_green", justify="center"),
                Text(" Hệ thống tiếp tục chu kỳ mới.", style="bright_green", justify="center"),
            )
            a_border = "bright_green"
        elif not hist_ok:
            ana      = Group(
                Text(""),
                Text(f" ⏳ Nạp lịch sử... ({self.logic.hist.size()}/5)",
                     style="yellow", justify="center"),
            )
            a_border = "yellow"
        else:

            sc_cnt  = ins.get("streak_cnt", 0)
            sc_side = ins.get("streak_side", "")
            gap_pct = ins.get("gap", 0.0)
            vol_m   = ins.get("total_vol_m", 0.0)
            regime  = ins.get("regime", "UNKNOWN")
            sq      = ins.get("session_quality", "UNKNOWN")
            vol_stable = ins.get("vol_stable", False)
            ud_vol  = self.market.get("tai_u", 0) - self.market.get("xiu_u", 0)
            tai_users = self.market.get("tai_u", 0)
            xiu_users = self.market.get("xiu_u", 0)
            

            signal_lines = []
            if bet:
                ps   = _tier_style(bet.get("tier", "SAFE"))
                sd_  = "🟢 TÀI" if bet.get("side") == "TAI" else "🔴 XỈU"
                conf = int(bet.get("confidence", 0))
                scr  = bet.get("score", 0)
                preview = bet.get("preview", False)
                preview_tag = " [dim yellow](PREVIEW)[/]" if preview else ""
                signal_lines = [
                    Text(""),
                    Text(f" ▶ PHÂN TÍCH TÍN HIỆU:{preview_tag}", style="dim yellow"),
                    Text(f" [{bet.get('tier', 'SAFE')}] {sd_}  Conf={conf}%  Score={scr}",
                         style=ps, justify="center"),
                    Text.from_markup(f" {bet.get('reason', '')}", justify="center", style="dim"),
                ]
                a_border = "bright_yellow"
            else:
                signal_lines = [
                    Text(""),
                    Text(" ⏳ ĐANG PHÂN TÍCH DỮ LIỆU...", style="dim yellow", justify="center"),
                ]
                a_border = "dim"
            
            ana    = Group(
                Text.from_markup(f" 🧾 Pattern: {pm_markup}"),
                Text(""),
                Text(f" 📊 SC: {sc_cnt}{sc_side}  |  GAP: {gap_pct:.1f}%  |  UD: {ud_vol:+.0f} (T:{tai_users} X:{xiu_users})", style="cyan"),
                Text(f" 📈 Vol: {_format_vnd_compact(vol_m, already_million=True)}  |  Regime: {regime}  |  SQ: {sq}", style="cyan"),
                Text(f" 🔒 Vol Stable: {'✅' if vol_stable else '❌'}", style="cyan"),
                *signal_lines,
            )

        layout["analysis"].update(Panel(ana, title="🧠 PHÂN TÍCH", border_style=a_border))




        p_col        = "bright_green" if br.get("profit", 0) > 0 else ("red" if br.get("profit", 0) < 0 else "white")
        kpi_pct      = float(br.get("kpi_pct", 0))
        kpi_next     = int(br.get("kpi_next", 0))
        kpi_bar      = _bar(min(kpi_pct, 100), 16,
                            "bright_green" if kpi_stop else ("green" if kpi_pct >= 50 else "yellow"))
        kpi_lbl      = (f"[bright_green]✅ KPI ĐẠT +{br.get('profit', 0):,.0f}đ[/]"
                        if kpi_stop else f"{kpi_pct:.0f}% → [cyan]{kpi_next:,.0f}đ[/]")
        bs           = self.logic.bankroll.bet_sizes_preview()
        locked_p     = br.get("locked_profit", 0)
        kpi_cycle_n  = br.get("kpi_hits", 0)
        cycle_start  = br.get("kpi_cycle_start", self.logic.bankroll.initial_seed)
        cum_p        = self.logic.bankroll.cumulative_profit
        cum_b        = self.logic.bankroll.total_balance
        cum_w2       = self.logic.bankroll.cumulative_wins
        cum_l2       = self.logic.bankroll.cumulative_losses
        cum_wr       = round(cum_w2 / max(cum_w2 + cum_l2, 1) * 100, 1)
        cp_col       = "bright_green" if cum_p > 0 else ("red" if cum_p < 0 else "white")
        locked_col   = "bright_yellow" if locked_p > 0 else "dim"
        tier_info    = br.get("tier_info", {})
        risk_state   = br.get("risk_state", "NORMAL")

        rs_col = {
            "HOT":        "bright_green",
            "NORMAL":     "white",
            "COLD":       "yellow",
            "LOSS_STREAK": "red",
            "MID_DD":     "yellow",
            "HIGH_DD":    "red",
            "GHOST":      "dark_orange3",
            "COOLDOWN":   "dark_orange3",
            "EMERGENCY":  "red",
            "STOP_LOSS":  "red",
        }.get(risk_state, "white")

        tier_lines = []
        for tn in ["SNIPER_MAX", "SNIPER_HIGH", "SNIPER_MID", "NORMAL"]:
            ti    = tier_info.get(tn, {})
            tot_t = ti.get("total", 0)
            wr_t  = ti.get("wr")
            bet_s = bs.get(tn, 0)
            if tot_t >= 3:
                wr_s = f"{wr_t:.0f}%" if wr_t is not None else "—"
                col  = _wr_col(wr_t or 0)
                tier_lines.append(Text.from_markup(
                    f" [{tn[:10]:>10}]  n={tot_t:>2}  [{col}]WR={wr_s}[/]  "
                    f"[dim]bet~{bet_s:,}đ[/]"
                ))
            else:
                tier_lines.append(Text.from_markup(
                    f" [{tn[:10]:>10}]  n={tot_t:>2}  [dim]—  bet~{bet_s:,}đ[/]"
                ))

        layout["wallet"].update(Panel(Group(
            Text(f" 💵 Vốn CK:  {cycle_start:>12,.0f}đ", style="dim white"),
            Text(f" 💰 Số dư:   {br.get('balance', 0):>12,.0f}đ", style="bold white"),
            Text(f" 📊 Lãi CK:  {br.get('profit', 0):>+12,.0f}đ", style=f"bold {p_col}"),
            Text(f"    ({br.get('profit_pct', 0):>+.2f}%)", style=p_col),
            Text(""),
            Text.from_markup(
                f" 🔒 Đã khóa: [{locked_col}]{locked_p:>11,.0f}đ[/]  [dim]×{kpi_cycle_n} chu kỳ[/]"
            ),
            Text.from_markup(f" 💎 Tổng TS:  [{cp_col}]{cum_b:>12,.0f}đ[/]"),
            Text.from_markup(
                f" 🏆 Tổng WR: [{_wr_col(cum_wr)}]{cum_w2}W/{cum_l2}L  {cum_wr:.1f}%[/]"
            ),
            Text(""),
            Text.from_markup(f" 🎯 KPI #{kpi_cycle_n+1}: {kpi_bar} {kpi_lbl}"),
            Text.from_markup(
                f" 📍 Target:  [yellow]{kpi_next:,.0f}đ[/]  (+{self.logic.bankroll.kpi_step:,.0f}đ)"
            ),
            Text(""),
            Text(f" 📌 CK:  {br.get('wins', 0)}W/{br.get('losses', 0)}L  "
                 f"WR:{br.get('wr', 0):.1f}%", style=_wr_col(br.get("wr", 0))),
            Text(f" 🔄 Recent:{br.get('recent_wr', 50):.1f}%  "
                 f"DD:{br.get('current_dd', 0):.1f}%(max{br.get('max_dd', 0):.1f}%)",
                 style="dim"),
            Text.from_markup(f" ⚙️  Risk:  [{rs_col}]{risk_state}[/]  "
                             f"[dim]Regime:{br.get('regime', '—')}[/]"),
            Text(""),
            *tier_lines,
        ), title="💼 TÀI SẢN + TIER WR", border_style="white"))




        sig_main = sig_sub = ""
        main_style = sub_style = "bold"
        s_border = "dim"

        if br.get("emergency"):
            sig_main   = f"⛔ {(br.get('stop_reason', '') or '')[:55]}"
            main_style = "bold white on red"
            s_border   = "red"
        elif kpi_stop:
            sig_main   = f"🎯 KPI ĐẠT! +{br.get('profit', 0):,}đ"
            sig_sub    = "⏳ Chu kỳ mới đang reset..."
            main_style = "bold black on bright_green"
            sub_style  = "green"
            s_border   = "bright_green"
        elif br.get("cooldown"):
            m  = br.get("cooldown_rem", 0) // 60
            s2 = br.get("cooldown_rem", 0) % 60
            sig_main   = f"❄️ COOLDOWN {m:02d}:{s2:02d}"
            sig_sub    = f"Sau {br.get('loss_streak', 0)} thua liên"
            main_style = "bold black on yellow"
            s_border   = "yellow"
        elif br.get("ghost_mode"):
            sig_main   = "👻 GHOST MODE"
            sig_sub    = "Tự thoát sau 1 thắng"
            main_style = "bold black on yellow"
            s_border   = "yellow"
        elif self.is_rest:
            sig_main   = "⏸ Chờ phiên mới"
            main_style = s_border = "dim"
        elif bet:
            sd_        = "🟢 TÀI" if bet.get("side") == "TAI" else "🔴 XỈU"
            sig_main   = (f"[{bet.get('tier', 'SAFE')}] {sd_}  "
                          f"{bet.get('amount', 0):,}đ  "
                          f"Conf={bet.get('confidence', 0)}%  Score={bet.get('score', 0)}")




            _rmt_r = self.market.get('rmt', 0.0)
            _rmt_at = self.market.get('rmt_at', 0.0)
            timer_mgr = get_timer()
            rmt = timer_mgr.get_remaining_time()
            has_data = self.logic.vol.has_snaps() if hasattr(self, 'logic') else False
            if self.bet_executed:
                sig_sub = "✅ ĐÃ CLICK"
                sub_style = "bold green"
            elif not has_data:
                sig_sub = f"⏳ THU THẬP ({rmt:.0f}s)"
                sub_style = "bold yellow"
            elif rmt >= 7:
                if self.mode == "AUTO":
                    sig_sub = f"🔒 CHỜ CLICK ({rmt:.0f}s)"
                else:
                    sig_sub = f"🔒 CHỐT LỆNH ({rmt:.0f}s)"
                sub_style = "bold cyan"
            else:
                sig_sub = f"🔴 ĐÃ QUA ({rmt:.0f}s)"
                sub_style = "bold red"
            main_style = _tier_style(bet.get("tier", "SAFE"))
            s_border   = "bright_green" if "SNIPER" in (bet.get("tier") or "") else "yellow"
        else:
            skip_d   = (skips[-1] if skips else "Chờ tín hiệu...")[:60]
            sig_main = "⏳ QUAN SÁT"
            sig_sub  = f"▸ {skip_d}"
            main_style = sub_style = s_border = "dim"

        layout["signal"].update(Panel(Group(
            Text(sig_main, justify="center", style=main_style),
            Text(sig_sub,  justify="center", style=sub_style),
        ), title="⚡ LỆNH HIỆN TẠI", border_style=s_border))





        max_history_rows = 12
        history_to_show = self.history[:max_history_rows]
        
        tbl = Table(box=box.SIMPLE_HEAD, expand=True, show_header=True,
                    header_style="bold cyan", padding=(0, 1))
        for col, w in [
            ("Giờ", 9), ("SID", 8), ("Cầu", 6), ("Cạnh", 6),
            ("Tier", 12), ("Số tiền", 13), ("Xúc xắc", 10),
            ("Thực", 5), ("W/L", 8), ("Lãi/Lỗ", 13),
            ("Số dư", 15), ("Score", 6), ("Lý do", 0),
        ]:
            if w:
                tbl.add_column(col, width=w, no_wrap=True)
            else:
                tbl.add_column(col, ratio=1)

        for h in history_to_show:
            res  = h.get("result", "SKIP")
            rs   = ("bold bright_green" if res == "WIN"
                    else "bold red"      if res == "LOSS" else "dim")
            wl   = ("✅ WIN"  if res == "WIN"
                    else "❌ LOSS" if res == "LOSS" else "⏭ SKIP")
            pnl  = h.get("pnl", 0)
            pnl_s = (f"+{pnl:,}đ" if pnl > 0 else f"{pnl:,}đ" if pnl < 0 else "—")
            ps   = "green" if pnl > 0 else ("red" if pnl < 0 else "dim")
            amt  = h.get("amount", 0)
            scr  = h.get("score", "")
            tbl.add_row(
                h.get("time", "—"),
                str(h.get("sid", ""))[-7:],
                h.get("streak", "—"),
                h.get("side", "—"),
                h.get("tier", "-"),
                f"{amt:,}đ" if amt else "—",
                h.get("dice", "—"),
                h.get("actual", "—"),
                Text(wl, style=rs),
                Text(pnl_s, style=ps),
                f"{h.get('balance', 0):,.0f}đ",
                str(scr) if scr else "—",
                (h.get("reason", "—") or "—")[:45],
            )
        layout["table"].update(Panel(
            tbl, title=f"📋 LỊCH SỬ PHIÊN ({len(self.history)} phiên gần nhất)",
            border_style="blue"
        ))




        hd = self.htr_history
        if not hd:
            layout["htr"].update(Panel(
                Text(" ⏳ Chưa nạp lịch sử...", style="dim"),
                title="📚 LỊCH SỬ SÀN", border_style="dim"
            ))
        else:
            tai_n  = sum(1 for h in hd if h.get("result") == "TAI")
            n      = len(hd)
            xiu_n  = n - tai_n
            tai_p  = tai_n / n * 100
            last_r = hd[-1].get("result", "?")
            sc_h   = 0
            for item in reversed(hd):
                if item.get("result") == last_r:
                    sc_h += 1
                else:
                    break
            bias_s = (
                ("green", "TÀI NHIỀU") if tai_p > 55
                else ("red",   "XỈU NHIỀU") if tai_p < 45
                else ("white", "CÂN BẰNG")
            )
            parts = [
                "[bold green]T[/]" if item.get("result") == "TAI"
                else "[bold red]X[/]"
                for item in hd[-55:]
            ]
            layout["htr"].update(Panel(Group(
                Text.from_markup(
                    f" [bold green]TÀI:{tai_n}({tai_p:.0f}%)[/]  "
                    f"[bold red]XỈU:{xiu_n}({100-tai_p:.0f}%)[/]  "
                    f"Cầu:[bold yellow]{sc_h}{'T' if last_r=='TAI' else 'X'}[/]  "
                    f"[{bias_s[0]}]{bias_s[1]}[/]"
                ),
                Text.from_markup(" " + " ".join(parts)),
            ), title=f"📚 LỊCH SỬ SÀN ({n}/100 — 24/7)", border_style="dark_cyan"))




        auto_s = " │ 🤖 " + " · ".join(self.auto_log[:3]) if self.auto_log else ""
        layout["footer"].update(Panel(
            Text(f" {self.status}{auto_s}", style="white"),
            title="🧾 STATUS", border_style=border_main, padding=(0, 1)
        ))

        return layout



class CSVWriter:
    HEADERS = [
        "date", "datetime", "time", "sid", "pattern_pre", "streak",
        "side", "amount", "tier", "reason", "confidence",
        "dice", "total", "actual", "result", "is_win", "pnl",
        "gap_lock", "gap_early", "gap_change", "gap_pct", "vol_lead",
        "late_heavy", "vol_stable", "remaining_s", "total_vol_m", "tai_pct",
        "balance", "profit", "profit_pct", "wr", "wins", "losses",
        "win_streak", "loss_streak", "current_dd", "max_dd",
        "ghost", "emergency", "cooldown", "cooldown_rem",
        "kpi", "kpi_pct", "kpi_hits", "kpi_next", "kpi_floor",
        "kpi_reached", "kpi_hit", "take_profit_hit", "stop_loss_hit", "kpi_stop_signal",
        "tai_wr", "xiu_wr", "bias", "history_21", "kpi_mode",
        "cum_profit", "cum_balance", "cum_wins", "cum_losses", "cum_wr", "total_balance",
        "hist_size", "skip_reason", 

        "Vec", "Z", "M", "Trap", "Rev",
        "pattern_13", "pattern_21", "pattern_100",
        "tai_users", "xiu_users", "ud_diff", "crowd_imbalance", "smart_money_divergence",
        "regime", "session_quality",


        "bridge_pattern", "bridge_status",



        "earcp_decision", "earcp_side", "earcp_confidence", "earcp_threshold",
        "earcp_coherence", "earcp_n_active", "earcp_high_vol", "earcp_agrees_with_live",




        "primary_engine", "legacy_decision", "legacy_side", "legacy_tier",
        "legacy_confidence", "legacy_agrees_with_earcp",
        "ffa_action", "ffa_original_side", "ffa_note",
        "auto_reset_triggered", "auto_reset_count_total", "auto_reset_reason",
    ]

    def __init__(self, filepath: str):
        import csv as _csv
        self.f = open(filepath, "w", newline="", encoding="utf-8-sig")
        self.w = _csv.DictWriter(self.f, fieldnames=self.HEADERS, extrasaction="ignore")
        self.w.writeheader()
        self.f.flush()

        try:
            self.jsonl_path = filepath.replace('.csv', '.jsonl')
            import json as _json
            self.jf = open(self.jsonl_path, 'w', encoding='utf-8')

        except Exception:
            self.jf = None

    def write(self, row: dict):
        self.w.writerow(row)
        self.f.flush()
        try:
            if getattr(self, 'jf', None):
                import json as _json
                self.jf.write(_json.dumps(row, ensure_ascii=False) + "\n")
                self.jf.flush()
        except Exception:
            pass

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass
        try:
            if getattr(self, 'jf', None):
                self.jf.close()
        except Exception:
            pass


class LiveStatusWriter:
    """Minimal status writer stub for compatibility with legacy run flow."""
    def __init__(self, ui, logic, mode):
        self.ui = ui
        self.logic = logic
        self.mode = mode
        self._running = False

    def start(self):
        self._running = True

    def stop(self):
        self._running = False



def _load_csv_history_warmup(logic):
    """Load previous CSV history to warm up logic engine"""
    import csv
    from glob import glob
    
    print("[*] 🔄 Loading previous CSV history...")







    csv_files = glob("apex_sniper_*.csv")
    if not csv_files:
        print("[!] ⚠️ No previous CSV found - cold start")
        return 0
    
    csv_files.sort(reverse=True)
    items = []
    
    try:
        with open(csv_files[0], 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('dice') and row['dice'] != '?':
                    try:
                        d1, d2, d3 = map(int, row['dice'].split('-'))
                        total = d1 + d2 + d3
                        actual = 'TAI' if total >= 11 else 'XIU'
                        items.append({
                            'dice': row['dice'],
                            'total': total,
                            'actual': actual,
                            'result': row.get('actual', actual),
                            'sid': int(row.get('sid', 0))
                        })
                    except:
                        pass
        
        if items:
            items = items[-100:]
            logic._load_history_items(items)
            print(f"[✓] ✅ Loaded {len(items)} sessions from {csv_files[0]}")
            return len(items)
    except Exception as e:
        print(f"[!] Error: {e}")
        return 0
    
    return 0



def get_startup_urls(config: dict) -> list[str]:
    urls = []
    primary_url = config.get("URL")
    if primary_url:
        urls.append(primary_url)

    for fallback_url in config.get("URL_FALLBACKS", []):
        if fallback_url and fallback_url not in urls:
            urls.append(fallback_url)

    if not urls:
        urls.append("https://www.google.com")

    return urls


def open_target_url(driver, config: dict) -> str:
    last_error: Optional[Exception] = None
    for url in get_startup_urls(config):
        try:
            driver.get(url)
            print(f"[✓] Mở trang thành công: {url}")
            return url
        except WebDriverException as exc:
            last_error = exc
            print(f"[!] Không mở được {url}: {exc}")

    try:
        driver.get("about:blank")
    except Exception:
        pass

    raise RuntimeError(
        f"Không thể mở được trang mục tiêu. Đã thử: {', '.join(get_startup_urls(config))}. "
        f"Lỗi cuối: {last_error}"
    ) from last_error


def run(initial_balance: float, kpi: float, mode: str, stop_on_kpi: bool = False):
    csv_file = f"apex_sniper_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    logic    = LogicEngine(initial_balance, kpi, stop_on_kpi=stop_on_kpi)

    logic.conf_thresholds = CONFIG.get("CONF_THRESHOLDS", [55, 60, 65])
    logic.exploration_rate = CONFIG.get("EXPLORATION_RATE", 0.05)
    logic.exploration_bet_pct = CONFIG.get("EXPLORATION_BET_PCT", 0.25)
    

    history_loaded = 0
    if CONFIG.get("USE_CSV_WARMUP", False):
        history_loaded = _load_csv_history_warmup(logic)
        if history_loaded >= 21:
            print(f"[✓] System ready! {history_loaded} sessions loaded")
        elif history_loaded > 0:
            print(f"[!] Only {history_loaded} sessions - may need more for signals")
    else:
        print("[*] Running in LIVE-ONLY mode — not loading historical CSV warmup")
    
    beteng   = AutoBetEngine()
    csv_w    = CSVWriter(csv_file)
    ui       = Dashboard(mode, logic, beteng)
    status_writer = LiveStatusWriter(ui, logic, mode)
    status_writer.start()
    




    try:
        discord = get_discord_notifier()
        discord.on_start(initial_balance, kpi, mode)
        console.print("[dim]📱 Discord: tính năng đang TẮT (stub no-op) — không gửi thông báo thật[/]")
    except Exception as e:
        console.print(f"[yellow]⚠️ Discord lỗi (không block): {e}[/]")

    opts = Options()
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    driver = webdriver.Chrome(options=opts)
    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass

    try:
        open_target_url(driver, CONFIG)
    except Exception as exc:
        console.print(f"[red]❌ Không thể mở trang mục tiêu, dừng bot an toàn: {exc}[/]")
        try:
            driver.quit()
        except Exception:
            pass
        return


    def safe_get_log(driver, log_type: str, timeout: float = 1.0):
        """Get logs với timeout - nếu lâu hơn timeout thì skip"""
        import threading as _t

        nonlocal _log_threads
        now = time.monotonic()
        _log_threads = [
            item for item in _log_threads
            if item[0].is_alive() and now - item[1] < 8.0
        ]
        if len(_log_threads) >= _log_thread_limit:
            try:
                driver.execute_cdp_cmd("Runtime.discardConsoleEntries", {})
            except Exception:
                pass
            return []

        result = []
        exception = None

        def _fetch():
            nonlocal result, exception
            try:
                result = driver.get_log(log_type)
            except Exception as e:
                exception = e

        thread = _t.Thread(target=_fetch, daemon=True)
        thread.start()
        _log_threads.append((thread, now))
        thread.join(timeout=timeout)

        if thread.is_alive():

            return []

        if exception:
            raise exception

        return result

    

    from concurrent.futures import ThreadPoolExecutor
    _bet_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bet_")
    _active_bet_threads = []
    _log_threads = []
    _log_thread_limit = 3

    _st = {
        "bet_open":        False,
        "sess_mono":       0.0,
        "tick_count":      0,
        "cur_sid":         0,
        "hist_loaded":     False,
        "locked_decisions": {},
        "placed_sids":     set(),
        "clicking_sids":   set(),
        "manual_bet_sids": set(),
        "pending_bets":    {},
        "_debug_logged_sids": set(),
        "_auto_debug_sids": set(),



        "drought_index":   0,
        "last_skip_sid":   0,
    }

    _signal_sent = set()
    _bet_done = threading.Event()
    _bet_done.set()

    def remaining_s() -> float:
        mono = _st["sess_mono"]
        if mono == 0.0:
            return 0.0
        return max(0.0, SESSION_TOTAL_S - (time.monotonic() - mono))

    def actionable_decision(dec: Optional[dict]) -> bool:
        return (
            isinstance(dec, dict)
            and (bool(dec.get("allow_execution", dec.get("allow_prediction", True))) or bool(dec.get("log_only", False)))
            and int(dec.get("amount") or 0) > 0
            and dec.get("tier") not in ("NO_ACTION", "SKIP", "-")
            and dec.get("side") in ("TAI", "XIU")
        )

    def _do_bet(dec: dict):
        sid = dec.get("sid")

        if ui.is_rest:
            console.print(f"[yellow]⚠️ Skip click #{sid}: Session ended[/]")
            return False
        ui.add_auto_log(f"⚡ #{sid}: AUTO click {dec['side']} {dec['amount']:,}đ")
        ok = beteng.place_bet(dec["side"], dec["amount"])
        try:
            if ok:
                _st["placed_sids"].add(sid)
                ui.bet_executed = True
                logic.mark_bet_placed()
                side_lbl = "TÀI" if dec["side"] == "TAI" else "XỈU"
                ui.set_status(
                    f"✅ CLICK: {side_lbl} {dec['amount']:,}đ "
                    f"[{dec['tier']}] Conf={dec['confidence']}% Score={dec.get('score',0)}"
                )
                ui.add_auto_log(f"✅ #{sid}: Click OK")
            else:

                ui.bet_executed = False
                ui.set_status(f"⚠️ Click thất bại → KHÔNG ghi CSV: {beteng.last_result}")
                ui.add_auto_log(f"⚠️ #{sid}: Click fail ({beteng.last_result})")
        finally:
            _st["clicking_sids"].discard(sid)

    def _safe_do_bet(d):
        """Wrapper để đảm bảo _bet_done.set() luôn được gọi"""
        if not d:
            _bet_done.set()
            return

        try:
            if not _session_active or ui.is_rest:
                console.print(f"[yellow]⚠️ Skip late bet #{d.get('sid')}: session inactive[/]")
                _bet_done.set()
                return
        except Exception:
            pass
        try:
            _do_bet(d)
        except Exception as e:
            console.print(f"[red]❌ Lỗi thread bet: {e}[/]")
        finally:
            _st["clicking_sids"].discard(d.get("sid"))
            _bet_done.set()




    _last_sid = 0
    _last_closed_sid = 0
    _last_cmd_at = time.monotonic()
    _recover_attempts = 0
    _last_recover_at = 0.0

    def _clear_session_state():
        _st["locked_decisions"].clear()
        _st["pending_bets"].clear()
        _st["placed_sids"].clear()
        _st["manual_bet_sids"].clear()
        _st["clicking_sids"].clear()
        _st["_debug_logged_sids"].clear()
        _st["_auto_debug_sids"].clear()
        _signal_sent.clear()
        ui.bet_in_window = False
        ui.bet_executed = False
        _bet_done.set()

    def _recover_log_stream():
        nonlocal _recover_attempts, _last_recover_at, _last_cmd_at
        now = time.monotonic()
        if now - _last_cmd_at < 15.0:
            return
        if now - _last_recover_at < 30.0:
            return
        _last_recover_at = now
        _recover_attempts += 1
        ui.set_status("⚠️ Không có event WebSocket 15s, recover luồng log...")
        try:

            def _recover():
                try:
                    driver.execute_cdp_cmd("Network.disable", {})
                except Exception:
                    pass
                time.sleep(0.1)
                try:
                    driver.execute_cdp_cmd("Network.enable", {})
                except Exception:
                    pass
                try:
                    safe_get_log(driver, "performance", timeout=0.5)
                except Exception:
                    pass
            
            import threading as _t
            thread = _t.Thread(target=_recover, daemon=True)
            thread.start()
            thread.join(timeout=2.0)
            
            _last_cmd_at = time.monotonic()
            ui.set_status("🔁 Đã recover luồng log, chờ event WebSocket...")
        except Exception as e:
            ui.set_status(f"❌ Recover log stream thất bại: {type(e).__name__}")
        return


    _session_active = False
    

    _last_market_state = None
    with Live(ui.generate(), refresh_per_second=4, screen=True) as live:
        while True:
            try:

                live.update(ui.generate())

                time.sleep(0.03)



                if logic.bankroll.kpi_stop_signal:
                    profit = logic.bankroll.balance - logic.bankroll.initial_seed
                    pct    = profit / max(logic.bankroll.initial_seed, 1) * 100
                    ui.set_status(
                        f"🎯 KPI ĐẠT! +{profit:,.0f}đ (+{pct:.1f}%) — Tiếp tục 24/7 (kill để dừng) CSV:{csv_file}"
                    )








                if os.path.exists(KILL_FILE):
                    ui.set_status("⛔ KILL_SIGNAL — Dừng an toàn... (24/7 mode ended)")
                    live.update(ui.generate())
                    time.sleep(2)
                    try:
                        os.remove(KILL_FILE)
                    except Exception:
                        pass
                    break


                try:
                    try:
                        _ = safe_get_log(driver, "browser", timeout=0.15)
                    except Exception:
                        pass

                    try:
                        driver.execute_cdp_cmd("Runtime.discardConsoleEntries", {})
                    except Exception:
                        pass
                    _last_sid = _st.get("cur_sid", 0)
                except Exception:
                    pass
                
                try:
                    logs = safe_get_log(driver, "performance", timeout=0.25)
                except Exception as e:

                    ui.set_status(f"⚠️ Chrome log error: {type(e).__name__} — Attempting CDP reconnect...")
                    try:

                        driver.execute_cdp_cmd("Network.enable", {})
                        ui.set_status("✅ CDP reconnected")
                        time.sleep(0.2)
                    except Exception as e2:
                        ui.set_status(f"❌ Chrome may have crashed: {type(e2).__name__}")
                        time.sleep(1)
                    continue
                

                if not logs:
                    if time.monotonic() - _last_cmd_at > 6 and not ui.is_rest:
                        ui.set_status("⚠️ Không có event WebSocket trong 6s — kiểm tra kết nối...")
                        _recover_log_stream()
                    time.sleep(0.02)
                    in_s2, sl2, wr2, rem2 = in_trading_hours()
                    if CONFIG.get("USE_SCHEDULE", False) and not in_s2:
                        wh2, wm2 = rem2 // 60, rem2 % 60
                        ui.set_status(
                            f"💤 NGHỈ — Ngoài khung giờ  •  Tiếp: {sl2} (sau {wh2}g{wm2:02d}p)"
                        )
                    else:
                        if ui.market.get("sid", 0) and not ui.is_rest:
                            _r = ui.market.get("rmt", 0.0)
                            _at = ui.market.get("rmt_at", 0.0)
                            try:
                                if isinstance(_at, (int, float)) and _at > 0:
                                    rmt_temp = max(0.0, _r - (time.monotonic() - _at))
                                elif hasattr(_at, 'timestamp'):
                                    rmt_temp = max(0.0, _r - (time.time() - _at.timestamp()))
                                else:
                                    rmt_temp = max(0.0, float(_r or 0.0))
                            except Exception:
                                rmt_temp = max(0.0, float(_r or 0.0))
                            ui.set_status(
                                f"⏳ Chờ dữ liệu WebSocket... SID#{ui.market.get('sid')} rmt={rmt_temp:.1f}s"
                            )
                        else:
                            ui.set_status(
                                f"⏳ Chờ WebSocket... ({datetime.now().strftime('%H:%M:%S')}) "
                                f"Đăng nhập + mở bàn TÀI XỈU"
                            )
                else:
                    timer_mgr = get_timer()
                    rmt_show = timer_mgr.get_remaining_time()



                    if ui.is_rest:

                        ui.set_status(f"⏸️ Nghỉ giữa phiên… (chờ phiên mới)")
                        rmt_show = 0.0
                    else:

                        if rmt_show >= 20:
                            ui.set_status(f"🔎 Gom data… {rmt_show:.0f}s")
                        elif 13 < rmt_show < 20:

                            try:
                                g    = logic.vol.best_gap()
                                gc   = round(logic.vol.gap_change(), 1)
                                sc   = logic._streak_snap_cnt
                                ss   = logic._streak_snap_side
                                lh   = logic.vol.late_heavy()
                                ud   = logic.vol.best_ud()
                                reg  = logic._cur_regime
                                sq   = logic._cur_sq
                                lh_t = "LH" if lh else "✅"
                                ui.set_status(
                                    f"🟡 ANALYZE  SC={sc}{ss}  gap:{g:.1f}%(Δ{gc:+.0f})  "
                                    f"ud:{ud:+d}  LH:{lh_t}  {reg}|{sq[:3]}  {rmt_show:.1f}s"
                                )
                            except Exception:
                                ui.set_status(f"🟡 ANALYZE {rmt_show:.1f}s (calculating...)")
                        elif 11.5 <= rmt_show <= 14:
                            emoji = "🟢" if 12 <= rmt_show <= 13 else "🟠"
                            ui.set_status(f"{emoji} LOCK ({rmt_show:.1f}s) ← BEST WINDOW")
                        elif 11 <= rmt_show < 11.5:
                            ui.set_status(f"🟡 Ready ({rmt_show:.1f}s) — prep 12s...")
                        elif 0.1 < rmt_show < 11:
                            ui.set_status(f"🔴 Qua cửa sổ — chờ phiên ({rmt_show:.1f}s)")



                    MAX_LOGS_PER_CYCLE = 20
                    logs_to_process = logs[:MAX_LOGS_PER_CYCLE] if len(logs) > MAX_LOGS_PER_CYCLE else logs


                    _active_bet_threads = [f for f in _active_bet_threads if not f.done()]
                    for f in list(_active_bet_threads):
                        if f.done() and f.exception() is not None:
                            ui.add_auto_log(f"⚠️ Bet task error: {f.exception()}")

                    for entry in logs_to_process:
                        try:
                            _pkt_at = time.monotonic()
                            _lag    = 0.0
                            

                            try:
                                entry_msg = json.loads(entry["message"])
                                msg = entry_msg.get("message", {})
                            except Exception:
                                continue
                            

                            event_time = None
                            try:
                                _wt = (
                                    msg.get("params", {}).get("timestamp") or
                                    msg.get("params", {}).get("wallTime", 0)
                                )
                                if _wt and _wt > 1e9:
                                    import time as _t
                                    _lag = max(0.0, min(_t.time() - _wt, 3.0))
                                    import datetime as _dt
                                    event_time = _dt.datetime.fromtimestamp(_wt)
                            except Exception:
                                event_time = None
                            
                            if msg.get("method") != "Network.webSocketFrameReceived":
                                continue
                            params = msg.get("params", {})
                            raw    = (
                                params.get("payloadData") or
                                params.get("response", {}).get("payloadData") or
                                params.get("frame", {}).get("payloadData")
                            )
                            if not raw:
                                continue
                            data = decode_payload(raw)
                            if not isinstance(data, (dict, list)):
                                continue
                            if isinstance(data, list):
                                body = None
                                for el in data:
                                    if isinstance(el, dict) and (
                                        "cmd" in el or "gi" in el or "htr" in el
                                    ):
                                        body = el
                                        break
                                if body is None and len(data) >= 2 and isinstance(data[1], dict):
                                    body = data[1]
                                if body is None:
                                    continue
                            else:
                                body = data
                            if not isinstance(body, dict):
                                continue
                            cmd = body.get("cmd", 0)

                            target_game = CONFIG.get("TARGET_GAME", "TX_THUONG")
                            if target_game == "TX_THUONG":
                                if cmd == 1015 or cmd not in (1002, 1003, 1005, 1008):
                                    continue
                            elif target_game == "TX_MD5":
                                if cmd in (1008, 1003, 1002, 1005):
                                    continue
                                if cmd == 1015:
                                    inner_data = body.get("d", {})
                                    if not isinstance(inner_data, dict):
                                        continue
                                    md5_cmd = inner_data.get("cmd", 0)
                                    if md5_cmd == 2011:
                                        cmd = 1005
                                    elif md5_cmd == 2007:
                                        cmd = 1008
                                    elif md5_cmd == 2006:
                                        cmd = 1003
                                    else:
                                        continue
                                    body = inner_data
                                else:
                                    continue

                            if cmd in (1002, 1003, 1005, 1008):
                                _last_cmd_at = time.monotonic()
                            






                            if logic.hist.size() >= 100 or (ui.htr_history and len(ui.htr_history) >= HIST_MIN):
                                if not _st["hist_loaded"]:
                                    _st["hist_loaded"] = True
                                    if ui.htr_history:
                                        ui.set_status(f"✅ HTR READY: {len(ui.htr_history)}/100 phiên")
                            
                            if not _st["hist_loaded"] and logic.hist.size() < 100:

                                htr = (
                                    body.get("htr") or body.get("hist") or
                                    body.get("H") or body.get("history") or 
                                    _find_htr(body)
                                )
                                if htr:
                                    try:
                                        loaded_cnt = _ingest_history(logic, htr, ui, f"CMD{cmd or 'INIT'}")
                                        if loaded_cnt >= 100 or logic.hist.size() >= 100 or len(ui.htr_history) >= 100:
                                            _st["hist_loaded"] = True

                                            ui.set_status(f"✅ Nạp xong history: {loaded_cnt} phiên")
                                        elif loaded_cnt >= HIST_MIN:
                                            _st["hist_loaded"] = True

                                            ui.set_status(f"⚠️ HTR tối thiểu: {loaded_cnt} phiên")
                                        else:
                                            _st["hist_loaded"] = False
                                    except Exception as e:

                                        ui.set_status(f"❌ Lỗi load history: {e}")


                            if cmd == 1008:
                                sid = int(body.get("sid") or _st.get("cur_sid", 0) or 0)








                                if sid and sid != _st.get("cur_sid"):
                                    _st["activation_mono"] = time.monotonic()
                                    _st["sess_mono"] = 0.0


                                if not _session_active:
                                    if sid and sid != _last_closed_sid:
                                        _session_active = True
                                        _last_closed_sid = 0
                                        _st["activation_mono"] = time.monotonic()

                                        try:
                                            _st["session_br_wins_start"] = logic.bankroll.wins
                                            _st["session_br_losses_start"] = logic.bankroll.losses
                                        except Exception:
                                            pass
                                    else:
                                        continue
                                _st["cur_sid"] = sid
                                tai, xiu, est_vol = _extract_taixiu_from_1008(body)
                                tai_u, xiu_u = _extract_ud_from_1008(body)

                                rmt_raw = body.get("rmT", 0)
                                if rmt_raw > 0:
                                    lag2    = _lag if 0 < _lag < 2 else 0.15
                                    rmt_now = max(0.0, rmt_raw / 1000.0 - lag2)



                                    if rmt_now < 5.0 and _st.get("activation_mono"):
                                        _elapsed_since_activation = time.monotonic() - _st["activation_mono"]
                                        if _elapsed_since_activation < (SESSION_TOTAL_S - 8.0):
                                            _rmt_fallback = max(0.0, SESSION_TOTAL_S - _elapsed_since_activation)
                                            if _rmt_fallback > rmt_now:
                                                try:
                                                    with open("rmt_crosscheck_override.log", "a", encoding="utf-8") as _xf:
                                                        _xf.write(
                                                            f"{datetime.now().isoformat()} sid={_st.get('cur_sid')} "
                                                            f"rmt_raw={rmt_raw} rmt_now_goc={rmt_now:.2f} "
                                                            f"elapsed_since_activation={_elapsed_since_activation:.2f} "
                                                            f"-> rmt_now_moi={_rmt_fallback:.2f}\n"
                                                        )
                                                except Exception:
                                                    pass
                                                rmt_now = _rmt_fallback
                                    _st["sess_mono"] = _pkt_at - (SESSION_TOTAL_S - rmt_now)
                                    _st["bet_open"] = True
                                    ui.is_rest      = False
                                else:


                                    if _st["sess_mono"] == 0.0:
                                        _st["sess_mono"] = _pkt_at
                                    rmt_now = max(0.0, SESSION_TOTAL_S - (time.monotonic() - _st["sess_mono"]))

                                in_window = (BET_WINDOW_MIN <= rmt_now <= BET_WINDOW_MAX)
                                ui.is_rest = (rmt_now == 0.0) or not in_window
                                ui.update_market(_st["cur_sid"], tai, xiu, rmt_now, tai_u, xiu_u)






                                if _st["cur_sid"] in _st["locked_decisions"]:

                                    dec = _st["locked_decisions"][_st["cur_sid"]]
                                else:

                                    dec = logic.analyze(_st["cur_sid"], tai, xiu, tai_u, xiu_u, rmt_now, estimated_volume=est_vol)
                                    






                                    _st["drought_index"] = logic.skip_streak
                                    if (
                                        dec.get("tier") in ("WEAK_ACTION", "OSCILLATION_MODE")
                                        and "REAL_DROUGHT_RECOVERY" in str(dec.get("reason", ""))
                                    ):

                                        if sid not in _st.get("_logged_drought_sids", set()):
                                            ui.add_auto_log(f"⚠ Drought #{logic.skip_streak}: forcing {dec['tier']}")
                                            _st.setdefault("_logged_drought_sids", set()).add(sid)


                                in_sched = in_trading_hours()[0] if CONFIG.get("USE_SCHEDULE", False) else True
                                can_place_now = (
                                    _st["hist_loaded"] and
                                    (BET_WINDOW_MIN <= rmt_now <= BET_WINDOW_MAX) and
                                    (not logic.bankroll.kpi_stop_signal) and
                                    in_sched
                                )














                                _dec_streak_str = dec.get("streak", "")
                                _dec_streak_num = 0
                                try:
                                    if _dec_streak_str and len(_dec_streak_str) > 1:
                                        _num_part = _dec_streak_str[:-1]
                                        if _num_part.isdigit():
                                            _dec_streak_num = int(_num_part)
                                except Exception:
                                    _dec_streak_num = 0
                                _is_ultra_streak = _dec_streak_num >= 6



                                _can_lock_ultra = (
                                    _is_ultra_streak and
                                    _st["hist_loaded"] and
                                    actionable_decision(dec) and
                                    not logic.bankroll.kpi_stop_signal and
                                    (in_sched if CONFIG.get("USE_SCHEDULE", False) else True)
                                )




                                if (actionable_decision(dec) and 
                                    sid not in _st["pending_bets"] and 
                                    sid not in _st["manual_bet_sids"] and 
                                    sid not in _st["locked_decisions"] and
                                    (can_place_now or _can_lock_ultra)):
                                    dec_ts = dec.get("decision_ts") or event_time or datetime.now()
                                    dec["decision_ts"] = dec_ts
                                    _st["pending_bets"][sid]    = dict(dec)
                                    _st["locked_decisions"][sid] = dict(dec)


                                in_sched, sched_label, sched_wr, sched_rem = in_trading_hours()
                                can_place = (
                                    _st["hist_loaded"] and
                                    (BET_WINDOW_MIN <= rmt_now <= BET_WINDOW_MAX) and
                                    (not logic.bankroll.kpi_stop_signal) and
                                    (in_sched if CONFIG.get("USE_SCHEDULE", False) else True)
                                )


                                if mode == "MANUAL":
                                    if sid not in _st["manual_bet_sids"] and actionable_decision(dec):
                                        _st["manual_bet_sids"].add(sid)
                                        logic.mark_bet_placed(sid)

                                        ui.bet_executed = can_place
                                        side_lbl = "TÀI" if dec["side"] == "TAI" else "XỈU"
                                        tier_for_disp = dec.get("tier", "SNIPER_MID")
                                        status_msg = (
                                            f"🎯 TÍN HIỆU: {side_lbl} {dec['amount']:,}đ "
                                            f"[{dec['tier']}] Conf={dec['confidence']}% "
                                            f"Score={dec.get('score', 0)} ← ĐẶT TAY"
                                        ) if can_place else (
                                            f"⏳ CHUẨN BỊ: {side_lbl} {dec['amount']:,}đ "
                                            f"[{dec['tier']}] (chờ {BET_WINDOW_MIN}s-{BET_WINDOW_MAX}s)"
                                        )
                                        ui.set_status(status_msg)







                                ui.bet_in_window = can_place






                                if can_place and sid in _st["locked_decisions"]:
                                    dec14    = _st["locked_decisions"][sid]
                                    side_lbl = "TÀI" if dec14["side"] == "TAI" else "XỈU"
                                    if sid not in _st["placed_sids"] and sid not in _st["manual_bet_sids"]:
                                        ui.set_status(
                                            f"🎯 CHỐT {rmt_now:.1f}s: {side_lbl} "
                                            f"{dec14['amount']:,}đ [{dec14['tier']}] "
                                            f"Conf={dec14['confidence']}%"
                                        )

                                    if sid not in _signal_sent:
                                        try:
                                            discord = get_discord_notifier()

                                            _signal_sent.add(sid)
                                        except Exception:
                                            pass
                                    



                                    can_click = (
                                        mode == "AUTO" and
                                        sid not in _st["clicking_sids"] and
                                        sid not in _st["placed_sids"] and
                                        dec14.get("amount", 0) > 0 and
                                        (rmt_now <= EXECUTION_START_S and rmt_now > EXECUTION_END_S)
                                    )

                                    can_click = (
                                        mode == "AUTO" and
                                        sid not in _st["clicking_sids"] and  
                                        sid not in _st["placed_sids"] and
                                        dec14.get("amount", 0) > 0 and
                                        (rmt_now <= EXECUTION_START_S and rmt_now > EXECUTION_END_S)
                                    )


                                    if can_click:

                                        pag = get_pyautogui()
                                        pgw = get_pygetwindow()

                                        if not pag or not pgw:
                                            ui.add_auto_log(f"⚠️ #{sid}: Missing libs (pyautogui/pygetwindow)")
                                            continue

                                        if beteng._win() is None:
                                            ui.add_auto_log(f"⚠️ #{sid}: Window not found")
                                            continue


                                        if dec14.get("log_only"):
                                            ui.add_auto_log(f"🔍 LOG_ONLY #{sid}: Not clicking (preview)")
                                            continue


                                        _st["clicking_sids"].add(sid)
                                        _st["placed_sids"].add(sid)


                                        _bet_done.clear()
                                        dec14["sid"] = sid

                                        try:

                                            future = _bet_executor.submit(_safe_do_bet, dec14)
                                            _active_bet_threads.append(future)
                                        except Exception as e:

                                            _st["clicking_sids"].discard(sid)
                                            _st["placed_sids"].discard(sid)
                                            _bet_done.set()
                                            ui.add_auto_log(f"⚠️ #{sid}: Bet thread submit fail: {e}")
                                            
                                    elif mode == "AUTO" and actionable_decision(dec):
                                        why = []
                                        if not _st["hist_loaded"]:   why.append("hist=0")
                                        if rmt_now <= EXECUTION_END_S or rmt_now > EXECUTION_START_S:
                                            why.append(f"rmt={rmt_now:.1f}")
                                        if logic.bankroll.kpi_stop_signal: why.append("KPI-stop")
                                        if CONFIG.get("USE_SCHEDULE", False) and not in_sched:
                                            why.append("out-of-schedule")
                                        if why:
                                            ui.add_auto_log("no-click: " + ",".join(why))

                                        side_lbl = "TÀI" if dec["side"] == "TAI" else "XỈU"
                                        ui.set_status(
                                            f"⏳ DỰ ĐOÁN: {side_lbl} {dec['amount']:,}đ [{dec['tier']}] "
                                            f"({', '.join(why) if why else 'chờ cửa sổ'})"
                                        )


                            elif cmd == 1002:
                                sid_new = body.get("sid", 0)
                                if sid_new:
                                    _st["cur_sid"] = sid_new
                                    _st.update({
                                        "bet_open": False, "sess_mono": 0.0, "tick_count": 0,
                                        "locked_decisions": {}, "pending_bets": {}, "placed_sids": set(),
                                        "manual_bet_sids": set(), "_debug_logged_sids": set(),
                                        "_auto_debug_sids": set(),
                                    })
                                    _st["clicking_sids"].clear()
                                    _bet_done.set()
                                    _signal_sent.clear()
                                    _last_closed_sid = 0
                                    ui.bet_in_window = False
                                    ui.bet_executed = False
                                    ui.update_market(sid_new, 0, 0, 0.0)
                                ui.is_rest = False
                                _session_active = True
                                _st["activation_mono"] = time.monotonic()


                            elif cmd == 1005:
                                sid_i = body.get("sid", 0)
                                if sid_i:
                                    _st["cur_sid"] = sid_i
                                    _st["locked_decisions"].pop(sid_i, None)
                                    _st["pending_bets"] = {}
                                    _st["placed_sids"].discard(sid_i)
                                    _st["manual_bet_sids"].discard(sid_i)
                                    _last_closed_sid = 0
                                    ui.update_market(sid_i, 0, 0, 0.0)
                                    ui.bet_executed = False
                                    ui.bet_in_window = False
                                    _st["bet_open"] = (body.get("st", 0) == 1)

                                _session_active = True
                                _st["activation_mono"] = time.monotonic()
                                rmt_1005 = body.get("rmT", 0)
                                if rmt_1005 > 0:
                                    lag3    = _lag if 0 < _lag < 2 else 0.15
                                    rmt_s2  = max(0.0, rmt_1005 / 1000.0 - lag3)
                                    _st["sess_mono"] = _pkt_at - (SESSION_TOTAL_S - rmt_s2)
                                    ui.is_rest = False
                                else:
                                    _st["sess_mono"] = 0.0

                                    ui.is_rest = True
                                _st["tick_count"] = 0


                            elif cmd == 1003:

                                console.print(f"[magenta bold]📨 CMD 1003 RECEIVED[/]")

                                _session_active = False

                                _st.update({
                                    "bet_open": False, "sess_mono": 0.0, "tick_count": 0,
                                })
                                _st["clicking_sids"].clear()
                                _bet_done.set()
                                ui.is_rest = True
                                ui.bet_in_window = False
                                ui.bet_executed = False
                                ui.update_market(0, 0, 0, 0.0)
                                prev_sid = _st.get("cur_sid", 0)
                                try:
                                    live.update(ui.generate())
                                except Exception:
                                    pass
                                
                                sid_1003, d1, d2, d3, total, actual = _extract_result_from_1003(body)
                                if sid_1003 == 0:
                                    sid_1003 = prev_sid
                                _last_closed_sid = sid_1003
                                _st["cur_sid"] = 0
                                
                                console.print(f"[cyan]🎲 #{sid_1003}: {actual} {d1}-{d2}-{d3}[/]")


                                has_valid_result = not (d1 == 0 and d2 == 0 and d3 == 0 and total == 0)
                                


                                def _write_show(_row: dict, sw: str = "SKIP", rmt_for_csv: float = 0, event_time: Optional[datetime] = None):
                                    """Write row to CSV - must be always callable"""
                                    if event_time is None:
                                        event_time = datetime.now()

                                    if _row is None:
                                        _row = {
                                            "date": event_time.strftime("%Y-%m-%d"),
                                            "datetime": event_time.strftime("%Y-%m-%d %H:%M:%S"),
                                            "time": event_time.strftime("%H:%M:%S"),
                                            "sid": sid_1003,
                                            "pattern_pre": "",
                                            "streak": logic._streak_str() if logic else "—",
                                            "side": "—",
                                            "amount": 0,
                                            "tier": "SKIP",
                                            "reason": "NoRow:process_result_failed",
                                            "confidence": 0,
                                            "dice": f"{d1}-{d2}-{d3}",
                                            "total": d1 + d2 + d3,
                                            "actual": actual,
                                            "result": "SKIP",
                                            "is_win": False,
                                            "pnl": 0,
                                            "gap_lock": 0,
                                            "gap_early": 0,
                                            "gap_change": 0,
                                            "gap_pct": 0,
                                            "vol_lead": 0,
                                            "late_heavy": 0,
                                            "vol_stable": 0,
                                            "remaining_s": 0,
                                            "total_vol_m": 0,
                                            "tai_pct": 0,
                                            "balance": logic.bankroll.balance if logic else 0,
                                            "profit": 0,
                                            "profit_pct": 0,
                                            "wr": 0,
                                            "wins": 0,
                                            "losses": 0,
                                            "win_streak": 0,
                                            "loss_streak": 0,
                                            "current_dd": 0,
                                            "max_dd": 0,
                                            "ghost": False,
                                            "emergency": False,
                                            "cooldown": False,
                                            "cooldown_rem": 0,
                                            "kpi": 0,
                                            "kpi_pct": 0,
                                            "kpi_hits": 0,
                                            "kpi_next": 0,
                                            "kpi_floor": 0,
                                            "kpi_reached": False,
                                            "kpi_hit": False,
                                            "take_profit_hit": False,
                                            "stop_loss_hit": False,
                                            "kpi_stop_signal": "",
                                            "tai_wr": 0,
                                            "xiu_wr": 0,
                                            "bias": "",
                                            "history_21": "",
                                            "kpi_mode": False,
                                            "cum_profit": 0,
                                            "cum_balance": logic.bankroll.total_balance if logic else 0,
                                            "cum_wins": 0,
                                            "cum_losses": 0,
                                            "cum_wr": 0,
                                            "total_balance": logic.bankroll.total_balance if logic else 0,
                                            "hist_size": 0,
                                            "skip_reason": "",
                                            "regime": "UNKNOWN",
                                            "session_quality": "UNKNOWN",
                                        }
                                    tai_m = ui.market.get("tai", 0)
                                    xiu_m = ui.market.get("xiu", 0)
                                    if _row.get("date") in (None, ""):
                                        _row["date"] = event_time.strftime("%Y-%m-%d")
                                    if _row.get("datetime") in (None, ""):
                                        _row["datetime"] = event_time.strftime("%Y-%m-%d %H:%M:%S")
                                    if _row.get("time") in (None, ""):
                                        _row["time"] = event_time.strftime("%H:%M:%S")
                                    if _row.get("total_vol_m", 0) == 0 and (tai_m + xiu_m) > 0:
                                        _row["total_vol_m"] = round((tai_m + xiu_m) / 1e6, 2)
                                    if _row.get("tai_pct", None) in (None, 0) and (tai_m + xiu_m) > 0:
                                        _row["tai_pct"] = round(
                                            tai_m / max(tai_m + xiu_m, 1) * 100, 1
                                        )
                                    csv_w.write(_row)
                                
                                if has_valid_result:
                                    
                                    try:

                                        start_wait = time.monotonic()
                                        max_wait = 0.25
                                        while time.monotonic() - start_wait < max_wait:
                                            if _bet_done.wait(timeout=0.05):
                                                break
                                            if sid_1003 not in _st["clicking_sids"]:
                                                break
                                        _bet_done.set()
                                        

                                        ui.append_htr(actual, f"{d1}-{d2}-{d3}", total, sid_1003)


                                        has_pending = sid_1003 in _st["pending_bets"]
                                        ov = _st["pending_bets"].get(sid_1003)
                                        if mode == "AUTO":

                                            bet_exec = sid_1003 in _st["placed_sids"]
                                        else:



                                            bet_exec = bool(ov) or ui.bet_executed

                                        row = logic.process_result(
                                            sid_1003, d1, d2, d3,
                                            bet_executed=bet_exec, mode=mode, override=ov
                                        )
                                        

                                        if not isinstance(row, dict):
                                            console.print(f"[red]❌ process_result returned invalid row for sid={sid_1003}: {type(row)}[/]")
                                            dec_info = ov or _st.get("pending_bets", {}).get(sid_1003) or _st.get("locked_decisions", {}).get(sid_1003)
                                            side = dec_info.get("side") if isinstance(dec_info, dict) else None
                                            amount = dec_info.get("amount") if isinstance(dec_info, dict) else 0
                                            tier = dec_info.get("tier") if isinstance(dec_info, dict) else "SKIP"
                                            confidence_val = dec_info.get("confidence", 0) if isinstance(dec_info, dict) else 0
                                            dec_ts = None
                                            if isinstance(dec_info, dict):
                                                dec_ts = dec_info.get("decision_ts")
                                                if not isinstance(dec_ts, datetime):
                                                    dec_ts = None
                                            event_time_for_row = dec_ts or event_time or datetime.now()
                                            row = {
                                                "date": event_time_for_row.strftime("%Y-%m-%d"),
                                                "datetime": event_time_for_row.strftime("%Y-%m-%d %H:%M:%S"),
                                                "time": event_time_for_row.strftime("%H:%M:%S"),
                                                "sid": sid_1003,
                                                "pattern_pre": "",
                                                "streak": logic._streak_str() if logic else "—",
                                                "side": side if side else "—",
                                                "amount": int(amount) if amount else 0,
                                                "tier": tier or "SKIP",
                                                "reason": "process_result_returned_invalid",
                                                "confidence": confidence_val,
                                                "dice": f"{d1}-{d2}-{d3}",
                                                "total": d1 + d2 + d3,
                                                "actual": actual,
                                                "result": "SKIP",
                                                "is_win": False,
                                                "pnl": 0,
                                                "gap_lock": 0,
                                                "gap_early": 0,
                                                "gap_change": 0,
                                                "gap_pct": 0,
                                                "vol_lead": 0,
                                                "late_heavy": 0,
                                                "vol_stable": 0,
                                                "remaining_s": 0,
                                                "total_vol_m": 0,
                                                "tai_pct": 0,
                                                "balance": logic.bankroll.balance if logic else 0,
                                                "profit": 0,
                                                "profit_pct": 0,
                                                "wr": 0,
                                                "wins": 0,
                                                "losses": 0,
                                                "win_streak": 0,
                                                "loss_streak": 0,
                                                "current_dd": 0,
                                                "max_dd": 0,
                                                "ghost": False,
                                                "emergency": False,
                                                "cooldown": False,
                                                "cooldown_rem": 0,
                                                "kpi": 0,
                                                "kpi_pct": 0,
                                                "kpi_hits": 0,
                                                "kpi_next": 0,
                                                "kpi_floor": 0,
                                                "kpi_reached": False,
                                                "kpi_hit": False,
                                                "take_profit_hit": False,
                                                "stop_loss_hit": False,
                                                "kpi_stop_signal": "",
                                                "tai_wr": 0,
                                                "xiu_wr": 0,
                                                "bias": "",
                                                "history_21": "",
                                                "kpi_mode": False,
                                                "cum_profit": 0,
                                                "cum_balance": logic.bankroll.total_balance if logic else 0,
                                                "cum_wins": 0,
                                                "cum_losses": 0,
                                                "cum_wr": 0,
                                                "total_balance": logic.bankroll.total_balance if logic else 0,
                                                "hist_size": 0,
                                                "skip_reason": "",
                                                "regime": _st.get("regime", "UNKNOWN"),
                                                "session_quality": _st.get("session_quality", "UNKNOWN"),
                                            }


                                        try:
                                            discord = get_discord_notifier()
                                            result = row.get('result', '-')
                                            tier = row.get('tier', '-')
                                            pnl = row.get('pnl', 0)
                                            streak = row.get('streak', '-')
                                            reason = row.get('reason', '')[:80]
                                            







                                        except Exception:
                                            pass





















                                        pass



                                        _st["locked_decisions"].clear()
                                        _st["pending_bets"].clear()
                                        _st["placed_sids"].clear()
                                        _st["manual_bet_sids"].clear()
                                        _st["_debug_logged_sids"].clear()
                                        _st["_auto_debug_sids"].clear()

                                        br2 = logic.bankroll.stats()


                                        console.print(f"[dim cyan]📝 Ghi CSV: sid={sid_1003}  bet_exec={bet_exec}[/]")
                                        _write_show(row, row.get("result", "SKIP") if row else "SKIP")


                                        if row:
                                            history_row = {
                                                "time":    row.get("time", datetime.now().strftime("%H:%M:%S")),
                                                "sid":     sid_1003,
                                                "streak":  row.get("streak", "—"),
                                                "side":    row.get("side", "—"),
                                                "tier":    row.get("tier", "-"),
                                                "amount":  row.get("amount", 0),
                                                "dice":    row.get("dice") or f"{d1}-{d2}-{d3}",
                                                "actual":  actual,
                                                "result":  row.get("result", "SKIP"),
                                                "pnl":     row.get("pnl", 0),
                                                "balance": row.get("balance", 0),
                                                "reason":  row.get("reason", "—"),
                                                "score":   row.get("confidence", 0),
                                            }
                                            ui.add_history(history_row)


                                        try:
                                            start_w = _st.pop("session_br_wins_start", None)
                                            start_l = _st.pop("session_br_losses_start", None)
                                            if start_w is not None and start_l is not None:
                                                curr_w = logic.bankroll.wins
                                                curr_l = logic.bankroll.losses
                                                delta_w = max(0, int(curr_w - int(start_w)))
                                                delta_l = max(0, int(curr_l - int(start_l)))
                                                logic.bankroll.record_session(delta_w, delta_l, session_quality=_st.get("session_quality", "UNKNOWN"))
                                                if logic.bankroll.emergency:
                                                    ui.set_status(logic.bankroll.stop_reason())
                                        except Exception:
                                            pass


                                        if br2.get("kpi_just_hit") or row.get("kpi_hit"):
                                            profit = br2.get("locked_profit", 0)
                                            ui.set_status(
                                                f"🎯 KPI ĐẠT! Đã khoá +{profit:,.0f}đ — "
                                                f"Chu kỳ mới bắt đầu!"
                                            )
                                        elif br2.get("emergency"):
                                            ui.set_status(br2.get("stop_reason", "🚨 EMERGENCY"))
                                        elif br2.get("stop_loss_hit"):
                                            ui.set_status(br2.get("stop_reason", "🛑 STOP LOSS"))
                                            live.update(ui.generate())
                                            time.sleep(5)
                                            break
                                    except Exception as e:
                                        import traceback
                                        console.print(f"[red]❌ Result processing error: {e}[/]")
                                        console.print(f"[dim red]{traceback.format_exc()}[/]")
                                        ui.set_status(f"❌ Result processing error: {str(e)[:60]}")

                                        try:
                                            _write_show(None, "ERROR", 0, event_time=event_time)
                                        except:
                                            pass
                                else:


                                    console.print(f"[yellow]⚠️ CMD 1003 không có valid result: d1={d1}, d2={d2}, d3={d3}[/]")
                                    ui.set_status(f"⚠️ CMD 1003 với dữ liệu không hợp lệ: d1={d1}, d2={d2}, d3={d3}")

                                    try:
                                        now = datetime.now()
                                        error_row = {
                                            "date": now.strftime("%Y-%m-%d"),
                                            "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
                                            "time": now.strftime("%H:%M:%S"),
                                            "sid": sid_1003,
                                            "pattern_pre": "",
                                            "streak": logic._streak_str() if logic else "—",
                                            "side": "—",
                                            "amount": 0,
                                            "tier": "SKIP",
                                            "reason": f"InvalidResult:d1={d1},d2={d2},d3={d3}",
                                            "confidence": 0,
                                            "dice": f"{d1}-{d2}-{d3}",
                                            "total": d1 + d2 + d3,
                                            "actual": "—",
                                            "result": "INVALID",
                                            "is_win": False,
                                            "pnl": 0,
                                            "gap_lock": 0,
                                            "gap_early": 0,
                                            "gap_change": 0,
                                            "gap_pct": 0,
                                            "vol_lead": 0,
                                            "late_heavy": 0,
                                            "vol_stable": 0,
                                            "remaining_s": 0,
                                            "total_vol_m": 0,
                                            "tai_pct": 0,
                                            "balance": logic.bankroll.balance if logic else 0,
                                            "profit": 0,
                                            "profit_pct": 0,
                                            "wr": 0,
                                            "wins": 0,
                                            "losses": 0,
                                            "win_streak": 0,
                                            "loss_streak": 0,
                                            "current_dd": 0,
                                            "max_dd": 0,
                                            "ghost": False,
                                            "emergency": False,
                                            "cooldown": False,
                                            "cooldown_rem": 0,
                                            "kpi": 0,
                                            "kpi_pct": 0,
                                            "kpi_hits": 0,
                                            "kpi_next": 0,
                                            "kpi_floor": 0,
                                            "kpi_reached": False,
                                            "kpi_hit": False,
                                            "take_profit_hit": False,
                                            "stop_loss_hit": False,
                                            "kpi_stop_signal": "",
                                            "tai_wr": 0,
                                            "xiu_wr": 0,
                                            "bias": "",
                                            "history_21": "",
                                            "kpi_mode": False,
                                            "cum_profit": 0,
                                            "cum_balance": logic.bankroll.total_balance if logic else 0,
                                            "cum_wins": 0,
                                            "cum_losses": 0,
                                            "cum_wr": 0,
                                            "total_balance": logic.bankroll.total_balance if logic else 0,
                                            "hist_size": 0,
                                            "skip_reason": "",
                                            "regime": "UNKNOWN",
                                            "session_quality": "UNKNOWN",
                                        }
                                        _write_show(error_row, "INVALID", event_time=event_time)
                                        console.print(f"[dim cyan]📝 Ghi CSV lỗi: sid={sid_1003}[/]")
                                    except Exception as csv_err:
                                        console.print(f"[dim red]⚠️ Lỗi ghi CSV: {csv_err}[/]")
                                    

                                    try:
                                        if sid_1003 > 0:
                                            history_row = {
                                                "time":    (event_time or datetime.now()).strftime("%H:%M:%S"),
                                                "sid":     sid_1003,
                                                "streak":  "—",
                                                "side":    "—",
                                                "tier":    "-",
                                                "amount":  0,
                                                "dice":    f"{d1}-{d2}-{d3}",
                                                "actual":  "—",
                                                "result":  "ERROR",
                                                "pnl":     0,
                                                "balance": logic.bankroll.stats().get("balance", 0),
                                                "reason":  "NoData",
                                                "score":   0,
                                            }
                                            ui.add_history(history_row)
                                            console.print(f"[yellow]⚠️ Added ERROR row to history[/]")
                                    except Exception as e2:
                                        console.print(f"[red]❌ Lỗi add ERROR history: {e2}[/]")


                                _clear_session_state()

                        except Exception as e:
                            import traceback
                            err_msg = str(e)
                            full_trace = traceback.format_exc()
                            console.print(f"[red]❌ Parse err: {err_msg}[/]")
                            ui.set_status(f"⚠️ Parse err: {err_msg[:70]}")

                            try:
                                with open("parse_err_traceback.log", "a", encoding="utf-8") as _ef:
                                    _ef.write(f"\n{'='*80}\n{datetime.now().isoformat()}\n")
                                    _ef.write(f"ERROR: {err_msg}\n")
                                    _ef.write(full_trace)
                                    _ef.write("\n")
                            except Exception as log_err:
                                console.print(f"[red]Không ghi được traceback: {log_err}[/]")
                            time.sleep(0.05)

            except KeyboardInterrupt:
                break
            except Exception as e:
                ui.set_status(f"❌ Loop error: {str(e)[:70]}")
                time.sleep(0.5)
                

    report = ReportGenerator.generate(logic, csv_w, mode)
    console.print(f"[bold green]📈 Tổng kết: Lãi {report['total_profit']:,.0f}đ, WR {report['win_rate']}%, KPI {report['kpi_hits']} lần[/]")
    status_writer.stop()
    csv_w.close()
    try:
        driver.quit()
    except Exception:
        pass
    console.print(f"\n✅ Xong! CSV: [cyan]{csv_file}[/]")



if __name__ == "__main__":
    os.system("cls" if os.name == "nt" else "clear")
    console.print("[bold cyan]╔══════════════════════════════════════════════════════════════════╗[/]")
    console.print("[bold cyan]║  🚀 SUNWIN SNIPER V6.4                                            ║[/]")
    console.print("[bold cyan]╚══════════════════════════════════════════════════════════════════╝[/]\n")

    if get_pygetwindow() is None or get_pyautogui() is None:
        console.print("[yellow]⚠️  AUTO cần: pip install pygetwindow pyautogui[/]\n")


    console.print("[bold white]─── 💰 VỐN BAN ĐẦU ───────────────────────────────────────[/]")
    bal = 10_000_000.0
    while True:
        try:
            raw = input("  Vốn ban đầu (VNĐ): ").strip()
            if raw.startswith("&") or ".ps1" in raw:
                console.print("  [red]⚠️  Nhập số tiền![/]")
                continue
            if raw == "":
                bal = 10_000_000.0
                break

            bal = float(re.sub(r'[^\d.]', '', raw))
            if bal <= 0:
                console.print("  [red]⚠️  Phải > 0[/]")
                continue
            break
        except ValueError:
            console.print("  [red]⚠️  Chỉ nhập số[/]")
        except (KeyboardInterrupt, EOFError):
            console.print("\n  [yellow]Nhập số rồi Enter.[/]")


    default_kpi = round(bal * 0.30)
    console.print(f"\n[bold white]─── 🎯 MỤC TIÊU KPI ─────────────────────────────────────[/]")
    console.print(f"  Vốn:        [bold green]{bal:,.0f}đ[/]")
    console.print(f"  KPI mặc định = +30% = [bold green]+{default_kpi:,.0f}đ[/]")
    console.print(f"  Đạt KPI → Khoá profit, carry overshoot, chu kỳ mới tự động.")
    kpi = float(default_kpi)
    while True:
        try:
            raw_k = input(f"\n  Mục tiêu KPI (VNĐ) [Enter = {default_kpi:,.0f}]: ").strip()
            if raw_k == "":
                kpi = float(default_kpi)
                break
            kpi = float(raw_k.replace(",", "").replace(".", ""))
            if kpi <= 0:
                console.print("  [red]⚠️  KPI phải > 0[/]")
                continue
            break
        except ValueError:
            console.print("  [red]⚠️  Chỉ nhập số[/]")
        except (KeyboardInterrupt, EOFError):
            pass


    console.print(f"\n[bold white]─── 🎯 SAU KHI ĐẠT KPI ─────────────────────────────────[/]")
    console.print(f"  [1]  TIẾP TỤC — Khoá profit, reset chu kỳ mới tự động")
    console.print(f"  [2]  DỪNG    — Tắt bot ngay sau khi đạt KPI")
    stop_kpi = False
    while True:
        try:
            raw_s = input("\n  Chọn (1/2) [Enter = 1]: ").strip()
            if raw_s in ("", "1"):
                stop_kpi = False
                break
            elif raw_s == "2":
                stop_kpi = True
                break
            else:
                console.print("  [red]⚠️  Chỉ 1 hoặc 2[/]")
        except (KeyboardInterrupt, EOFError):
            pass


    console.print(f"\n[bold white]─── ⚙️  CHẾ ĐỘ VẬN HÀNH ────────────────────────────────[/]")
    console.print(f"  💵 Vốn:    [bold green]{bal:,.0f}đ[/]")
    console.print(f"  🎯 KPI:    [bold green]+{kpi:,.0f}đ  (+{kpi/bal*100:.0f}%)[/]")
    console.print(f"  ⛔ Stop Loss: [red]-{bal*0.50:,.0f}đ  (-50%)[/]")
    console.print(f"  👻 Ghost:  4 thua liên   ❄️  Cooldown: 6 thua   🚨 Emergency: 8 thua\n")
    console.print("  [1]  MANUAL  — Xem tín hiệu, đặt tay")
    console.print("  [2]  AUTO   — Bot tự click")
    mode = "MANUAL"
    while True:
        try:
            raw_m = input("\n  Chọn chế độ (1/2) [Enter = 1]: ").strip()
            if raw_m in ("", "1"):
                mode = "MANUAL"
                break
            elif raw_m == "2":
                mode = "AUTO"
                break
            else:
                console.print("  [red]⚠️  Chỉ 1 hoặc 2[/]")
        except (KeyboardInterrupt, EOFError):
            pass

    kpi_mode_lbl = "🔒 DỪNG sau KPI" if stop_kpi else "♻️ Tiếp tục sau KPI"
    console.print(f"\n[bold green]✅ Chốt:[/] Vốn {bal:,.0f}đ │ KPI +{kpi:,.0f}đ │ {mode} │ {kpi_mode_lbl}")
    console.print("[bold cyan]👉 Đang mở Chrome...[/]\n")

    try:
        run(bal, kpi, mode, stop_on_kpi=stop_kpi)
    except KeyboardInterrupt:
        console.print("\n[yellow]⛔ Dừng an toàn.[/]")
    finally:




        pass
