"""Unit tests for RequestIdFilter ContextVar isolation.

Verifies that concurrent coroutines setting different request_ids / accounts
do NOT bleed into each other's log records.
"""

import asyncio
import logging

import pytest

from app.core.logging import RequestIdFilter


def _get_log_record(request_id: str, username: str) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="test",
        args=(),
        exc_info=None,
    )
    f = RequestIdFilter()
    RequestIdFilter.set_request_id(request_id)
    RequestIdFilter.set_account(username)
    f.filter(record)
    return record


async def test_concurrent_coroutines_do_not_bleed_request_id():
    """Two coroutines running concurrently must each see their own request_id."""
    results: dict[str, str] = {}

    async def run(name: str, rid: str) -> None:
        RequestIdFilter.set_request_id(rid)
        await asyncio.sleep(0)  # yield — let the other coroutine run
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )
        RequestIdFilter().filter(record)
        results[name] = record.request_id  # type: ignore[attr-defined]

    await asyncio.gather(
        run("A", "rid-A"),
        run("B", "rid-B"),
    )

    assert results["A"] == "rid-A", f"A saw {results['A']!r} instead of 'rid-A'"
    assert results["B"] == "rid-B", f"B saw {results['B']!r} instead of 'rid-B'"


async def test_concurrent_coroutines_do_not_bleed_account():
    """Two coroutines running concurrently must each see their own account."""
    results: dict[str, str] = {}

    async def run(name: str, account: str) -> None:
        RequestIdFilter.set_account(account)
        await asyncio.sleep(0)
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )
        RequestIdFilter().filter(record)
        results[name] = record.username  # type: ignore[attr-defined]

    await asyncio.gather(
        run("A", "alice"),
        run("B", "bob"),
    )

    assert results["A"] == "alice", f"A saw {results['A']!r} instead of 'alice'"
    assert results["B"] == "bob", f"B saw {results['B']!r} instead of 'bob'"
