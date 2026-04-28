"""用户画像接口"""

from fastapi import APIRouter, Depends

from app.api.schemas import UserProfileResponse
from app.core.auth import require_auth, TokenData
from app.memory.store import memory

router = APIRouter(prefix="/user", tags=["user"])


@router.get("/profile", response_model=UserProfileResponse)
async def get_user_profile(auth: TokenData = Depends(require_auth)):
    """获取当前认证用户的画像"""
    profile = await memory.get_profile(auth.user_id)

    return UserProfileResponse(
        user_id=profile.user_id,
        constitution=profile.constitution,
        main_concerns=profile.main_concerns,
        emotion_trend=profile.emotion_trend,
        healing_progress=profile.healing_progress,
        last_meditation=profile.last_meditation,
        last_interaction=None,
    )
