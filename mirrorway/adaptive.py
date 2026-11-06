"""Adaptive video quality: adjusts the H.264 bitrate and frame rate to the state of the connection.

Measurements (on the Mac, every second):
- Send queue: bytes waiting in the socket's send buffer (macOS SO_NWRITE, Linux TIOCOUTQ),
  divided by the recent send rate to get milliseconds. It grows when the network cannot keep up.
- Blocked share: how much of the sending thread's time is spent inside sendall().
  It rises when TCP is congested.
- Round trip: measured on the control channel; a clear rise over the minimum means queues are building up.

Rule: on congestion the bitrate drops by 30% (at the bottom, the frame rate drops); after 3 s without problems
the frame rate is restored first, then the bitrate rises by 15% if the encoder is using most of its budget.
No increase for 10 s after the last drop (prevents oscillation).
"""
from __future__ import annotations

import logging
import socket
import sys
import time
from typing import Optional

log = logging.getLogger("mirrorway.adaptive")

SO_NWRITE = 0x1024          # macOS: bytes in the send buffer


def unsent_bytes(sock: socket.socket) -> Optional[int]:
    """Bytes waiting (not yet acknowledged) in the socket's send buffer; None when it cannot be measured."""
    try:
        if sys.platform == "darwin":
            return int(sock.getsockopt(socket.SOL_SOCKET, SO_NWRITE))
        if sys.platform.startswith("linux"):
            import fcntl
            import struct
            import termios
            buf = fcntl.ioctl(sock.fileno(), termios.TIOCOUTQ, struct.pack("i", 0))
            return struct.unpack("i", buf)[0]
    except Exception:
        return None
    return None


FPS_STEPS = (60, 45, 30, 20, 15)


class RateController:
    QUEUE_MS = 80.0          # send queue longer than this = congestion
    BLOCKED = 0.35           # this share of time spent blocked in send = congestion
    RTT_RISE_MS = 60.0       # round trip this far above the minimum = congestion
    DECREASE = 0.7
    INCREASE = 1.15
    STABLE_S = 3.0
    HOLD_AFTER_DROP_S = 10.0
    USE_RATIO = 0.8          # send rate above this share of the target = the encoder is at its budget

    def __init__(self, start_kbps: int, min_kbps: int, max_kbps: int, max_fps: int, clock=time.monotonic):
        self.clock = clock
        self.min_kbps, self.max_kbps = int(min_kbps), int(max(max_kbps, start_kbps))
        self.kbps = int(min(max(start_kbps, self.min_kbps), self.max_kbps))
        self.max_fps = int(max_fps)
        self.fps = self.max_fps
        now = clock()
        self.good_since = now
        self.last_drop = -1e9
        self.base_rtt: Optional[float] = None
        self._rtt_strikes = 0
        self.decreases = 0
        self.increases = 0
        self.last_reason = ""

    def _fps_down(self) -> bool:
        for f in FPS_STEPS:
            if f < self.fps:
                self.fps = f
                return True
        return False

    def _fps_up(self) -> bool:
        if self.fps >= self.max_fps:
            return False
        ups = [f for f in FPS_STEPS if f > self.fps and f <= self.max_fps]
        self.fps = min(ups) if ups else self.max_fps
        return True

    def congestion(self, queue_ms: Optional[float], blocked: float, rtt_ms: Optional[float]) -> str:
        if rtt_ms is not None and rtt_ms > 0:
            self.base_rtt = rtt_ms if self.base_rtt is None else min(self.base_rtt, rtt_ms)
        if queue_ms is not None and queue_ms > self.QUEUE_MS:
            return f"send queue {queue_ms:.0f} ms"
        if blocked > self.BLOCKED:
            return f"blocked in send {blocked * 100:.0f}%"
        if rtt_ms is not None and self.base_rtt is not None and rtt_ms > self.base_rtt + self.RTT_RISE_MS:
            # Wi-Fi has the odd spike: count it only when two measurements in a row are high
            self._rtt_strikes += 1
            if self._rtt_strikes >= 2:
                self._rtt_strikes = 0
                return f"round trip {rtt_ms:.0f} ms (minimum {self.base_rtt:.0f})"
        else:
            self._rtt_strikes = 0
        return ""

    def update(self, queue_ms: Optional[float], blocked: float, rtt_ms: Optional[float],
               sent_kbps: float) -> bool:
        """Result of one measurement window; True if the target changed."""
        now = self.clock()
        reason = self.congestion(queue_ms, blocked, rtt_ms)
        if reason:
            self.good_since = now
            self.last_drop = now
            self.last_reason = reason
            if self.kbps > self.min_kbps:
                self.kbps = max(self.min_kbps, int(round(self.kbps * self.DECREASE)))
                self.decreases += 1
                return True
            if self._fps_down():
                self.decreases += 1
                return True
            return False
        if now - self.good_since < self.STABLE_S:
            return False
        if self._fps_up():
            self.good_since = now
            self.increases += 1
            self.last_reason = "connection recovered"
            return True
        if (self.kbps < self.max_kbps and sent_kbps >= self.USE_RATIO * self.kbps
                and now - self.last_drop >= self.HOLD_AFTER_DROP_S):
            self.kbps = min(self.max_kbps, int(round(self.kbps * self.INCREASE)))
            self.good_since = now
            self.increases += 1
            self.last_reason = "encoder at its budget, connection has room"
            return True
        return False
