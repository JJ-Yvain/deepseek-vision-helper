#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZCode Vision MCP Server —— 把视觉识别能力注册为 MCP 工具，
让主 Agent / 子 Agent 在"需要识图"时看得见、调得动（用户贴图之外的任务场景）。

设计原则：薄壳，不复制任何识别逻辑。
  - 工具调用 = subprocess 调 vision_hook.py CLI（--files / --folder / --question）
  - 识别后端 / provider 路由 / 降级 / 配置 / 日志 全部复用 vision_hook.py —— 单点维护：
    后续优化 hook/CLI，MCP 自动继承，不会失效或脱节。
  - 与 hook 的关系：hook 管"用户贴图自动识别"（被动）；本 server 管"任务中遇图主动识别"（主动）。

协议：MCP 2024-11-05，JSON-RPC 2.0 over stdio（Content-Length 帧）。
方法：initialize / notifications/initialized / ping / tools/list / tools/call。
日志：写入同目录 vision_mcp.log（协议帧只走 stdout，日志不污染协议）。
"""
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(_HERE, "vision_hook.py")
LOG = os.path.join(_HERE, "vision_mcp.log")
PROTOCOL_VERSION = "2024-11-05"
SERVER_VERSION = "0.1.0"
CALL_TIMEOUT = 300  # 单次识别调用超时(秒), 与 hook 的识别耗时量级一致
MAX_INLINE_BATCH = 3000  # vision_batch 未指定 out 时返回文本的上限


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg + "\n")
    except Exception:
        pass


TOOLS = [
    {
        "name": "vision_recognize",
        "description": (
            "识别本地图片文件的内容并返回文本描述。"
            "当需要理解图片/截图/图表/表格/报错界面/UI 参考图时使用——"
            "包括任务中遇到的图片文件（不限于用户贴图）。"
            "path 为图片文件绝对路径；question 可指定关注点（如'完整提取文字'/'只描述左上角区域'），不填则通用识别。"
            "识别结果是纯事实文本，不含分析建议。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "图片文件绝对路径（png/jpg/jpeg/webp/gif/bmp）"},
                "question": {"type": "string",
                             "description": "可选。具体识别问题，例如'请完整提取文字'、'描述左上角区域'"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "vision_batch",
        "description": (
            "批量识别目录下所有图片，结果写入文件后返回文件路径"
            "（避免大量文本灌入上下文）。当需要分析多张图片/整个文件夹时使用。"
            "建议指定 out 参数；不指定时返回截断文本。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "图片目录绝对路径（递归扫描）"},
                "out": {"type": "string", "description": "可选。结果文件路径（推荐指定）"},
            },
            "required": ["folder"],
        },
    },
]


# ---------- stdio 传输（Content-Length 帧） ----------

def send(obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()


def read_message():
    """从 stdin 读一帧, 返回 body 字节; EOF 返回 None。"""
    headers = {}
    line = sys.stdin.buffer.readline()
    while line and line.strip():
        k, _, v = line.decode("ascii", "replace").partition(":")
        headers[k.strip().lower()] = v.strip()
        line = sys.stdin.buffer.readline()
    if not line:
        return None  # EOF
    n = 0
    try:
        n = int(headers.get("content-length", "0"))
    except ValueError:
        n = 0
    return sys.stdin.buffer.read(n) if n > 0 else b""


# ---------- JSON-RPC 分发 ----------

def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "vision-mcp", "version": SERVER_VERSION},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        return call_tool(mid, msg.get("params") or {})
    if method and method.startswith("notifications/"):
        return None  # 通知无需响应
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": "method not found: %s" % method}}


def call_tool(mid, params):
    name = params.get("name")
    args = params.get("arguments") or {}
    if name == "vision_recognize":
        return tool_recognize(mid, args)
    if name == "vision_batch":
        return tool_batch(mid, args)
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32602, "message": "unknown tool: %s" % name}}


def tool_error(mid, message):
    return {"jsonrpc": "2.0", "id": mid, "result": {
        "content": [{"type": "text", "text": message}], "isError": True}}


def run_cli(cmd):
    """调 vision_hook.py CLI, 成功返回 (True, 文本); 失败返回 (False, 错误信息)。

    强制子进程 stdout 用 UTF-8（PYTHONIOENCODING），避免 Windows 管道编码
    （gbk）与协议层 UTF-8 解码不一致导致乱码。
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run(cmd, capture_output=True, env=env, timeout=CALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "识别超时（%ds），请稍后重试或换更小的图片" % CALL_TIMEOUT
    except Exception as e:
        return False, "调用识别脚本失败: %s" % e
    text = (p.stdout or b"").decode("utf-8", "replace").strip()
    if not text:
        return False, ("识别无输出（可能未配置 API key 或全部识别失败）; stderr: %s"
                       % (p.stderr or b"")[:300].decode("utf-8", "replace"))
    return True, text


def tool_recognize(mid, args):
    path = str(args.get("path") or "").strip()
    if not path or not os.path.isfile(path):
        return tool_error(mid, "图片文件不存在或不可读: %s" % path)
    question = str(args.get("question") or "").strip() or None
    cmd = [sys.executable, HOOK, "--files", path]
    if question:
        cmd += ["--question", question]
    ok, text = run_cli(cmd)
    if not ok:
        return tool_error(mid, text)
    return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}}


def tool_batch(mid, args):
    folder = str(args.get("folder") or "").strip()
    if not folder or not os.path.isdir(folder):
        return tool_error(mid, "目录不存在或不可读: %s" % folder)
    out = str(args.get("out") or "").strip() or None
    if out:
        # CLI 的 --out 不自动建目录, 这里补齐(新增代码, 不动既有 CLI 行为)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
        except OSError as e:
            return tool_error(mid, "无法创建结果目录: %s" % e)
    cmd = [sys.executable, HOOK, "--folder", folder]
    if out:
        cmd += ["--out", out]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run(cmd, capture_output=True, env=env, timeout=CALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return tool_error(mid, "批量识别超时（%ds），建议分批或指定 out 后重试" % CALL_TIMEOUT)
    except Exception as e:
        return tool_error(mid, "调用识别脚本失败: %s" % e)
    stderr = (p.stderr or b"")[:300].decode("utf-8", "replace")
    if out:
        # CLI 批量落盘模式 stdout 为空是正常行为, 以结果文件判定成败
        if os.path.isfile(out):
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "批量识别完成，结果已写入: %s" % out}]}}
        return tool_error(mid, "批量识别失败（结果文件未生成）; stderr: %s" % stderr)
    text = (p.stdout or b"").decode("utf-8", "replace").strip()
    if not text:
        return tool_error(mid, "批量识别无输出; stderr: %s" % stderr)
    if len(text) > MAX_INLINE_BATCH:
        text = text[:MAX_INLINE_BATCH] + "\n…（结果过长已截断，建议指定 out 参数写入文件后读取）"
    return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}}


def main():
    log("vision mcp server started (protocol %s)" % PROTOCOL_VERSION)
    while True:
        body = read_message()
        if body is None:
            break  # stdin 关闭(客户端退出)
        if not body:
            continue
        try:
            msg = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            log("malformed frame ignored")
            continue  # 畸形帧忽略, 不崩溃
        if not isinstance(msg, dict):
            continue
        resp = handle(msg)
        if resp is not None:
            send(resp)


if __name__ == "__main__":
    main()
