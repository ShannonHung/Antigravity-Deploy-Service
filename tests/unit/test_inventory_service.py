"""Unit tests for InventoryService."""

from __future__ import annotations

import pytest

from app.core.exceptions import CommandExecutionException, NotFoundException
from app.repositories.inventory_repository import (
    BastionMapping,
    ClusterBastionResolution,
    ClusterNodeInfo,
    ClusterRef,
    NodeBastionResolution,
    NodeInfo,
)
from app.services.inventory_service import InventoryService
from tests.fixtures.cluster import InMemoryInventoryRepository

_NODE_TYPE_MAP = {"baremetal": "type1", "virtual-machine": "type2"}


def _repo(
    cluster_name: str = "type1-cluster-c1",
    node_type: str = "baremetal",
    mappings: dict | None = None,
) -> InMemoryInventoryRepository:
    return InMemoryInventoryRepository(
        nodes={
            "node1": ClusterNodeInfo(
                node_type=node_type,
                node=NodeInfo(id="1", name="node1", labels={"mgmt_ip": "10.0.1.5/24"}),
                cluster=ClusterRef(id="1", name=cluster_name),
            )
        },
        mappings=mappings or {},
    )


def _service(repo, node_type_map=None) -> InventoryService:
    return InventoryService(repo=repo, node_type_map=node_type_map or _NODE_TYPE_MAP)


# ── Happy path ────────────────────────────────────────────────────────────────


async def test_resolve_uses_config_node_type_map():
    mappings = {
        "type1": [
            BastionMapping(
                patterns=["type1-cluster-c.*"],
                runner="runner1",
                bastion="bastion1",
                bastion_ip="10.0.0.1",
            )
        ]
    }
    svc = _service(_repo(cluster_name="type1-cluster-c1", mappings=mappings))
    result = await svc.resolve_node_bastion("node1")

    assert isinstance(result, NodeBastionResolution)
    assert result.node_type == "baremetal"
    assert result.node.name == "node1"
    assert result.cluster.name == "type1-cluster-c1"
    assert result.bastion_type == "type1"
    assert result.bastion_type_source == "config"
    assert result.matched_mapping.runner == "runner1"
    assert result.matched_pattern == "type1-cluster-c.*"


async def test_resolve_bastion_type_override_sets_query_param_source():
    mappings = {
        "override-type": [
            BastionMapping(
                patterns=[".*"],
                runner="override-runner",
                bastion="override-bastion",
                bastion_ip="10.9.9.9",
            )
        ]
    }
    # node_type=baremetal would map to type1 via config, but override forces override-type
    svc = _service(_repo(cluster_name="any-cluster", mappings=mappings))
    result = await svc.resolve_node_bastion(
        "node1", bastion_type_override="override-type"
    )

    assert result.bastion_type == "override-type"
    assert result.bastion_type_source == "query_param"
    assert result.matched_mapping.runner == "override-runner"


async def test_resolve_first_matching_pattern_wins():
    """First pattern in first mapping entry that matches cluster_name wins."""
    mappings = {
        "type1": [
            BastionMapping(
                patterns=["type1-cluster-(c1|c2|c3)", "type1-cluster.*"],
                runner="runner1",
                bastion="bastion1",
                bastion_ip="10.0.0.1",
            ),
            BastionMapping(
                patterns=["type1-kind"],
                runner="runner2",
                bastion="bastion2",
                bastion_ip="10.0.0.2",
            ),
        ]
    }
    svc = _service(_repo(cluster_name="type1-cluster-c1", mappings=mappings))
    result = await svc.resolve_node_bastion("node1")

    assert result.matched_mapping.runner == "runner1"
    assert result.matched_pattern == "type1-cluster-(c1|c2|c3)"


async def test_resolve_second_entry_matches_when_first_does_not():
    mappings = {
        "type1": [
            BastionMapping(
                patterns=["type1-cluster-(c1|c2|c3)"],
                runner="runner1",
                bastion="bastion1",
                bastion_ip="10.0.0.1",
            ),
            BastionMapping(
                patterns=["type1-kind"],
                runner="runner2",
                bastion="bastion2",
                bastion_ip="10.0.0.2",
            ),
        ]
    }
    svc = _service(_repo(cluster_name="type1-kind", mappings=mappings))
    result = await svc.resolve_node_bastion("node1")

    assert result.matched_mapping.runner == "runner2"
    assert result.matched_pattern == "type1-kind"


