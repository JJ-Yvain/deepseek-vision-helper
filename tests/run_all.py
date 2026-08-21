#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键运行全部测试(双层 CI 节奏)。

快速档(秒级、免费、确定性):L0 单元/性质 + L1 契约/模糊 + L2 故障注入 + 攻击集
慢速档(分钟级、真实 API/DeepSeek):回归 + 多轮模拟 + 交接质量

用法:
  python run_all.py            # 快速档 + 慢速档
  python run_all.py --fast     # 仅快速档
  python run_all.py --slow     # 仅慢速档
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

FAST = ["test_unit.py", "test_contract.py", "test_fuzz.py",
        "test_fault_injection.py", "test_adversarial.py", "test_stale_attachment.py",
        "test_mcp_server.py"]
SLOW = ["test_regression.py", "test_multi_round.py", "test_handoff.py"]


def run_suite(files):
    report = []
    for f in files:
        t0 = time.time()
        p = subprocess.run([sys.executable, os.path.join(HERE, f)],
                           capture_output=True, text=True, timeout=1800)
        dt = time.time() - t0
        tail = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""
        ok = p.returncode == 0
        report.append({"suite": f, "ok": ok, "seconds": round(dt, 1), "summary": tail})
        print(("✅" if ok else "❌"), f, "(%.1fs)" % dt, "|", tail)
        if not ok:
            print("   stderr:", p.stderr[-300:])
    return report


def main():
    ap = argparse.ArgumentParser(description="deepseek-vision-helper 验证体系一键运行")
    ap.add_argument("--fast", action="store_true", help="仅快速档")
    ap.add_argument("--slow", action="store_true", help="仅慢速档")
    args = ap.parse_args()

    if args.fast:
        suites = {"fast": FAST}
    elif args.slow:
        suites = {"slow": SLOW}
    else:
        suites = {"fast": FAST, "slow": SLOW}

    all_report = {}
    for name, files in suites.items():
        print("=" * 60)
        print("【%s 档】" % name)
        all_report[name] = run_suite(files)

    # 版本化结果(供趋势追踪)
    result = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "commit": os.popen("cd " + os.path.expanduser("~/.zcode/vision-hook")
                           + " 2>nul & git -C " + os.path.expanduser("~/.zcode/vision-hook")
                           + " rev-parse --short HEAD 2>nul").read().strip() or "n/a",
        "suites": all_report,
    }
    out_path = os.path.join(HERE, "last_result.json")
    json.dump(result, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("=" * 60)
    print("结果已写入:", out_path)

    failed = any(not s["ok"] for suite in all_report.values() for s in suite)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
