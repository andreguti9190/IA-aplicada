"""Descubrimiento acotado de posibles cámaras RTSP en la red local."""
from __future__ import annotations

import ipaddress
import socket
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from urllib.parse import quote


DEFAULT_RTSP_PORT = 554
DEFAULT_PREFIX_LENGTH = 24
MAX_SCAN_HOSTS = 1024


def build_rtsp_url(
    address: str,
    username: str,
    password: str,
    *,
    port: int = DEFAULT_RTSP_PORT,
    path: str = "/",
) -> str:
    """Construye una URL RTSP escapando credenciales y normalizando la ruta."""
    clean_path = path.strip() or "/"
    if not clean_path.startswith("/"):
        clean_path = f"/{clean_path}"
    encoded_user = quote(username.strip(), safe="")
    encoded_password = quote(password, safe="")
    credentials = f"{encoded_user}:{encoded_password}@" if encoded_user or encoded_password else ""
    return f"rtsp://{credentials}{address.strip()}:{int(port)}{clean_path}"


def local_ipv4() -> str:
    """Devuelve la IPv4 usada para salir a la red, sin enviar tráfico."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 80))
        return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def candidate_addresses(local_ip: str, prefix_length: int = DEFAULT_PREFIX_LENGTH) -> list[str]:
    """Lista hosts de una subred local con un límite deliberadamente pequeño."""
    address = ipaddress.ip_address(local_ip)
    if not isinstance(address, ipaddress.IPv4Address) or address.is_loopback:
        return []
    network = ipaddress.ip_network(f"{address}/{prefix_length}", strict=False)
    if network.num_addresses - 2 > MAX_SCAN_HOSTS:
        raise ValueError(f"La red {network} es demasiado grande para el descubrimiento automático.")
    return [str(host) for host in network.hosts() if host != address]


def rtsp_port_open(address: str, port: int = DEFAULT_RTSP_PORT, timeout: float = 0.35) -> bool:
    """Comprueba únicamente si el puerto TCP indicado acepta conexiones."""
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


def discover_rtsp_hosts(
    local_ip: str,
    *,
    port: int = DEFAULT_RTSP_PORT,
    prefix_length: int = DEFAULT_PREFIX_LENGTH,
    timeout: float = 0.35,
    workers: int = 64,
    stopping: threading.Event | None = None,
    on_found: Callable[[str], None] | None = None,
) -> list[str]:
    """Escanea la subred local y devuelve IPs cuyo puerto RTSP está abierto."""
    stop = stopping or threading.Event()
    addresses = candidate_addresses(local_ip, prefix_length)
    found: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 128))) as pool:
        futures = {
            pool.submit(rtsp_port_open, address, port, timeout): address
            for address in addresses
            if not stop.is_set()
        }
        for future in as_completed(futures):
            if stop.is_set():
                for pending in futures:
                    pending.cancel()
                break
            address = futures[future]
            try:
                is_open = future.result()
            except Exception:
                is_open = False
            if is_open:
                found.append(address)
                if on_found is not None:
                    on_found(address)
    return sorted(found, key=ipaddress.ip_address)
