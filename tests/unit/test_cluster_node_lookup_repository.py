import httpx
import pytest

from app.core.exceptions import (
    NotFoundException,
    UpstreamTimeoutException,
    UpstreamUnavailableException,
)
from app.repositories.inventory_repository import (
    ClusterNodeInfo,
    ClusterRef,
    HttpInventoryRepository,
    NodeInfo,
)


def _repo(handler) -> HttpInventoryRepository:
    transport = httpx.MockTransport(handler)
    return HttpInventoryRepository(
        base_url="http://fake",
        token="t",
        timeout_seconds=5,
        transport=transport,
    )


async def test_lookup_success_returns_cluster_node_info():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/k8s-clusters/node-cluster-lookup"
        assert dict(request.url.params) == {"node_name": "node1"}
        assert request.headers.get("authorization") == "Token t"
        return httpx.Response(
            200,
            json={
                "node_type": "baremetal",
                "node": {
                    "id": "1",
                    "name": "node1",
                    "labels": {"mgmt_ip": "10.0.1.5/8", "router_id": "10.0.1.1"},
                },
                "cluster": {"id": "123", "name": "type1-cluster-c1"},
            },
        )

    repo = _repo(handler)
    info = await repo.lookup_by_name("node1")
    assert info == ClusterNodeInfo(
        node_type="baremetal",
        node=NodeInfo(
            id="1",
            name="node1",
            labels={"mgmt_ip": "10.0.1.5/8", "router_id": "10.0.1.1"},
        ),
        cluster=ClusterRef(id="123", name="type1-cluster-c1"),
    )


async def test_lookup_404_raises_not_found():
    repo = _repo(lambda r: httpx.Response(404, json={"detail": "not found"}))
    with pytest.raises(NotFoundException):
        await repo.lookup_by_name("missing")


async def test_lookup_500_raises_upstream_unavailable():
    repo = _repo(lambda r: httpx.Response(500))
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup_by_name("x")


async def test_lookup_401_raises_upstream_unavailable():
    repo = _repo(lambda r: httpx.Response(401, json={"detail": "no auth"}))
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup_by_name("x")


async def test_lookup_timeout_raises_upstream_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    repo = _repo(handler)
    with pytest.raises(UpstreamTimeoutException):
        await repo.lookup_by_name("x")


async def test_lookup_connect_error_raises_upstream_unavailable():
    def handler(request):
        raise httpx.ConnectError("nope", request=request)

    repo = _repo(handler)
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup_by_name("x")


async def test_lookup_non_json_200_raises_upstream_unavailable():
    repo = _repo(lambda r: httpx.Response(200, content=b"<html>error</html>"))
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup_by_name("x")


async def test_lookup_malformed_payload_raises_upstream_unavailable():
    """200 with missing required fields must raise UpstreamUnavailableException."""
    repo = _repo(lambda r: httpx.Response(200, json={"data": {}}))
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup_by_name("x")


async def test_lookup_null_labels_defaults_to_empty_dict():
    """node.labels: null in API response must be treated as an empty dict."""
    repo = _repo(
        lambda r: httpx.Response(
            200,
            json={
                "node_type": "baremetal",
                "node": {"id": "1", "name": "node1", "labels": None},
                "cluster": {"id": "123", "name": "cluster1"},
            },
        )
    )
    info = await repo.lookup_by_name("node1")
    assert info.node.labels == {}


async def test_lookup_missing_labels_defaults_to_empty_dict():
    """node.labels key absent in API response must be treated as an empty dict."""
    repo = _repo(
        lambda r: httpx.Response(
            200,
            json={
                "node_type": "baremetal",
                "node": {"id": "1", "name": "node1"},
                "cluster": {"id": "123", "name": "cluster1"},
            },
        )
    )
    info = await repo.lookup_by_name("node1")
    assert info.node.labels == {}
