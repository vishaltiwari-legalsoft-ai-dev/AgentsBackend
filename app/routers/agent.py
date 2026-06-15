import logging

from fastapi import APIRouter, Depends

from app.security import get_current_user
from app.services.agent_config import public_settings_payload

router = APIRouter()
logger = logging.getLogger("agentos.api")


@router.get("/agent/settings")
def agent_settings(_user: dict = Depends(get_current_user)) -> dict:
    """Return Graphic Designer configuration options for the console UI."""
    return public_settings_payload()
