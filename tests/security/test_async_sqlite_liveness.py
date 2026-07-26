from __future__ import annotations

import asyncio
import sqlite3
from threading import Event, Thread

import httpx
import pytest
from fastapi.testclient import TestClient

from server.async_store import (
    AsyncSQLiteStore,
    AsyncStoreClosedError,
)
from server.config import RuntimeSettings
from server.main import create_app
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import (
    SQLITE_BUSY_TIMEOUT_SECONDS,
    SQLITE_PROCESS_LOCK_TIMEOUT_SECONDS,
    SQLiteStore,
    SQLiteStoreContentionError,
)


def test_blocked_recovery_creation_keeps_health_and_event_loop_live(tmp_path) -> None:
    database_path = tmp_path / "async-sqlite-liveness.sqlite3"
    store = SQLiteStore(database_path)
    app = create_app(RuntimeSettings(live_ready=False), store=store)
    writer_ready = Event()
    writer_release = Event()

    def hold_writer() -> None:
        with sqlite3.connect(database_path, timeout=1) as connection:
            connection.execute("BEGIN IMMEDIATE")
            writer_ready.set()
            assert writer_release.wait(timeout=3)
            connection.rollback()

    writer = Thread(target=hold_writer, name="async-sqlite-liveness-writer")
    writer.start()
    assert writer_ready.wait(timeout=2)

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                creation = asyncio.create_task(
                    client.post(
                        "/api/recoveries",
                        json={
                            "scenarioId": "hotel",
                            "executionMode": "replay_fixture",
                        },
                    )
                )
                await asyncio.sleep(0.05)
                assert not creation.done()
                assert await asyncio.wait_for(
                    asyncio.sleep(0.05, result="responsive"),
                    timeout=0.2,
                ) == "responsive"
                health = await asyncio.wait_for(client.get("/health"), timeout=0.2)
                assert health.status_code == 200
                writer_release.set()
                created = await asyncio.wait_for(creation, timeout=2)
                assert created.status_code == 201

    try:
        asyncio.run(exercise())
    finally:
        writer_release.set()
        writer.join(timeout=3)
    assert not writer.is_alive()


def test_lock_and_busy_timeouts_leave_nominal_graceful_shutdown_headroom(
    tmp_path,
) -> None:
    assert SQLITE_BUSY_TIMEOUT_SECONDS == 3.0
    assert SQLITE_PROCESS_LOCK_TIMEOUT_SECONDS == 1.0
    assert (
        SQLITE_PROCESS_LOCK_TIMEOUT_SECONDS + SQLITE_BUSY_TIMEOUT_SECONDS
    ) < 5.0
    store = SQLiteStore(tmp_path / "busy-timeout-contract.sqlite3")
    with store._connect() as connection:  # noqa: SLF001 - exact runtime contract
        busy_timeout_ms = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    assert busy_timeout_ms == int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)


def test_shutdown_settles_when_an_external_thread_holds_the_store_lock(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "bounded-process-lock.sqlite3")
    store_io = AsyncSQLiteStore.for_store(store)
    holder_entered = Event()
    holder_release = Event()
    worker_entered = Event()

    def hold_process_lock() -> None:
        assert store._lock.acquire()  # noqa: SLF001 - exact bounded-lock contract
        try:
            holder_entered.set()
            assert holder_release.wait(timeout=4)
        finally:
            store._lock.release()  # noqa: SLF001 - exact bounded-lock contract

    def blocked_read() -> None:
        worker_entered.set()
        store.get_recovery("blocked-by-process-lock")

    holder = Thread(target=hold_process_lock, name="sqlite-process-lock-holder")
    holder.start()
    assert holder_entered.wait(timeout=2)

    async def exercise() -> None:
        operation = asyncio.create_task(store_io.read(blocked_read))
        while not worker_entered.is_set():
            await asyncio.sleep(0)
        shutdown = asyncio.create_task(store_io.shutdown())
        with pytest.raises(SQLiteStoreContentionError):
            await asyncio.wait_for(operation, timeout=2)
        await asyncio.wait_for(shutdown, timeout=2)
        with pytest.raises(AsyncStoreClosedError):
            await store_io.read(lambda: None)

    try:
        asyncio.run(exercise())
    finally:
        holder_release.set()
        holder.join(timeout=3)
    assert not holder.is_alive()


