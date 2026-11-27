"""Pruebas de integración locales para la API HTTP de VigiWork."""
from __future__ import annotations

import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from video_server import VideoHub


class VideoServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = VideoHub()
        self.hub.set_access_key("test-secret")
        self.selected: list[tuple[str, bool]] = []
        self.hub.select_callback = lambda url, ai: self.selected.append((url, ai))
        self.hub.set_cameras([
            {
                "name": "Entrada",
                "url": "rtsp://camera.local/live",
                "ai_enabled": True,
            }
        ])
        self.hub.start(host="127.0.0.1", port=0)
        assert self.hub.server is not None
        self.base_url = f"http://127.0.0.1:{self.hub.server.server_address[1]}"
        self.headers = {"Authorization": "Bearer test-secret"}

    def tearDown(self) -> None:
        self.hub.stop()

    def request_json(self, path: str, *, data: dict | None = None) -> dict:
        payload = json.dumps(data).encode("utf-8") if data is not None else None
        headers = dict(self.headers)
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=payload,
            headers=headers,
            method="POST" if payload is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_authentication_list_selection_and_status(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(f"{self.base_url}/status", timeout=2)
        self.assertEqual(context.exception.code, 401)

        cameras = self.request_json("/cameras")
        camera_id = cameras["cameras"][0]["id"]
        selection = self.request_json("/select", data={"id": camera_id})
        status = self.request_json("/status")

        self.assertTrue(selection["ok"])
        self.assertEqual(status["selected"], camera_id)
        self.assertEqual(self.selected, [("rtsp://camera.local/live", True)])

    def test_stream_returns_the_published_jpeg(self) -> None:
        jpeg = b"\xff\xd8vigiwork-test\xff\xd9"
        self.hub.publish(jpeg)
        request = urllib.request.Request(
            f"{self.base_url}/stream",
            headers=self.headers,
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertIn(
                "multipart/x-mixed-replace",
                response.headers.get("Content-Type", ""),
            )
            self.assertEqual(response.readline(), b"--frame\r\n")
            part_headers: dict[bytes, bytes] = {}
            while True:
                line = response.readline()
                if line == b"\r\n":
                    break
                key, value = line.split(b":", 1)
                part_headers[key.lower()] = value.strip()
            length = int(part_headers[b"content-length"])
            self.assertEqual(response.read(length), jpeg)


if __name__ == "__main__":
    unittest.main()
