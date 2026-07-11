from fastapi import APIRouter

from app.api.endpoints import chat, tasks, upload


api_router = APIRouter()
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])
api_router.include_router(upload.router, prefix="/files", tags=["files"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
