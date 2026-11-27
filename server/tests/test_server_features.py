"""Pruebas para descubrimiento RTSP y alertas sostenidas."""
from __future__ import annotations

import os
import sys
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

import camera_discovery
import email_alerts
from camera_discovery import build_rtsp_url, candidate_addresses, discover_rtsp_hosts
from email_alerts import EmailAlertSender, EmailSettings, HelmetAlertTracker, _AlertJob


class CameraDiscoveryTests(unittest.TestCase):
    def test_rtsp_url_is_built_from_credentials_and_path(self) -> None:
        url = build_rtsp_url(
            "192.168.1.38",
            "admin@example.com",
            "p:a ss",
            path="Streaming/Channels/101",
        )
        self.assertEqual(
            url,
            "rtsp://admin%40example.com:p%3Aa%20ss@192.168.1.38:554/Streaming/Channels/101",
        )

    def test_candidates_are_limited_to_local_24_and_exclude_server(self) -> None:
        addresses = candidate_addresses("192.168.50.20")
        self.assertEqual(len(addresses), 253)
        self.assertNotIn("192.168.50.20", addresses)
        self.assertIn("192.168.50.1", addresses)
        self.assertIn("192.168.50.254", addresses)

    def test_discovery_returns_only_open_hosts_in_address_order(self) -> None:
        with (
            patch.object(camera_discovery, "candidate_addresses", return_value=[
                "192.168.1.11", "192.168.1.2", "192.168.1.8",
            ]),
            patch.object(
                camera_discovery,
                "rtsp_port_open",
                side_effect=lambda address, _port, _timeout: address.endswith((".11", ".2")),
            ),
        ):
            found = discover_rtsp_hosts("192.168.1.20", workers=3)
        self.assertEqual(found, ["192.168.1.2", "192.168.1.11"])

    def test_cancelled_discovery_does_not_schedule_hosts(self) -> None:
        stopping = threading.Event()
        stopping.set()
        with patch.object(camera_discovery, "candidate_addresses", return_value=["192.168.1.2"]):
            self.assertEqual(discover_rtsp_hosts("192.168.1.20", stopping=stopping), [])


class EmailAlertTests(unittest.TestCase):
    def test_tracker_alerts_once_after_five_continuous_seconds(self) -> None:
        tracker = HelmetAlertTracker(threshold_seconds=5, cooldown_seconds=300)
        self.assertFalse(tracker.update(True, now=10))
        self.assertFalse(tracker.update(True, now=14.99))
        self.assertTrue(tracker.update(True, now=15))
        self.assertFalse(tracker.update(True, now=100))

    def test_tracker_resets_when_detection_disappears(self) -> None:
        tracker = HelmetAlertTracker(threshold_seconds=5, cooldown_seconds=0)
        tracker.update(True, now=0)
        self.assertFalse(tracker.update(False, now=4))
        self.assertFalse(tracker.update(True, now=10))
        self.assertTrue(tracker.update(True, now=15))

    def test_email_settings_strip_spaces_from_app_password(self) -> None:
        values = {
            "VIGIWORK_SMTP_USER": "alerts@example.com",
            "VIGIWORK_SMTP_PASSWORD": "abcd efgh ijkl mnop",
            "VIGIWORK_ALERT_RECIPIENT": "safety@example.com",
        }
        with patch.dict(os.environ, values, clear=False):
            settings = EmailSettings.from_environment()
        assert settings is not None
        self.assertEqual(settings.password, "abcdefghijklmnop")
        self.assertEqual(settings.recipient, "safety@example.com")

    def test_sender_builds_authenticated_message_with_jpeg(self) -> None:
        sent_messages = []

        class FakeSmtp:
            def __enter__(self):
                return self

            def __exit__(self, _kind, _value, _traceback):
                return False

            def login(self, username: str, password: str) -> None:
                self.credentials = (username, password)

            def send_message(self, message) -> None:
                sent_messages.append((self.credentials, message))

        settings = EmailSettings(
            "smtp.example.com", 465, "alerts@example.com", "secret", "safety@example.com"
        )
        sender = EmailAlertSender(settings)
        try:
            job = _AlertJob(
                "Entrada",
                datetime(2026, 11, 26, 21, 0, tzinfo=timezone.utc),
                b"\xff\xd8test\xff\xd9",
            )
            with patch.object(email_alerts.smtplib, "SMTP_SSL", return_value=FakeSmtp()):
                sender._send(job)
        finally:
            sender.stop()

        self.assertEqual(sent_messages[0][0], ("alerts@example.com", "secret"))
        message = sent_messages[0][1]
        self.assertEqual(message["To"], "safety@example.com")
        self.assertIn("Entrada", message["Subject"])
        self.assertTrue(any(part.get_content_type() == "image/jpeg" for part in message.walk()))


if __name__ == "__main__":
    unittest.main()
