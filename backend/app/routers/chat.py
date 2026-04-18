from fastapi import APIRouter
from app.schemas.chat import ChatRequest
from app.services.chat_service import chat_with_user_context

router = APIRouter(prefix="/chat", tags=["Chat"])

@router.post("")
def chat(req: ChatRequest):
    return chat_with_user_context(req.user_id, req.message)

