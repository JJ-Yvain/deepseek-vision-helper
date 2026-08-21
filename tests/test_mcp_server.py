#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 通道独立性测试: 协议握手 / 帧格式 / 工具契约 / 功能 / 错误处理。

验证 vision_mcp.py（stdio JSON-RPC 薄壳）与 ZCode MCP 客户端（2024-11-05）的契约：
  - initialize 握手响应结构（protocolVersion / capabilities / serverInfo）
  - Content-Length 帧格式（协议传输层）
  - tools/list 工具 schema（模型可见性契约：name/description/inputSchema.required）
  - tools/call vision_recognize：真实图片 + mock API → 文本结果（指纹断言）
  - 错误：文件不存在 → isError；未知工具 → JSON-RPC error；畸形帧不崩溃
  - vision_batch 批量通道（结果落盘 + 未指定 out 的截断）

独立性：临时目录 + mock API + 临时 VISION_CONFIG，不碰真实环境/真实 key。
用法: python test_mcp_server.py
"""
import base64
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mock_api import MockVisionAPI, make_test_config

MCP = os.path.expanduser("~/.zcode/vision-hook/vision_mcp.py")
ROOT = os.path.join(tempfile.gettempdir(), "vhtest", "mcp")
IMG_DIR = os.path.join(ROOT, "img")
OUT_DIR = os.path.join(ROOT, "out")

# 1x1 透明 PNG（真实可解码图片，仅用于走通识别链路）
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

results = []


def rec(name, ok, detail=""):
    results.append((name, ok))
    print(("OK" if ok else "FAIL"), name, ("| " + str(detail) if detail else ""))


def fp_of(path):
    """与 mock 回声一致的指纹: md5(CLI 发出的 data URI 文本)。"""
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8").strip()
        if text.startswith("data:"):
            data_uri = text
        else:
            data_uri = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        data_uri = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    return hashlib.md5(data_uri.encode("utf-8")).hexdigest()[:12]


class MCPClient:
    """极简 MCP stdio 客户端（模拟 ZCode 客户端行为）。"""

    def __init__(self, cfg_path):
        env = dict(os.environ, VISION_CONFIG=cfg_path)
        self.p = subprocess.Popen(
            [sys.executable, MCP], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env)
        self._q = queue.Queue()
        self._t = threading.Thread(target=self._reader, daemon=True)
        self._t.start()
        self._seq = 0

    def _reader(self):
        while True:
            chunk = self.p.stdout.read(1)
            if not chunk:
                return
            header = chunk
            while b"\r\n\r\n" not in header:
                b = self.p.stdout.read(1)
                if not b:
                    return
                header += b
            head, _, _ = header.decode("ascii", "replace").partition("\r\n\r\n")
            n = 0
            for line in head.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    try:
                        n = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        n = 0
            body = self.p.stdout.read(n) if n > 0 else b""
            try:
                self._q.put(json.loads(body.decode("utf-8", "replace")))
            except Exception:
                self._q.put(None)

    def call(self, method, params=None, notify=False):
        self._seq += 1
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = self._seq
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        self.p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.p.stdin.flush()
        if notify:
            return None
        return self._q.get(timeout=60)

    def close(self):
        try:
            self.p.terminate()
        except Exception:
            pass


def test_mcp_server():
    api = MockVisionAPI(mode="ok")
    api.echo = True
    base = make_test_config(api.base_url, timeout_seconds=3)
    cfg_path = os.path.join(ROOT, "_cfg.json")
    os.makedirs(ROOT, exist_ok=True)
    json.dump(base, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False)

    os.makedirs(IMG_DIR, exist_ok=True)
    img = os.path.join(IMG_DIR, "test.png")
    with open(img, "wb") as f:
        f.write(PNG_1PX)

    c = MCPClient(cfg_path)
    try:
        # 1. initialize 握手
        r = c.call("initialize", {"protocolVersion": "2024-11-05",
                                  "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        res = r.get("result", {})
        rec("1. initialize 握手(版本/capabilities/serverInfo)",
            res.get("protocolVersion") == "2024-11-05"
            and "tools" in res.get("capabilities", {})
            and res.get("serverInfo", {}).get("name") == "vision-mcp",
            json.dumps(res, ensure_ascii=False)[:150])
        c.call("notifications/initialized", notify=True)

        # 2. 帧格式: 响应必须是 Content-Length 帧(读 reader 线程已验证可解析, 此处检查 serverInfo 完整)
        rec("2. stdio 帧格式(Content-Length 可解析)", r is not None and "id" in r)

        # 3. tools/list 工具契约
        r = c.call("tools/list")
        tools = r.get("result", {}).get("tools", [])
        names = [t["name"] for t in tools]
        rec_ok = ("vision_recognize" in names and "vision_batch" in names
                  and tools[0]["inputSchema"].get("required") == ["path"]
                  and "description" in tools[0])
        rec("3. tools/list 契约(工具名/description/inputSchema.required)", rec_ok, str(names))

        # 4. tools/call vision_recognize 真实图片 → 指纹断言
        r = c.call("tools/call", {"name": "vision_recognize",
                                  "arguments": {"path": img}})
        text = ""
        try:
            text = r["result"]["content"][0]["text"]
        except Exception:
            pass
        rec("4. vision_recognize 识别真实图片(返回文本+指纹匹配)",
            r.get("result", {}).get("isError") is None and fp_of(img) in text,
            "text=%s" % text[:120])

        # 4b. question 参数透传
        r = c.call("tools/call", {"name": "vision_recognize",
                                  "arguments": {"path": img, "question": "只描述左上角"}})
        ok = "指纹" in r["result"]["content"][0]["text"]
        rec("4b. question 参数透传(识别正常)", ok)

        # 5. 错误: 文件不存在 → isError
        r = c.call("tools/call", {"name": "vision_recognize",
                                  "arguments": {"path": os.path.join(IMG_DIR, "nope.png")}})
        res = r.get("result", {})
        rec("5. 文件不存在 → isError=true", res.get("isError") is True
            and "不存在" in res["content"][0]["text"])

        # 6. 错误: 未知工具 → JSON-RPC error
        r = c.call("tools/call", {"name": "no_such_tool", "arguments": {}})
        rec("6. 未知工具 → JSON-RPC error(-32602)", r.get("error", {}).get("code") == -32602)

        # 7. ping
        r = c.call("ping")
        rec("7. ping → result{}", r.get("result") == {})

        # 8. 畸形帧(非 JSON) → 不崩溃, 后续请求正常
        raw = b"this-is-not-json"
        c.p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(raw) + raw)
        c.p.stdin.flush()
        time.sleep(0.5)
        r = c.call("ping")
        rec("8. 畸形帧后进程存活且响应正常", r.get("result") == {})

        # 9. vision_batch: 指定 out → 结果落盘
        batch_img = os.path.join(IMG_DIR, "b2.png")
        with open(batch_img, "wb") as f:
            f.write(PNG_1PX)
        out_file = os.path.join(OUT_DIR, "batch.md")
        r = c.call("tools/call", {"name": "vision_batch",
                                  "arguments": {"folder": IMG_DIR, "out": out_file}})
        text = r["result"]["content"][0]["text"]
        rec("9. vision_batch 批量识别(结果落盘)",
            "已写入" in text and os.path.isfile(out_file), text)

        # 10. vision_batch 未指定 out → 返回文本(含识别结果)
        r = c.call("tools/call", {"name": "vision_batch",
                                  "arguments": {"folder": IMG_DIR}})
        text = r["result"]["content"][0]["text"]
        rec("10. vision_batch 未指定 out(返回内联文本)", "图指纹" in text or "识别" in text)

        # 11. 目录不存在 → isError
        r = c.call("tools/call", {"name": "vision_batch",
                                  "arguments": {"folder": os.path.join(IMG_DIR, "no")}})
        res = r.get("result", {})
        rec("11. 批量目录不存在 → isError=true", res.get("isError") is True)
    finally:
        c.close()

    api.stop()


if __name__ == "__main__":
    shutil.rmtree(ROOT, ignore_errors=True)
    test_mcp_server()
    fails = [r for r in results if not r[1]]
    print("MCP 通道: %d/%d 通过" % (len(results) - len(fails), len(results)))
    sys.exit(1 if fails else 0)
