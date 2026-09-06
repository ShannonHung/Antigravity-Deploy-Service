import pytest

from app.core.exceptions import NotFoundException
from app.domain.command import HostType
from app.repositories.host_resolver import (
    ClusterNameResolver,
    create_host_resolver,
)
from app.repositories.inventory_repository import BastionMapping
from tests.fixtures.cluster import InMemoryInventoryRepository

SLASH_MAP = {"no_slash": "type1", "with_slash": "type2"}


def _repo():
    return InMemoryInventoryRepository(
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


async def test_no_slash_resolves_via_type1():
    r = ClusterNameResolver(_repo(), SLASH_MAP)
    resolved = await r.resolve("taiwan-taipei-my-cluster")
    assert resolved.ip == "10.1.0.1"
    assert resolved.metadata["bastion_type"] == "type1"
    assert resolved.metadata["has_slash"] == "False"


async def test_slash_resolves_via_type2():
    r = ClusterNameResolver(_repo(), SLASH_MAP)
    resolved = await r.resolve("taiwan-taipei/my-cluster")
    assert resolved.ip == "10.2.0.2"
    assert resolved.metadata["bastion_type"] == "type2"


async def test_no_pattern_match_raises_not_found():
    repo = InMemoryInventoryRepository(
        mappings={
            "type1": [
                BastionMapping(
                    patterns=["nope-.*"], runner="r", bastion="b", bastion_ip="9.9.9.9"
                )
            ]
        }
    )
    r = ClusterNameResolver(repo, SLASH_MAP)
    with pytest.raises(NotFoundException):
        await r.resolve("taiwan-taipei-my-cluster")


def test_factory_returns_cluster_resolver():
    r = create_host_resolver(
        HostType.CLUSTER, inventory_repo=_repo(), slash_map=SLASH_MAP
    )
    assert isinstance(r, ClusterNameResolver)


def test_factory_cluster_requires_slash_map():
    with pytest.raises(ValueError):
        create_host_resolver(HostType.CLUSTER, inventory_repo=_repo())
