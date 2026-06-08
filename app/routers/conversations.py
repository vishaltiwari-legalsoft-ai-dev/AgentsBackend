from fastapi import APIRouter, Depends, HTTPException

from app.security import get_current_user
from app.services import firestore_repo, storage

router = APIRouter()


@router.get("/conversations")
def list_conversations(user: dict = Depends(get_current_user)) -> dict:
    return {"conversations": firestore_repo.list_conversations(str(user["id"]))}


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str, user: dict = Depends(get_current_user)
) -> dict:
    convo = firestore_repo.get_conversation(conversation_id, str(user["id"]))
    if not convo:
        raise HTTPException(404, "Conversation not found")

    # Re-sign any expired asset/gallery URLs so a resumed chat still renders.
    for message in convo.get("messages", []):
        result = message.get("result")
        if isinstance(result, dict):
            message["result"] = storage.rehydrate_result(result)
    return {"conversation": convo}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: str, user: dict = Depends(get_current_user)
) -> dict:
    if not firestore_repo.delete_conversation(conversation_id, str(user["id"])):
        raise HTTPException(404, "Conversation not found")
    return {"deleted": True}
