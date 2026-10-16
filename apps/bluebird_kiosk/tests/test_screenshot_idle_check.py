"""screenshot-idle-check: take-screenshot's "is someone using the kiosk?" gate.

Contract: exit 10 = skip this tick (touched within the idle window AND uploaded
within the defer budget); exit 0 = capture. Everything ambiguous captures —
fail-open — because this helper may make the fleet console a little staler but
must never leave it blind.
"""
import importlib.machinery
import importlib.util
import json
import os
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = next(p for p in _HERE.parents if (p / "build").is_dir() and (p / "apps").is_dir())
_SCRIPT = _ROOT / (
    "build/live-build/config/includes.chroot"
    "/opt/bluebird-kiosk/bin/screenshot-idle-check"
)


def _load():
    loader = importlib.machinery.SourceFileLoader("screenshot_idle_check", str(_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


sic = _load()


def _touch_stamp(state: Path, age_secs: float) -> None:
    (state / sic.LAST_INPUT_NAME).write_text(
        json.dumps({"last_input": time.time() - age_secs}), encoding="utf-8")


def _upload_stamp(state: Path, age_secs: float) -> Path:
    p = state / sic.UPLOAD_STAMP_NAME
    p.touch()
    os.utime(p, (time.time() - age_secs, time.time() - age_secs))
    return p


# ── decide(): the pure rule ───────────────────────────────────────────────────

def test_decide_skips_when_touched_recently_and_upload_is_fresh():
    assert sic.decide(5, 100, idle_secs=30, max_defer_secs=600) is not None


def test_decide_captures_once_idle_long_enough():
    assert sic.decide(31, 100, 30, 600) is None
    assert sic.decide(30, 100, 30, 600) is None      # boundary: >= idle window => capture


def test_decide_captures_with_no_touch_signal():
    assert sic.decide(None, 0, 30, 600) is None


def test_decide_captures_once_defer_budget_is_spent():
    assert sic.decide(5, 600, 30, 600) is None       # boundary: >= budget => capture
    assert sic.decide(5, 601, 30, 600) is None


def test_decide_reason_is_human_readable():
    assert "touch input 5s ago" in sic.decide(5.9, 12, 30, 600)


# ── last_input_age() ──────────────────────────────────────────────────────────

def test_last_input_age_missing_or_malformed_is_unknown(tmp_path):
    f = tmp_path / "last_input.json"
    assert sic.last_input_age(f, time.time()) is None            # missing
    f.write_text("not json", encoding="utf-8")
    assert sic.last_input_age(f, time.time()) is None            # malformed
    f.write_text(json.dumps({"last_input": "soon"}), encoding="utf-8")
    assert sic.last_input_age(f, time.time()) is None            # wrong type
    f.write_text(json.dumps({"nope": 1}), encoding="utf-8")
    assert sic.last_input_age(f, time.time()) is None            # missing key


def test_last_input_age_clamps_small_skew_but_distrusts_a_future_stamp(tmp_path):
    f = tmp_path / "last_input.json"
    now = time.time()
    f.write_text(json.dumps({"last_input": now + 5}), encoding="utf-8")
    assert sic.last_input_age(f, now) == 0.0                     # a few seconds ahead: "just now"
    f.write_text(json.dumps({"last_input": now + 120}), encoding="utf-8")
    assert sic.last_input_age(f, now) is None                    # clock stepped: untrusted
    f.write_text(json.dumps({"last_input": now - 42}), encoding="utf-8")
    assert abs(sic.last_input_age(f, now) - 42) < 0.01


# ── upload_age() ──────────────────────────────────────────────────────────────

def test_upload_age_reads_mtime(tmp_path):
    p = _upload_stamp(tmp_path, 100)
    assert abs(sic.upload_age(p, time.time()) - 100) < 2


def test_upload_age_missing_stamp_starts_the_budget_now(tmp_path):
    p = tmp_path / "state" / sic.UPLOAD_STAMP_NAME
    assert sic.upload_age(p, time.time()) == 0.0
    assert p.exists()                                            # created so the budget is bounded


# ── main(): end-to-end exit codes ─────────────────────────────────────────────

def _run(state: Path, *extra: str) -> int:
    return sic.main(["--state-dir", str(state), *extra])


def test_main_skips_during_active_touch(tmp_path, capsys):
    _touch_stamp(tmp_path, 3)
    _upload_stamp(tmp_path, 10)
    assert _run(tmp_path) == sic.EXIT_SKIP
    assert "touch input 3s ago" in capsys.readouterr().out


def test_main_captures_when_idle_and_leaves_no_side_effects(tmp_path):
    _touch_stamp(tmp_path, 120)
    assert _run(tmp_path) == sic.EXIT_CAPTURE
    assert not (tmp_path / sic.UPLOAD_STAMP_NAME).exists()       # no stamp created when not skipping


def test_main_captures_with_no_touch_stamp_at_all(tmp_path):
    # Older gesture daemon, or a kiosk with no touchscreen: behave exactly as before.
    assert _run(tmp_path) == sic.EXIT_CAPTURE
    assert _run(tmp_path / "does-not-exist") == sic.EXIT_CAPTURE


def test_main_captures_once_defer_budget_is_spent(tmp_path):
    _touch_stamp(tmp_path, 3)
    _upload_stamp(tmp_path, 700)                                 # > 600s since last fresh frame
    assert _run(tmp_path) == sic.EXIT_CAPTURE


def test_main_first_sight_during_touch_skips_and_starts_budget(tmp_path):
    _touch_stamp(tmp_path, 3)                                    # no upload stamp yet
    assert _run(tmp_path) == sic.EXIT_SKIP
    assert (tmp_path / sic.UPLOAD_STAMP_NAME).exists()


def test_main_honours_custom_thresholds(tmp_path):
    _touch_stamp(tmp_path, 8)
    _upload_stamp(tmp_path, 10)
    assert _run(tmp_path, "--idle-secs", "5") == sic.EXIT_CAPTURE
    assert _run(tmp_path, "--idle-secs", "20") == sic.EXIT_SKIP
    assert _run(tmp_path, "--idle-secs", "20", "--max-defer-secs", "5") == sic.EXIT_CAPTURE
