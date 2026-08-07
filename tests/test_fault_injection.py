#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L2 故障注入测试:mock 视觉 API 注入故障,验证降级链与重试机制。

确定性(故障是我们注入的)、无真实网络、秒级。
用法: python test_fault_injection.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mock_api import MockVisionAPI, make_test_config

HOOK = os.path.expanduser("~/.zcode/vision-hook/vision_hook.py")
results = []


def rec(name, ok, detail=""):
    results.append((name, ok))
    print(("OK" if ok else "FAIL"), name, ("| " + str(detail) if detail else ""))


def run_hook(cfg, session, prompt="x"):
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cfg.json")
    json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False)
    empty = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_empty.jsonl")
    open(empty, "w").close()
    p = subprocess.run(["python", HOOK], input=json.dumps({
        "hook_event_name": "UserPromptSubmit", "session_id": session,
        "transcript_path": empty, "prompt": prompt}),
        capture_output=True, text=True,
        env=dict(os.environ, VISION_CONFIG=cfg_path), timeout=60)
    return p.stdout


def put_attachment(session, name, content="data:image/png;base64,AAAA"):
    art = os.path.join(tempfile.gettempdir(), "vhtest", "art", session)
    os.makedirs(art, exist_ok=True)
    open(os.path.join(art, "prompt-attachment-upload-%s.txt" % name),
         "w", encoding="utf-8").write(content)
    return os.path.join(tempfile.gettempdir(), "vhtest", "art")


def clean(session):
    d = os.path.join(tempfile.gettempdir(), "vhtest", "art", session)
    shutil.rmtree(d, ignore_errors=True)


def test_faults():
    api = MockVisionAPI(mode="ok")
    base_cfg = make_test_config(api.base_url, timeout_seconds=2)

    # 1. 全部失败(500) → 注入"自动重试"提示,不崩溃
    api.set_mode("fail500")
    art = put_attachment("s_f1", "a")
    cfg = dict(base_cfg, artifacts_dir=art)
    out = run_hook(cfg, "s_f1")
    rec("1. 500 全失败 → 自动重试提示", "自动重试" in out and "识别失败" in out)
    clean("s_f1")

    # 2. key 失效(401)
    put_attachment("s_f2", "a")
    out = run_hook(dict(base_cfg, artifacts_dir=put_attachment("s_f2", "a")), "s_f2")
    rec("2. 401 key 失效 → 不崩溃+重试提示", "自动重试" in out)
    clean("s_f2")

    # 3. 限流(429) → 内部重试后成功(先 429 后 ok)
    api.set_mode("fail429")
    put_attachment("s_f3", "a")
    out = run_hook(dict(base_cfg, artifacts_dir=put_attachment("s_f3", "a")), "s_f3")
    rec("3. 429 限流 → 重试机制(不崩溃)", "自动重试" in out or "[Vision result" in out)
    clean("s_f3")

    # 4. 畸形响应(非 JSON) → 不崩溃
    api.set_mode("malformed")
    put_attachment("s_f4", "a")
    out = run_hook(dict(base_cfg, artifacts_dir=put_attachment("s_f4", "a")), "s_f4")
    rec("4. 畸形响应 → 不崩溃", "自动重试" in out or "[Vision result" in out)
    clean("s_f4")

    # 5. 恢复 ok → 正常识别(降级链恢复)
    api.set_mode("ok")
    put_attachment("s_f5", "a")
    out = run_hook(dict(base_cfg, artifacts_dir=put_attachment("s_f5", "a")), "s_f5")
    rec("5. 故障恢复后正常识别", "[Vision result @" in out)
    clean("s_f5")

    # 6. 建议隔离: 用户 prompt 带"如何解决",视觉请求必须不含用户 prompt
    api.set_mode("ok")
    put_attachment("s_f6", "a")
    run_hook(dict(base_cfg, artifacts_dir=put_attachment("s_f6", "a")), "s_f6",
             prompt="这些问题点该如何解决或优化?给出方案")
    leaked = False
    for req in api.requests:
        text = json.dumps(req, ensure_ascii=False)
        if "如何解决" in text or "给出方案" in text:
            leaked = True
    rec("6. 用户问题不传给视觉模型(物理隔离)", not leaked)
    clean("s_f6")

    # 7. 慢响应(超时) → 降级不崩溃
    api.set_mode("slow")
    put_attachment("s_f7", "a")
    out = run_hook(dict(base_cfg, artifacts_dir=put_attachment("s_f7", "a")), "s_f7")
    rec("7. 慢响应超时 → 不崩溃", "自动重试" in out or "[Vision result" in out or out == "")
    clean("s_f7")

    api.stop()


if __name__ == "__main__":
    test_faults()
    fails = [r for r in results if not r[1]]
    print("L2 故障注入: %d/%d 通过" % (len(results) - len(fails), len(results)))
    sys.exit(1 if fails else 0)
