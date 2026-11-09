"""
Model Management API Routes
Handles model listing, downloading, deletion, and progress SSE.
"""
import asyncio
import json
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.model_manager import get_model_manager, ModelInfo, DownloadProgress


router = APIRouter(prefix="/api/models", tags=["models"])


class DownloadRequest(BaseModel):
    model_id: str
    token: str | None = None


class ModelResponse(BaseModel):
    models: list[ModelInfo]


@router.get("", response_model=ModelResponse)
async def list_models():
    """List all available models with download status."""
    manager = get_model_manager()
    models = manager.list_models()
    return ModelResponse(models=models)


@router.get("/{model_id}")
async def get_model(model_id: str):
    """Get details of a specific model."""
    manager = get_model_manager()
    models = manager.list_models()
    for model in models:
        if model.id == model_id:
            return model
    raise HTTPException(status_code=404, detail="Model not found")


@router.post("/download")
async def download_model(request: DownloadRequest, req: Request):
    """Start downloading a model. Returns SSE stream with progress."""
    manager = get_model_manager()

    async def progress_stream() -> AsyncGenerator[str, None]:
        async for progress in manager.download_model(request.model_id, request.token):
            yield f"data: {progress.model_dump_json()}\n\n"
            if progress.status in ("complete", "error"):
                break

    return StreamingResponse(
        progress_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("/{model_id}")
async def delete_model(model_id: str):
    """Delete a downloaded model."""
    manager = get_model_manager()
    if manager.delete_model(model_id):
        return {"status": "deleted", "model_id": model_id}
    raise HTTPException(status_code=404, detail="Model not found or not downloaded")


@router.get("/{model_id}/path")
async def get_model_path(model_id: str):
    """Get the local filesystem path for a downloaded model."""
    manager = get_model_manager()
    path = manager.get_model_path(model_id)
    if path:
        return {"model_id": model_id, "path": str(path)}
    raise HTTPException(status_code=404, detail="Model not downloaded")