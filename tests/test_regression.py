#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试(固化):hook 8 项关键路径,防止改动引入回归。

用法: python test_regression.py
"""
import base64
import glob
import json
import os
import shutil
import subprocess
import sys

HOOK = os.path.expanduser("~/.zcode/vision-hook/vision_hook.py")
REAL_CFG = os.path.expanduser("~/.zcode/vision-hook/config.json")
TEST_CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cfg.json")
TEST_SESS = "sess_test_reg"
ART_DIR = os.path.expanduser("~/.zcode/cli/artifacts/" + TEST_SESS)
RES_DIR = os.path.expanduser("~/.zcode/vision-hook/results")
EMPTY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_empty.jsonl")

results = []


def rec(name, ok):
    results.append((name, ok))
    print(("OK" if ok else "FAIL"), name)


def run(args, stdin=None, env=None, timeout=960):
    p = subprocess.run(["python", HOOK] + args, input=stdin, capture_output=True,
                       text=True, env=dict(os.environ, **(env or {})), timeout=timeout)
    return p.stdout, p.returncode


def reset():
    shutil.rmtree(ART_DIR, ignore_errors=True)
    os.makedirs(ART_DIR, exist_ok=True)
    sys.path.insert(0, os.path.dirname(HOOK))
    import vision_hook as vh
    st = vh.load_state()
    st.pop(TEST_SESS, None)
    st.get("_results_path", {}).pop(TEST_SESS, None)
    vh.save_state(st)


def add_att(name, uri):
    open(os.path.join(ART_DIR, "prompt-attachment-upload-" + name + ".txt"),
         "w", encoding="utf-8").write(uri)


def main():
    real = json.load(open(REAL_CFG, encoding="utf-8"))
    src = os.path.expanduser("~/.zcode/cli/artifacts/sess_8d8c973b-137c-43d9-8694-397c4109d253")
    att = [f for f in os.listdir(src) if f.startswith("prompt-attachment-upload")][0]
    uri = open(os.path.join(src, att), encoding="utf-8").read()
    img = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_t.png")
    open(img, "wb").write(base64.b64decode(uri.split(",", 1)[1]))
    open(EMPTY, "w").close()
    ENV = {"VISION_CONFIG": TEST_CFG}

    reset()
    add_att("a", uri)
    json.dump(real, open(TEST_CFG, "w", encoding="utf-8"), ensure_ascii=False)
    out, _ = run([], json.dumps({"hook_event_name": "UserPromptSubmit",
                                 "session_id": TEST_SESS, "transcript_path": EMPTY,
                                 "prompt": "识别"}), env=ENV)
    rec("1. hook 单图注入(时间戳前缀)", "[Vision result @" in out)
    out2, _ = run([], json.dumps({"hook_event_name": "PreToolUse", "session_id": TEST_SESS,
                                  "transcript_path": EMPTY, "tool_name": "Bash"}), env=ENV)
    rec("2. 重复触发去重", out2.strip() == "")
    reset()
    add_att("b1", uri)
    add_att("b2", uri)
    out3, _ = run([], json.dumps({"hook_event_name": "UserPromptSubmit",
                                  "session_id": TEST_SESS, "transcript_path": EMPTY,
                                  "prompt": "x"}), env=ENV)
    rec("3. 双图注入", "图1:" in out3 and "图2:" in out3)
    reset()
    add_att("c", uri)
    cfg = json.loads(json.dumps(real))
    cfg["total_max_chars"] = 100
    cfg["per_image_max_chars"] = 100
    json.dump(cfg, open(TEST_CFG, "w", encoding="utf-8"), ensure_ascii=False)
    out4, _ = run([], json.dumps({"hook_event_name": "UserPromptSubmit",
                                  "session_id": TEST_SESS, "transcript_path": EMPTY,
                                  "prompt": "x"}), env=ENV)
    files = glob.glob(os.path.join(RES_DIR, TEST_SESS + "_*.md"))
    rec("4. 超限落盘+提示", "完整结果已存至" in out4 and len(files) > 0)
    reset()
    add_att("d", uri)
    cfg = json.loads(json.dumps(real))
    cfg["max_image_bytes"] = 100
    json.dump(cfg, open(TEST_CFG, "w", encoding="utf-8"), ensure_ascii=False)
    out5, _ = run([], json.dumps({"hook_event_name": "UserPromptSubmit",
                                  "session_id": TEST_SESS, "transcript_path": EMPTY,
                                  "prompt": "x"}), env=ENV)
    rec("5. 大图尽力识别+标注", "[Vision result" in out5 and "超过大小限制" in out5)
    reset()
    add_att("e", uri)
    cfg = json.loads(json.dumps(real))
    cfg["skip_when_multimodal"] = True
    json.dump(cfg, open(TEST_CFG, "w", encoding="utf-8"), ensure_ascii=False)
    out6, _ = run([], json.dumps({"hook_event_name": "UserPromptSubmit",
                                  "session_id": TEST_SESS, "transcript_path": EMPTY,
                                  "prompt": "x"}), env=ENV)
    rec("6. skip_when_multimodal", out6.strip() == "")
    out7, code7 = run(["--files", img, "--question", "描述"], env={"VISION_CONFIG": TEST_CFG})
    rec("7. CLI 单图纯文本", code7 == 0 and len(out7.strip()) > 20)
    placeholder = json.loads(json.dumps(real))
    for p in placeholder["providers"].values():
        p["api_key"] = "YOUR_X"
    json.dump(placeholder, open(TEST_CFG, "w", encoding="utf-8"), ensure_ascii=False)
    out8, code8 = run(["--files", img], env={"VISION_CONFIG": TEST_CFG})
    rec("8. 未配置 key 引导+exit1", "未配置可用的 API key" in out8 and code8 == 1)

    shutil.rmtree(ART_DIR, ignore_errors=True)
    sys.path.insert(0, os.path.dirname(HOOK))
    import vision_hook as vh
    st = vh.load_state()
    st.pop(TEST_SESS, None)
    st.get("_results_path", {}).pop(TEST_SESS, None)
    vh.save_state(st)
    for f in glob.glob(os.path.join(RES_DIR, TEST_SESS + "_*.md")):
        os.remove(f)
    for f in [TEST_CFG, img, EMPTY]:
        try:
            os.remove(f)
        except OSError:
            pass
    fails = [r for r in results if not r[1]]
    print("回归: %d/%d 通过" % (len(results) - len(fails), len(results)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