def test_store_lock_contention_is_a_retryable_public_503(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "process-lock-public-503.sqlite3")
    app = create_app(RuntimeSettings(live_ready=False), store=store)
    holder_entered = Event()
    holder_release = Event()

    def hold_process_lock() -> None:
        assert store._lock.acquire()  # noqa: SLF001 - exact bounded-lock contract
        try:
            holder_entered.set()
            assert holder_release.wait(timeout=4)
        finally:
            store._lock.release()  # noqa: SLF001 - exact bounded-lock contract

    with TestClient(app) as client:
        holder = Thread(target=hold_process_lock, name="sqlite-api-lock-holder")
        holder.start()
        assert holder_entered.wait(timeout=2)
        try:
            response = client.post(
                "/api/recoveries",
                json={
                    "scenarioId": "hotel",
                    "executionMode": "replay_fixture",
                },
            )
        finally:
            holder_release.set()
            holder.join(timeout=3)

    assert not holder.is_alive()
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["error"]["code"] == "internal_error"
    assert store.count_recoveries() == 0


def test_async_store_overload_is_a_retryable_public_503(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "async-store-overload.sqlite3")
    store_io = AsyncSQLiteStore(
        store=store,
        max_pending=1,
        reserved_control=0,
    )
    orchestrator = RecoveryOrchestrator(
        store=store,
        store_io=store_io,
        hotel_provider=HotelSimulator(store=store),
    )
    app = create_app(
        RuntimeSettings(live_ready=False),
        store=store,
        orchestrator=orchestrator,
    )
    entered = Event()
    release = Event()
    admission_calls = 0
    original_admission = store.claim_public_creation_admission

    def blocked_lane() -> None:
        entered.set()
        assert release.wait(timeout=3)

    def tracked_admission(*args, **kwargs):
        nonlocal admission_calls
        admission_calls += 1
        return original_admission(*args, **kwargs)

    def occupy_lane() -> None:
        asyncio.run(store_io.control(blocked_lane))

    monkeypatch.setattr(store, "claim_public_creation_admission", tracked_admission)
    occupier = Thread(target=occupy_lane, name="async-store-overload-occupier")
    occupier.start()
    assert entered.wait(timeout=2)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/recoveries",
                json={
                    "scenarioId": "hotel",
                    "executionMode": "replay_fixture",
                },
            )
            release.set()
            occupier.join(timeout=3)
            assert not occupier.is_alive()
    finally:
        release.set()
        occupier.join(timeout=3)

    assert admission_calls == 0
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["error"]["code"] == "internal_error"


def test_async_store_503_is_declared_for_every_async_sqlite_route(tmp_path) -> None:
    app = create_app(
        RuntimeSettings(live_ready=False),
        store=SQLiteStore(tmp_path / "async-store-openapi.sqlite3"),
    )
    paths = app.openapi()["paths"]
    for path, method in (
        ("/api/recoveries", "post"),
        ("/api/recoveries/{recovery_id}/decisions", "post"),
        ("/api/recoveries/{recovery_id}/events", "get"),
        ("/api/recoveries/{recovery_id}", "get"),
        ("/api/recoveries/{recovery_id}/receipt", "get"),
        ("/api/demo/reset", "post"),
    ):
        response = paths[path][method]["responses"]["503"]
        assert response["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/PublicErrorResponse"
        }
        assert response["headers"]["Retry-After"]["schema"] == {
            "maximum": 1,
            "minimum": 1,
            "type": "integer",
        }
    readiness = paths["/readyz"]["get"]["responses"]["503"]
    assert readiness["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PublicErrorResponse"
    }
    assert "Retry-After" not in readiness.get("headers", {})


def test_apps_and_orchestrators_share_one_store_lane_until_final_owner_exit(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "shared-app-lane.sqlite3")
    settings = RuntimeSettings(live_ready=False)
    first = create_app(settings, store=store)
    second = create_app(settings, store=store)
    store_io = first.state.store_io

    assert second.state.store_io is store_io
    assert first.state.recovery_orchestrator.store_io is store_io
    assert second.state.recovery_orchestrator.store_io is store_io

    async def exercise() -> None:
        async with first.router.lifespan_context(first):
            async with second.router.lifespan_context(second):
                assert await store_io.read(lambda: "nested") == "nested"
            assert await store_io.read(lambda: "outer-still-active") == (
                "outer-still-active"
            )
        with pytest.raises(AsyncStoreClosedError):
            await store_io.read(lambda: "closed")

    asyncio.run(exercise())


