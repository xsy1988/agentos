"""docling-serve 解析客户端：REST /v1/convert/file → markdown。

解析容器按需启动（docker compose up -d parser）；不可用时管道步骤
parsing 失败落库 error，人工修复后断点重试。
"""

import httpx

from app.core.config import settings

# 解析超时：docling CPU 首次解析要下载/预热模型，大 PDF 也可能数分钟
TIMEOUT = httpx.Timeout(600.0, connect=10.0)


async def parse_document(filename: str, content: bytes) -> str:
    """上传文件到解析容器，返回 markdown 文本。"""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{settings.parser_url}/v1/convert/file",
            files={"files": (filename, content)},
            data={
                "to_formats": "md",
                "image_export_mode": "placeholder",
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"解析容器返回 {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
    if body.get("status") != "success":
        raise RuntimeError(f"解析失败: {str(body.get('status'))[:300]}")
    doc = body.get("document") or {}
    md = doc.get("md_content")
    if not md or not md.strip():
        raise RuntimeError("解析结果为空（md_content 缺失）")
    return md


async def parser_health() -> bool:
    """解析容器健康探测（管理端展示用，不阻断上传）。"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.parser_url}/health")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False
