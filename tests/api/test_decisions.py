from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from queue import Empty
from threading import Barrier
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server.config import RuntimeSettings
from server.main import create_app
from server.models import ApprovalDecisionRequest
from server.orchestrator import RecoveryOrchestrator
from server.providers.hotel_simulator import HotelSimulator
from server.store import ApprovalDecisionError, SQLiteStore


def decision_payload(
    snapshot: dict[str, object],
    *,
    client_decision_id: str = "decision-001",
) -> dict[str, str]:
    approval = snapshot["pendingApproval"]
    assert isinstance(approval, dict)
    return {
        "decision": "approve",
        "clientDecisionId": client_decision_id,
        "remedyId": str(approval["remedyId"]),
        "remedyDigest": str(approval["remedyDigest"]),
        "toolCallId": str(approval["toolCallId"]),
    }


@pytest.fixture
def sdk_client(tmp_path):
    store = SQLiteStore(tmp_path / "decision-api.sqlite3")
    provider = HotelSimulator(store=store)
    with TestClient(
        create_app(
            RuntimeSettings(live_ready=False),
            store=store,
            hotel_provider=provider,
        )
    ) as client:
        yield client, store, provider


def create_sdk_recovery(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/recoveries",
        json={"scenarioId": "hotel", "executionMode": "sdk_stub"},
    )
    assert response.status_code == 201
    snapshot = response.json()
    assert snapshot["status"] == "pending_approval"
    assert snapshot["pendingApproval"]["executionStarted"] is False
    return snapshot


def _decision_process_worker(
    database_path: str,
    job_queue: Any,
    result_queue: Any,
    phase_queue: Any,
    start_barrier: Any,
) -> None:
    """Race direct orchestrator decisions from one independent interpreter."""

    iteration = -1
    phase = "initializing"
    store: SQLiteStore | None = None

    def report_phase(next_phase: str) -> None:
        nonlocal phase
        phase = next_phase
        phase_queue.put(
            {
                "iteration": iteration,
                "pid": os.getpid(),
                "phase": phase,
            }
        )

    try:
        report_phase("worker_started")
        store = SQLiteStore(database_path)
        report_phase("store_ready")
        provider = HotelSimulator(store=store)
        orchestrator = RecoveryOrchestrator(
            store=store,
            hotel_provider=provider,
        )
        report_phase("ready")
        while True:
            report_phase("waiting_for_job")
            job = job_queue.get()
            if job is None:
                report_phase("stopping")
                return
            iteration, recovery_id, payload = job
            request = ApprovalDecisionRequest.model_validate(payload)
            report_phase("waiting_at_barrier")
            start_barrier.wait(timeout=20)
            before_dispatches = provider.dispatch_count
            report_phase("approving")
            try:
                response = asyncio.run(
                    orchestrator.approve_decision(recovery_id, request)
                )
            except ApprovalDecisionError as error:
                status_code = error.status_code
                body: dict[str, object] = {"detail": error.public_detail}
                report_phase("decision_conflict")
            else:
                status_code = 200
                body = response.model_dump(mode="json", by_alias=True)
                report_phase("decision_completed")
            result_queue.put(
                {
                    "iteration": iteration,
                    "pid": os.getpid(),
                    "phase": phase,
                    "status": status_code,
                    "body": body,
                    "dispatchDelta": provider.dispatch_count - before_dispatches,
                }
            )
            report_phase("result_sent")
    except BaseException as error:
        result_queue.put(
            {
                "iteration": iteration,
                "pid": os.getpid(),
                "phase": phase,
                "errorType": type(error).__name__,
                "error": str(error)[:200],
            }
        )
    finally:
        if store is not None:
            store.close()


def _drain_process_phases(phase_queue: Any) -> list[dict[str, object]]:
    diagnostics: list[dict[str, object]] = []
    while True:
        try:
            diagnostics.append(phase_queue.get_nowait())
        except Empty:
            return diagnostics


