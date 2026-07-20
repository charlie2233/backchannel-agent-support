from __future__ import annotations

from collections.abc import Mapping

import pytest

from scripts import start


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({}, "127.0.0.1"),
        ({"BACKCHANNEL_DEPLOYED": "false"}, "127.0.0.1"),
        ({"BACKCHANNEL_DEPLOYED": " true "}, "0.0.0.0"),
    ],
)
def test_bind_host_is_loopback_unless_deployment_is_explicit(
    environment: Mapping[str, str],
    expected: str,
) -> None:
    assert start.bind_host(environment) == expected


@pytest.mark.parametrize("value", ["1", "yes", "on", "production"])
def test_bind_host_does_not_guess_non_boolean_deployment_values(value: str) -> None:
    assert start.bind_host({"BACKCHANNEL_DEPLOYED": value}) == "127.0.0.1"


@pytest.mark.parametrize(
    ("environment", "expected"),
    [({}, 8000), ({"PORT": " 9000 "}, 9000), ({"PORT": "65535"}, 65535)],
)
def test_port_accepts_only_valid_tcp_ports(
    environment: Mapping[str, str],
    expected: int,
) -> None:
    assert start.port_from_environment(environment) == expected


@pytest.mark.parametrize(
    "value",
    ["", "abc", "0", "65536", "-1", "8.5", "+8000", "８０００"],
)
def test_port_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="PORT"):
        start.port_from_environment({"PORT": value})


def test_main_runs_exactly_one_sanitized_uvicorn_worker(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_run(application: str, **options: object) -> None:
        observed["application"] = application
        observed.update(options)

    monkeypatch.setattr(start.uvicorn, "run", fake_run)

    start.main({"BACKCHANNEL_DEPLOYED": "true", "PORT": "8123"})

    assert observed == {
        "application": "server.main:app",
        "host": "0.0.0.0",
        "port": 8123,
        "workers": 1,
        "proxy_headers": False,
        "access_log": False,
        "server_header": False,
        "timeout_graceful_shutdown": 5,
    }
