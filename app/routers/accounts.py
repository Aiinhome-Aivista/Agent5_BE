"""
Cloud accounts router — fully credential-aware CRUD plus real-time
connection testing.

Endpoints:
  GET    /api/accounts                  list all accounts (no secrets)
  GET    /api/accounts/{id}             fetch one (no secrets)
  POST   /api/accounts                  create with credentials
  PATCH  /api/accounts/{id}             update (incl. credential rotation)
  DELETE /api/accounts/{id}             remove
  PATCH  /api/accounts/{id}/toggle      enable/disable
  POST   /api/accounts/test             test credentials WITHOUT saving
  POST   /api/accounts/{id}/test        test a saved account live
"""
from datetime import datetime
from typing import Optional, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session
from loguru import logger

from app.database import get_db
from app.models.cloud_account import CloudAccount
from app.services.aws_service import AWSService, aws_service_from_account
from app.services.azure_service import AzureService, azure_service_from_account
from app.services.crypto import encrypt, decrypt

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


# ============================================================
# Schemas
# ============================================================

class AwsCredentials(BaseModel):
    aws_access_key_id: str = Field(..., min_length=16, max_length=128)
    aws_secret_access_key: str = Field(..., min_length=16, max_length=256)
    region: str = Field(default="us-east-1", min_length=2, max_length=32)


class AzureCredentials(BaseModel):
    tenant_id: str = Field(..., min_length=10, max_length=64)
    client_id: str = Field(..., min_length=10, max_length=64)
    client_secret: str = Field(..., min_length=4, max_length=256)
    subscription_id: str = Field(..., min_length=10, max_length=64)


class TestPayload(BaseModel):
    """Payload to test credentials *before* saving (real-time validation)."""
    provider: Literal["aws", "azure"]
    aws: Optional[AwsCredentials] = None
    azure: Optional[AzureCredentials] = None


class CreateAccountPayload(BaseModel):
    provider: Literal["aws", "azure"]
    display_name: Optional[str] = None
    aws: Optional[AwsCredentials] = None
    azure: Optional[AzureCredentials] = None


class UpdateAccountPayload(BaseModel):
    display_name: Optional[str] = None
    enabled: Optional[bool] = None
    aws: Optional[AwsCredentials] = None
    azure: Optional[AzureCredentials] = None


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    provider: str
    account_identifier: str
    display_name: str
    region: Optional[str] = None
    enabled: bool
    last_test_status: Optional[str] = None
    last_test_message: Optional[str] = None
    last_tested_at: Optional[datetime] = None
    last_scanned_at: Optional[datetime] = None
    created_at: datetime
    has_credentials: bool = False


def _to_out(a: CloudAccount) -> dict:
    """Serialize without leaking secrets."""
    if a.provider == "aws":
        has_creds = bool(a.aws_access_key_id_enc and a.aws_secret_access_key_enc)
    else:
        has_creds = bool(
            a.azure_tenant_id_enc and a.azure_client_id_enc
            and a.azure_client_secret_enc and a.account_identifier
        )
    return {
        "id": a.id,
        "provider": a.provider,
        "account_identifier": a.account_identifier,
        "display_name": a.display_name,
        "region": a.region,
        "enabled": a.enabled,
        "last_test_status": a.last_test_status,
        "last_test_message": a.last_test_message,
        "last_tested_at": a.last_tested_at,
        "last_scanned_at": a.last_scanned_at,
        "created_at": a.created_at,
        "has_credentials": has_creds,
    }


# ============================================================
# List / get
# ============================================================

@router.get("")
def list_accounts(db: Session = Depends(get_db)):
    accounts = (
        db.query(CloudAccount)
        .order_by(CloudAccount.provider, CloudAccount.display_name)
        .all()
    )
    return [_to_out(a) for a in accounts]


