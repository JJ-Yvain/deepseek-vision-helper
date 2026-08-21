#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立性复现测试: 粘贴错图→删图→换图→提交, 从未发送的旧图不得被识别注入。

事故场景(用户 2026-08-11 实测报告):
  用户在输入框贴了一张截图(截错了) → 从输入框删除 → 重新截图粘贴并提交。
  视觉 hook 却识别出了"删掉的那张旧图"的内容 —— 一张从未真正发送的图。

平台事实(源码级, ZCode 3.7.x zcode.cjs / app.asar):
  1. 附件在粘贴时即物化写盘(writePromptAttachment → artifacts/<会话>/
     prompt-attachment-upload-*.txt), 文件名每次粘贴唯一, retention=session
     但无任何代码消费它;
  2. UI 移除附件(removeAttachment)只撤销 objectURL + 取消传输, 没有任何
     删文件 RPC → 被删除的附件文件永久残留磁盘;
  3. hook 的 UserPromptSubmit transcript 只含 prompt 纯文本
     (F2n("user", e.prompt)), 不含任何附件信号;
  4. 唯一权威信号: ~/.zcode/cli/db/db.sqlite 的 input_history.attachments,
     记录本次实际提交的附件(zcode-artifact:// URI), 实测写入时机早于
     hook 触发约 1s。

本测试在 %TEMP%/vhtest/sta 下模拟该场景(独立于真实环境, 不碰真实
artifacts/DB/state, 会话名唯一并在结尾清理):
  文件 A(错图, 先贴后删) + 文件 B(正确图, 后贴并提交) 都残留在磁盘;
  DB 中 input_history 只记录 B(本次实际提交的附件)。
断言: 注入内容只包含 B 的识别指纹, 不得包含 A。

旧版 hook 行为: 扫描全部未记账文件 → A、B 都被识别注入 → 本测试 FAIL(复现 bug)。
修复后: 以 input_history.attachments 为权威信号, 只识别 B → PASS。

用法: python test_stale_attachment.py
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _mock_api import MockVisionAPI, make_test_config

HOOK = os.path.expanduser("~/.zcode/vision-hook/vision_hook.py")
ROOT = os.path.join(tempfile.gettempdir(), "vhtest", "sta")
ART = os.path.join(ROOT, "art")
DB_DIR = os.path.join(ROOT, "db")
DB = os.path.join(DB_DIR, "db.sqlite")

results = []


def rec(name, ok, detail=""):
    results.append((name, ok))
    print(("OK" if ok else "FAIL"), name, ("| " + str(detail) if detail else ""))


def run_hook(cfg, session, prompt="x", event="UserPromptSubmit"):
    cfg_path = os.path.join(ROOT, "_cfg.json")
    json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False)
    empty = os.path.join(ROOT, "_empty.jsonl")
    open(empty, "w").close()
    p = subprocess.run(["python", HOOK], input=json.dumps({
        "hook_event_name": event, "session_id": session,
        "transcript_path": empty, "prompt": prompt}),
        capture_output=True, text=True,
        env=dict(os.environ, VISION_CONFIG=cfg_path), timeout=120)
    return p.stdout


def put(session, name, tag):
    """写一个粘贴附件文件, tag 是内容标记(不同图不同 base64), 返回文件绝对路径。"""
    d = os.path.join(ART, session)
    os.makedirs(d, exist_ok=True)
    content = "data:image/png;base64,%s" % tag
    p = os.path.join(d, "prompt-attachment-upload-%s-tool-result-%s.txt" % (name, name))
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


def set_mtime(path, when):
    os.utime(path, (when, when))


def fp_of(tag):
    """与 mock 回声一致的图指纹: md5(data uri 文本)。"""
    return hashlib.md5(("data:image/png;base64," + tag).encode("utf-8")).hexdigest()[:12]