def test_same_app_nested_lifespans_keep_outer_lane_and_live_client_open(
    tmp_path,
    monkeypatch,
) -> None:
    created_clients = []

    class FakeAsyncOpenAI:
        def __init__(self, **_kwargs) -> None:
            self.close_calls = 0
            created_clients.append(self)

        async def close(self) -> None:
            self.close_calls += 1

    monkeypatch.setattr("server.main.AsyncOpenAI", FakeAsyncOpenAI)
    store = SQLiteStore(tmp_path / "same-app-nested-lane.sqlite3")
    app = create_app(
        RuntimeSettings(
            live_ready=True,
            identity_hmac_secret="test-identity-secret-that-is-at-least-32-bytes",
        ),
        store=store,
    )
    assert created_clients == []
    store_io = app.state.store_io

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            assert len(created_clients) == 1
            live_client = created_clients[0]
            outer_cleanup = app.state.cleanup_service
            assert outer_cleanup.running
            async with app.router.lifespan_context(app):
                inner_cleanup = app.state.cleanup_service
                assert inner_cleanup is not outer_cleanup
                assert inner_cleanup.running
                assert live_client.close_calls == 0
            assert not inner_cleanup.running
            assert app.state.cleanup_service is outer_cleanup
            assert outer_cleanup.running
            assert live_client.close_calls == 0
            assert await store_io.read(lambda: "outer-operational") == (
                "outer-operational"
            )
        assert not outer_cleanup.running
        assert live_client.close_calls == 1
        with pytest.raises(AsyncStoreClosedError):
            await store_io.read(lambda: "closed")

    asyncio.run(exercise())


def test_sequential_live_lifespans_recreate_and_close_their_own_client(
    tmp_path,
    monkeypatch,
) -> None:
    created_clients = []

    class FakeAsyncOpenAI:
        def __init__(self, **_kwargs) -> None:
            self.close_calls = 0
            created_clients.append(self)

        async def close(self) -> None:
            self.close_calls += 1

    monkeypatch.setattr("server.main.AsyncOpenAI", FakeAsyncOpenAI)
    app = create_app(
        RuntimeSettings(
            live_ready=True,
            identity_hmac_secret="test-identity-secret-that-is-at-least-32-bytes",
        ),
        store=SQLiteStore(tmp_path / "sequential-live-clients.sqlite3"),
    )

    assert created_clients == []

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            assert len(created_clients) == 1
            first_client = created_clients[0]
            assert first_client.close_calls == 0
        assert first_client.close_calls == 1

        async with app.router.lifespan_context(app):
            assert len(created_clients) == 2
            second_client = created_clients[1]
            assert second_client is not first_client
            assert first_client.close_calls == 1
            assert second_client.close_calls == 0
        assert first_client.close_calls == 1
        assert second_client.close_calls == 1

    asyncio.run(exercise())


def test_cancelled_final_live_client_close_settles_before_reopening(
    tmp_path,
    monkeypatch,
) -> None:
    created_clients = []

    class BlockingAsyncOpenAI:
        def __init__(self, **_kwargs) -> None:
            self.close_calls = 0
            self.close_entered = asyncio.Event()
            self.close_release = asyncio.Event()
            self.close_cancelled = False
            self.closed = False
            created_clients.append(self)

        async def close(self) -> None:
            self.close_calls += 1
            self.close_entered.set()
            try:
                await self.close_release.wait()
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise
            self.closed = True

    monkeypatch.setattr("server.main.AsyncOpenAI", BlockingAsyncOpenAI)
    app = create_app(
        RuntimeSettings(
            live_ready=True,
            identity_hmac_secret="test-identity-secret-that-is-at-least-32-bytes",
        ),
        store=SQLiteStore(tmp_path / "cancelled-live-client-close.sqlite3"),
    )

    async def exercise() -> None:
        first_lifespan = app.router.lifespan_context(app)
        await first_lifespan.__aenter__()
        first_client = created_clients[0]
        exit_task = asyncio.create_task(
            first_lifespan.__aexit__(None, None, None)
        )
        await asyncio.wait_for(first_client.close_entered.wait(), timeout=1)

        exit_task.cancel("first-close-cancel")
        await asyncio.sleep(0)
        assert not exit_task.done()
        exit_task.cancel("second-close-cancel")
        await asyncio.sleep(0)
        assert not exit_task.done()

        blocked_lifespan = app.router.lifespan_context(app)
        with pytest.raises(
            RuntimeError,
            match="shared resources are shutting down",
        ):
            await blocked_lifespan.__aenter__()

        first_client.close_release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await exit_task
        assert raised.value.args == ("first-close-cancel",)
        assert first_client.close_calls == 1
        assert not first_client.close_cancelled
        assert first_client.closed

        second_lifespan = app.router.lifespan_context(app)
        await second_lifespan.__aenter__()
        second_client = created_clients[1]
        assert second_client is not first_client
        second_client.close_release.set()
        await second_lifespan.__aexit__(None, None, None)
        assert first_client.close_calls == 1
        assert second_client.close_calls == 1
        assert second_client.closed

    asyncio.run(exercise())


