#!/usr/bin/env python3
"""Cliente de escritorio para visualizar el video del servidor VigiWork."""
from __future__ import annotations

import re
import sys
import threading
import time
import json
import errno
import urllib.request
import urllib.error
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, QSettings, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QFrame, QLabel, QLineEdit, QMainWindow, QPushButton,
    QVBoxLayout, QWidget,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.theme import APP_STYLESHEET, app_icon, brand_header


CLIENT_STYLESHEET = """
    QWidget#clientRoot { background: #1F1F1F; }
    QFrame#clientPanel {
        background: #292929;
        border: 1px solid #414141;
        border-radius: 10px;
    }
    QLabel#connectionDot { color: #FF7A00; font-size: 16px; }
    QLabel#clientStatus { background: transparent; border: 0; padding: 0; }
    QLabel#clientHint { background: transparent; padding: 2px 4px; }
    QPushButton { min-height: 22px; }
    QPushButton:focus, QComboBox:focus { border: 1px solid #FF7A00; }
"""


class ServerConfigDialog(QDialog):
    server_requested = Signal(str, str)

    def __init__(self, initial_address: str = "", initial_key: str = "", parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle("Configurar servidor")
        self.setWindowIcon(app_icon())
        self.setStyleSheet(APP_STYLESHEET)
        self.setMinimumWidth(440)
        outer = QVBoxLayout(self)
        outer.addWidget(brand_header("Conectar al servidor", "Configurá el acceso al sistema de cámaras"))
        layout = QFormLayout()
        outer.addLayout(layout)
        self.address = QLineEdit(initial_address)
        self.address.setPlaceholderText("192.168.1.20:8765")
        layout.addRow("Dirección del servidor", self.address)
        self.access_key = QLineEdit(initial_key)
        self.access_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.access_key.setPlaceholderText("Clave de acceso del servidor")
        layout.addRow("Clave de acceso", self.access_key)
        self.status = QLabel("Ingresá la dirección que muestra la app servidor.")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        layout.addRow(self.status)
        buttons = QDialogButtonBox()
        self.connect_button = buttons.addButton("Conectar", QDialogButtonBox.ButtonRole.ActionRole)
        self.connect_button.setObjectName("primary")
        self.connect_button.clicked.connect(self._submit)
        buttons.addButton("Cancelar", QDialogButtonBox.ButtonRole.RejectRole).clicked.connect(self.reject)
        layout.addRow(buttons)

    def _submit(self) -> None:
        address = server_url(self.address.text())
        if not address:
            self.address.setFocus()
            return
        key = self.access_key.text().strip()
        if not key:
            self.access_key.setFocus()
            self.status.setText("Ingresá la clave que muestra la app servidor.")
            return
        self.server_requested.emit(address, key)
        self.status.setText("Conectando… La vista del cliente se abrirá en otra ventana.")

    def set_connection_status(self, message: str) -> None:
        self.status.setText(message)


def server_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    if not re.match(r"^https?://", value, re.I):
        value = f"http://{value}"
    return value


class ClientWorker(QObject):
    frame_available = Signal()
    status = Signal(str, str)
    finished = Signal()

    def __init__(self, base_url: str, access_key: str):
        super().__init__()
        self.base_url = base_url
        self.access_key = access_key
        self.stopping = threading.Event()
        self.response = None
        self._frame_lock = threading.Lock()
        self._latest_frame: QImage | None = None
        self._frame_signal_pending = False

    def take_latest_frame(self) -> QImage | None:
        with self._frame_lock:
            image = self._latest_frame
            self._latest_frame = None
            self._frame_signal_pending = False
            return image

    def _publish_frame(self, image: QImage) -> None:
        with self._frame_lock:
            self._latest_frame = image
            if self._frame_signal_pending:
                return
            self._frame_signal_pending = True
        self.frame_available.emit()

    def stop(self) -> None:
        self.stopping.set()
        response = self.response
        if response is not None:
            try:
                response.close()
            except OSError:
                pass

    def _stream(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/stream", headers={"Accept": "multipart/x-mixed-replace", "Authorization": f"Bearer {self.access_key}"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.response = response
            try:
                content_type = response.headers.get("Content-Type", "")
                if "multipart/x-mixed-replace" not in content_type:
                    raise RuntimeError("El servidor no entregó un flujo MJPEG válido.")
                while not self.stopping.is_set():
                    line = response.readline()
                    if not line:
                        raise ConnectionError("El servidor cerró el video.")
                    if not line.startswith(b"--frame"):
                        continue
                    headers: dict[str, str] = {}
                    while True:
                        header = response.readline()
                        if not header or header in (b"\r\n", b"\n"):
                            break
                        key, separator, value = header.decode("latin-1").partition(":")
                        if separator:
                            headers[key.strip().lower()] = value.strip()
                    length = int(headers.get("content-length", "0"))
                    if length <= 0 or length > 20_000_000:
                        raise RuntimeError("El servidor envió un cuadro inválido.")
                    payload = response.read(length)
                    if len(payload) != length:
                        raise ConnectionError("Se cortó un cuadro de video.")
                    response.readline()  # CRLF al final de cada parte.
                    image = QImage.fromData(payload, "JPEG")
                    if not image.isNull():
                        self._publish_frame(image)
            finally:
                self.response = None

    def run(self) -> None:
        try:
            while not self.stopping.is_set():
                try:
                    request = urllib.request.Request(f"{self.base_url}/status", headers={"Authorization": f"Bearer {self.access_key}"})
                    with urllib.request.urlopen(request, timeout=4) as response:
                        status = json.loads(response.read().decode("utf-8"))
                    if not status.get("ready"):
                        raise ConnectionError("El servidor está activo, pero todavía no recibe video de la cámara.")
                    self.status.emit("Conectado al servidor", "live")
                    self._stream()
                except Exception as exc:
                    if self.stopping.is_set():
                        break
                    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                    refused = (
                        getattr(reason, "winerror", None) == 10061
                        or getattr(reason, "errno", None) in (errno.ECONNREFUSED, 10061)
                    )
                    if refused:
                        message = (
                            f"El servidor no responde en {self.base_url}. "
                            "Iniciá Probar en el servidor y verificá esta dirección. Reintentando en 2 s…"
                        )
                    else:
                        message = f"Se perdió el video ({exc}). Reintentando en 2 s…"
                    self.status.emit(message, "working")
                    self.stopping.wait(2)
        finally:
            self.finished.emit()


class CameraRequestWorker(QObject):
    finished = Signal(object, str)

    def __init__(self, base_url: str, access_key: str, action: str, camera_id: str = ""):
        super().__init__()
        self.base_url = base_url
        self.access_key = access_key
        self.action = action
        self.camera_id = camera_id

    def run(self) -> None:
        try:
            if self.action == "list":
                request = urllib.request.Request(f"{self.base_url}/cameras", headers={"Authorization": f"Bearer {self.access_key}"})
            else:
                payload = json.dumps({"id": self.camera_id}).encode("utf-8")
                request = urllib.request.Request(
                    f"{self.base_url}/select", data=payload,
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.access_key}"}, method="POST",
                )
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
            self.finished.emit(result, "")
        except Exception as exc:
            self.finished.emit(None, str(exc))


class VideoSurface(QLabel):
    """Dibuja el cuadro original ajustado a la ventana sin crear pixmaps por cuadro."""

    def __init__(self, placeholder: str, parent=None):
        super().__init__(placeholder, parent)
        self._frame: QImage | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_frame(self, image: QImage) -> None:
        self._frame = image
        self.setText("")
        self.update()

    def clear_frame(self, message: str) -> None:
        self._frame = None
        self.setText(message)
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        image = self._frame
        if image is None or image.isNull():
            return
        area = QRectF(self.contentsRect())
        if area.width() <= 0 or area.height() <= 0:
            return
        scale = min(area.width() / image.width(), area.height() / image.height())
        width = image.width() * scale
        height = image.height() * scale
        target = QRectF(
            area.x() + (area.width() - width) / 2,
            area.y() + (area.height() - height) / 2,
            width,
            height,
        )
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(target, image)


class VideoWindow(QDialog):
    """Ventana independiente para el video en vivo y sus FPS."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setWindowTitle("VigiWork · Video en vivo")
        self.setWindowIcon(app_icon())
        self.setStyleSheet(APP_STYLESHEET)
        self.setMinimumSize(720, 440)
        self.resize(1120, 700)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(12)
        header = QHBoxLayout()
        title = QLabel("VIDEO EN VIVO")
        title.setObjectName("eyebrow")
        header.addWidget(title)
        header.addStretch()
        self.fps = QLabel("0 FPS")
        self.fps.setObjectName("fps")
        header.addWidget(self.fps)
        self.fullscreen_button = QPushButton("Pantalla completa")
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        header.addWidget(self.fullscreen_button)
        layout.addLayout(header)

        self.video = VideoSurface("Esperando video del servidor…")
        self.video.setObjectName("video")
        self.video.setMinimumSize(640, 360)
        layout.addWidget(self.video, 1)

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self.fullscreen_button.setText("Pantalla completa")
        else:
            self.showFullScreen()
            self.fullscreen_button.setText("Salir de pantalla completa")

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_F11:
            self.toggle_fullscreen()
            return
        if event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.showNormal()
            self.fullscreen_button.setText("Pantalla completa")
            return
        super().keyPressEvent(event)

    def set_frame(self, image: QImage) -> None:
        self.video.set_frame(image)


class ClientWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VigiWork · Cliente")
        self.setWindowIcon(app_icon())
        self.resize(900, 500)
        self.thread: QThread | None = None
        self.worker: ClientWorker | None = None
        self.camera_request_thread: QThread | None = None
        self.camera_request_worker: CameraRequestWorker | None = None
        self.pending_camera_action = ""
        self.previewing = False
        self.frames = 0
        self.fps_started = 0.0
        self.current_frame: QImage | None = None
        self.video_window: VideoWindow | None = None
        self.settings = QSettings("VigiWork", "VigiWorkClient")
        old_settings = QSettings("CiberBoom", "RTSPScoutClient")
        if not self.settings.value("server/address", ""):
            previous_address = old_settings.value("server/address", "")
            if previous_address:
                self.settings.setValue("server/address", previous_address)
        self.access_key = str(self.settings.value("server/access_key", ""))
        self.base_url = ""
        self.connection_window: ServerConfigDialog | None = None
        self._initial_config = False
        self._handoff_to_config = False
        self._build_ui()
        QTimer.singleShot(0, self._initialize_server)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("clientRoot")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)

        header = QHBoxLayout()
        header.setSpacing(20)
        header.addWidget(brand_header("Monitor de cámaras", "Vista en vivo del sistema de seguridad"), 1)
        self.disconnect_button = QPushButton("Desconectar del servidor")
        self.disconnect_button.setObjectName("danger")
        self.disconnect_button.clicked.connect(self.handle_server_button)
        header.addWidget(self.disconnect_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header)

        status_panel = QFrame()
        status_panel.setObjectName("clientPanel")
        status_row = QHBoxLayout(status_panel)
        status_row.setContentsMargins(18, 14, 18, 14)
        status_row.setSpacing(12)
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("connectionDot")
        status_row.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignTop)
        self.status = QLabel("Ingresá la dirección que muestra la app servidor.")
        self.status.setObjectName("clientStatus")
        self.status.setWordWrap(True)
        status_row.addWidget(self.status, 1)
        layout.addWidget(status_panel)

        camera_panel = QFrame()
        camera_panel.setObjectName("clientPanel")
        camera_layout = QVBoxLayout(camera_panel)
        camera_layout.setContentsMargins(20, 18, 20, 20)
        camera_layout.setSpacing(12)
        cameras_heading = QLabel("Cámaras disponibles")
        cameras_heading.setObjectName("pageTitle")
        camera_layout.addWidget(cameras_heading)
        cameras_row = QHBoxLayout()
        cameras_row.setSpacing(10)
        self.cameras = QComboBox()
        self.cameras.setMinimumWidth(240)
        cameras_row.addWidget(self.cameras, 1)
        self.refresh_cameras_button = QPushButton("Actualizar lista")
        self.refresh_cameras_button.clicked.connect(self.refresh_cameras)
        cameras_row.addWidget(self.refresh_cameras_button)
        self.select_camera_button = QPushButton("Usar cámara")
        self.select_camera_button.setObjectName("primary")
        self.select_camera_button.clicked.connect(self.select_camera)
        cameras_row.addWidget(self.select_camera_button)
        camera_layout.addLayout(cameras_row)
        layout.addWidget(camera_panel)

        self.video_hint = QLabel("Elegí una cámara y presioná «Usar cámara» para abrir el video en una ventana independiente.")
        self.video_hint.setObjectName("clientHint")
        self.video_hint.setWordWrap(True)
        layout.addWidget(self.video_hint)
        layout.addStretch(1)
        self.setCentralWidget(root)
        self.setStyleSheet(APP_STYLESHEET + CLIENT_STYLESHEET)

    def connect_server(self, address: str | None = None, access_key: str | None = None) -> None:
        base_url = server_url(address if address is not None else self.base_url)
        if not base_url:
            self.status.setText("Ingresá la dirección del servidor.")
            return
        key = (access_key if access_key is not None else self.access_key).strip()
        if not key:
            self.status.setText("Ingresá la clave de acceso que muestra el servidor.")
            return
        if not self.disconnect_server():
            self.status.setText("Esperando que termine la conexión anterior…")
            return
        self.base_url = base_url
        self.access_key = key
        self.settings.setValue("server/address", base_url)
        self.settings.setValue("server/access_key", key)
        self.frames = 0
        self.fps_started = time.monotonic()
        self._set_video_fps("0 FPS")
        self.status.setText("Conectando al servidor…")
        self.disconnect_button.setText("Desconectar del servidor")
        self.disconnect_button.setObjectName("danger")
        thread = QThread(self)
        worker = ClientWorker(base_url, key)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.frame_available.connect(self.show_latest_frame)
        worker.status.connect(self.show_status)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._thread_finished)
        self.thread, self.worker = thread, worker
        thread.start()
        self.refresh_cameras()

    def _initialize_server(self) -> None:
        saved_address = str(self.settings.value("server/address", "")).strip()
        if saved_address and self.access_key:
            self.show()
            self.connect_server(saved_address, self.access_key)
            return

        self._initial_config = True
        self.open_connection_window()

    def open_connection_window(self) -> None:
        if self.connection_window and self.connection_window.isVisible():
            self.connection_window.raise_()
            self.connection_window.activateWindow()
            return
        self.connection_window = ServerConfigDialog(
            self.base_url or str(self.settings.value("server/address", "")), self.access_key
        )
        self.connection_window.server_requested.connect(self._connect_from_window)
        self.connection_window.rejected.connect(self._connection_window_cancelled)
        self.connection_window.finished.connect(self._connection_window_finished)
        self.connection_window.show()

    def _connect_from_window(self, address: str, access_key: str) -> None:
        self.connect_server(address, access_key)
        self.show()
        self._initial_config = False

    def handle_server_button(self) -> None:
        if self.worker and self.thread and self.thread.isRunning():
            if not self.disconnect_server():
                self.status.setText("Esperando que termine la conexión…")
                return
            self.open_connection_window()
            self._handoff_to_config = True
            self.close()
            self._handoff_to_config = False
            return
        self.open_connection_window()

    def _connection_window_cancelled(self) -> None:
        if not self.isVisible():
            self.show()
            self.status.setText("Conectate a un servidor para ver sus cámaras.")
            self.disconnect_button.setText("Configurar servidor")
            self.disconnect_button.setObjectName("primary")
            self.disconnect_button.style().unpolish(self.disconnect_button)
            self.disconnect_button.style().polish(self.disconnect_button)
        self._initial_config = False

    def _connection_window_finished(self, _result: int) -> None:
        self.connection_window = None

    def _start_preview(self, base_url: str) -> None:
        self._ensure_video_window()
        self.video_window.show()
        self.video_window.raise_()
        self.video_window.activateWindow()
        if self.thread and self.thread.isRunning():
            return
        self.base_url = base_url
        self.frames = 0
        self.fps_started = time.monotonic()
        self.video_window.video.clear_frame("Conectando al video…")
        self._set_video_fps("0 FPS")
        self.status.setText("Conectando al video…")
        thread = QThread(self)
        worker = ClientWorker(base_url, self.access_key)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.frame_available.connect(self.show_latest_frame)
        worker.status.connect(self.show_status)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._thread_finished)
        self.thread, self.worker = thread, worker
        thread.start()
        self.previewing = True

    def refresh_cameras(self) -> None:
        base_url = server_url(self.base_url)
        if not base_url:
            return
        self._request_camera_action(base_url, "list")

    def select_camera(self) -> None:
        base_url = server_url(self.base_url)
        camera_id = self.cameras.currentData()
        if not base_url or not camera_id:
            self.status.setText("Elegí una cámara guardada en el servidor.")
            return
        self._request_camera_action(base_url, "select", camera_id)

    def _request_camera_action(self, base_url: str, action: str, camera_id: str = "") -> None:
        thread = self.camera_request_thread
        if thread is not None and thread.isRunning():
            return
        thread = QThread(self)
        worker = CameraRequestWorker(base_url, self.access_key, action, camera_id)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._camera_request_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._camera_request_thread_finished)
        self.camera_request_thread, self.camera_request_worker = thread, worker
        self.pending_camera_action = action
        self.refresh_cameras_button.setEnabled(False)
        self.select_camera_button.setEnabled(False)
        thread.start()

    def _camera_request_finished(self, result, error: str) -> None:
        action, self.pending_camera_action = self.pending_camera_action, ""
        self.refresh_cameras_button.setEnabled(True)
        self.select_camera_button.setEnabled(True)
        if error:
            if "401" in error or "Unauthorized" in error:
                message = "Clave incorrecta o faltante. Copiá la clave desde la app servidor."
                self.status.setText(message)
                if self.connection_window and self.connection_window.isVisible():
                    self.connection_window.set_connection_status(message)
                return
            if "10061" in error or "refused" in error.lower():
                address = server_url(self.base_url)
                self.status.setText(
                    f"El servidor no responde en {address}. Tocá Probar en el servidor y verificá su dirección."
                )
            else:
                self.status.setText(f"No se pudo conectar con el servidor: {error}")
            return
        if result and "cameras" in result:
            selected = result.get("selected", "")
            self.cameras.blockSignals(True)
            self.cameras.clear()
            self.cameras.addItem("Seleccioná una cámara…", None)
            selected_index = 0
            for camera in result["cameras"]:
                self.cameras.addItem(camera["name"], camera["id"])
                if camera["id"] == selected:
                    selected_index = self.cameras.count() - 1
            self.cameras.setCurrentIndex(selected_index)
            self.cameras.blockSignals(False)
            self.status.setText(f"{len(result['cameras'])} cámara(s) disponibles en el servidor." if result["cameras"] else "El servidor todavía no tiene cámaras guardadas.")
            if action == "list" and self.connection_window and self.connection_window.isVisible():
                self.connection_window.close()
        elif result and result.get("ok"):
            if action == "select":
                self._start_preview(server_url(self.base_url))
                self.status.setText("Cámara solicitada al servidor; esperando video…")
        else:
            self.status.setText("El servidor devolvió una respuesta inesperada.")

    def _camera_request_thread_finished(self) -> None:
        self.camera_request_thread = None
        self.camera_request_worker = None

    def show_latest_frame(self) -> None:
        worker = self.worker
        image = worker.take_latest_frame() if worker is not None else None
        if image is None:
            return
        self.current_frame = image
        self._fit_video_frame()
        self.frames += 1
        elapsed = time.monotonic() - self.fps_started
        if elapsed >= 1:
            self._set_video_fps(f"{self.frames / elapsed:.1f} FPS")
            self.frames = 0
            self.fps_started = time.monotonic()

    def _ensure_video_window(self) -> None:
        if self.video_window is None:
            self.video_window = VideoWindow(self)
        self.video_window.show()

    def _set_video_fps(self, value: str) -> None:
        if self.video_window is not None:
            self.video_window.fps.setText(value)

    def _fit_video_frame(self) -> None:
        if self.video_window is not None and self.current_frame is not None:
            self.video_window.set_frame(self.current_frame)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_video_frame()

    def show_status(self, message: str, kind: str) -> None:
        self.status.setText(message)
        if self.connection_window and self.connection_window.isVisible():
            self.connection_window.set_connection_status(message)
        color = "#80D7A4" if kind == "live" else "#FF7A00"
        self.status_dot.setStyleSheet(f"color: {color}; background: transparent;")
        self.status.setStyleSheet("background: transparent; border: 0; padding: 0;")
        if kind != "live":
            self.frames = 0
            self.fps_started = time.monotonic()
            self._set_video_fps("0 FPS")

    def disconnect_server(self) -> bool:
        worker, thread = self.worker, self.thread
        if worker and thread and thread.isRunning():
            worker.stop()
            thread.quit()
            thread.wait(1200)
            if thread.isRunning():
                return False
        self.worker = None
        self.thread = None
        self.previewing = False
        self.disconnect_button.setText("Configurar servidor")
        self.disconnect_button.setObjectName("primary")
        self.disconnect_button.style().unpolish(self.disconnect_button)
        self.disconnect_button.style().polish(self.disconnect_button)
        if self.video_window is not None:
            self.video_window.video.clear_frame("Esperando conexión al servidor…")
            self.video_window.video.setMinimumHeight(360)
            self.video_window.hide()
        self.current_frame = None
        self._set_video_fps("0 FPS")
        self.status.setText("Desconectado")
        self.status.setStyleSheet("")
        self.status_dot.setStyleSheet("color: #777777; background: transparent;")
        return True

    def _thread_finished(self) -> None:
        self.thread = None
        self.worker = None
        self.previewing = False
        if not self._handoff_to_config:
            self.disconnect_button.setText("Configurar servidor")
            self.disconnect_button.setObjectName("primary")
            self.disconnect_button.style().unpolish(self.disconnect_button)
            self.disconnect_button.style().polish(self.disconnect_button)

    def closeEvent(self, event) -> None:
        if self._handoff_to_config:
            event.accept()
            return
        if self.disconnect_server():
            if self.connection_window and self.connection_window.isVisible():
                self.connection_window.close()
            event.accept()
        else:
            event.ignore()


def main() -> None:
    app = QApplication(sys.argv)
    window = ClientWindow()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
