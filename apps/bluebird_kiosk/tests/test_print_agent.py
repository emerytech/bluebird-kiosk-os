"""Tests for the visitor badge PrintAgent.

The agent polls the cloud `/api/public/kiosk/visitor/print-jobs` with the device bearer, sends each
job's ZPL payload to the printer over raw TCP, and acks the result. It is GATED OFF unless
PRINT_AGENT_ENABLED=1. We mock config + requests + socket so no real network / printer is touched.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))  # apps/ → makes `bluebird_kiosk` importable

from bluebird_kiosk.services import print_agent as pa  # noqa: E402


class FakeResponse:
    def __init__(self, status_code: int = 200, json_body: Optional[Dict[str, Any]] = None):
        self.status_code = status_code
        self._json = json_body

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSock:
    def __init__(self, sink: List[bytes]):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def settimeout(self, _t):
        pass

    def sendall(self, data):
        self._sink.append(data)


def _cfg(enabled: bool = True, backend: str = "https://bluebird-alerts.com") -> Dict[str, str]:
    d = {"BLUEBIRD_BACKEND": backend}
    if enabled:
        d["PRINT_AGENT_ENABLED"] = "1"
    return d


def _setup(monkeypatch, *, enabled=True, token="tok", jobs=None, get_status=200):
    monkeypatch.setattr(pa.config, "read_config", lambda: _cfg(enabled=enabled))
    monkeypatch.setattr(pa.config, "read_license_token", lambda: token)
    cap: Dict[str, Any] = {"get": None, "posts": [], "sent": [], "conn_addr": None}

    def _get(url, params=None, headers=None, timeout=None, **kw):
        cap["get"] = {
            "url": url,
            "auth": (headers or {}).get("Authorization"),
            "allow_redirects": kw.get("allow_redirects"),
            "params": params,
        }
        body = {"ok": True, "jobs": (jobs or [])}
        return FakeResponse(get_status, body if get_status == 200 else None)

    def _post(url, json=None, headers=None, timeout=None, **kw):
        cap["posts"].append({"url": url, "json": json})
        return FakeResponse(200, {"ok": True})

    def _conn(addr, timeout=None):
        cap["conn_addr"] = addr
        return FakeSock(cap["sent"])

    monkeypatch.setattr(pa.requests, "get", _get)
    monkeypatch.setattr(pa.requests, "post", _post)
    monkeypatch.setattr(pa.socket, "create_connection", _conn)
    return cap


# ── Gating ───────────────────────────────────────────────────────────────────

def test_disabled_does_nothing(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not poll when PRINT_AGENT_ENABLED is off")

    monkeypatch.setattr(pa.config, "read_config", lambda: {"BLUEBIRD_BACKEND": "https://x"})
    monkeypatch.setattr(pa.config, "read_license_token", lambda: "tok")
    monkeypatch.setattr(pa.requests, "get", _boom)
    assert pa.PrintAgent().poll_once() == 0


def test_enabled_no_token_no_network(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not poll without a license token")

    monkeypatch.setattr(pa.config, "read_config", lambda: _cfg(enabled=True))
    monkeypatch.setattr(pa.config, "read_license_token", lambda: None)
    monkeypatch.setattr(pa.requests, "get", _boom)
    assert pa.PrintAgent().poll_once() == 0


# ── Print + ack ──────────────────────────────────────────────────────────────

def test_prints_job_and_acks_success(monkeypatch):
    job = {"id": 7, "payload": "^XADEMO^XZ", "printer_host": "10.0.0.42", "printer_port": 9100}
    cap = _setup(monkeypatch, jobs=[job])
    handled = pa.PrintAgent().poll_once()
    assert handled == 1
    # Poll uses the device-authed /api/public/ path + bearer + no redirect (bearer-safe).
    assert cap["get"]["url"].endswith("/api/public/kiosk/visitor/print-jobs")
    assert cap["get"]["auth"] == "Bearer tok"
    assert cap["get"]["allow_redirects"] is False
    # Sent the ZPL to the job's printer address over raw TCP.
    assert cap["conn_addr"] == ("10.0.0.42", 9100)
    assert cap["sent"] == [b"^XADEMO^XZ"]
    # Acked success.
    assert len(cap["posts"]) == 1
    assert cap["posts"][0]["url"].endswith("/api/public/kiosk/visitor/print-jobs/ack")
    assert cap["posts"][0]["json"] == {"job_id": 7, "ok": True, "error": None}


def test_print_failure_acks_failed(monkeypatch):
    job = {"id": 9, "payload": "^XA^XZ", "printer_host": "10.0.0.5", "printer_port": 9100}
    cap = _setup(monkeypatch, jobs=[job])

    def _conn_fail(addr, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(pa.socket, "create_connection", _conn_fail)
    assert pa.PrintAgent().poll_once() == 1
    assert cap["sent"] == []                       # nothing printed
    ack = cap["posts"][0]["json"]
    assert ack["job_id"] == 9 and ack["ok"] is False and "printer_error" in (ack["error"] or "")


def test_multiple_jobs_each_printed_and_acked(monkeypatch):
    jobs = [
        {"id": 1, "payload": "A", "printer_host": "10.0.0.1", "printer_port": 9100},
        {"id": 2, "payload": "B", "printer_host": "10.0.0.1", "printer_port": 9100},
    ]
    cap = _setup(monkeypatch, jobs=jobs)
    assert pa.PrintAgent().poll_once() == 2
    assert cap["sent"] == [b"A", b"B"]
    assert [p["json"]["job_id"] for p in cap["posts"]] == [1, 2]


def test_empty_jobs_no_print_no_ack(monkeypatch):
    cap = _setup(monkeypatch, jobs=[])
    assert pa.PrintAgent().poll_once() == 0
    assert cap["sent"] == [] and cap["posts"] == []


def test_poll_non_200_no_print(monkeypatch):
    cap = _setup(monkeypatch, jobs=[{"id": 1, "payload": "x", "printer_host": "h"}], get_status=403)
    assert pa.PrintAgent().poll_once() == 0
    assert cap["sent"] == [] and cap["posts"] == []