def test_default_live_client_closes_after_orchestrator_startup_failure(
    tmp_path,
    monkeypatch,
) -> None:
    created_clients = []

    class FakeAsyncOpenAI:
        def __init__(self, **_kwargs) -> None:
            self.close_calls = 0
            self.closed = False
            created_clients.append(self)

        async def close(self) -> None:
            self.close_calls += 1
            self.closed = True

    monkeypatch.setattr("server.main.AsyncOpenAI", FakeAsyncOpenAI)
    app = create_app(
        RuntimeSettings(
            live_ready=True,
            identity_hmac_secret="test-identity-secret-that-is-at-least-32-bytes",
        ),
        store=SQLiteStore(tmp_path / "live-client-startup-failure.sqlite3"),
    )
    orchestrator = app.state.recovery_orchestrator
    original_startup = orchestrator.startup

    async def fail_startup(_owner: object) -> None:
        raise RuntimeError("live-startup-failure-canary")

    monkeypatch.setattr(orchestrator, "startup", fail_startup)

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="live-startup-failure-canary"):
            async with app.router.lifespan_context(app):
                raise AssertionError("Failed startup cannot yield app control")
        assert len(created_clients) == 1
        assert created_clients[0].close_calls == 1
        assert created_clients[0].closed

        monkeypatch.setattr(orchestrator, "startup", original_startup)
        async with app.router.lifespan_context(app):
            assert len(created_clients) == 2
            assert created_clients[1] is not created_clients[0]
        assert created_clients[0].close_calls == 1
        assert created_clients[1].close_calls == 1
        assert created_clients[1].closed

    asyncio.run(exercise())


def test_live_client_close_failure_wins_over_caller_cancellation(
    tmp_path,
    monkeypatch,
) -> None:
    expected_close_error = RuntimeError("live-client-close-failure-canary")
    created_clients = []

    class FailingFirstAsyncOpenAI:
        def __init__(self, **_kwargs) -> None:
            self.close_calls = 0
            self.close_entered = asyncio.Event()
            self.close_release = asyncio.Event()
            self.should_fail = not created_clients
            if not self.should_fail:
                self.close_release.set()
            created_clients.append(self)

        async def close(self) -> None:
            self.close_calls += 1
            self.close_entered.set()
            await self.close_release.wait()
            if self.should_fail:
                raise expected_close_error

    monkeypatch.setattr("server.main.AsyncOpenAI", FailingFirstAsyncOpenAI)
    app = create_app(
        RuntimeSettings(
            live_ready=True,
            identity_hmac_secret="test-identity-secret-that-is-at-least-32-bytes",
        ),
        store=SQLiteStore(tmp_path / "live-client-close-failure.sqlite3"),
    )

    async def exercise() -> None:
        first_lifespan = app.router.lifespan_context(app)
        await first_lifespan.__aenter__()
        first_client = created_clients[0]
        exit_task = asyncio.create_task(
            first_lifespan.__aexit__(None, None, None)
        )
        await asyncio.wait_for(first_client.close_entered.wait(), timeout=1)
        exit_task.cancel("close-failure-caller-cancel")
        await asyncio.sleep(0)
        assert not exit_task.done()
        first_client.close_release.set()

        with pytest.raises(RuntimeError) as raised:
            await exit_task
        assert raised.value is expected_close_error
        assert isinstance(raised.value.__cause__, asyncio.CancelledError)
        assert raised.value.__cause__.args == ("close-failure-caller-cancel",)
        assert first_client.close_calls == 1

        async with app.router.lifespan_context(app):
            second_client = created_clients[1]
            assert second_client is not first_client
        assert first_client.close_calls == 1
        assert second_client.close_calls == 1

    asyncio.run(exercise())


def test_mismatched_store_orchestrator_and_lane_are_rejected(tmp_path) -> None:
    first_store = SQLiteStore(tmp_path / "mismatch-first.sqlite3")
    second_store = SQLiteStore(tmp_path / "mismatch-second.sqlite3")
    second_lane = AsyncSQLiteStore.for_store(second_store)

    with pytest.raises(ValueError, match="store and async SQLite adapter must match"):
        RecoveryOrchestrator(
            store=first_store,
            store_io=second_lane,
            hotel_provider=HotelSimulator(store=first_store),
        )

    second_orchestrator = RecoveryOrchestrator(
        store=second_store,
        hotel_provider=HotelSimulator(store=second_store),
    )
    with pytest.raises(ValueError, match="bound to a different SQLiteStore"):
        create_app(
            RuntimeSettings(live_ready=False),
            store=first_store,
            orchestrator=second_orchestrator,
        )
