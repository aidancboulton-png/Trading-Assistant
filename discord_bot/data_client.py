"""Thin client over the existing FastAPI backend so the bot reuses live data."""
import httpx
from . import config


async def _get(path: str) -> dict:
    url = f"{config.BACKEND_URL.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.json()


async def snapshot() -> dict:
    return await _get("/api/snapshot")


async def news() -> dict:
    return await _get("/api/news")


async def sentiment() -> dict:
    return await _get("/api/sentiment")


async def analysis() -> dict:
    return await _get("/api/analysis")


async def correlations() -> dict:
    return await _get("/api/correlations")
