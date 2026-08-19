









import time
from typing import Tuple, Optional, Dict, Any
from threading import Lock

SESSION_TOTAL_S = 50.0


class TimerManager:
    """
    Manages session timer and remaining time (RMT) calculations.
    Ensures monotonic, non-freezing timer updates.
    
    Thread-safe with internal locking.
    """
    
    def __init__(self, session_total_s: float = SESSION_TOTAL_S):
        self.SESSION_TOTAL_S = session_total_s
        self._lock = Lock()
        

        self._market_rmt = 0.0
        self._market_ts = 0.0
        self._sid = 0
        

        self._session_mono = 0.0
        self._fallback_active = False
        
    def update_market_rmt(self, sid: int, rmt_ms: int) -> None:
        """
        Update timer from market data (rmT field from WebSocket).
        
        Args:
            sid: Session ID (ignored if same)
            rmt_ms: Remaining time in milliseconds from server
        """
        if rmt_ms <= 0:
            return
        
        with self._lock:
            self._market_rmt = rmt_ms / 1000.0
            self._market_ts = time.monotonic()
            self._sid = sid
            self._session_mono = 0.0
            self._fallback_active = False
    
    def set_session_start(self, mono_ts: Optional[float] = None) -> None:
        """
        Manually set session start (fallback mode).
        Used when market doesn't provide rmT yet.
        
        Args:
            mono_ts: Monotonic timestamp (default: now)
        """
        with self._lock:
            self._session_mono = mono_ts or time.monotonic()
            self._fallback_active = True
    
    def get_remaining_time(self) -> float:
        """
        Calculate remaining time robustly.
        
        Returns:
            Remaining seconds (0.0 to SESSION_TOTAL_S)
            Never returns negative values.
        """
        with self._lock:

            if self._market_ts > 0:
                elapsed = time.monotonic() - self._market_ts
                rmt = max(0.0, self._market_rmt - elapsed)
                return rmt
            

            if self._fallback_active and self._session_mono > 0:
                elapsed = time.monotonic() - self._session_mono
                rmt = max(0.0, self.SESSION_TOTAL_S - elapsed)
                return rmt
            

            return 0.0
    
    def get_timer_display(self) -> Tuple[float, str, str]:
        """
        Get display data for RADAR UI timer.
        
        Returns:
            (remaining_seconds: float,
             color: str ('bright_green'|'yellow'|'red'),
             bar: str (colored bar with █ and ░))
        """
        rmt = self.get_remaining_time()
        

        if rmt >= 30:
            color = "bold bright_green"
        elif rmt >= 15:
            color = "bold yellow"
        else:
            color = "bold red"
        

        timer_width = max(0, min(26, int(rmt / self.SESSION_TOTAL_S * 26)))
        bar = (f"[{color}]" + 
               "█" * timer_width + 
               "[/][dim]" + 
               "░" * (26 - timer_width) + 
               "[/]")
        
        return rmt, color, bar
    
    def get_betting_window(self,
                          bet_window_min: float = 7.0,
                          bet_window_max: float = 50.0,
                          execution_start: float = 50.0,
                          execution_end: float = 7.0,
                          streak_cnt: int = 0) -> Dict[str, Any]:
        """
        Determine betting window status.
        
        Args:
            bet_window_min: Minimum RMT to place bet (seconds)
            bet_window_max: Maximum RMT to place bet (seconds)
            execution_start: Start execution window (RMT seconds)
            execution_end: End execution window (RMT seconds)
            streak_cnt: Current streak count — SC6+ gets wider window [V22]
        
        Returns:
            {
                'in_window': bool,
                'can_execute': bool,
                'is_rest': bool,
                'rmt': float,
                'phase': str ('EARLY'|'WINDOW'|'LATE'|'REST')
            }

        [V22] SC6+ window expansion:
            SC6+ has 100% WR (data-validated) — use wider window 3s→57s
            to capture early-session ultra-streak signals that were being
            missed when rmt was outside the standard 7s-50s window.
        """

        if streak_cnt >= 6:
            bet_window_min = 3.0
            bet_window_max = 57.0
            execution_start = 57.0
            execution_end = 3.0

        rmt = self.get_remaining_time()
        

        if rmt == 0:
            phase = "REST"
            in_window = False
            can_execute = False
            is_rest = True
        elif rmt > bet_window_max or rmt < bet_window_min:
            phase = "EARLY" if rmt > bet_window_max else "LATE"
            in_window = False
            can_execute = False
            is_rest = True
        else:
            phase = "WINDOW"
            in_window = True
            can_execute = (rmt <= execution_start and rmt > execution_end)
            is_rest = False
        
        return {
            'in_window': in_window,
            'can_execute': can_execute,
            'is_rest': is_rest,
            'rmt': rmt,
            'phase': phase,
        }
    
    def should_skip_heavy_calc(self, threshold_s: float = 4.0) -> bool:
        """
        Check if remaining time is too short for heavy calculations.
        
        Used to avoid late-heavy execution when Kalman, State modeling, etc.
        are too slow and would cause lệnh nhồi trễ (late order placement).
        
        Args:
            threshold_s: Threshold in seconds (default 4.0)
        
        Returns:
            True if time < threshold (skip heavy calc), False if ok to proceed
        """
        rmt = self.get_remaining_time()
        return rmt < threshold_s and rmt > 0.5
    
    def reset(self) -> None:
        """Reset timer state completely."""
        with self._lock:
            self._market_rmt = 0.0
            self._market_ts = 0.0
            self._sid = 0
            self._session_mono = 0.0
            self._fallback_active = False
    
    def get_state(self) -> Dict[str, Any]:
        """Get current timer state for debugging."""
        with self._lock:
            return {
                'market_rmt': self._market_rmt,
                'market_ts': self._market_ts,
                'sid': self._sid,
                'session_mono': self._session_mono,
                'fallback_active': self._fallback_active,
                'current_rmt': self.get_remaining_time(),
            }



_timer_instance: Optional[TimerManager] = None
_timer_lock = Lock()


def get_timer() -> TimerManager:
    """Get or create global timer manager."""
    global _timer_instance
    if _timer_instance is None:
        with _timer_lock:
            if _timer_instance is None:
                _timer_instance = TimerManager()
    return _timer_instance


def reset_timer() -> None:
    """Reset global timer."""
    global _timer_instance
    if _timer_instance:
        _timer_instance.reset()


__all__ = [
    'TimerManager',
    'get_timer',
    'reset_timer',
    'SESSION_TOTAL_S',
]
