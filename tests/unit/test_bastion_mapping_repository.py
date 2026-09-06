import httpx
import pytest

from app.core.exceptions import (
    NotFoundException,
    UpstreamTimeoutException,
    UpstreamUnavailableException,
)
from app.repositories.inventory_repository import (
    BastionMapping,
    HttpInventoryRepository,
)


def _repo(handler) -> HttpInventoryRepository:
    transport = httpx.MockTransport(handler)
    return HttpInventoryRepository(
        base_url="http://fake",
        token="t",
        timeout_seconds=5,
        transport=transport,
    )


async def test_list_success_returns_mappings_in_order():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/bastion-cluster-mappings"
        assert dict(request.url.params) == {"name": "type1"}
        assert request.headers.get("authorization") == "Token t"
        return httpx.Response(
            200,
            json={
                "count": 1,
                "next": None,
                "previous": None,
                "results": [
                    {
                        "id": "123",
                        "name": "type1",
                        "data": [
                            {
                                "runner": "r1",
                                "bastion": "b1",
                                "patterns": [
                                    "type1-cluster-(c1|c2)",
                                    "type1-cluster.*",
                                ],
                                "bastion_ip": "10.0.0.1",
                            },
                            {
                                "runner": "r2",
                                "bastion": "b2",
                                "patterns": ["type1-kind"],
                                "bastion_ip": "10.0.0.2",
                            },
                        ],
                    }
                ],
            },
        )

    repo = _repo(handler)
    result = await repo.list_mappings("type1")
    assert result == [
        BastionMapping(
            patterns=["type1-cluster-(c1|c2)", "type1-cluster.*"],
            runner="r1",
            bastion="b1",
            bastion_ip="10.0.0.1",
        ),
        BastionMapping(
            patterns=["type1-kind"],
            runner="r2",
            bastion="b2",
            bastion_ip="10.0.0.2",
        ),
    ]


async def test_list_empty_results_raises_not_found():
    repo = _repo(lambda r: httpx.Response(200, json={"count": 0, "results": []}))
    with pytest.raises(NotFoundException):
        await repo.list_mappings("unknown")


async def test_list_multiple_results_raises_upstream_unavailable():
    """results with more than one item violates API contract."""
    repo = _repo(
        lambda r: httpx.Response(
            200,
            json={
                "count": 2,
                "results": [
                    {"id": "1", "name": "type1", "data": []},
                    {"id": "2", "name": "type1", "data": []},
                ],
            },
        )
    )
    with pytest.raises(UpstreamUnavailableException):
        await repo.list_mappings("type1")


async def test_list_missing_data_field_raises_upstream_unavailable():
    """Single result but no 'data' key is a contract violation."""
    repo = _repo(
        lambda r: httpx.Response(
            200,
            json={
                "count": 1,
                "results": [{"id": "1", "name": "type1"}],
            },
        )
    )
    with pytest.raises(UpstreamUnavailableException):
        await repo.list_mappings("type1")


async def test_list_malformed_payload_raises_upstream_unavailable():
    """200 with missing 'results' key is treated as upstream contract violation."""
    repo = _repo(lambda r: httpx.Response(200, json={"data": []}))
    with pytest.raises(UpstreamUnavailableException):
        await repo.list_mappings("type1")


async def test_list_500_raises_upstream_unavailable():
    repo = _repo(lambda r: httpx.Response(500))
    with pytest.raises(UpstreamUnavailableException):
        await repo.list_mappings("type1")


async def test_list_timeout_raises_upstream_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    repo = _repo(handler)
    with pytest.raises(UpstreamTimeoutException):
        await repo.list_mappings("type1")


async def test_list_connect_error_raises_upstream_unavailable():
    def handler(request):
        raise httpx.ConnectError("nope", request=request)

    repo = _repo(handler)
    with pytest.raises(UpstreamUnavailableException):
        await repo.list_mappings("type1")


async def test_list_non_json_200_raises_upstream_unavailable():
    """200 with non-JSON body (e.g. WAF HTML page) must raise UpstreamUnavailableException."""
    repo = _repo(lambda r: httpx.Response(200, content=b"<html>error</html>"))
    with pytest.raises(UpstreamUnavailableException):
        await repo.list_mappings("type1")