def _wait_for_process_workers(
    phase_queue: Any,
    processes: list[Any],
) -> list[dict[str, object]]:
    deadline = time.monotonic() + 30
    diagnostics: list[dict[str, object]] = []
    ready_pids: set[int] = set()
    while len(ready_pids) < len(processes):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            worker_states = [
                {
                    "pid": process.pid,
                    "alive": process.is_alive(),
                    "exitcode": process.exitcode,
                }
                for process in processes
            ]
            pytest.fail(
                "Timed out waiting for direct decision workers to initialize: "
                f"workers={worker_states}, phases={diagnostics[-20:]}"
            )
        try:
            diagnostic = phase_queue.get(timeout=remaining)
        except Empty:
            continue
        diagnostics.append(diagnostic)
        if diagnostic.get("phase") == "ready":
            ready_pids.add(int(diagnostic["pid"]))
    return diagnostics


def test_public_sdk_stub_creation_and_typed_approval_complete_once(
    sdk_client,
) -> None:
    client, store, provider = sdk_client
    snapshot = create_sdk_recovery(client)
    recovery_id = str(snapshot["recoveryId"])
    payload = decision_payload(snapshot)

    response = client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "clientDecisionId": "decision-001",
        "recoveryId": recovery_id,
        "decision": "approve",
        "status": "completed",
        "approvedRemedyDigest": payload["remedyDigest"],
        "executionStarted": True,
    }
    assert provider.dispatch_count == 1
    assert store.count_executions(recovery_id) == 1
    completed = client.get(f"/api/recoveries/{recovery_id}").json()
    assert completed["status"] == "completed"
    assert completed["pendingApproval"] is None
    receipt = client.get(f"/api/recoveries/{recovery_id}/receipt").json()
    assert receipt["approvedRemedyDigest"] == payload["remedyDigest"]
    assert receipt["providerExecution"] is True
    public_output = json.dumps({"snapshot": completed, "receipt": receipt}).lower()
    assert "state_json" not in public_output
    assert "statejson" not in public_output
    assert "consumer_proof" not in public_output
    assert "provider_proof" not in public_output


def test_openai_live_creation_without_key_returns_stable_unavailable_code(
    sdk_client,
) -> None:
    client, store, provider = sdk_client

    response = client.post(
        "/api/recoveries",
        json={"scenarioId": "hotel", "executionMode": "openai_live"},
    )

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "live_unavailable"
    assert error["fallback"] == {
        "kind": "show_replay_fixture",
        "scenarioId": "hotel",
        "executionMode": "replay_fixture",
    }
    assert store.count_recoveries() == 0
    assert provider.dispatch_count == 0


def test_exact_duplicate_replays_stored_response_without_redispatch(sdk_client) -> None:
    client, store, provider = sdk_client
    snapshot = create_sdk_recovery(client)
    recovery_id = str(snapshot["recoveryId"])
    payload = decision_payload(snapshot)

    first = client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload)
    second = client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload)

    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert provider.dispatch_count == 1
    assert store.count_executions(recovery_id) == 1
    assert len([event for event in store.list_events(recovery_id) if event.terminal]) == 1


