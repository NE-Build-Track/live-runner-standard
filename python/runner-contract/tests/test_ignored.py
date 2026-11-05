"""log_ignored: one line per runner, once, with the reason and the link."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from livepeer_runner_contract import REPOSITORY, RunnerKey, forget_ignored, log_ignored, parse_discovery


@pytest.fixture(autouse=True)
def fresh() -> Iterator[None]:
    forget_ignored()
    yield
    forget_ignored()


def test_once_per_runner_with_reason_and_link(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("gateway")
    key = RunnerKey("https://orch.example.com:8935", "r1")
    with caplog.at_level(logging.INFO):
        assert log_ignored(logger, key, "no runner_contract key")
        assert not log_ignored(logger, key, "no runner_contract key")
        assert not log_ignored(logger, key, "another reason")
        assert log_ignored(logger, RunnerKey("https://orch.example.com:8935", "r2"), "too large")
    assert len(caplog.records) == 2
    assert caplog.records[0].getMessage() == (
        f'ignoring runner r1 at "https://orch.example.com:8935": no runner_contract key; see {REPOSITORY}'
    )
    assert caplog.records[0].levelno == logging.WARNING


def test_forget_logs_again(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("gateway")
    log_ignored(logger, "r1", "x")
    forget_ignored("r1")
    assert log_ignored(logger, "r1", "x", level=logging.INFO)


def test_runner_supplied_text_cant_forge_lines(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("gateway")
    with caplog.at_level(logging.INFO):
        log_ignored(logger, RunnerKey("https://o", "evil\nINFO approved runner"), "bad\r\nthing")
    (message,) = [r.getMessage() for r in caplog.records]
    assert "\n" not in message and "\r" not in message


def test_logs_what_discovery_ignored(caplog: pytest.LogCaptureFixture) -> None:
    answer = [
        {"address": "https://o", "runners": [{"runner_id": "old", "app": "chat", "metadata": '{"tier": "gold"}'}]}
    ]
    logger = logging.getLogger("gateway")
    with caplog.at_level(logging.INFO):
        for ignored in parse_discovery(answer).ignored:
            log_ignored(logger, ignored.key, ignored.reason)
    assert "ignoring runner old at" in caplog.text and "before runner contract v1" in caplog.text
