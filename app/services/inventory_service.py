"""Inventory resolution service."""

from __future__ import annotations

import logging
import re
from typing import Dict, Optional

from app.core.exceptions import CommandExecutionException, NotFoundException
from app.repositories.host_resolver import cluster_type_from_name
from app.repositories.inventory_repository import (
    ClusterBastionResolution,
    InventoryRepository,
    NodeBastionResolution,
)

_logger = logging.getLogger(__name__)


class InventoryService:
    def __init__(
        self,
        repo: InventoryRepository,
        node_type_map: Dict[str, str],
        slash_map: Optional[Dict[str, str]] = None,
    ) -> None:
        self._repo = repo
        self._node_type_map = node_type_map
        self._slash_map = slash_map or {}

    async def resolve_node_bastion(
        self,
        node_name: str,
        bastion_type_override: Optional[str] = None,
    ) -> NodeBastionResolution:
        node_info = await self._repo.lookup_by_name(node_name)
        cluster_name = node_info.cluster.name

        if bastion_type_override is not None:
            bastion_type = bastion_type_override
            bastion_type_source = "query_param"
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
                    detail={"node_type": node_type},
                )
            bastion_type_source = "config"

        mappings = await self._repo.list_mappings(bastion_type)

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
                    return NodeBastionResolution(
                        node_type=node_info.node_type,
                        node=node_info.node,
                        cluster=node_info.cluster,
                        bastion_type=bastion_type,
                        bastion_type_source=bastion_type_source,
                        matched_mapping=mapping,
                        matched_pattern=pattern,
                    )

        raise NotFoundException(
            f"No bastion mapping matched cluster '{cluster_name}' "
            f"for type '{bastion_type}'.",
            detail={
                "node_name": node_name,
                "cluster_name": cluster_name,
                "bastion_type": bastion_type,
            },
        )

    async def resolve_cluster_bastion(
        self, cluster_name: str
    ) -> ClusterBastionResolution:
        bastion_type, has_slash = cluster_type_from_name(cluster_name, self._slash_map)
        mappings = await self._repo.list_mappings(bastion_type)

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
                    return ClusterBastionResolution(
                        cluster_name=cluster_name,
                        has_slash=has_slash,
                        bastion_type=bastion_type,
                        matched_mapping=mapping,
                        matched_pattern=pattern,
                    )

        raise NotFoundException(
            f"No bastion mapping matched cluster '{cluster_name}' "
            f"for type '{bastion_type}'.",
            detail={"cluster_name": cluster_name, "bastion_type": bastion_type},
        )