def make_db(attachments_json, session, text="test msg"):
    os.makedirs(DB_DIR, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS input_history (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, session_id TEXT,
        text TEXT NOT NULL, attachments TEXT, kind TEXT NOT NULL,
        time_created INTEGER NOT NULL)""")
    con.execute(
        "INSERT INTO input_history (id, project_id, session_id, text, attachments, kind, time_created) "
        "VALUES (?,?,?,?,?,?,?)",
        ("id_" + session, "p", session, text, attachments_json, "prompt",
         int(time.time() * 1000)))
    con.commit()
    con.close()


def clean_session(session):
    shutil.rmtree(os.path.join(ART, session), ignore_errors=True)
    import sys as _s
    _s.path.insert(0, os.path.dirname(HOOK))
    import vision_hook as vh
    st = vh.load_state()
    st.pop(session, None)
    st.get("_skipped", {}).pop(session, None)
    vh.save_state(st)


def test_stale_attachment():
    api = MockVisionAPI(mode="ok")
    api.echo = True  # 响应带图片指纹, 可断言"识别了哪张图"
    base = make_test_config(api.base_url, timeout_seconds=3)
    base["fresh_seconds"] = 300
    os.makedirs(ART, exist_ok=True)
    if os.path.exists(DB):
        os.remove(DB)

    # ---------- 场景1(用户事故): 贴错图A→删→换图B→提交, 只应识别 B ----------
    s1 = "s_sta1"
    clean_session(s1)
    t0 = time.time()
    pa = put(s1, "oldA", "AAAA-old-wrong-image-AAAA")
    set_mtime(pa, t0 - 60)                     # A 先贴(已删, 但文件残留)
    pb = put(s1, "newB", "BBBB-new-correct-BBBB")  # B 后贴(提交)
    set_mtime(pb, t0 - 10)
    # DB 只记录 B —— 模拟"本次提交的附件"
    make_db(json.dumps([{"type": "image", "path": "image.png",
                         "content": "zcode-artifact://%s/tool-result-newB" % s1}],
                       ensure_ascii=False), s1)
    cfg1 = dict(base, artifacts_dir=ART)
    out1 = run_hook(cfg1, s1)
    rec("1. 换图重贴: 注入不含旧图A指纹(复现: 旧版会同时识别A)",
        fp_of("BBBB-new-correct-BBBB") in out1 and fp_of("AAAA-old-wrong-image-AAAA") not in out1,
        "out=%s" % out1[:200])
    clean_session(s1)

    # ---------- 场景2: 删掉的图从未提交, 后续纯文本消息也不得识别它 ----------
    s2 = "s_sta2"
    clean_session(s2)
    put(s2, "ghostA", "CCCC-ghost-CCCC")
    make_db(None, s2)                          # 本次提交无附件
    out2 = run_hook(dict(base, artifacts_dir=ART), s2)
    rec("2. 纯文本消息: 不识别残留旧图(复现: 旧版会识别)", out2 == "",
        "out=%s" % out2[:200])
    clean_session(s2)

    # ---------- 场景3: 预算截断的续传仍可用(回归保护) ----------
    s3 = "s_sta3"
    clean_session(s3)
    p3 = put(s3, "contA", "DDDD-continue-DDDD")
    mt3 = os.path.getmtime(p3)
    sys.path.insert(0, os.path.dirname(HOOK))
    import vision_hook as vh
    st = vh.load_state()
    st.setdefault("_skipped", {}).setdefault(s3, {})[os.path.basename(p3)] = mt3
    vh.save_state(st)
    make_db(None, s3)                          # "继续"消息无附件
    out3 = run_hook(dict(base, artifacts_dir=ART), s3)
    rec("3. 续传: 已跳过附件仍可被'继续'识别(回归保护)",
        fp_of("DDDD-continue-DDDD") in out3, "out=%s" % out3[:200])
    clean_session(s3)

    # ---------- 场景4: DB 不可用时的兜底 —— 新鲜度门控 ----------
    s4 = "s_sta4"
    clean_session(s4)
    if os.path.exists(DB):
        os.remove(DB)                          # 模拟 DB 缺失
    put(s4, "freshX", "EEEE-fresh-EEEE")       # 刚贴的新图
    out4 = run_hook(dict(base, artifacts_dir=ART), s4)
    rec("4a. DB缺失兜底: 新鲜附件仍识别(发图必识别不退步)",
        fp_of("EEEE-fresh-EEEE") in out4, "out=%s" % out4[:200])
    clean_session(s4)

    s5 = "s_sta5"
    clean_session(s5)
    p5 = put(s5, "staleY", "FFFF-stale-FFFF")
    set_mtime(p5, time.time() - 600)           # 十分钟前的残留
    out5 = run_hook(dict(base, artifacts_dir=ART, fresh_seconds=60), s5)
    rec("4b. DB缺失兜底: 超新鲜度窗口的残留不被识别",
        out5 == "", "out=%s" % out5[:200])
    clean_session(s5)

    api.stop()


if __name__ == "__main__":
    test_stale_attachment()
    fails = [r for r in results if not r[1]]
    print("残图攻击集: %d/%d 通过" % (len(results) - len(fails), len(results)))
    sys.exit(1 if fails else 0)
