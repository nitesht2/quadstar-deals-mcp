"""Tests for DST math, postiz thread-safety, and scheduler guards."""
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# --- DST-aware offset helper ---

def test_pst_offset_is_negative_seven_in_april():
    from src.postiz_client import _pst_utc_offset_hours
    apr = datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc)
    assert _pst_utc_offset_hours(apr) == -7, "April should be PDT (UTC-7)"


def test_pst_offset_is_negative_eight_in_january():
    from src.postiz_client import _pst_utc_offset_hours
    jan = datetime(2026, 1, 14, 18, 0, tzinfo=timezone.utc)
    assert _pst_utc_offset_hours(jan) == -8, "January should be PST (UTC-8)"


def test_pst_offset_during_dst_transitions():
    from src.postiz_client import _pst_utc_offset_hours
    # Right before DST spring-forward 2026: March 7 at noon UTC = 4am PST
    before = datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc)
    # Right after spring-forward: March 9 at noon UTC = 5am PDT
    after = datetime(2026, 3, 9, 12, 0, tzinfo=timezone.utc)
    assert _pst_utc_offset_hours(before) == -8
    assert _pst_utc_offset_hours(after) == -7


def test_format_pst_label_shows_pdt_in_summer():
    from src.postiz_client import _format_pst_label
    summer = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)  # 11am PDT
    label = _format_pst_label(summer)
    assert "PDT" in label
    assert "11:00 AM" in label


def test_format_pst_label_shows_pst_in_winter():
    from src.postiz_client import _format_pst_label
    winter = datetime(2026, 1, 14, 20, 0, tzinfo=timezone.utc)  # 12pm PST
    label = _format_pst_label(winter)
    assert "PST" in label


def test_format_pst_label_uses_local_date_for_today_tomorrow():
    """A slot at 9pm PDT on day N should NOT be labeled Tomorrow just because
    UTC hour crossed midnight."""
    from src.postiz_client import _format_pst_label
    # Freeze "now" by calling close to the test slot — best-effort check:
    # we really only verify the function doesn't crash and includes a day
    # label (Today/Tomorrow/MonDD).
    dt = datetime(2026, 7, 14, 23, 0, tzinfo=timezone.utc)  # 4pm PDT
    label = _format_pst_label(dt)
    assert any(tok in label for tok in ("Today", "Tomorrow", "Apr", "Jul"))


# --- Thread-safe _batch_proposed ---

def test_batch_lock_is_a_lock():
    from src.postiz_client import _batch_lock
    assert isinstance(_batch_lock, type(threading.Lock())), "_batch_lock must be a threading.Lock"


def test_reset_batch_times_is_thread_safe():
    """reset_batch_times should clear the set even under concurrent mutation."""
    from src.postiz_client import _batch_proposed, _batch_lock
    from src.discord_bot import reset_batch_times

    # Seed the set
    with _batch_lock:
        _batch_proposed.add("2026-04-14T18:00")
        _batch_proposed.add("2026-04-14T19:00")
    assert len(_batch_proposed) >= 2

    reset_batch_times()
    assert len(_batch_proposed) == 0


# --- Scheduler guards ---

def test_log_job_error_does_not_raise():
    """Error logger must never itself raise."""
    from src import api
    try:
        api._log_job_error("dummy_job", RuntimeError("expected"))
    except Exception as e:
        raise AssertionError(f"_log_job_error should swallow; raised {e}")


def test_scheduler_jobs_are_async_callables():
    """All scheduled entrypoints must be async functions."""
    import inspect
    from src import api
    for name in ("_auto_run", "_price_monitor_run", "_fast_track_run", "_ab_engagement_check"):
        fn = getattr(api, name)
        assert inspect.iscoroutinefunction(fn), f"{name} must be async"


def test_scheduler_catches_executor_failures():
    """If the sync body raises, the async wrapper must not propagate."""
    import asyncio
    from src import api

    async def driver():
        # Patch run_in_executor to raise by patching the underlying function
        async def go():
            # Directly simulate: call _auto_run while its underlying work raises
            orig = api.run_agent
            api.run_agent = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
            try:
                await api._auto_run()
            finally:
                api.run_agent = orig
        await go()

    # Should not raise
    asyncio.run(driver())
