#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享 Mock 视觉 API:本地 HTTP 服务器,模拟 agnes/mimo 的 chat/completions。

支持故障注入与请求记录,供 L0-L3 快速档测试使用(确定性、无网络、秒级)。

用法:
    from _mock_api import MockVisionAPI
    api = MockVisionAPI(mode="ok")          # 启动,随机端口
    base_url = api.base_url                  # http://127.0.0.1:<port>/v1
    api.requests                             # 所有收到的请求体(断言用)
    api.set_mode("fail500")                  # 运行中切换故障模式
    api.stop()
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# 可注入的响应内容(模拟视觉 API 的识别文本)
FAKE_OK_TEXT = "这是一个终端窗口截图，显示 GitHub CLI 登录界面，包含 HTTPS 和 SSH 两个选项。"


class _Handler(BaseHTTPRequestHandler):
    api = None  # 由 MockVisionAPI 注入

    def log_message(self, *a):
        pass  # 静默访问日志

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", "replace")
        try:
            req = json.loads(body)
        except Exception:
            req = {"_raw": body}
        self.api.requests.append(req)

        mode = self.api.mode
        if mode == "fail500":
            self._send(500, {"error": {"message": "mock 500"}})
        elif mode == "fail429":
            self._send(429, {"error": {"message": "mock rate limited"}})
        elif mode == "invalid_key":
            self._send(401, {"error": {"message": "mock invalid key"}})
        elif mode == "malformed":
            self._send(200, "not-json-at-all")
        elif mode == "empty":
            self._send(200, {})
        elif mode == "slow":
            import time
            time.sleep(5)  # 模拟超时
            self._send(200, self._ok_payload())
        else:  # ok
            self._send(200, self._ok_payload())

    def _ok_payload(self):
        return {
            "choices": [{"message": {"content": FAKE_OK_TEXT}, "index": 0}],
            "model": "mock-vision",
        }

    def _send(self, code, obj):
        if isinstance(obj, str):
            data = obj.encode("utf-8")
        else:
            data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class MockVisionAPI:
    """启动一个可故障注入的 mock 视觉 API。"""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.requests = []
        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        _Handler.api = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = "http://127.0.0.1:%d/v1" % self._server.server_port

    def set_mode(self, mode):
        self.mode = mode

    @property
    def last_request(self):
        return self.requests[-1] if self.requests else None

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


def make_test_config(api_base_url, **overrides):
    """构造指向 mock API 的测试配置(真实 key 结构,base_url 指向 mock)。"""
    cfg = {
        "provider": "mock",
        "batch_provider": "mock",
        "fallback_provider": "mock",
        "batch_threshold": 3,
        "max_images": 4,
        "per_image_max_chars": 2000,
        "total_max_chars": 8000,
        "max_image_bytes": 10485760,
        "max_tokens": 4000,
        "timeout_seconds": 3,
        "log_max_bytes": 1048576,
        "skip_when_multimodal": False,
        "recognition_time_budget": 900,
        "update_check_interval_hours": 24,
        "default_question": "请识别这张图片：如果包含文字请完整提取；如果是界面、报错页面或图表，请说明其内容与含义。只输出图片中的事实，不要分析、建议或反问。",
        "providers": {
            "mock": {
                "base_url": api_base_url.rstrip("/") + "/chat/completions",
                "api_key": "mock-key",
                "model": "mock-vision",
                "header": "Authorization",
                "auth_prefix": "Bearer ",
            }
        },
    }
    cfg.update(overrides)
    return cfg