def test_same_decision_id_with_changed_tuple_is_conflict(sdk_client) -> None:
    client, store, provider = sdk_client
    snapshot = create_sdk_recovery(client)
    recovery_id = str(snapshot["recoveryId"])
    payload = decision_payload(snapshot)
    first = client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload)
    assert first.status_code == 200

    conflict = client.post(
        f"/api/recoveries/{recovery_id}/decisions",
        json={**payload, "toolCallId": "different-call"},
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "decision_id_conflict"
    assert provider.dispatch_count == 1
    assert store.count_executions(recovery_id) == 1


def test_different_decision_after_winner_is_already_decided(sdk_client) -> None:
    client, store, provider = sdk_client
    snapshot = create_sdk_recovery(client)
    recovery_id = str(snapshot["recoveryId"])
    payload = decision_payload(snapshot)
    assert (
        client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload).status_code
        == 200
    )

    loser = client.post(
        f"/api/recoveries/{recovery_id}/decisions",
        json={**payload, "clientDecisionId": "decision-loser"},
    )

    assert loser.status_code == 409
    assert loser.json()["error"]["code"] == "already_decided"
    assert provider.dispatch_count == 1
    assert store.count_executions(recovery_id) == 1


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("remedyId", "wrong-remedy", "remedy_mismatch"),
        ("remedyDigest", "sha256:" + "0" * 64, "remedy_digest_mismatch"),
        ("toolCallId", "wrong-call", "tool_call_mismatch"),
    ],
)
def test_exact_identity_mismatches_fail_closed(
    sdk_client,
    field: str,
    value: str,
    code: str,
) -> None:
    client, store, provider = sdk_client
    snapshot = create_sdk_recovery(client)
    recovery_id = str(snapshot["recoveryId"])
    payload = {**decision_payload(snapshot), field: value}

    response = client.post(f"/api/recoveries/{recovery_id}/decisions", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert provider.dispatch_count == 0
    assert store.count_executions(recovery_id) == 0
    assert store.count_decisions(recovery_id) == 0


def test_digest_from_another_recovery_fails_closed(sdk_client) -> None:
    client, store, provider = sdk_client
    first = create_sdk_recovery(client)
    second = create_sdk_recovery(client)
    first_id = str(first["recoveryId"])
    second_approval = second["pendingApproval"]
    assert isinstance(second_approval, dict)
    payload = {
        **decision_payload(first),
        "remedyDigest": str(second_approval["remedyDigest"]),
    }

    response = client.post(f"/api/recoveries/{first_id}/decisions", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "remedy_digest_mismatch"
    assert provider.dispatch_count == 0
    assert store.count_executions(first_id) == 0


def test_two_processes_race_twenty_times_with_one_durable_winner(tmp_path) -> None:
    database_path = tmp_path / "decision-process-races.sqlite3"
    creator_store = SQLiteStore(database_path)
    creator_provider = HotelSimulator(store=creator_store)
    creator_app = create_app(
        RuntimeSettings(live_ready=False),
        store=creator_store,
        hotel_provider=creator_provider,
    )
    process_context = multiprocessing.get_context("fork")
    first_jobs = process_context.Queue()
    second_jobs = process_context.Queue()
    job_queues = (first_jobs, second_jobs)
    results = process_context.Queue()
    phases = process_context.Queue()
    start_barrier = process_context.Barrier(2)
    processes = [
        process_context.Process(
            target=_decision_process_worker,
            args=(str(database_path), jobs, results, phases, start_barrier),
        )
        for jobs in job_queues
    ]
    started_processes: list[Any] = []
    worker_pids: set[int] = set()

    try:
        for process in processes:
            process.start()
            started_processes.append(process)
        _wait_for_process_workers(phases, processes)
        with TestClient(creator_app) as creator:
            for iteration in range(20):
                _drain_process_phases(phases)
                snapshot = create_sdk_recovery(creator)
                recovery_id = str(snapshot["recoveryId"])
                first_jobs.put(
                    (
                        iteration,
                        recovery_id,
                        decision_payload(
                            snapshot,
                            client_decision_id=f"process-a-{iteration}",
                        ),
                    )
                )
                second_jobs.put(
                    (
                        iteration,
                        recovery_id,
                        decision_payload(
                            snapshot,
                            client_decision_id=f"process-b-{iteration}",
                        ),
                    )
                )
                outcomes: list[dict[str, object]] = []
                try:
                    for _worker in processes:
                        outcomes.append(results.get(timeout=20))
                except Empty:
                    worker_states = [
                        {
                            "pid": process.pid,
                            "alive": process.is_alive(),
                            "exitcode": process.exitcode,
                        }
                        for process in processes
                    ]
                    received = [
                        {
                            key: outcome.get(key)
                            for key in (
                                "iteration",
                                "pid",
                                "phase",
                                "status",
                                "errorType",
                            )
                        }
                        for outcome in outcomes
                    ]
                    phase_diagnostics = _drain_process_phases(phases)[-20:]
                    pytest.fail(
                        "Timed out waiting for direct decision worker: "
                        f"iteration={iteration}, received={received}, "
                        f"workers={worker_states}, phases={phase_diagnostics}"
                    )

                assert all("error" not in outcome for outcome in outcomes), outcomes
                assert {outcome["iteration"] for outcome in outcomes} == {iteration}
                assert len({outcome["pid"] for outcome in outcomes}) == 2
                worker_pids.update(outcome["pid"] for outcome in outcomes)
                assert sorted(outcome["status"] for outcome in outcomes) == [200, 409]
                loser = next(outcome for outcome in outcomes if outcome["status"] == 409)
                assert loser["body"]["detail"]["code"] == "already_decided"
                assert sum(outcome["dispatchDelta"] for outcome in outcomes) == 1

                verifier = SQLiteStore(database_path)
                assert verifier.count_decisions(recovery_id) == 1
                assert verifier.count_executions(recovery_id) == 1
                assert verifier.get_receipt(recovery_id).provider_execution is True
                assert len(
                    [event for event in verifier.list_events(recovery_id) if event.terminal]
                ) == 1
                verifier.close()
                with sqlite3.connect(database_path) as connection:
                    assert connection.execute(
                        "SELECT COUNT(*) FROM receipts WHERE recovery_id = ?",
                        (recovery_id,),
                    ).fetchone() == (1,)
                    assert connection.execute(
                        """
                        SELECT COUNT(*) FROM executions
                        WHERE recovery_id = ? AND provider_execution = 1
                        """,
                        (recovery_id,),
                    ).fetchone() == (1,)
    finally:
        for jobs in job_queues[: len(started_processes)]:
            jobs.put(None)
        for process in started_processes:
            process.join(timeout=5)
        for process in started_processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        for queue in (first_jobs, second_jobs, results, phases):
            queue.close()
            queue.join_thread()
        creator_store.close()

    assert len(worker_pids) == 2
    assert os.getpid() not in worker_pids
    assert len(started_processes) == 2
    assert all(process.exitcode == 0 for process in processes)


def test_same_decision_id_can_race_and_replay_one_result(tmp_path) -> None:
    database_path = tmp_path / "same-decision-race.sqlite3"
    first_store = SQLiteStore(database_path)
    first_provider = HotelSimulator(store=first_store)
    first_app = create_app(
        RuntimeSettings(live_ready=False),
        store=first_store,
        hotel_provider=first_provider,
    )
    with TestClient(first_app) as creator:
        snapshot = create_sdk_recovery(creator)
        session_cookie = creator.cookies.get("backchannel_demo_session")
        assert session_cookie is not None
    recovery_id = str(snapshot["recoveryId"])
    second_store = SQLiteStore(database_path)
    second_provider = HotelSimulator(store=second_store)
    second_app = create_app(
        RuntimeSettings(live_ready=False),
        store=second_store,
        hotel_provider=second_provider,
    )
    barrier = Barrier(2)

    def submit(app):
        with TestClient(app) as client:
            client.cookies.set("backchannel_demo_session", session_cookie)
            barrier.wait()
            response = client.post(
                f"/api/recoveries/{recovery_id}/decisions",
                json=decision_payload(snapshot, client_decision_id="same-winner"),
            )
            return response.status_code, response.content

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result(timeout=15)
            for future in [executor.submit(submit, first_app), executor.submit(submit, second_app)]
        ]

    assert [status for status, _content in outcomes] == [200, 200]
    assert outcomes[0][1] == outcomes[1][1]
    assert first_provider.dispatch_count + second_provider.dispatch_count == 1
    verifier = SQLiteStore(database_path)
    assert verifier.count_decisions(recovery_id) == 1
    assert verifier.count_executions(recovery_id) == 1
    assert len([event for event in verifier.list_events(recovery_id) if event.terminal]) == 1
