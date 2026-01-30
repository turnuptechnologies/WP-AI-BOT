from fastapi import APIRouter
from services.user_data import get_all_user_data

router = APIRouter()

@router.get("/user/{user_id}")
def user_data(user_id: int):
    return get_all_user_data(user_id)
