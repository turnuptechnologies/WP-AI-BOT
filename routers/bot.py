from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from services.bot_engine import generate_bot_response

router = APIRouter()


class AskRequest(BaseModel):
    user_id: int
    question: str


@router.post("/ask")
def ask_bot(request: AskRequest):
    try:
        answer = generate_bot_response(request.user_id, request.question)
        return {"question": request.question, "answer": answer}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
