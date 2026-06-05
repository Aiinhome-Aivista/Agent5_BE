"""
Dynamic Rulebook router.

Exposes the optimization playbooks ("rules") stored in ChromaDB as a live,
editable knowledge base. Users can list, add, edit, approve, or delete rules
on the fly. Approved rules are the ones retrieved by the analyzer/action
agents via RAG.
"""
from typing import Optional, List
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from loguru import logger

from app.services.vector_store import get_vector_store

router = APIRouter(prefix="/api/rules", tags=["rules"])


# --------- Schemas ---------

class RuleCreate(BaseModel):
    title: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    provider: str = "any"          # aws | azure | gcp | any
    resource_type: str = "any"
    category: str = "custom"
    status: str = "draft"          # draft | approved


class RuleUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    provider: Optional[str] = None
    resource_type: Optional[str] = None
    category: Optional[str] = None
    status: Optional[str] = None


def _shape(rule: dict) -> dict:
    md = rule.get("metadata") or {}
    return {
        "id": rule["id"],
        "content": rule.get("content", ""),
        "title": md.get("title", "Untitled rule"),
        "provider": md.get("provider", "any"),
        "resource_type": md.get("resource_type", "any"),
        "category": md.get("category", "custom"),
        "status": md.get("status", "approved"),
        "source": md.get("source", "seed"),
        "updated_at": md.get("updated_at"),
        "approved_by": md.get("approved_by"),
        "approved_at": md.get("approved_at"),
    }


# --------- Endpoints ---------

@router.get("")
def list_rules(status: Optional[str] = None, provider: Optional[str] = None):
    """List all rules in the dynamic rulebook."""
    vs = get_vector_store()
    rules = [_shape(r) for r in vs.list_playbooks()]
    if status:
        rules = [r for r in rules if r["status"] == status]
    if provider and provider != "all":
        rules = [r for r in rules if r["provider"] in (provider, "any")]
    rules.sort(key=lambda r: (r["status"] != "draft", r["title"].lower()))
    return {"rules": rules, "total": len(rules)}


@router.get("/{rule_id}")
def get_rule(rule_id: str):
    vs = get_vector_store()
    rule = vs.get_playbook(rule_id)
    if not rule:
        raise HTTPException(404, "Rule not found")
    return _shape(rule)


@router.post("")
def create_rule(body: RuleCreate):
    """Add a new rule on the fly. Defaults to 'draft' until approved."""
    vs = get_vector_store()
    metadata = {
        "title": body.title,
        "provider": body.provider,
        "resource_type": body.resource_type,
        "category": body.category,
        "status": body.status,
        "source": "user",
        "updated_at": datetime.utcnow().isoformat(),
    }
    try:
        rule_id = vs.add_playbook(body.content, metadata)
    except Exception as e:
        logger.exception("Failed to add rule: {}", e)
        raise HTTPException(500, f"Failed to add rule: {e}")
    return _shape({"id": rule_id, "content": body.content, "metadata": metadata})


@router.patch("/{rule_id}")
def update_rule(rule_id: str, body: RuleUpdate):
    """Edit a rule's content or metadata."""
    vs = get_vector_store()
    md_updates = {
        k: v
        for k, v in {
            "title": body.title,
            "provider": body.provider,
            "resource_type": body.resource_type,
            "category": body.category,
            "status": body.status,
        }.items()
        if v is not None
    }
    md_updates["updated_at"] = datetime.utcnow().isoformat()
    ok = vs.update_playbook(rule_id, text=body.content, metadata=md_updates)
    if not ok:
        raise HTTPException(404, "Rule not found")
    return get_rule(rule_id)


@router.post("/{rule_id}/approve")
def approve_rule(rule_id: str, approved_by: str = "ui-user"):
    """Approve a draft rule so the agents will use it."""
    vs = get_vector_store()
    ok = vs.update_playbook(
        rule_id,
        metadata={
            "status": "approved",
            "approved_by": approved_by,
            "approved_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        },
    )
    if not ok:
        raise HTTPException(404, "Rule not found")
    return get_rule(rule_id)


@router.delete("/{rule_id}")
def delete_rule(rule_id: str):
    vs = get_vector_store()
    ok = vs.delete_playbook(rule_id)
    if not ok:
        raise HTTPException(404, "Rule not found")
    return {"status": "deleted", "id": rule_id}
