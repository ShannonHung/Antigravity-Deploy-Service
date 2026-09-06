import pytest

from app.core.exceptions import CommandExecutionException, NotFoundException
from app.repositories.inventory_repository import BastionMapping
from app.repositories.inventory_repository import ClusterNodeInfo, ClusterRef, NodeInfo
from app.repositories.host_resolver import ClusterBastionHostResolver
from tests.fixtures.cluster import InMemoryInventoryRepository

_DEFAULT_NODE_TYPE_MAP = {"baremetal": "type1", "virtual-machine": "type2"}


def _inventory_repo(
    cluster_name: str, node_type: str = "baremetal", mappings_by_type=None
) -> InMemoryInventoryRepository:
    return InMemoryInventoryRepository(
        nodes={
            "node1": ClusterNodeInfo(
                node_type=node_type,
                node=NodeInfo(
                    id="1",
                    name="node1",
                    labels={"mgmt_ip": "10.0.1.5/8", "router_id": "10.0.1.1"},
                ),
                cluster=ClusterRef(id="1", name=cluster_name),
            )
        },
        mappings=mappings_by_type or {},
    )


async def test_first_pattern_in_first_entry_wins():
    mappings = {
        "type1": [
            BastionMapping(
                patterns=["type1-cluster-(c1|c2|c3)", "type1-cluster.*"],
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
    }
    resolver = ClusterBastionHostResolver(
        _inventory_repo("type1-cluster-c1", mappings_by_type=mappings),
        node_type_map=_DEFAULT_NODE_TYPE_MAP,
    )
    resolved = await resolver.resolve("node1")
    assert resolved.ip == "10.0.0.1"
    assert resolved.source_input == "node1"
    assert resolved.metadata == {
        "node_name": "node1",
        "cluster_name": "type1-cluster-c1",
        "bastion_hostname": "b1",
        "bastion_type": "type1",
        "matched_pattern": "type1-cluster-(c1|c2|c3)",
    }


async def test_second_entry_priority_when_first_doesnt_match():
    mappings = {
        "type1": [
            BastionMapping(
                patterns=["type1-cluster-(c1|c2|c3)"],
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
    }
    resolver = ClusterBastionHostResolver(
        _inventory_repo("type1-kind", mappings_by_type=mappings),
        node_type_map=_DEFAULT_NODE_TYPE_MAP,
    )
    resolved = await resolver.resolve("node1")
    assert resolved.ip == "10.0.0.2"
    assert resolved.metadata["matched_pattern"] == "type1-kind"


async def test_no_pattern_matches_raises_not_found():
    mappings = {
        "type1": [
            BastionMapping(
                patterns=["type1-cluster-(c1|c2|c3)"],
                runner="r1",
                bastion="b1",
                bastion_ip="10.0.0.1",
            ),
        ]
    }
    resolver = ClusterBastionHostResolver(
        _inventory_repo("type1-cluster-c99", mappings_by_type=mappings),
        node_type_map=_DEFAULT_NODE_TYPE_MAP,
    )
    with pytest.raises(NotFoundException) as exc_info:
        await resolver.resolve("node1")
    detail = exc_info.value.detail
    assert detail["node_name"] == "node1"
    assert detail["cluster_name"] == "type1-cluster-c99"
    assert detail["bastion_type"] == "type1"


async def test_node_not_found_propagates():
    mappings = {
        "type1": [
            BastionMapping(
                patterns=[".*"], runner="r", bastion="b", bastion_ip="1.1.1.1"
            )
        ]
    }
    resolver = ClusterBastionHostResolver(
        InMemoryInventoryRepository(nodes={}, mappings=mappings),
        node_type_map=_DEFAULT_NODE_TYPE_MAP,
    )
    with pytest.raises(NotFoundException):
        await resolver.resolve("missing-node")


async def test_fullmatch_boundary_dotstar():
    """type1-cluster.* matches the whole string only when re.fullmatch is used."""
    mappings = {
        "type1": [
            BastionMapping(
                patterns=["type1-cluster.*"],
                runner="r",
                bastion="b",
                bastion_ip="10.0.0.1",
            )
        ]
    }
    for cluster in ["type1-cluster-c1", "type1-cluster", "type1-clusterX"]:
        resolver = ClusterBastionHostResolver(
            _inventory_repo(cluster, mappings_by_type=mappings),
            node_type_map=_DEFAULT_NODE_TYPE_MAP,
        )
        resolved = await resolver.resolve("node1")
        assert resolved.ip == "10.0.0.1", f"should match {cluster!r}"


async def test_fullmatch_boundary_strict_alternation():
    """'type1-cluster-(c1|c2|c3)' under fullmatch matches '...c1' but not '...c99'."""
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
    resolver = ClusterBastionHostResolver(
        _inventory_repo("type1-cluster-c1", mappings_by_type=mappings),
        node_type_map=_DEFAULT_NODE_TYPE_MAP,
    )
    assert (await resolver.resolve("node1")).ip == "10.0.0.1"
    resolver = ClusterBastionHostResolver(
        _inventory_repo("type1-cluster-c99", mappings_by_type=mappings),
        node_type_map=_DEFAULT_NODE_TYPE_MAP,
    )
    with pytest.raises(NotFoundException):
        await resolver.resolve("node1")


async def test_fullmatch_rejects_prefix_only_match():
    """A pattern that matches only a prefix of cluster_name must NOT be selected."""
    mappings = {
        "type1": [
            BastionMapping(
                patterns=["type1-cluster"],
                runner="r",
                bastion="b",
                bastion_ip="10.0.0.1",
            )
        ]
    }
    resolver = ClusterBastionHostResolver(
        _inventory_repo("type1-cluster-extra", mappings_by_type=mappings),
        node_type_map=_DEFAULT_NODE_TYPE_MAP,
    )
    with pytest.raises(NotFoundException):
        await resolver.resolve("node1")


# ── node_type → bastion_type map tests ───────────────────────────────────────


async def test_node_type_map_resolves_bastion_type_from_node_type():
    """node_type=baremetal maps to type1 via node_type_map; no explicit bastion_type needed."""
    mappings = {
        "type1": [
            BastionMapping(
                patterns=[".*"], runner="r", bastion="b", bastion_ip="10.1.0.1"
            )
        ],
    }
    resolver = ClusterBastionHostResolver(
        _inventory_repo(
            "any-cluster", node_type="baremetal", mappings_by_type=mappings
        ),
        node_type_map=_DEFAULT_NODE_TYPE_MAP,
    )
    resolved = await resolver.resolve("node1")
    assert resolved.ip == "10.1.0.1"
    assert resolved.metadata["bastion_type"] == "type1"


async def test_node_type_map_selects_correct_type_per_node_type():
    """node_type=virtual-machine maps to type2, independently of type1 mappings."""
    mappings = {
        "type1": [
            BastionMapping(
                patterns=[".*"], runner="r1", bastion="b1", bastion_ip="10.1.0.1"
            )
        ],
        "type2": [
            BastionMapping(
                patterns=[".*"], runner="r2", bastion="b2", bastion_ip="10.2.0.2"
            )
        ],
    }
    resolver = ClusterBastionHostResolver(
        _inventory_repo(
            "any-cluster", node_type="virtual-machine", mappings_by_type=mappings
        ),
        node_type_map=_DEFAULT_NODE_TYPE_MAP,
    )
    resolved = await resolver.resolve("node1")
    assert resolved.ip == "10.2.0.2"
    assert resolved.metadata["bastion_type"] == "type2"


async def test_bastion_type_overrides_node_type_map():
    """Explicit bastion_type takes priority over node_type_map lookup."""
    mappings = {
        "type1": [
            BastionMapping(
                patterns=[".*"], runner="r1", bastion="b1", bastion_ip="10.1.0.1"
            )
        ],
        "override-type": [
            BastionMapping(
                patterns=[".*"], runner="r2", bastion="b2", bastion_ip="10.9.9.9"
            )
        ],
    }
    # node_type=baremetal would map to type1, but bastion_type forces override-type
    resolver = ClusterBastionHostResolver(
        _inventory_repo(
            "any-cluster", node_type="baremetal", mappings_by_type=mappings
        ),
        node_type_map=_DEFAULT_NODE_TYPE_MAP,
        bastion_type="override-type",
    )
    resolved = await resolver.resolve("node1")
    assert resolved.ip == "10.9.9.9"
    assert resolved.metadata["bastion_type"] == "override-type"


async def test_unknown_node_type_raises_with_clear_message():
    """node_type not present in map raises CommandExecutionException with node_type and map contents."""
    mappings = {
        "type1": [
            BastionMapping(
                patterns=[".*"], runner="r", bastion="b", bastion_ip="1.1.1.1"
            )
        ],
    }
    resolver = ClusterBastionHostResolver(
        _inventory_repo(
            "any-cluster", node_type="unknown-type", mappings_by_type=mappings
        ),
        node_type_map={"baremetal": "type1"},
    )
    with pytest.raises(CommandExecutionException) as exc_info:
        await resolver.resolve("node1")
    msg = str(exc_info.value)
    assert "unknown-type" in msg
    assert "baremetal" in msg  # map contents shown
