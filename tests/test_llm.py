import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import llm


@pytest.mark.asyncio
async def test_chat_returns_content():
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "choices": [{"message": {"content": "disk full on vault44"}}]
    }

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=fake_response)

    with patch("llm.httpx.AsyncClient", return_value=mock_client):
        result = await llm.chat([{"role": "user", "content": "what happened?"}])

    assert result == "disk full on vault44"
    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args
    assert call_kwargs[1]["json"]["model"] == "qwen2.5-14b"
    assert call_kwargs[1]["json"]["messages"] == [{"role": "user", "content": "what happened?"}]


@pytest.mark.asyncio
async def test_chat_uses_custom_url():
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=fake_response)

    with patch("llm.httpx.AsyncClient", return_value=mock_client):
        with patch("llm.LITELLM_URL", "http://custom-litellm:4000/v1/chat/completions"):
            result = await llm.chat([{"role": "user", "content": "hi"}])

    assert mock_client.post.call_args[0][0] == "http://custom-litellm:4000/v1/chat/completions"
