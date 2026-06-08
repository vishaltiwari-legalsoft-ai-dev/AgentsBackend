from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.security import get_current_user
from app.services import firestore_repo, storage

router = APIRouter()

MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MB cap to prevent storage bloat.
ALLOWED_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/svg+xml",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@router.post("/upload-reference")
async def upload_reference(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
) -> dict:
    if file.content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(415, f"Unsupported file type: {file.content_type}")

    data = await file.read()
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "File exceeds the 25 MB limit")
    if not data:
        raise HTTPException(400, "Empty file")

    file_name = file.filename or "upload"
    gs_uri, signed_url = storage.upload_reference(
        user["id"], file_name, data, file.content_type or "application/octet-stream"
    )
    reference = firestore_repo.create_reference(user["id"], file_name, gs_uri)
    return {
        "asset_id": reference["asset_id"],
        "file_name": file_name,
        "view_url": signed_url,
    }


@router.get("/references")
def list_references(user: dict = Depends(get_current_user)) -> dict:
    return {"references": firestore_repo.list_references_by_user(user["id"])}
