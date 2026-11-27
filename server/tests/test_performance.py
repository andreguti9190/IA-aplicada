"""Pruebas del ajuste automático y del codificador asíncrono."""
from __future__ import annotations

import sys
import time
import unittest
from collections import deque
from pathlib import Path

import numpy as np


SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from app import AsyncStreamPublisher, _rolling_fps, _target_ai_rate


class FakeHub:
    def __init__(self):
        self.running = True
        self.frames: list[bytes] = []

    def publish(self, jpeg: bytes) -> None:
        self.frames.append(jpeg)


class PerformanceTests(unittest.TestCase):
    def test_rolling_fps_uses_arrival_times(self) -> None:
        samples: deque[float] = deque()
        measured = 0.0
        for index in range(31):
            measured = _rolling_fps(samples, index / 15.0)
        self.assertAlmostEqual(measured, 15.0, places=6)

    def test_target_rate_respects_camera_and_profile(self) -> None:
        self.assertAlmostEqual(_target_ai_rate(30.0, 10.0, 9), 10.0)
        self.assertAlmostEqual(_target_ai_rate(5.0, 10.0, 9), 5.0)
        self.assertAlmostEqual(_target_ai_rate(30.0, 10.0, 0), 8.2)

    def test_stream_encoder_publishes_jpeg_off_the_capture_thread(self) -> None:
        hub = FakeHub()
        publisher = AsyncStreamPublisher(hub)
        try:
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            publisher.submit(frame)
            deadline = time.monotonic() + 2
            while not hub.frames and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(hub.frames)
            self.assertTrue(hub.frames[0].startswith(b"\xff\xd8"))
            self.assertTrue(hub.frames[0].endswith(b"\xff\xd9"))
        finally:
            publisher.stop()


if __name__ == "__main__":
    unittest.main()
