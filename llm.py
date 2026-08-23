import json
import os
import httpx

LITELLM_URL = os.environ.get(
    "LITELLM_URL",
    "http://localai-litellm:4000/v1/chat/completions",
)
MODEL = "gemma4:e4b"


async def chat(messages: list[dict], max_tokens: int = 800) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            LITELLM_URL,
            json={"model": MODEL, "messages": messages, "max_tokens": max_tokens},
            headers={"Authorization": "Bearer dummy"},
            timeout=180.0,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def chat_json(messages: list[dict]) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            LITELLM_URL,
            json={
                "model": MODEL,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "max_tokens": 600,
            },
            headers={"Authorization": "Bearer dummy"},
            timeout=120.0,
        )
        resp.raise_for_status()
        return json.loads(resp.json()["choices"][0]["message"]["content"])


async def chat_with_tools(messages: list[dict], tools: list[dict], max_tokens: int = 1000) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            LITELLM_URL,
            json={"model": MODEL, "messages": messages, "tools": tools, "max_tokens": max_tokens},
            headers={"Authorization": "Bearer dummy"},
            timeout=180.0,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]
