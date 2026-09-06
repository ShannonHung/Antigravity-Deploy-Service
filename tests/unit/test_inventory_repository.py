# The InventoryRepository ABC and its lookup() method have been removed.
# ClusterNodeLookupRepository and BastionMappingRepository tests are
# covered by tests/unit/test_inventory_client.py (Task 2).


def test_cluster_ref_context_defaults_empty():
    from app.repositories.inventory_repository import ClusterRef

    ref = ClusterRef(id="1", name="type1-cluster-c1")
    assert ref.context == ""


def test_cluster_ref_context_coerces_null():
    from app.repositories.inventory_repository import ClusterRef

    ref = ClusterRef.model_validate({"id": "1", "name": "c1", "context": None})
    assert ref.context == ""


def test_cluster_ref_context_passthrough():
    from app.repositories.inventory_repository import ClusterRef

    ref = ClusterRef(id="1", name="c1", context="c1")
    assert ref.context == "c1"
