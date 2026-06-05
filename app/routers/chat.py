"""Chat router — RAG conversational interface that summarizes ALL platform data."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from loguru import logger

from app.database import get_db
from app.schemas.schemas import ChatRequest, ChatResponse
from app.agents.chat_agent import ChatAgent
from app.models.chat_history import ChatSession, ChatMessage

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
def chat(request: ChatRequest, db: Session = Depends(get_db)):
    """
    Main RAG chat endpoint.

    The chat agent assembles a complete context from ALL data sources:
      - Cost snapshots (7-day rolling + prior period comparison)
      - Telemetry snapshots (utilization across resources)
      - Open anomalies
      - Pending recommendations with potential savings
      - Recent action history
      - Vector store (playbooks + episodic + semantic memory)

    The Mistral frontier model then produces a summary with citations to
    specific recommendations, anomalies, and playbooks.
    """
    try:
        agent = ChatAgent(db)
        result = agent.chat(
            message=request.message,
            session_uuid=request.session_uuid,
            user_email=request.user_email,
            provider=request.provider,
            account_id=request.account_id,
        )
        return ChatResponse(**result)
    except Exception as e:
        logger.exception("Chat error: {}", e)
        raise HTTPException(status_code=500, detail=f"Chat failed: {str(e)}")


@router.get("/sessions")
def list_sessions(limit: int = 20, db: Session = Depends(get_db)):
    """List recent chat sessions."""
    sessions = (
        db.query(ChatSession)
        .order_by(ChatSession.updated_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "session_uuid": s.session_uuid,
            "user_email": s.user_email,
            "title": s.title,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }
        for s in sessions
    ]


@router.get("/sessions/{session_uuid}/messages")
def get_session_messages(session_uuid: str, db: Session = Depends(get_db)):
    """Get full message history for a chat session."""
    session = db.query(ChatSession).filter(ChatSession.session_uuid == session_uuid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    return {
        "session": {
            "session_uuid": session.session_uuid,
            "user_email": session.user_email,
            "title": session.title,
            "created_at": session.created_at,
        },
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "citations": m.citations,
                "created_at": m.created_at,
                "latency_ms": m.latency_ms,
            }
            for m in messages
        ],
    }


@router.delete("/sessions/{session_uuid}")
def delete_session(session_uuid: str, db: Session = Depends(get_db)):
    """Delete a chat session and its messages."""
    session = db.query(ChatSession).filter(ChatSession.session_uuid == session_uuid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    db.query(ChatMessage).filter(ChatMessage.session_id == session.id).delete()
    db.delete(session)
    db.commit()
    return {"deleted": session_uuid}
