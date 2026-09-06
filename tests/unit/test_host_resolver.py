import re

import pytest

from app.core.exceptions import NotFoundException
from app.domain.command import HostType
from app.repositories.inventory_repository import BastionMapping
from app.repositories.inventory_repository import ClusterNodeInfo, ClusterRef, NodeInfo
from app.repositories.host_resolver import (
    ClusterBastionHostResolver,
    HostnameHostResolver,
    IpHostResolver,
    ResolvedHost,
    create_host_resolver,
)
from tests.fixtures.cluster import InMemoryInventoryRepository

_NODE_TYPE_MAP = {"baremetal": "type1"}


def _inventory_repo():
    return InMemoryInventoryRepository(
        nodes={
            "node1": ClusterNodeInfo(
                node_type="baremetal",
                node=NodeInfo(
                    id="1",
                    name="node1",
                    labels={"mgmt_ip": "10.0.1.5/8", "router_id": "10.0.1.1"},
                ),
                cluster=ClusterRef(id="1", name="type1-cluster-c1"),
            ),
        },
        mappings={
            "type1": [
                BastionMapping(
                    patterns=["type1-cluster.*"],
                    runner="r",
                    bastion="b",
                    bastion_ip="10.0.0.1",
                )
            ]
        },
    )


async def test_ip_resolver_returns_input_unchanged():
    resolver = IpHostResolver()
    resolved = await resolver.resolve("10.0.0.1")
    assert resolved == ResolvedHost(ip="10.0.0.1", source_input="10.0.0.1", metadata={})


async def test_hostname_resolver_returns_label_ip():
    repo = InMemoryInventoryRepository(
        nodes={
            "node1": ClusterNodeInfo(
                node_type="baremetal",
                node=NodeInfo(
                    id="1",
                    name="node1",
                    labels={"mgmt_ip": "10.1.2.3/8", "router_id": "10.2.3.4"},
                ),
                cluster=ClusterRef(id="1", name="cluster-c1"),
            ),
        }
    )
    resolver = HostnameHostResolver(inventory_repo=repo, ip_label="mgmt_ip")
    resolved = await resolver.resolve("node1")
    assert resolved.ip == "10.1.2.3"  # CIDR suffix stripped
    assert resolved.source_input == "node1"
    assert resolved.metadata["ip_label"] == "mgmt_ip"


async def test_hostname_resolver_router_id_label():
    repo = InMemoryInventoryRepository(
        nodes={
            "node1": ClusterNodeInfo(
                node_type="baremetal",
                node=NodeInfo(
                    id="1",
                    name="node1",
                    labels={"mgmt_ip": "10.1.2.3/8", "router_id": "10.2.3.4"},
                ),
                cluster=ClusterRef(id="1", name="cluster-c1"),
            ),
        }
    )
    resolver = HostnameHostResolver(inventory_repo=repo, ip_label="router_id")
    resolved = await resolver.resolve("node1")
    assert resolved.ip == "10.2.3.4"


async def test_hostname_resolver_missing_label_raises_command_execution_exception():
    from app.core.exceptions import CommandExecutionException

    repo = InMemoryInventoryRepository(
        nodes={
            "node1": ClusterNodeInfo(
                node_type="baremetal",
                node=NodeInfo(id="1", name="node1", labels={"mgmt_ip": "10.1.2.3/8"}),
                cluster=ClusterRef(id="1", name="cluster-c1"),
            ),
        }
    )
    resolver = HostnameHostResolver(inventory_repo=repo, ip_label="nonexistent")
    with pytest.raises(CommandExecutionException):
        await resolver.resolve("node1")


async def test_hostname_resolver_node_not_found_raises_not_found():
    repo = InMemoryInventoryRepository()
    resolver = HostnameHostResolver(inventory_repo=repo, ip_label="mgmt_ip")
    with pytest.raises(NotFoundException):
        await resolver.resolve("missing")


def test_factory_hostname_uses_inventory_repo():
    repo = InMemoryInventoryRepository()
    resolver = create_host_resolver(
        HostType.HOSTNAME,
        inventory_repo=repo,
        ip_label="mgmt_ip",
    )
    assert isinstance(resolver, HostnameHostResolver)


def test_factory_hostname_without_repo_raises():
    with pytest.raises(ValueError):
        create_host_resolver(HostType.HOSTNAME)


def test_factory_returns_ip_resolver():
    assert isinstance(create_host_resolver(HostType.IP), IpHostResolver)


def test_factory_returns_bastion_resolver():
    assert isinstance(
        create_host_resolver(
            HostType.BASTION,
            inventory_repo=_inventory_repo(),
            node_type_map=_NODE_TYPE_MAP,
        ),
        ClusterBastionHostResolver,
    )


def test_factory_bastion_missing_node_type_map_raises():
    with pytest.raises(ValueError):
        create_host_resolver(HostType.BASTION)


async def test_malformed_pattern_raises_not_found_not_500():
    """A syntactically invalid regex from the mapping API must not crash the request."""
    repo = InMemoryInventoryRepository(
        nodes={
            "node1": ClusterNodeInfo(
                node_type="baremetal",
                node=NodeInfo(
                    id="1",
                    name="node1",
                    labels={"mgmt_ip": "10.0.1.5/8", "router_id": "10.0.1.1"},
                ),
                cluster=ClusterRef(id="1", name="type1-cluster-c1"),
            ),
        },
        mappings={
            "type1": [
                BastionMapping(
                    patterns=["type1-cluster-(unclosed"],  # invalid regex
                    runner="r",
                    bastion="b",
                    bastion_ip="10.0.0.1",
                )
            ]
        },
    )
    resolver = ClusterBastionHostResolver(repo, node_type_map=_NODE_TYPE_MAP)
    with pytest.raises(NotFoundException):
        await resolver.resolve("node1")
