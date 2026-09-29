"""本地元数据目录服务入口。

启动：uv run uvicorn main:app --reload
"""
from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from app.api import router as api_router

app = FastAPI(title="Metadata & Lineage Catalog", version="0.1.0")
app.include_router(api_router)


@app.get("/")
def read_root():
    return {"service": "metadata-lineage-catalog", "status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
