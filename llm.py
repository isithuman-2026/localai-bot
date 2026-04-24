import os
import httpx

LITELLM_URL = os.environ.get(
    "LITELLM_URL",
    "http://localai-litellm:4000/v1/chat/completions",
)
MODEL = "qwen2.5-14b"


async def chat(messages: list[dict]) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            LITELLM_URL,
            json={"model": MODEL, "messages": messages},
            headers={"Authorization": "Bearer dummy"},
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
