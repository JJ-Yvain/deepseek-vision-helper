#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L1 模糊测试:边界与畸形输入轰炸,断言"不崩溃、行为合理"。

覆盖:空/畸形 payload、0 图、超长文件名、unicode 文件名、
非 JSON 输入、类型错误字段、千张附件目录。
用法: python test_fuzz.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HOOK = os.path.expanduser("~/.zcode/vision-hook/vision_hook.py")
REAL_CFG = os.path.expanduser("~/.zcode/vision-hook/config.json")
results = []


def rec(name, ok, detail=""):
    results.append((name, ok))
    print(("OK" if ok else "FAIL"), name, ("| " + str(detail) if detail else ""))


def run_hook_stdin(stdin_text, timeout=60):
    """直接喂 stdin(模拟 ZCode 事件管道)。"""
    try:
        p = subprocess.run(["python", HOOK], input=stdin_text, capture_output=True,
                           text=True, timeout=timeout,
                           env=dict(os.environ, VISION_CONFIG=REAL_CFG))
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"


def test_fuzz():
    # 1. 空输入
    code, out, err = run_hook_stdin("")
    rec("1. 空 stdin 静默退出", code == 0 and out == "")

    # 2. 非 JSON
    code, out, err = run_hook_stdin("not-json{{{")
    rec("2. 非 JSON stdin 静默退出", code == 0 and out == "")

    # 3. JSON 数组(类型错误,脚本期望 dict)
    code, out, err = run_hook_stdin("[1,2,3]")
    rec("3. JSON 数组(非 dict)静默", code == 0)

    # 4. 缺字段 payload
    code, out, err = run_hook_stdin(json.dumps({"foo": "bar"}))
    rec("4. 缺字段 payload 静默", code == 0)

    # 5. 畸形 transcript 路径(不存在)
    code, out, err = run_hook_stdin(json.dumps({
        "hook_event_name": "UserPromptSubmit", "session_id": "sess_f",
        "transcript_path": "Z:/nonexistent/xxx.jsonl", "prompt": "hi"}))
    rec("5. 不存在的 transcript 路径不崩溃", code == 0)

    # 6. 超长 prompt
    code, out, err = run_hook_stdin(json.dumps({
        "hook_event_name": "UserPromptSubmit", "session_id": "sess_f",
        "transcript_path": "Z:/nonexistent/x.jsonl", "prompt": "x" * 50000}))
    rec("6. 超长 prompt 不崩溃", code == 0)

    # 7. unicode/emoji 会话 ID 与路径
    code, out, err = run_hook_stdin(json.dumps({
        "hook_event_name": "UserPromptSubmit", "session_id": "会话-测试-🌦️",
        "transcript_path": "Z:/nonexistent/x.jsonl", "prompt": "hi"}))
    rec("7. unicode/emoji 字段不崩溃", code == 0)

    # 8. 千张附件的目录(性能与鲁棒性)
    tmp = tempfile.mkdtemp()
    art = os.path.join(tmp, "art", "sess_f")
    os.makedirs(art)
    for i in range(1000):
        open(os.path.join(art, "prompt-attachment-upload-%d.txt" % i), "w").write(
            "data:image/png;base64,AAAA")
    sys.path.insert(0, os.path.dirname(HOOK))
    import vision_hook as vh
    t0 = time.time()
    files = vh.list_attachments("sess_f", {"artifacts_dir": os.path.join(tmp, "art")})
    dt = time.time() - t0
    rec("8. 千张附件目录扫描(完整+<2s)", len(files) == 1000 and dt < 2.0, "%.2fs" % dt)
    shutil.rmtree(tmp, ignore_errors=True)

    # 9. 超长文件名(Windows 单文件名上限约 255 字符,取 200 内安全值)
    tmp = tempfile.mkdtemp()
    art = os.path.join(tmp, "art", "sess_f")
    os.makedirs(art)
    long_name = "prompt-attachment-upload-" + "x" * 150 + ".txt"
    open(os.path.join(art, long_name), "w").write("data:image/png;base64,AAAA")
    files = vh.list_attachments("sess_f", {"artifacts_dir": os.path.join(tmp, "art")})
    rec("9. 超长文件名不崩溃且被发现", len(files) == 1)
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_fuzz()
    fails = [r for r in results if not r[1]]
    print("L1 模糊: %d/%d 通过" % (len(results) - len(fails), len(results)))
    sys.exit(1 if fails else 0)
