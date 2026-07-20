from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from server.events import stream_recovery_events
from server.models import ExecutionMode, RecoveryStatus, ScenarioId
from server.store import SQLiteStore

_SESSION_HASH = "hmac-sha256:" + "a" * 64


async def _never_disconnected() -> bool:
    return False


def _create_in_progress(store: SQLiteStore, recovery_id: str) -> None:
    store.create_recovery(
        recovery_id=recovery_id,
        scenario_id=ScenarioId.HOTEL,
        execution_mode=ExecutionMode.SDK_STUB,
        current_step=0,
        current_step_summary="Streaming isolation started.",
        session_hash=_SESSION_HASH,
    )


def test_stream_stops_before_later_event_or_heartbeat_after_access_detach(
    tmp_path,
) -> None:
    database_path = tmp_path / "sse-detach.sqlite3"
    store = SQLiteStore(database_path)
    recovery_id = "sse-detach"
    _create_in_progress(store, recovery_id)

    async def exercise() -> None:
        stream = stream_recovery_events(
            store,
            recovery_id,
            after_seq=0,
            is_disconnected=_never_disconnected,
            heartbeat_seconds=0.01,
            poll_interval_seconds=0.001,
            session_hash=_SESSION_HASH,
            session_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        first = await anext(stream)
        assert first.startswith("id: 1\n")
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "DELETE FROM recovery_access WHERE recovery_id = ?",
                (recovery_id,),
            )
        store.record_transition(
            recovery_id,
            status=RecoveryStatus.IN_PROGRESS,
            current_step=1,
            current_step_summary="Must not leak.",
            event_type="recovery.detected",
            event_data={"summary": "Must not leak."},
        )
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=1)

    asyncio.run(exercise())


def test_stream_stops_before_later_event_or_heartbeat_after_cookie_expiry(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "sse-expiry.sqlite3")
    recovery_id = "sse-expiry"
    _create_in_progress(store, recovery_id)
    expiry = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    clock = [expiry - timedelta(seconds=1)]

    async def exercise() -> None:
        stream = stream_recovery_events(
            store,
            recovery_id,
            after_seq=0,
            is_disconnected=_never_disconnected,
            heartbeat_seconds=0.01,
            poll_interval_seconds=0.001,
            session_hash=_SESSION_HASH,
            session_expires_at=expiry,
            now=lambda: clock[0],
        )
        first = await anext(stream)
        assert first.startswith("id: 1\n")
        clock[0] = expiry
        store.record_transition(
            recovery_id,
            status=RecoveryStatus.IN_PROGRESS,
            current_step=1,
            current_step_summary="Must not leak after expiry.",
            event_type="recovery.detected",
            event_data={"summary": "Must not leak after expiry."},
        )
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=1)

    asyncio.run(exercise())
