"""Host resolver strategy: chooses the SSH target IP based on host_type.

Adding a new host type:
  1. Add a value to HostType in app/domain/command.py.
  2. Add a HostResolver subclass here.
  3. Add a branch to create_host_resolver().
CommandService does not need to change.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import Dict, Optional

from pydantic import BaseModel, Field

from app.core.exceptions import CommandExecutionException, NotFoundException
from app.domain.command import HostType
from app.repositories.inventory_repository import InventoryRepository

_logger = logging.getLogger(__name__)


def cluster_type_from_name(
    cluster_name: str, slash_map: Dict[str, str]
) -> tuple[str, bool]:
    """Derive (bastion_type, has_slash) from a cluster_name.

    Slash-presence selects the key in slash_map: "with_slash" when the name
    contains '/', else "no_slash". Raises CommandExecutionException naming the
    missing key if slash_map lacks it (operator misconfig).
    """
    has_slash = "/" in cluster_name
    key = "with_slash" if has_slash else "no_slash"
    bastion_type = slash_map.get(key)
    if bastion_type is None:
        raise CommandExecutionException(
            f"CLUSTER_SLASH_TYPE_MAP is missing key '{key}'. "
            f"Current map: {slash_map}. Add both 'no_slash' and 'with_slash'.",
            detail={"missing_key": key, "slash_map": slash_map},
        )
    return bastion_type, has_slash


class ResolvedHost(BaseModel):
    ip: str
    source_input: str
    metadata: Dict[str, str] = Field(default_factory=dict)


class HostResolver(ABC):
    @abstractmethod
    async def resolve(self, raw_host: str) -> ResolvedHost: ...


class IpHostResolver(HostResolver):
    async def resolve(self, raw_host: str) -> ResolvedHost:
        return ResolvedHost(ip=raw_host, source_input=raw_host)


class HostnameHostResolver(HostResolver):
    def __init__(
        self,
        inventory_repo: InventoryRepository,
        ip_label: str,
    ) -> None:
        self._inventory_repo = inventory_repo
        self._ip_label = ip_label

    async def resolve(self, raw_host: str) -> ResolvedHost:
        node_info = await self._inventory_repo.lookup_by_name(raw_host)
        raw_ip = node_info.node.labels.get(self._ip_label)
        if raw_ip is None:
            raise CommandExecutionException(
                f"Label '{self._ip_label}' not found in node labels for '{raw_host}'.",
                detail={
                    "node_name": raw_host,
                    "ip_label": self._ip_label,
                    "available_labels": list(node_info.node.labels.keys()),
                },
            )
        ip = raw_ip.split("/")[0]
        return ResolvedHost(
            ip=ip,
            source_input=raw_host,
            metadata={"node_name": raw_host, "ip_label": self._ip_label},
        )


class ClusterBastionHostResolver(HostResolver):
    """Resolve node_name → cluster_name → bastion_ip via two API calls.

    bastion_type is derived from node_type via node_type_map unless
    bastion_type_override is given (e.g. from request option).
    """

    def __init__(
        self,
        inventory_repo: InventoryRepository,
        node_type_map: Dict[str, str],
        bastion_type: Optional[str] = None,
    ) -> None:
        self._inventory_repo = inventory_repo
        self._node_type_map = node_type_map
        self._bastion_type = bastion_type

    async def resolve(self, raw_host: str) -> ResolvedHost:
        node_info = await self._inventory_repo.lookup_by_name(raw_host)
        cluster_name = node_info.cluster.name

        if self._bastion_type:
            bastion_type = self._bastion_type
        else:
            node_type = node_info.node_type
            bastion_type = self._node_type_map.get(node_type)
            if bastion_type is None:
                known = ", ".join(
                    f"{k!r}→{v!r}" for k, v in self._node_type_map.items()
                )
                raise CommandExecutionException(
                    f"node_type '{node_type}' has no bastion mapping. "
                    f"Known mappings: {{{known}}}. "
                    "Update BASTION_NODE_TYPE_MAP to include this node_type.",
                    detail={
                        "node_type": node_type,
                        "node_type_map": self._node_type_map,
                    },
                )

        mappings = await self._inventory_repo.list_mappings(bastion_type)

        for mapping in mappings:
            for pattern in mapping.patterns:
                try:
                    matched = re.fullmatch(pattern, cluster_name)
                except re.error:
                    _logger.warning(
                        "Skipping invalid regex pattern %r in bastion mapping "
                        "(type=%s) — fix the mapping API data",
                        pattern,
                        bastion_type,
                    )
                    continue
                if matched:
                    return ResolvedHost(
                        ip=mapping.bastion_ip,
                        source_input=raw_host,
                        metadata={
                            "node_name": raw_host,
                            "cluster_name": cluster_name,
                            "bastion_hostname": mapping.bastion,
                            "bastion_type": bastion_type,
                            "matched_pattern": pattern,
                        },
                    )

        raise NotFoundException(
            f"No bastion mapping matched cluster '{cluster_name}' "
            f"for type '{bastion_type}'.",
            detail={
                "node_name": raw_host,
                "cluster_name": cluster_name,
                "bastion_type": bastion_type,
            },
        )


class ClusterNameResolver(HostResolver):
    """Resolve a cluster_name directly to a bastion IP.

    Slash-presence in the cluster_name selects bastion_type via slash_map
    (see cluster_type_from_name); the cluster_name is then regex-matched
    against the inventory mappings for that type. No node-lookup is performed.
    """

    def __init__(
        self,
        inventory_repo: InventoryRepository,
        slash_map: Dict[str, str],
    ) -> None:
        self._inventory_repo = inventory_repo
        self._slash_map = slash_map

    async def resolve(self, raw_host: str) -> ResolvedHost:
        cluster_name = raw_host
        bastion_type, has_slash = cluster_type_from_name(cluster_name, self._slash_map)
        mappings = await self._inventory_repo.list_mappings(bastion_type)

        for mapping in mappings:
            for pattern in mapping.patterns:
                try:
                    matched = re.fullmatch(pattern, cluster_name)
                except re.error:
                    _logger.warning(
                        "Skipping invalid regex pattern %r in bastion mapping "
                        "(type=%s) — fix the mapping API data",
                        pattern,
                        bastion_type,
                    )
                    continue
                if matched:
                    return ResolvedHost(
                        ip=mapping.bastion_ip,
                        source_input=cluster_name,
                        metadata={
                            "cluster_name": cluster_name,
                            "bastion_type": bastion_type,
                            "has_slash": str(has_slash),
                            "bastion_hostname": mapping.bastion,
                            "matched_pattern": pattern,
                        },
                    )

        raise NotFoundException(
            f"No bastion mapping matched cluster '{cluster_name}' "
            f"for type '{bastion_type}'.",
            detail={"cluster_name": cluster_name, "bastion_type": bastion_type},
        )


def create_host_resolver(
    host_type: HostType,
    *,
    inventory_repo: Optional[InventoryRepository] = None,
    node_type_map: Optional[Dict[str, str]] = None,
    bastion_type: Optional[str] = None,
    ip_label: Optional[str] = None,
    slash_map: Optional[Dict[str, str]] = None,
) -> HostResolver:
    if host_type == HostType.IP:
        return IpHostResolver()
    if host_type == HostType.HOSTNAME:
        if inventory_repo is None or ip_label is None:
            raise ValueError("HOSTNAME resolver requires inventory_repo and ip_label")
        return HostnameHostResolver(inventory_repo, ip_label)
    if host_type == HostType.BASTION:
        if inventory_repo is None or node_type_map is None:
            raise ValueError(
                "BASTION resolver requires inventory_repo and node_type_map"
            )
        return ClusterBastionHostResolver(inventory_repo, node_type_map, bastion_type)
    if host_type == HostType.CLUSTER:
        if inventory_repo is None or slash_map is None:
            raise ValueError("CLUSTER resolver requires inventory_repo and slash_map")
        return ClusterNameResolver(inventory_repo, slash_map)
    raise ValueError(f"Unsupported host_type: {host_type}")
