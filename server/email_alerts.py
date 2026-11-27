"""Alertas SMTP asíncronas para episodios sostenidos sin casco."""
from __future__ import annotations

import os
import queue
import smtplib
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from collections.abc import Callable

import cv2


ALERT_AFTER_SECONDS = 5.0
ALERT_COOLDOWN_SECONDS = 300.0


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


@dataclass(frozen=True)
class EmailSettings:
    host: str
    port: int
    username: str
    password: str
    recipient: str
    use_ssl: bool = True

    @classmethod
    def from_environment(cls) -> "EmailSettings | None":
        username = os.environ.get("VIGIWORK_SMTP_USER", "").strip()
        password = os.environ.get("VIGIWORK_SMTP_PASSWORD", "").replace(" ", "").strip()
        recipient = os.environ.get("VIGIWORK_ALERT_RECIPIENT", username).strip()
        if not username or not password or not recipient:
            return None
        try:
            port = int(os.environ.get("VIGIWORK_SMTP_PORT", "465"))
        except ValueError:
            port = 465
        use_ssl = os.environ.get("VIGIWORK_SMTP_SSL", "1").strip().lower() not in {
            "0", "false", "no", "off",
        }
        return cls(
            host=os.environ.get("VIGIWORK_SMTP_HOST", "smtp.gmail.com").strip(),
            port=port,
            username=username,
            password=password,
            recipient=recipient,
            use_ssl=use_ssl,
        )


class HelmetAlertTracker:
    """Decide cuándo un episodio continuo debe generar una única alerta."""

    def __init__(
        self,
        threshold_seconds: float = ALERT_AFTER_SECONDS,
        cooldown_seconds: float = ALERT_COOLDOWN_SECONDS,
    ):
        self.threshold_seconds = max(0.1, threshold_seconds)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self.detected_since: float | None = None
        self.alerted_for_event = False
        self.last_alert_at: float | None = None

    def update(self, detected: bool, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        if not detected:
            self.detected_since = None
            self.alerted_for_event = False
            return False
        if self.detected_since is None:
            self.detected_since = current
            return False
        if self.alerted_for_event or current - self.detected_since < self.threshold_seconds:
            return False
        if self.last_alert_at is not None and current - self.last_alert_at < self.cooldown_seconds:
            return False
        self.alerted_for_event = True
        self.last_alert_at = current
        return True


@dataclass
class _AlertJob:
    camera_name: str
    captured_at: datetime
    jpeg: bytes


class EmailAlertSender:
    """Codifica y envía alertas fuera del hilo de captura de video."""

    def __init__(
        self,
        settings: EmailSettings,
        status_callback: Callable[[str, bool], None] | None = None,
    ):
        self.settings = settings
        self.status_callback = status_callback
        self._jobs: queue.Queue[_AlertJob | None] = queue.Queue(maxsize=1)
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="smtp-alerts")
        self._thread.start()

    def submit(self, camera_name: str, frame, captured_at: datetime | None = None) -> bool:
        encoded, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if not encoded:
            self._notify("No se pudo preparar la captura de la alerta.", False)
            return False
        job = _AlertJob(camera_name, captured_at or datetime.now().astimezone(), buffer.tobytes())
        try:
            self._jobs.put_nowait(job)
            return True
        except queue.Full:
            return False

    def stop(self) -> None:
        self._stopping.set()
        try:
            self._jobs.put_nowait(None)
        except queue.Full:
            try:
                self._jobs.get_nowait()
            except queue.Empty:
                pass
            try:
                self._jobs.put_nowait(None)
            except queue.Full:
                pass
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _notify(self, message: str, succeeded: bool) -> None:
        if not self._stopping.is_set() and self.status_callback is not None:
            self.status_callback(message, succeeded)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                self._send(job)
            except Exception as exc:
                self._notify(f"No se pudo enviar la alerta por correo: {exc}", False)
            else:
                self._notify(f"Alerta por falta de casco enviada para «{job.camera_name}».", True)

    def _send(self, job: _AlertJob) -> None:
        message = EmailMessage()
        message["Subject"] = f"VigiWork: persona sin casco · {job.camera_name}"
        message["From"] = self.settings.username
        message["To"] = self.settings.recipient
        timestamp = job.captured_at.strftime("%d/%m/%Y %H:%M:%S %Z")
        message.set_content(
            "VigiWork detectó de forma continua una persona sin casco durante más de "
            f"cinco segundos.\n\nCámara: {job.camera_name}\nFecha y hora: {timestamp}\n"
        )
        safe_name = "".join(character if character.isalnum() else "_" for character in job.camera_name)
        message.add_attachment(
            job.jpeg,
            maintype="image",
            subtype="jpeg",
            filename=f"alerta_{safe_name or 'camara'}.jpg",
        )
        if self.settings.use_ssl:
            with smtplib.SMTP_SSL(self.settings.host, self.settings.port, timeout=20) as smtp:
                smtp.login(self.settings.username, self.settings.password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(self.settings.host, self.settings.port, timeout=20) as smtp:
                smtp.starttls()
                smtp.login(self.settings.username, self.settings.password)
                smtp.send_message(message)


def alert_timing_from_environment() -> tuple[float, float]:
    return (
        _env_float("VIGIWORK_ALERT_AFTER_SECONDS", ALERT_AFTER_SECONDS, 0.1),
        _env_float("VIGIWORK_ALERT_COOLDOWN_SECONDS", ALERT_COOLDOWN_SECONDS, 0.0),
    )