@router.get("/{account_id}")
def get_account(account_id: int, db: Session = Depends(get_db)):
    acc = db.query(CloudAccount).filter(CloudAccount.id == account_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    return _to_out(acc)


# ============================================================
# Test credentials  (no save)
# ============================================================

@router.post("/test")
def test_credentials(payload: TestPayload):
    """
    Real-time test BEFORE saving. Builds a temporary client from the
    submitted credentials and makes a single low-impact API call.
    """
    if payload.provider == "aws":
        if not payload.aws:
            raise HTTPException(status_code=400, detail="aws credentials required")
        svc = AWSService(
            access_key=payload.aws.aws_access_key_id,
            secret_key=payload.aws.aws_secret_access_key,
            region=payload.aws.region,
        )
        result = svc.test_connection()
        logger.info("AWS test: ok={}", result.get("ok"))
        return result

    # azure
    if not payload.azure:
        raise HTTPException(status_code=400, detail="azure credentials required")
    svc = AzureService(
        tenant_id=payload.azure.tenant_id,
        client_id=payload.azure.client_id,
        client_secret=payload.azure.client_secret,
        subscription_id=payload.azure.subscription_id,
    )
    result = svc.test_connection()
    logger.info("Azure test: ok={}", result.get("ok"))
    return result


# ============================================================
# Create
# ============================================================

@router.post("")
def create_account(payload: CreateAccountPayload, db: Session = Depends(get_db)):
    """
    Create an account. Behavior:
    1. Tests credentials live FIRST.
    2. On success, persists the account with encrypted credentials.
    3. On failure, returns 400 with the upstream error message.
    """
    if payload.provider == "aws":
        if not payload.aws:
            raise HTTPException(status_code=400, detail="aws credentials required")
        svc = AWSService(
            access_key=payload.aws.aws_access_key_id,
            secret_key=payload.aws.aws_secret_access_key,
            region=payload.aws.region,
        )
        test = svc.test_connection()
        if not test.get("ok"):
            raise HTTPException(
                status_code=400,
                detail={"message": "AWS credential test failed", "error": test.get("error")},
            )
        account_identifier = test["account_id"]
        # Unique check
        if (
            db.query(CloudAccount)
            .filter(CloudAccount.provider == "aws", CloudAccount.account_identifier == account_identifier)
            .first()
        ):
            raise HTTPException(status_code=409, detail=f"AWS account {account_identifier} already registered")

        acc = CloudAccount(
            provider="aws",
            account_identifier=account_identifier,
            display_name=payload.display_name or f"AWS · {account_identifier}",
            region=payload.aws.region,
            enabled=True,
            aws_access_key_id_enc=encrypt(payload.aws.aws_access_key_id),
            aws_secret_access_key_enc=encrypt(payload.aws.aws_secret_access_key),
            last_test_status="ok",
            last_test_message=f"Connected as {test.get('arn')}",
            last_tested_at=datetime.utcnow(),
        )
    else:
        if not payload.azure:
            raise HTTPException(status_code=400, detail="azure credentials required")
        svc = AzureService(
            tenant_id=payload.azure.tenant_id,
            client_id=payload.azure.client_id,
            client_secret=payload.azure.client_secret,
            subscription_id=payload.azure.subscription_id,
        )
        test = svc.test_connection()
        if not test.get("ok"):
            raise HTTPException(
                status_code=400,
                detail={"message": "Azure credential test failed", "error": test.get("error")},
            )
        account_identifier = payload.azure.subscription_id
        if (
            db.query(CloudAccount)
            .filter(CloudAccount.provider == "azure", CloudAccount.account_identifier == account_identifier)
            .first()
        ):
            raise HTTPException(status_code=409, detail=f"Azure subscription {account_identifier} already registered")

        acc = CloudAccount(
            provider="azure",
            account_identifier=account_identifier,
            display_name=payload.display_name or test.get("display_name") or f"Azure · {account_identifier[:8]}",
            region=None,
            enabled=True,
            azure_tenant_id_enc=encrypt(payload.azure.tenant_id),
            azure_client_id_enc=encrypt(payload.azure.client_id),
            azure_client_secret_enc=encrypt(payload.azure.client_secret),
            last_test_status="ok",
            last_test_message=f"Subscription: {test.get('display_name')}",
            last_tested_at=datetime.utcnow(),
        )

    db.add(acc)
    db.commit()
    db.refresh(acc)
    return _to_out(acc)


# ============================================================
# Update (incl. credential rotation)
# ============================================================

@router.patch("/{account_id}")
def update_account(account_id: int, payload: UpdateAccountPayload, db: Session = Depends(get_db)):
    acc = db.query(CloudAccount).filter(CloudAccount.id == account_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    if payload.display_name is not None:
        acc.display_name = payload.display_name
    if payload.enabled is not None:
        acc.enabled = payload.enabled

    if acc.provider == "aws" and payload.aws:
        test_svc = AWSService(
            access_key=payload.aws.aws_access_key_id,
            secret_key=payload.aws.aws_secret_access_key,
            region=payload.aws.region,
        )
        test = test_svc.test_connection()
        if not test.get("ok"):
            raise HTTPException(status_code=400,
                                detail={"message": "AWS credential test failed", "error": test.get("error")})
        acc.aws_access_key_id_enc = encrypt(payload.aws.aws_access_key_id)
        acc.aws_secret_access_key_enc = encrypt(payload.aws.aws_secret_access_key)
        acc.region = payload.aws.region
        acc.last_test_status = "ok"
        acc.last_test_message = "Credentials rotated"
        acc.last_tested_at = datetime.utcnow()

    if acc.provider == "azure" and payload.azure:
        test_svc = AzureService(
            tenant_id=payload.azure.tenant_id,
            client_id=payload.azure.client_id,
            client_secret=payload.azure.client_secret,
            subscription_id=payload.azure.subscription_id,
        )
        test = test_svc.test_connection()
        if not test.get("ok"):
            raise HTTPException(status_code=400,
                                detail={"message": "Azure credential test failed", "error": test.get("error")})
        acc.azure_tenant_id_enc = encrypt(payload.azure.tenant_id)
        acc.azure_client_id_enc = encrypt(payload.azure.client_id)
        acc.azure_client_secret_enc = encrypt(payload.azure.client_secret)
        acc.account_identifier = payload.azure.subscription_id
        acc.last_test_status = "ok"
        acc.last_test_message = "Credentials rotated"
        acc.last_tested_at = datetime.utcnow()

    db.commit()
    db.refresh(acc)
    return _to_out(acc)


# ============================================================
# Test saved account
# ============================================================

@router.post("/{account_id}/test")
def test_saved_account(account_id: int, db: Session = Depends(get_db)):
    """Test a saved account's stored credentials live; updates last_test_* fields."""
    acc = db.query(CloudAccount).filter(CloudAccount.id == account_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    if acc.provider == "aws":
        svc = aws_service_from_account(acc)
    else:
        svc = azure_service_from_account(acc)

    result = svc.test_connection()
    acc.last_test_status = "ok" if result.get("ok") else "error"
    acc.last_test_message = (
        f"Connected: {result.get('arn') or result.get('display_name', '')}"
        if result.get("ok")
        else result.get("error", "Connection failed")[:500]
    )
    acc.last_tested_at = datetime.utcnow()
    db.commit()
    return {**result, "account_id": account_id}


# ============================================================
# Toggle / delete
# ============================================================

@router.patch("/{account_id}/toggle")
def toggle_account(account_id: int, db: Session = Depends(get_db)):
    acc = db.query(CloudAccount).filter(CloudAccount.id == account_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    acc.enabled = not acc.enabled
    db.commit()
    return {"id": acc.id, "enabled": acc.enabled}


@router.delete("/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    acc = db.query(CloudAccount).filter(CloudAccount.id == account_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    db.delete(acc)
    db.commit()
    return {"deleted": account_id}
