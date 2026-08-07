#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多轮贴图模拟测试:验证 hook 的轮次隔离机制。

- 每轮只识别"当前轮"的新附件(state 记账),历史轮不重复
- 每轮注入带递增时间戳轮次标识
- 与 test_handoff.py 配合:本测试验证"注入侧"机制,
  test_handoff.py 验证"DeepSeek 理解侧"。

用法: python test_multi_round.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

HOOK = os.path.expanduser("~/.zcode/vision-hook/vision_hook.py")
REAL_CFG = os.path.expanduser("~/.zcode/vision-hook/config.json")
TEST_SESS = "sess_test_mr"
ART_DIR = os.path.expanduser("~/.zcode/cli/artifacts/" + TEST_SESS)
EMPTY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_empty.jsonl")


def run_hook():
    p = subprocess.run(["python", HOOK], input=json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "session_id": TEST_SESS, "transcript_path": EMPTY, "prompt": "识别"}),
        capture_output=True, text=True,
        env=dict(os.environ, VISION_CONFIG=REAL_CFG), timeout=960)
    return p.stdout


def main():
    sys.path.insert(0, os.path.dirname(HOOK))
    import vision_hook as vh

    # 准备:测试会话 + 3 张不同来源的附件
    shutil.rmtree(ART_DIR, ignore_errors=True)
    os.makedirs(ART_DIR, exist_ok=True)
    st = vh.load_state()
    st.pop(TEST_SESS, None)
    st.get("_results_path", {}).pop(TEST_SESS, None)
    vh.save_state(st)

    src = os.path.expanduser("~/.zcode/cli/artifacts/sess_8d8c973b-137c-43d9-8694-397c4109d253")
    atts = [f for f in os.listdir(src) if f.startswith("prompt-attachment-upload")][:3]
    uris = [open(os.path.join(src, a), encoding="utf-8").read() for a in atts]
    open(EMPTY, "w").close()

    results = []
    timestamps = []
    for i, uri in enumerate(uris, 1):
        # 每轮放一张"新"附件(模拟用户本轮贴图)
        open(os.path.join(ART_DIR, "prompt-attachment-upload-r%d.txt" % i),
             "w", encoding="utf-8").write(uri)
        out = run_hook()
        m = re.search(r"\[Vision result @(\d{2}:\d{2}:\d{2})\]", out)
        timestamps.append(m.group(1) if m else None)
        # 轮次隔离:本轮注入应只含 1 张图(单图无前缀)或不含"图2:"
        isolated = "图2:" not in out
        results.append(("第%d轮:注入成功+轮次隔离" % i, "[Vision result" in out and isolated))

    # 时间戳递增(轮次可区分)
    increasing = all(a and b and a <= b for a, b in zip(timestamps, timestamps[1:]))
    results.append(("时间戳轮次标识递增", increasing and all(timestamps)))

    # state 记账:3 张全部记账,无累积
    st = vh.load_state()
    known = len(st.get(TEST_SESS, {}))
    results.append(("state 记账=3(无历史轮累积)", known == 3))

    for name, ok in results:
        print(("OK" if ok else "FAIL"), name)
    ok_n = sum(1 for _, r in results if r)
    print("多轮模拟: %d/%d 通过" % (ok_n, len(results)))

    # 清理
    shutil.rmtree(ART_DIR, ignore_errors=True)
    st = vh.load_state()
    st.pop(TEST_SESS, None)
    st.get("_results_path", {}).pop(TEST_SESS, None)
    vh.save_state(st)
    try:
        os.remove(EMPTY)
    except OSError:
        pass
    sys.exit(0 if ok_n == len(results) else 1)


if __name__ == "__main__":
    main()
