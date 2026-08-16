# -*- coding: utf-8 -*-
"""DeepSeek API 客户端（无第三方 SDK，失败自动降级）。"""
import json
import os

from . import utils

BASE_URL = "https://api.deepseek.com"


class DeepSeekClient:
    def __init__(self, api_key: str | None = None, model: str = "deepseek-chat",
                 timeout: int = 90, max_output_tokens: int = 1200):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "").strip()
        self.model = model
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def chat_json(self, system: str, user: str, expect_keys=None):
        """请求模型返回 JSON 对象；失败返回 None。"""
        if not self.available:
            return None
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
            "max_tokens": self.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        status, body = utils.http_post_json(
            f"{BASE_URL}/chat/completions",
            payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        if status != 200 or not body:
            print(f"[ai] DeepSeek 调用失败: {body}")
            return None
        try:
            content = body["choices"][0]["message"]["content"]
            data = json.loads(content)
            if expect_keys and not all(k in data for k in expect_keys):
                print(f"[ai] 返回缺少字段 {expect_keys}: {data}")
                return None
            return data
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            print(f"[ai] 解析失败: {e}")
            return None
