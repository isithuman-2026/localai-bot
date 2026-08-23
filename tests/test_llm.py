import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import llm


@pytest.mark.asyncio
async def test_chat_returns_content():
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "choices": [{"message": {"content": "disk full on server01"}}]
    }

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=fake_response)

    with patch("llm.httpx.AsyncClient", return_value=mock_client):
        result = await llm.chat([{"role": "user", "content": "what happened?"}])

    assert result == "disk full on server01"
    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args
    assert call_kwargs[1]["json"]["model"] == llm.MODEL
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


@pytest.mark.asyncio
async def test_chat_json_returns_parsed_dict():
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "choices": [{"message": {"content": '{"severity": "high", "cause": "disk full", "confidence": 0.9}'}}]
    }

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=fake_response)

    with patch("llm.httpx.AsyncClient", return_value=mock_client):
        result = await llm.chat_json([{"role": "user", "content": "triage this"}])

    assert result == {"severity": "high", "cause": "disk full", "confidence": 0.9}
    call_kwargs = mock_client.post.call_args
    assert call_kwargs[1]["json"]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_chat_json_raises_on_invalid_json():
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "choices": [{"message": {"content": "not valid json at all"}}]
    }

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=fake_response)

    with patch("llm.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(Exception):
            await llm.chat_json([{"role": "user", "content": "triage this"}])


@pytest.mark.asyncio
async def test_chat_with_tools_returns_tool_calls():
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "docker_inspect", "arguments": '{"container":"homelab-vector"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }]
    }

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=fake_response)

    tools = [{"type": "function", "function": {"name": "docker_inspect", "parameters": {}}}]

    with patch("llm.httpx.AsyncClient", return_value=mock_client):
        result = await llm.chat_with_tools(
            [{"role": "user", "content": "check homelab-vector"}], tools
        )

    assert result["tool_calls"][0]["function"]["name"] == "docker_inspect"
    call_kwargs = mock_client.post.call_args
    assert call_kwargs[1]["json"]["tools"] == tools
    assert call_kwargs[1]["json"]["max_tokens"] == 1000


@pytest.mark.asyncio
async def test_chat_with_tools_returns_content_when_no_tool_call():
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "choices": [{
            "message": {"role": "assistant", "content": '{"severity":"low","cause":"fine"}'},
            "finish_reason": "stop",
        }]
    }

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=fake_response)

    with patch("llm.httpx.AsyncClient", return_value=mock_client):
        result = await llm.chat_with_tools([{"role": "user", "content": "hi"}], tools=[])

    assert "tool_calls" not in result
    assert result["content"] == '{"severity":"low","cause":"fine"}'
