"""
Cloud accounts (AWS / Azure) registered with the agent.

Credentials are encrypted at rest (Fernet) via services.crypto.
Plaintext values are never returned by the API — only safe metadata.
"""
from sqlalchemy import Column, Integer, String, DateTime, Boolean, JSON, Index, Text
from sqlalchemy.sql import func
from app.database import Base


class CloudAccount(Base):
    __tablename__ = "cloud_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(20), nullable=False, index=True)  # 'aws' | 'azure'
    account_identifier = Column(String(255), nullable=False)   # AWS account ID or Azure subscription ID
    display_name = Column(String(255), nullable=False)
    region = Column(String(100), nullable=True)
    enabled = Column(Boolean, default=True, nullable=False)

    # ---- AWS credentials (encrypted) ----
    aws_access_key_id_enc = Column(Text, nullable=True)
    aws_secret_access_key_enc = Column(Text, nullable=True)

    # ---- Azure credentials (encrypted) ----
    azure_tenant_id_enc = Column(Text, nullable=True)
    azure_client_id_enc = Column(Text, nullable=True)
    azure_client_secret_enc = Column(Text, nullable=True)

    # ---- Health snapshot ----
    last_test_status = Column(String(20), nullable=True)   # 'ok' | 'error' | 'unknown'
    last_test_message = Column(Text, nullable=True)
    last_tested_at = Column(DateTime, nullable=True)
    last_scanned_at = Column(DateTime, nullable=True)

    tags = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_cloud_provider_account", "provider", "account_identifier", unique=True),
    )
