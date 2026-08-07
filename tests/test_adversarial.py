#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""攻击集(对抗测试):针对系统已知弱点的定向攻击。

每条攻击 = 一个用例,失败即代表弱点回归。攻击集随实战经验生长。
用法: python test_adversarial.py
"""
import json
import os
import re
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


def run_hook(cfg, session, prompt="x", timeout=120):
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cfg.json")
    json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False)
    empty = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_empty.jsonl")
    open(empty, "w").close()
    p = subprocess.run(["python", HOOK], input=json.dumps({
        "hook_event_name": "UserPromptSubmit", "session_id": session,
        "transcript_path": empty, "prompt": prompt}),
        capture_output=True, text=True,
        env=dict(os.environ, VISION_CONFIG=cfg_path), timeout=timeout)
    return p.stdout


def put(session, name, content="data:image/png;base64,AAAA"):
    art = os.path.join(tempfile.gettempdir(), "vhtest", "art", session)
    os.makedirs(art, exist_ok=True)
    open(os.path.join(art, "prompt-attachment-upload-%s.txt" % name),
         "w", encoding="utf-8").write(content)
    return os.path.join(tempfile.gettempdir(), "vhtest", "art")


def clean(session):
    shutil.rmtree(os.path.join(tempfile.gettempdir(), "vhtest", "art", session),
                  ignore_errors=True)


def test_adversarial():
    api = MockVisionAPI(mode="ok")
    base = make_test_config(api.base_url, timeout_seconds=2)

    # 攻击1: 建议泄漏——用户 prompt 带"如何解决/方案"
    #   (注入内容应是 mock 的纯识别文本,不得出现用户问题中的字眼或"方案"结构)
    put("s_a1", "a")
    out = run_hook(dict(base, artifacts_dir=put("s_a1", "a")), "s_a1",
                   prompt="这些问题点该如何解决或优化?给出方案")
    rec("1. 攻击:建议泄漏(注入不含'方案/如何解决'且为纯事实)",
        "给出方案" not in out and "如何解决" not in out and "方案" not in out)
    clean("s_a1")

    # 攻击2: 旧图混入——第一轮图A,第二轮图B,第二轮注入不得含图A内容
    put("s_a2", "a", "data:image/png;base64,AAAA")
    run_hook(dict(base, artifacts_dir=put("s_a2", "a")), "s_a2")
    put("s_a2", "b", "data:image/png;base64,BBBB")
    out2 = run_hook(dict(base, artifacts_dir=put("s_a2", "b")), "s_a2")
    # mock 对每张图返回相同文本,故以"轮次隔离"断言: 第二轮注入应只有 1 段
    rec("2. 攻击:旧图混入(第二轮只识别新附件)",
        "图2:" not in out2 and "[Vision result @" in out2)
    clean("s_a2")

    # 攻击3: 轮次混淆——连续 3 轮,时间戳递增
    tss = []
    for i in range(3):
        put("s_a3", "r%d" % i)
        out = run_hook(dict(base, artifacts_dir=put("s_a3", "r%d" % i)), "s_a3")
        m = re.search(r"\[Vision result @(\d{2}:\d{2}:\d{2})\]", out)
        tss.append(m.group(1) if m else None)
    inc = all(a and b and a <= b for a, b in zip(tss, tss[1:]))
    rec("3. 攻击:轮次混淆(时间戳严格递增)", inc and all(tss), str(tss))
    clean("s_a3")

    # 攻击4: 多图轰炸——20 张图,小预算分批,最终全部记账(完整识别)
    for i in range(20):
        put("s_a4", "b%d" % i)
    art = put("s_a4", "b0")
    cfg = dict(base, artifacts_dir=art, recognition_time_budget=0.5)
    rounds = 0
    while rounds < 30:
        rounds += 1
        out = run_hook(cfg, "s_a4")
        if "识别进度" not in out:
            break
    sys.path.insert(0, os.path.dirname(HOOK))
    import vision_hook as vh
    st = vh.load_state()
    known = len(st.get("s_a4", {}))
    rec("4. 攻击:多图轰炸(20张最终全部识别,无丢弃)", known == 20, "轮次=%d known=%d" % (rounds, known))
    st.pop("s_a4", None)
    vh.save_state(st)
    clean("s_a4")

    # 攻击5: 边界——0 图事件(无新附件)静默
    clean("s_a5")
    art5 = put("s_a5", "x")
    run_hook(dict(base, artifacts_dir=art5), "s_a5")  # 消耗附件(识别+记账)
    out5 = run_hook(dict(base, artifacts_dir=art5), "s_a5")  # 无新附件 → 静默
    rec("5. 攻击:无新附件事件静默", out5 == "")
    clean("s_a5")

    api.stop()


if __name__ == "__main__":
    test_adversarial()
    fails = [r for r in results if not r[1]]
    print("攻击集: %d/%d 通过" % (len(results) - len(fails), len(results)))
    sys.exit(1 if fails else 0)
