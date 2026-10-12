from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.api.deps import CurrentUser, current_user
from backend.broker import accounts
from backend.broker.neo_auth import session_manager
from backend.core.vault import BrokerCredentials, VaultLocked

router = APIRouter(prefix="/api/v1/broker", tags=["broker"])


class CredentialsRequest(BaseModel):
    consumer_key: str = Field(min_length=1, max_length=256)
    totp_key: str = Field(min_length=1, max_length=256)
    mpin: str = Field(min_length=1, max_length=64)
    mobile_number: str = Field(min_length=5, max_length=32)
    ucc: str = Field(min_length=1, max_length=64)
    environment: str = Field(default="prod", pattern="^(prod|uat)$")


@router.post("/credentials")
def save_credentials(payload: CredentialsRequest, _: CurrentUser = Depends(current_user)) -> dict:
    credentials = BrokerCredentials(**payload.model_dump())
    try:
        result = accounts.save_account(
            account_id=credentials.ucc,
            label=f"Kotak Neo · {credentials.ucc}",
            credentials=credentials,
            environment=credentials.environment,
        )
    except VaultLocked as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    session_manager.invalidate(credentials.ucc)
    return {**result, "status": "saved", "message": "Credentials saved securely. Broker login will reconnect on the next request."}
