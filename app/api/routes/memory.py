"""记忆碎片管理接口"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import require_auth, TokenData
from app.memory.fragments import fragment_store
from app.memory.extractor import extract_fragments

router = APIRouter(prefix="/memory", tags=["memory"])


class ManualExtractRequest(BaseModel):
    """手动触发提取请求"""
    session_id: str = ""
    messages: list[dict]  # [{"role": "user/assistant", "content": "..."}]


@router.get("/stats")
async def get_memory_stats(auth: TokenData = Depends(require_auth)):
    """获取当前用户记忆统计"""
    stats = fragment_store.get_stats(auth.user_id)
    return stats


@router.get("/fragments")
async def get_user_fragments(
    category: str | None = None,
    valid_only: bool = True,
    auth: TokenData = Depends(require_auth),
):
    """获取当前用户的记忆碎片列表"""
    user_id = auth.user_id
    mem = fragment_store.load_user_memory(user_id)
    fragments = mem.fragments

    if valid_only:
        fragments = [f for f in fragments if f.is_valid]

    if category:
        fragments = [f for f in fragments if f.category == category]

    # 按重要性降序排列
    fragments.sort(key=lambda f: f.importance, reverse=True)

    return {
        "user_id": user_id,
        "constitution": mem.constitution,
        "main_concerns": mem.main_concerns,
        "total": len(fragments),
        "fragments": [
            {
                "id": f.id,
                "content": f.content,
                "category": f.category,
                "tags": f.tags,
                "importance": f.importance,
                "emotion": f.emotion,
                "constitution": f.constitution,
                "source_time": f.source_time.isoformat() if f.source_time else None,
                "access_count": f.access_count,
                "is_valid": f.is_valid,
            }
            for f in fragments
        ],
    }


@router.get("/retrieve")
async def retrieve_fragments(
    query: str = "",
    top_k: int = 5,
    auth: TokenData = Depends(require_auth),
):
    """检索与当前话题相关的记忆碎片"""
    user_id = auth.user_id
    fragments = fragment_store.retrieve(
        user_id=user_id,
        query=query,
        top_k=top_k,
    )

    return {
        "user_id": user_id,
        "query": query,
        "total": len(fragments),
        "fragments": [
            {
                "id": f.id,
                "content": f.content,
                "category": f.category,
                "tags": f.tags,
                "importance": f.importance,
                "formatted": fragment_store.format_fragments_for_prompt([f]),
            }
            for f in fragments
        ],
    }


@router.post("/extract")
async def manual_extract(req: ManualExtractRequest, auth: TokenData = Depends(require_auth)):
    """手动触发记忆碎片提取"""
    if not req.messages or len(req.messages) < 2:
        raise HTTPException(status_code=400, detail="至少需要2条消息才能提取")

    fragments = await extract_fragments(
        user_id=auth.user_id,
        session_messages=req.messages,
        source_session=req.session_id,
    )

    return {
        "user_id": auth.user_id,
        "extracted": len(fragments),
        "fragments": [
            {
                "id": f.id,
                "content": f.content,
                "category": f.category,
                "tags": f.tags,
                "importance": f.importance,
            }
            for f in fragments
        ],
    }


@router.post("/cleanup")
async def run_cleanup(auth: TokenData = Depends(require_auth)):
    """执行衰减清理"""
    user_id = auth.user_id
    fragment_store.run_decay_cleanup(user_id)
    stats = fragment_store.get_stats(user_id)
    return {
        "message": "衰减清理完成",
        "stats": stats,
    }


@router.delete("/fragments/{fragment_id}")
async def delete_fragment(fragment_id: str, auth: TokenData = Depends(require_auth)):
    """删除指定碎片"""
    user_id = auth.user_id
    mem = fragment_store.load_user_memory(user_id)
    for frag in mem.fragments:
        if frag.id == fragment_id:
            frag.is_valid = False
            fragment_store.save_user_memory(mem)
            return {"message": f"碎片 {fragment_id} 已删除"}
    raise HTTPException(status_code=404, detail=f"碎片 {fragment_id} 不存在")
