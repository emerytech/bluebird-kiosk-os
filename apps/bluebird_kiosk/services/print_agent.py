"""Background agent that prints visitor badges on the school LAN.

A Legacy Wall / Beacon kiosk is already on the school network and already holds the device license
bearer, so it can reach a NAT'd LAN label printer that the cloud cannot. This agent polls the cloud
``GET <backend>/api/public/kiosk/visitor/print-jobs`` (device-license bearer) for pending badge
jobs, sends each job's ``payload`` (ZPL) to the configured network printer over a raw TCP socket
(``printer_host:printer_port``), and acks the result via ``POST .../print-jobs/ack``.

Tenant isolation is the backend's responsibility: the bearer resolves server-side to exactly one
tenant, and only that tenant's jobs are ever returned/ackable. The endpoints live under
``/api/public/`` so the dashed-host 308 redirect (which strips the Authorization header on a
cross-host hop) never fires — we also pin ``allow_redirects=False``, mirroring the IncidentPoller.

Gated OFF by default: only a kiosk with ``PRINT_AGENT_ENABLED=1`` in ``kiosk.conf`` acts as the
print agent, so a school designates ONE box as the bridge (checked live each cycle, no restart).
Fails safe: a print/network error acks the job failed and moves on; the loop never dies.
"""
from __future__ import annotations

import logging
import socket
import threading
from typing import Any, Dict, Optional, Tuple

import requests

from .. import __version__, config

logger = logging.getLogger("bluebird-kiosk.printagent")

# How often to pull pending jobs. Kept snappy so a badge prints within a few seconds of check-in.
POLL_INTERVAL_SEC = 4
REQUEST_TIMEOUT_SEC = 8
# Raw-TCP send to the label printer.
PRINTER_CONNECT_TIMEOUT_SEC = 6
PRINTER_WRITE_TIMEOUT_SEC = 10
MAX_JOBS_PER_POLL = 10

_TRUTHY = {"1", "true", "yes", "on"}


def _enabled(cfg: Dict[str, str]) -> bool:
    return str(cfg.get("PRINT_AGENT_ENABLED", "")).strip().lower() in _TRUTHY


class PrintAgent:
    """Owns a daemon thread that pulls badge jobs and prints them to the LAN printer."""

    def __init__(self, *, interval_sec: int = POLL_INTERVAL_SEC) -> None:
        self._interval = max(1, int(interval_sec))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="print-agent", daemon=True)
        self._thread.start()
        logger.info("print agent started (interval=%ss)", self._interval)

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # never let the loop die on an unexpected error
                logger.exception("print agent loop error")
            self._stop.wait(self._interval)

    def poll_once(self) -> int:
        """One cycle: if enabled + enrolled, pull pending jobs, print each, ack. Returns the
        number of jobs handled (0 when disabled / not enrolled / nothing pending)."""
        cfg = config.read_config()
        if not _enabled(cfg):
            return 0
        backend = (cfg.get("BLUEBIRD_BACKEND") or "").rstrip("/")
        token = config.read_license_token()
        if not backend or not token:
            return 0
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": f"BlueBirdKiosk/{__version__}",
        }
        try:
            resp = requests.get(
                f"{backend}/api/public/kiosk/visitor/print-jobs",
                params={"limit": MAX_JOBS_PER_POLL},
                headers=headers,
                timeout=REQUEST_TIMEOUT_SEC,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            logger.debug("print agent: poll network error: %s", exc)
            return 0
        if resp.status_code != 200:
            logger.debug("print agent: poll status=%s", resp.status_code)
            return 0
        try:
            data = resp.json()
        except ValueError:
            return 0
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not jobs:
            return 0
        handled = 0
        for job in jobs:
            if self._stop.is_set():
                break
            job_id = job.get("id")
            if job_id is None:
                continue
            ok, err = self._print_job(job)
            self._ack(backend, headers, int(job_id), ok, err)
            handled += 1
        return handled

    def _print_job(self, job: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Send one job's ZPL payload to its printer over raw TCP. Returns (ok, error)."""
        host = str(job.get("printer_host") or "").strip()
        if not host:
            return False, "no_printer_host"
        try:
            port = int(job.get("printer_port") or 9100)
        except (TypeError, ValueError):
            port = 9100
        payload = job.get("payload") or ""
        data = payload.encode("utf-8", "replace") if isinstance(payload, str) else bytes(payload)
        try:
            with socket.create_connection((host, port), timeout=PRINTER_CONNECT_TIMEOUT_SEC) as sock:
                sock.settimeout(PRINTER_WRITE_TIMEOUT_SEC)
                sock.sendall(data)
            logger.info(
                "print agent: sent badge job %s to %s:%s (%d bytes)",
                job.get("id"), host, port, len(data),
            )
            return True, None
        except OSError as exc:
            logger.warning("print agent: send to %s:%s failed: %s", host, port, exc)
            return False, ("printer_error: %s" % exc)[:400]

    def _ack(self, backend: str, headers: Dict[str, str], job_id: int, ok: bool,
             error: Optional[str]) -> None:
        try:
            requests.post(
                f"{backend}/api/public/kiosk/visitor/print-jobs/ack",
                json={"job_id": int(job_id), "ok": bool(ok), "error": error},
                headers=headers,
                timeout=REQUEST_TIMEOUT_SEC,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            # If the ack itself fails, the cloud re-offers the job after its reclaim window;
            # the attempts cap there stops an endless reprint loop. Nothing to do here.
            logger.debug("print agent: ack network error for job %s: %s", job_id, exc)
