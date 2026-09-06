"""
app/api/router.py

Top-level API router.

Route layout:
  POST /token                              → OAuth2 token endpoint (root-level)
  GET  /api/v1/auth/verify                 → Verify token
  POST /api/v1/auth/hash-password          → Hash a password
  GET  /api/v1/auth/my-scopes              → Inspect token scopes
  POST /api/v1/deploy/stage                → Trigger GitLab pipeline
  GET  /api/v1/deploy/stage/{id}           → Get pipeline status
  POST /api/v1/deploy/stage/{id}/cancel    → Cancel pipeline
  POST /api/v1/deploy/stage/{id}/retry     → Retry pipeline
  GET  /api/v1/inventory/nodes/{node_name} → Cluster node lookup
  GET  /api/v1/inventory/mappings          → Bastion-cluster mappings
  GET  /api/v1/inventory/nodes/{node_name}/bastion-resolution → Node-to-bastion resolution
  GET  /api/v1/inventory/cluster/bastion-resolution → Cluster-name-to-bastion resolution
  GET  /api/v1/command/running             → List in-flight commands (admin_api)
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.deploy import router as deploy_router
from app.api.v1.command import router as command_router
from app.api.v1.inventory import router as inventory_router

# ── /api/v1 sub-router ────────────────────────────────────────────────────────
v1_router = APIRouter(prefix="/api/v1")
v1_router.include_router(auth_router)  # mounts at /api/v1/auth/...
v1_router.include_router(deploy_router)  # mounts at /api/v1/deploy/...
v1_router.include_router(command_router)  # mounts at /api/v1/command/...
v1_router.include_router(inventory_router)  # mounts at /api/v1/inventory/...

# ── Root router (aggregates everything) ───────────────────────────────────────
api_router = APIRouter()
api_router.include_router(v1_router)
