from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.config import Settings
from app.core.exceptions import ModelInvocationError


class DeepSeekClient:
    """统一封装 DeepSeek 文本、JSON 与流式调用。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.endpoint = f"{self.settings.deepseek_base_url.rstrip('/')}/chat/completions"

    @staticmethod
    def _resolve_messages(
        messages: list[dict[str, str]] | None,
        system_prompt: str | None,
        user_prompt: str | None,
    ) -> list[dict[str, str]]:
        if messages is not None:
            return [
                {"role": str(item["role"]), "content": str(item["content"])}
                for item in messages
                if item.get("role") and item.get("content") is not None
            ]
        if system_prompt is None or user_prompt is None:
            raise ValueError("messages 或 system_prompt/user_prompt 必须提供")
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    async def complete_text(
        self, *, messages: list[dict[str, str]] | None = None,
        system_prompt: str | None = None, user_prompt: str | None = None,
        temperature: float = 0.3,
    ) -> dict[str, Any]:
        request_payload = {
            "model": self.settings.default_model,
            "messages": self._resolve_messages(messages, system_prompt, user_prompt),
            "temperature": temperature,
            "stream": False,
        }
        payload = await self._post_completion(request_payload)
        content = self._extract_content(payload)
        return {"content": content, "model": payload.get("model", self.settings.default_model),
                "usage": payload.get("usage", {}), "request_payload": request_payload,
                "raw_response": payload}

    async def complete_json(
        self, *, messages: list[dict[str, str]] | None = None,
        system_prompt: str | None = None, user_prompt: str | None = None,
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        request_payload = {
            "model": self.settings.default_model,
            "messages": self._resolve_messages(messages, system_prompt, user_prompt),
            "temperature": temperature,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        payload = await self._post_completion(request_payload)
        content = self._extract_content(payload)
        return {"content": self._parse_json_content(content),
                "model": payload.get("model", self.settings.default_model),
                "usage": payload.get("usage", {}), "raw_content": content,
                "request_payload": request_payload, "raw_response": payload}

    async def stream_text(
        self, *, messages: list[dict[str, str]] | None = None,
        system_prompt: str | None = None, user_prompt: str | None = None,
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        self._ensure_api_key()
        request_messages = self._resolve_messages(messages, system_prompt, user_prompt)
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", self.endpoint, headers=self._build_headers(),
                json={"model": self.settings.default_model, "messages": request_messages,
                      "temperature": temperature, "stream": True},
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise ModelInvocationError(
                        "DeepSeek 流式调用失败",
                        detail={"status_code": response.status_code,
                                "body": body.decode("utf-8", errors="ignore")[:1000]},
                    )
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line.removeprefix("data:").strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        payload = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    delta = payload.get("choices", [{}])[0].get("delta", {}).get("content")
                    if delta:
                        yield str(delta)

    async def _post_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_api_key()
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(self.endpoint, headers=self._build_headers(), json=payload)
        if response.status_code >= 400:
            raise ModelInvocationError(
                "DeepSeek 调用失败",
                detail={"status_code": response.status_code, "body": response.text[:1000]},
            )
        return response.json()

    def _ensure_api_key(self) -> None:
        if not self.settings.deepseek_api_key:
            raise ModelInvocationError("未配置 DeepSeek API Key")

    def _build_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.deepseek_api_key}",
                "Content-Type": "application/json"}

    @staticmethod
    def _build_messages(system_prompt: str, user_prompt: str) -> list[dict[str, str]]:
        return DeepSeekClient._resolve_messages(None, system_prompt, user_prompt)

    @staticmethod
    def _extract_content(payload: dict[str, Any]) -> str:
        try:
            return str(payload["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelInvocationError("DeepSeek 返回内容格式异常", detail={"payload": payload}) from exc

    @staticmethod
    def _parse_json_content(content: str) -> dict[str, Any]:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            start, end = content.find("{"), content.rfind("}")
            if start != -1 and end != -1 and start < end:
                try:
                    return json.loads(content[start:end + 1])
                except json.JSONDecodeError as exc:
                    raise ModelInvocationError("模型返回的 JSON 无法解析", detail={"content": content[:2000]}) from exc
            raise ModelInvocationError("模型未返回合法 JSON", detail={"content": content[:2000]})
