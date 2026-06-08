import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.agent.graph import run_agent
from app.security import get_current_user
from app.services import extract, firestore_repo

router = APIRouter()
logger = logging.getLogger("agentos.api")

# Hard cap per uploaded file. Attachments are read in memory for text extraction
# and are NEVER written to Firestore or Cloud Storage.
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("/agent")
async def agent(
    message: str = Form(..., min_length=1),
    brand_id: str | None = Form(default=None),
    conversation_id: str | None = Form(default=None),
    files: list[UploadFile] = File(default=[]),
    user: dict = Depends(get_current_user),
) -> dict:
    """Run the LangGraph agent for the authenticated user.

    Extracts text from any attachments (PDF/DOCX/image OCR), runs the agent,
    persists the exchange to the user's conversation history, and logs the
    request for admin analytics. Uploaded files are never stored.
    """
    merged = message.strip()
    attachment_names: list[str] = []

    for upload in files:
        if not upload.filename:
            continue
        data = await upload.read()
        if not data:
            continue
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(413, f"'{upload.filename}' exceeds the 15 MB limit")
        attachment_names.append(upload.filename)
        try:
            text = extract.extract_text(upload.filename, upload.content_type or "", data)
        except ValueError as exc:
            raise HTTPException(415, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - extraction is best-effort
            logger.warning("extraction failed for %s: %s", upload.filename, exc)
            continue
        if text:
            merged += f"\n\n[Attached: {upload.filename}]\n{text}"

    if not merged:
        raise HTTPException(400, "Empty request")

    try:
        result = run_agent(merged, brand_id or None)
    except Exception as exc:  # noqa: BLE001 - convert to a clean API error
        logger.exception("agent run failed")
        raise HTTPException(status_code=502, detail=f"Agent failed: {exc}") from exc

    user_id = str(user["id"])

    # Persist the exchange to conversation history (create one on first turn).
    try:
        if not conversation_id:
            convo = firestore_repo.create_conversation(user_id, title=message.strip())
            conversation_id = convo["id"]
        firestore_repo.append_messages(
            conversation_id,
            [
                {
                    "role": "user",
                    "text": message.strip(),
                    "attachments": attachment_names,
                    "created_at": _now(),
                },
                {"role": "assistant", "result": result, "created_at": _now()},
            ],
        )
    except Exception as exc:  # noqa: BLE001 - history is best-effort
        logger.warning("failed to persist conversation: %s", exc)

    # Log analytics for generated creatives only (not brand-analysis chatter).
    if result.get("type") == "assets":
        try:
            firestore_repo.log_creative_event(
                user_id=user_id,
                email=str(user.get("email", "")),
                brand=result.get("brand"),
                category=result.get("category", "other"),
                engine="openrouter-image",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to log analytics event: %s", exc)

    return {"conversation_id": conversation_id, **result}