# ── Error cases ───────────────────────────────────────────────────────────────


async def test_resolve_node_not_found_raises_not_found():
    svc = _service(InMemoryInventoryRepository(nodes={}, mappings={}))
    with pytest.raises(NotFoundException):
        await svc.resolve_node_bastion("missing-node")


async def test_resolve_unknown_node_type_raises_command_execution_exception():
    mappings = {
        "type1": [
            BastionMapping(
                patterns=[".*"], runner="r", bastion="b", bastion_ip="1.1.1.1"
            )
        ]
    }
    svc = _service(
        _repo(node_type="unknown-type", mappings=mappings),
        node_type_map={"baremetal": "type1"},
    )
    with pytest.raises(CommandExecutionException) as exc_info:
        await svc.resolve_node_bastion("node1")
    assert "unknown-type" in str(exc_info.value)


async def test_resolve_no_pattern_matches_raises_not_found():
    mappings = {
        "type1": [
            BastionMapping(
                patterns=["type1-cluster-(c1|c2|c3)"],
                runner="r",
                bastion="b",
                bastion_ip="10.0.0.1",
            )
        ]
    }
    svc = _service(_repo(cluster_name="type1-cluster-c99", mappings=mappings))
    with pytest.raises(NotFoundException) as exc_info:
        await svc.resolve_node_bastion("node1")
    assert "type1-cluster-c99" in str(exc_info.value)


async def test_resolve_invalid_regex_pattern_is_skipped(caplog):
    """An invalid regex in mapping data is logged and skipped; next pattern tried."""
    import logging

    mappings = {
        "type1": [
            BastionMapping(
                patterns=["[invalid-regex", ".*"],  # first is invalid, second matches
                runner="r",
                bastion="b",
                bastion_ip="10.0.0.1",
            )
        ]
    }
    svc = _service(_repo(cluster_name="any-cluster", mappings=mappings))
    with caplog.at_level(logging.WARNING):
        result = await svc.resolve_node_bastion("node1")
    assert result.matched_pattern == ".*"
    assert any(
        "invalid" in record.message.lower() or "regex" in record.message.lower()
        for record in caplog.records
    )


# ── Cluster bastion resolution ─────────────────────────────────────────────────

_SLASH = {"no_slash": "type1", "with_slash": "type2"}


def _svc():
    repo = InMemoryInventoryRepository(
        mappings={
            "type1": [
                BastionMapping(
                    patterns=["taiwan-.*"],
                    runner="r1",
                    bastion="b1",
                    bastion_ip="10.1.0.1",
                )
            ],
            "type2": [
                BastionMapping(
                    patterns=["taiwan-taipei/.*"],
                    runner="r2",
                    bastion="b2",
                    bastion_ip="10.2.0.2",
                )
            ],
        }
    )
    return InventoryService(repo=repo, node_type_map={}, slash_map=_SLASH)


async def test_resolve_cluster_bastion_no_slash():
    res = await _svc().resolve_cluster_bastion("taiwan-taipei-my-cluster")
    assert isinstance(res, ClusterBastionResolution)
    assert res.bastion_type == "type1"
    assert res.has_slash is False
    assert res.matched_mapping.bastion_ip == "10.1.0.1"


async def test_resolve_cluster_bastion_with_slash():
    res = await _svc().resolve_cluster_bastion("taiwan-taipei/my-cluster")
    assert res.bastion_type == "type2"
    assert res.has_slash is True
    assert res.matched_mapping.bastion_ip == "10.2.0.2"


async def test_resolve_cluster_bastion_no_match():
    repo = InMemoryInventoryRepository(
        mappings={
            "type1": [
                BastionMapping(
                    patterns=["nope-.*"], runner="r", bastion="b", bastion_ip="9.9.9.9"
                )
            ]
        }
    )
    svc = InventoryService(repo=repo, node_type_map={}, slash_map=_SLASH)
    with pytest.raises(NotFoundException):
        await svc.resolve_cluster_bastion("taiwan-taipei-x")
