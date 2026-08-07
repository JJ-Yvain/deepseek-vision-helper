#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交接质量测试:用真正的 DeepSeek(deepseek-v4-flash)作为测试主模型,
验证多轮贴图 + 上下文积累场景下的视觉信息交接质量。

用法:
  python test_handoff.py            # 全部用例
  python test_handoff.py --case 1   # 单用例

依赖:ZCode 配置中的 OpencodeGo provider(提供 deepseek-v4-flash)。
"""
import json
import os
import sys
import urllib.request

ZCODE_V2 = os.path.expanduser("~/.zcode/v2/config.json")
PROVIDER_ID = "2d8cbd9f-ee6f-46aa-b13d-33aae0096dc2"  # OpencodeGo
BASE_URL = "https://opencode.ai/zen/go/v1/chat/completions"
MODEL = "deepseek-v4-flash"


def _get_key():
    d = json.load(open(ZCODE_V2, encoding="utf-8"))
    return d["provider"][PROVIDER_ID]["options"]["apiKey"]


def ask_deepseek(messages, max_tokens=600):
    """调用真正的 DeepSeek 作为测试主模型。"""
    payload = {"model": MODEL, "messages": messages,
               "max_tokens": max_tokens, "stream": False}
    req = urllib.request.Request(
        BASE_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + _get_key(),
                 "User-Agent": "vision-handoff-test/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read().decode())
    return resp["choices"][0]["message"]["content"].strip()


# ---- 模拟的 3 轮识别结果(内容完全不同,便于验证轮次区分) ----
ROUNDS = [
    {"ts": "09:10:00", "text": "图1: 这是一张天气应用截图，显示北京今天晴，气温 23 度，空气质量良。"},
    {"ts": "09:15:00", "text": "图1: 这是一张 Excel 表格截图，标题为「Q2 销售数据」，包含 5 行 3 列数据。"},
    {"ts": "09:20:00", "text": "图1: 这是一张代码编辑器的截图，显示 Python 代码，函数名为 calculate_average。"},
]


def build_context(rounds):
    """按真实注入格式拼接多轮上下文(与 hook 输出格式一致)。"""
    parts = []
    for r in rounds:
        parts.append("[Vision result @%s] %s" % (r["ts"], r["text"]))
    return "\n\n".join(parts)


def case_latest_round():
    """用例 1:主模型应识别最新一轮(第 3 轮)。"""
    ctx = build_context(ROUNDS)
    messages = [
        {"role": "system", "content": "你是 DeepSeek 助手。上下文中可能有多轮 [Vision result] 图片识别结果，"
                                      "以最新时间戳的段落为最新一轮。"},
        {"role": "user", "content": ctx + "\n\n问题：用户最新一轮发的图片内容是什么？只回答内容，不要解释。"},
    ]
    ans = ask_deepseek(messages)
    ok = ("代码" in ans) or ("calculate_average" in ans) or ("编辑器" in ans)
    print("1. 最新轮识别(应指第3轮代码截图):", "OK" if ok else "FAIL", "|", ans[:80])
    return ok


def case_round_reference():
    """用例 2:主模型能回答历史轮次(用户说'之前那张')。"""
    ctx = build_context(ROUNDS)
    messages = [
        {"role": "system", "content": "你是 DeepSeek 助手。上下文含多轮 [Vision result] 识别结果，"
                                      "以最新时间戳为最新一轮。"},
        {"role": "user", "content": ctx + "\n\n问题：用户说'还记得第一次发的那张图吗'，那张图是什么内容？只回答内容。"},
    ]
    ans = ask_deepseek(messages)
    ok = ("天气" in ans) or ("23" in ans) or ("晴" in ans)
    print("2. 历史轮引用(应指第1轮天气截图):", "OK" if ok else "FAIL", "|", ans[:80])
    return ok


def case_multi_image_reference():
    """用例 3:多图时按图号引用。"""
    ctx = "[Vision result @10:00:00] 图1: 登录页面截图，有用户名和密码输入框。\n图2: 报错弹窗截图，提示「密码错误」。\n\n问题：用户问'图2 是什么'？"
    messages = [{"role": "user", "content": ctx}]
    ans = ask_deepseek(messages)
    ok = ("密码错误" in ans) or ("报错" in ans)
    print("3. 图号引用(应指图2报错弹窗):", "OK" if ok else "FAIL", "|", ans[:80])
    return ok


def case_no_confusion():
    """用例 4:最新轮与历史轮内容相似时,不被历史轮干扰。"""
    ctx = ("[Vision result @10:00:00] 图1: 天气应用截图，显示北京 23 度。\n\n"
           "[Vision result @10:05:00] 图1: 天气应用截图，显示上海 31 度。\n\n"
           "问题：用户最新发的截图显示哪个城市多少度？")
    messages = [{"role": "user", "content": ctx}]
    ans = ask_deepseek(messages)
    ok = ("上海" in ans) and ("31" in ans)
    print("4. 相似内容轮次不混淆(应答上海31度):", "OK" if ok else "FAIL", "|", ans[:80])
    return ok


CASES = {
    1: case_latest_round,
    2: case_round_reference,
    3: case_multi_image_reference,
    4: case_no_confusion,
}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="交接质量测试(DeepSeek 实测)")
    ap.add_argument("--case", type=int, choices=CASES.keys(), help="只跑指定用例")
    args = ap.parse_args()
    if args.case:
        cases = [args.case]
    else:
        cases = list(CASES.keys())
    results = []
    for c in cases:
        try:
            results.append((c, CASES[c]()))
        except Exception as e:
            print("用例 %d 异常: %s" % (c, str(e)[:150]))
            results.append((c, False))
    ok = sum(1 for _, r in results if r)
    print("交接质量: %d/%d 通过" % (ok, len(results)))
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    main()
