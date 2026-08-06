#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZCode Vision Hook —— 给纯文本模型（DeepSeek 等）装上"眼睛"。

触发时机：UserPromptSubmit / PreToolUse（任一事件触发都会尝试取图并识别）。
流程：
  1. 从 stdin 读取 ZCode 注入的 JSON（session_id / transcript_path / prompt / tool_name ...）
  2. 取图（按优先级）：
     a. transcript 里的图片 part（file / Anthropic 内联 base64 / zcode-artifact:// URI）
     b. transcript 用户文本里的 [Attached image...] 占位符 → artifacts 兜底
     c. 【核心】state 增量监控：扫描 ~/.zcode/cli/artifacts/<会话>/ 下新落盘的
        prompt-attachment-upload-*.txt（与 vision_hook_state.json 中已识别记录对比，
        只识别新文件，天然去重）——ZCode 的 hook transcript 只含纯文本（UserPromptSubmit
        仅 prompt、PreToolUse 为空），图片 part 永远不会出现在 transcript 里，
        因此附件增量监控是贴图识别的唯一可靠通道。
  3. 按路由规则选择视觉后端，逐张识别（见"路由"）
  4. stdout 输出 {"additionalContext": "[Vision result] ..."} 注入对话上下文
  5. 至少一张识别成功后，把本次识别的附件记入 state（下次不再重复注入）

路由（config.json 可调）：
  - 1 ~ batch_threshold 张  → provider（默认 agnes / agnes-2.5-flash），失败降级 fallback_provider
  - 超过 batch_threshold 张 → batch_provider（默认 mimo / 小米 MiMo-V2.5），失败降级 fallback_provider
  - 环境变量 VISION_PROVIDER=xxx 可强制只用某 provider（调试用）
  - 环境变量 VISION_CONFIG=/path/to/config.json 可指定配置文件（测试用）

安全：API key 只存放在 config.json（不要提交到仓库）。
行为：无图 / 全部识别失败时静默退出（无输出、exit 0），不影响正常对话。
调试：运行日志写入同目录 vision_hook.log。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_LOG = os.path.join(_HERE, "vision_hook.log")
_STATE_FILE = os.path.join(_HERE, "vision_hook_state.json")
_LOG_MAX_BYTES = 1024 * 1024  # 日志轮转阈值(默认 1MB,可由 config.log_max_bytes 覆盖)
_UPDATE_URL = os.environ.get(
    "VISION_UPDATE_URL",
    "https://raw.githubusercontent.com/JJ-Yvain/deepseek-vision-helper/main/VERSION")


def _rotate_log():
    """当前日志超过阈值时归档为 .1(覆盖旧备份),保留最近两段日志。"""
    try:
        if os.path.exists(_LOG):
            os.replace(_LOG, _LOG + ".1")
    except Exception:
        pass


def log(msg):
    try:
        if _LOG_MAX_BYTES and os.path.exists(_LOG) and os.path.getsize(_LOG) >= _LOG_MAX_BYTES:
            _rotate_log()
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg + "\n")
    except Exception:
        pass


def load_config():
    path = os.environ.get("VISION_CONFIG") or os.path.join(_HERE, "config.json")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    # 环境变量覆盖 API key（优先级高于 config.json）：
    #   VISION_API_KEY_<PROVIDER 大写、连字符转下划线>，如 VISION_API_KEY_ZHIPU / VISION_API_KEY_MIMO_DIRECT
    for name, p in cfg.get("providers", {}).items():
        env_key = os.environ.get("VISION_API_KEY_" + name.upper().replace("-", "_"))
        if env_key:
            p["api_key"] = env_key
    return cfg


def usable_providers(cfg):
    """返回配置了真实 key 的 provider（剔除 YOUR_ 占位符与空值）。"""
    return {n: p for n, p in cfg.get("providers", {}).items()
            if p.get("api_key") and not str(p["api_key"]).startswith("YOUR_")}


def config_guidance(cfg):
    """未配置任何可用 key 时返回引导文案（供日志/CLI 输出），否则返回 None。"""
    if usable_providers(cfg):
        return None
    return ("[vision helper] 未配置可用的 API key。两种配置方式："
            "① 设置环境变量 VISION_API_KEY_<PROVIDER>（如 VISION_API_KEY_ZHIPU）；"
            "② 编辑 %s 的 providers 填入真实 key（推荐 agnes：apihub.agnes-ai.com；国内用户可用智谱 bigmodel.cn 免费注册 GLM-4.6V-Flash）"
            % os.path.join(_HERE, "config.json"))


def parse_transcript(path):
    """把 transcript 解析为消息列表 [{'role','texts','images'}]，容忍多种序列化格式。"""
    messages = []
    if not path or not os.path.exists(path):
        return messages
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            msg = {"role": obj.get("role", "unknown"), "texts": [], "images": []}

            def scan(node):
                if isinstance(node, dict):
                    t = node.get("type")
                    if t == "file" and str(node.get("mime", "")).startswith("image/"):
                        msg["images"].append(node)
                    elif t == "image":  # Anthropic 风格内联图片
                        msg["images"].append(node)
                    elif t == "text" and node.get("text"):
                        msg["texts"].append(str(node["text"]))
                    else:
                        for v in node.values():
                            scan(v)
                elif isinstance(node, list):
                    for v in node:
                        scan(v)

            scan(obj)
            messages.append(msg)
    return messages


def read_uri_file(path, mime):
    """读取 artifacts 里的附件文件（内容是 data URI 或裸 base64），规整成 data URI。"""
    with open(path, encoding="utf-8", errors="replace") as f:
        content = f.read().strip()
    if content.startswith("data:"):
        return content
    if content.startswith("base64,"):
        return "data:%s;%s" % (mime, content)
    return "data:%s;base64,%s" % (mime, content)


def resolve_image(info, session_id, cfg):
    """把图片信息解析成 (mime, data_uri)；无法解析返回 None。"""
    mime = info.get("mime") or "image/png"
    url = info.get("url") or ""

    # 1) Anthropic 风格内联 base64 / data URI
    src = info.get("source")
    if isinstance(src, dict):
        if src.get("type") == "base64" and src.get("data"):
            return mime, "data:%s;base64,%s" % (mime, src["data"])
        d = src.get("data")
        if d and str(d).startswith("data:"):
            return mime, d

    # 2) 直接内联 data URI
    if str(url).startswith("data:"):
        return mime, url

    # 3) zcode-artifact:// URI → 磁盘 artifacts 目录
    if str(url).startswith("zcode-artifact://"):
        rest = url[len("zcode-artifact://"):]
        parts = rest.split("/", 1)
        sess = parts[0] or session_id
        name = parts[1] if len(parts) > 1 else ""
        base = os.path.expanduser(cfg.get("artifacts_dir", "~/.zcode/cli/artifacts"))
        d = os.path.join(base, sess)
        if not name or not os.path.isdir(d):
            return None
        # 优先：文件名包含 URI 尾部（实测格式 prompt-attachment-upload-*-<tail>.txt）
        for fn in os.listdir(d):
            if fn.endswith(".txt") and name in fn:
                return mime, read_uri_file(os.path.join(d, fn), mime)
        # 兜底：该会话最新的粘贴附件
        cands = [fn for fn in os.listdir(d)
                 if fn.startswith("prompt-attachment-upload") and fn.endswith(".txt")]
        if cands:
            newest = max(cands, key=lambda fn: os.path.getmtime(os.path.join(d, fn)))
            return mime, read_uri_file(os.path.join(d, newest), mime)
    return None


# ---------- state 增量附件监控（贴图识别的核心通道） ----------
# ZCode 的 hook transcript 只含纯文本（UserPromptSubmit 仅 prompt、PreToolUse 为空），
# 图片 part 永远不会出现在 transcript 里；粘贴的图片附件会落盘到
# ~/.zcode/cli/artifacts/<会话>/prompt-attachment-upload-*.txt。
# 因此：记录"已识别附件"状态，每次 hook 只识别新落盘的附件，可靠且天然去重。


def load_state():
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
            return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def save_state(state):
    try:
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, _STATE_FILE)  # 原子替换，避免半写状态
    except Exception:
        pass


def list_attachments(session_id, cfg):
    """列出该会话 artifacts 目录下全部粘贴附件，返回 [(filename, mtime, abspath)]（按新旧升序）。"""
    base = os.path.expanduser(cfg.get("artifacts_dir", "~/.zcode/cli/artifacts"))
    d = os.path.join(base, session_id or "")
    if not os.path.isdir(d):
        return []
    out = []
    for fn in os.listdir(d):
        if fn.startswith("prompt-attachment-upload") and fn.endswith(".txt"):
            p = os.path.join(d, fn)
            try:
                out.append((fn, os.path.getmtime(p), p))
            except OSError:
                continue
    out.sort(key=lambda x: x[1])
    return out


def new_attachments(session_id, cfg, state, max_n):
    """返回 state 未记录（或 mtime 已变化）的附件 [(fn, mtime, path)]，按新旧倒序，最多 max_n。

    附件在用户粘贴图片时即落盘，与提交时机无关：只要它在上一次 hook 运行之后
    出现（state 未记录），本次就会被识别——"发图必识别、已识别不重复"。
    """
    files = list_attachments(session_id, cfg)
    known = state.get(session_id, {}) if isinstance(state, dict) else {}
    new = [(fn, mt, p) for fn, mt, p in files if known.get(fn) != mt]
    new.sort(key=lambda x: x[1], reverse=True)
    return new[:max_n]


def mark_identified(state, session_id, attachments):
    """把已成功识别的附件记入 state（filename -> mtime）。"""
    known = state.get(session_id, {})
    for fn, mt, _ in attachments:
        known[fn] = mt
    state[session_id] = known


# ---------- 版本自检（agent-reach 模式：检查自动、更新需确认） ----------
# 仅在"识别成功注入"时顺带检查（有图场景用户在场），频率受
# update_check_interval_hours 控制（默认 24h）；有新版时在注入末尾附一行提示，
# 同版本不重复提醒；网络失败静默。更新由用户一句话触发（update.py）。


def local_version():
    """读取本地 VERSION 文件（数据目录优先，其次脚本目录）；缺失返回 None。"""
    for base in (os.environ.get("ZCODE_PLUGIN_DATA") or "", _HERE):
        p = os.path.join(base, "VERSION")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    v = f.read().strip()
                return v or None
            except Exception:
                return None
    return None


def check_update(cfg, state):
    """对比远程 VERSION；返回远程新版本号；无需检查/无更新/失败返回 None。"""
    cur = local_version()
    if not cur:
        return None  # 本地无版本标记 → 跳过（不打扰）
    interval = float(cfg.get("update_check_interval_hours", 24))
    last = state.get("last_update_check", 0)
    if time.time() - last < interval * 3600:
        return None  # 频率控制：interval 小时内只查一次
    state["last_update_check"] = time.time()  # 先记账：失败也不在 interval 内重试
    try:
        req = urllib.request.Request(_UPDATE_URL,
                                     headers={"User-Agent": "zcode-vision-hook/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            latest = resp.read().decode("utf-8").strip()
    except Exception:
        return None  # 网络失败静默
    return latest if (latest and latest != cur) else None


_PLACEHOLDER_RE = None  # 延迟初始化（避免 import 顺序问题）


def count_placeholder_images(texts):
    """统计最新用户消息文本里的图片占位符标记（如 [Attached image/png: image.png]）。

    当图片因主模型不支持图片输入而被裁剪成纯文本时，transcript 里只剩这种标记，
    但实际附件仍会落盘到 artifacts 目录——靠它触发兜底取图（保证"发图必识别"）。
    """
    global _PLACEHOLDER_RE
    if _PLACEHOLDER_RE is None:
        import re
        _PLACEHOLDER_RE = re.compile(r"\[Attached image[^\]]*\]", re.IGNORECASE)
    n = 0
    for t in texts:
        n += len(_PLACEHOLDER_RE.findall(t or ""))
    return n


# 识别角色约束：把视觉后端钉死在"纯识别工具"上，
# 只输出图片中的事实，禁止模型自己的分析/建议/总结/反问/常识补充。
_SYSTEM_PROMPT = (
    "你是一个图片识别工具。只输出图片中客观存在的内容：完整提取文字、"
    "描述界面元素/图表数据/场景。严禁添加你的分析、总结、建议、评价、猜测或反问；"
    "严禁输出图片之外的知识；如果图片中没有的信息，不要提及。"
)


def call_vision(cfg, data_uri, question, provider):
    """调用 OpenAI 兼容 /chat/completions，返回识别文本；失败返回 None。"""
    providers = cfg.get("providers", {})
    if provider not in providers:
        log("provider missing in config: %s" % provider)
        return None
    p = providers[provider]
    payload = {
        "model": p["model"],
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": question or cfg.get(
                    "default_question",
                    "请识别这张图片：如果包含文字请完整提取；如果是界面、报错页面或图表，请说明其内容与含义。只输出图片中的事实，不要分析、建议或反问。")},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]},
        ],
        "max_tokens": cfg.get("max_tokens", 4000),
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "zcode-vision-hook/1.0",
    }
    headers[p.get("header", "Authorization")] = p.get("auth_prefix", "") + p["api_key"]
    body = json.dumps(payload).encode("utf-8")
    last_err = "unknown"
    for attempt in range(2):
        try:
            req = urllib.request.Request(p["base_url"], data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=cfg.get("timeout_seconds", 90)) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return str(data["choices"][0]["message"]["content"]).strip()
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s: %s" % (e.code, e.read(300).decode("utf-8", "replace"))
            retry = e.code in (429, 500, 502, 503, 504) or "1302" in last_err or "1305" in last_err
            if not retry:
                break
            time.sleep(2)
        except Exception as e:
            last_err = str(e)[:300]
            if attempt == 0:
                time.sleep(2)
    log("vision api failed (%s): %s" % (provider, last_err))
    return None


def build_chain(cfg):
    """按路由规则返回 provider 链（保序去重）。"""
    forced = os.environ.get("VISION_PROVIDER")
    if forced:
        return [forced]
    threshold = cfg.get("batch_threshold", 3)
    return [cfg.get("provider", "agnes"), cfg.get("fallback_provider", "mimo")]


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        return
    try:
        payload = json.loads(raw)
    except Exception:
        return
    session_id = payload.get("session_id", "")
    log("hook fired: event=%s session=%s" % (payload.get("hook_event_name"), session_id))

    cfg = load_config()
    global _LOG_MAX_BYTES
    _LOG_MAX_BYTES = cfg.get("log_max_bytes", 1024 * 1024)
    state = load_state()
    msgs = parse_transcript(payload.get("transcript_path"))
    log("transcript=%s msgs=%d" % (payload.get("transcript_path"), len(msgs)))

    # 1) 收集本消息内的全部图片
    img_infos = []
    target_texts = []
    if msgs and msgs[-1].get("images"):
        img_infos = msgs[-1]["images"]
        target_texts = msgs[-1]["texts"]
    if msgs and not img_infos:
        target_texts = msgs[-1]["texts"]  # 无图时也记录文本，供占位符检测

    # 2) 逐张解析成 data URI（单张解析失败不影响其他张）
    resolved = []
    for info in img_infos:
        r = resolve_image(info, session_id, cfg)
        if r:
            resolved.append(r)
        else:
            log("image resolve failed for one part")

    # 3) 占位符兜底：transcript 用户文本里出现 [Attached image...] 标记时，按数量取图。
    #    （ZCode 的 hook transcript 只含纯文本，此路径通常不命中；保留以防其他客户端行为差异）
    identified = []  # 本次识别成功的附件（供 state 记账去重）
    if not resolved:
        n_ph = count_placeholder_images(target_texts)
        if n_ph:
            log("placeholder detected: %d image marker(s)" % n_ph)
            max_n = min(n_ph, cfg.get("max_images", 4))
            atts = new_attachments(session_id, cfg, state, max_n)
            if atts:
                log("placeholder resolved %d attachment(s)" % len(atts))
                resolved = [("image/png", read_uri_file(p, "image/png")) for _, _, p in atts]
                identified = atts

    # 4) 【核心】state 增量附件监控：transcript 拿不到图（UserPromptSubmit 仅纯文本
    #    prompt、PreToolUse 为空 transcript），改从 artifacts 目录识别"新落盘"的附件，
    #    与已识别记录对比，可靠且天然去重——"发图必识别"。
    if not resolved:
        atts = new_attachments(session_id, cfg, state, cfg.get("max_images", 4))
        if atts:
            log("new attachment(s) detected: %d (state-based)" % len(atts))
            resolved = [("image/png", read_uri_file(p, "image/png")) for _, _, p in atts]
            identified = atts
    if not resolved:
        log("no image found")
        return

    # 未配置可用 key：日志给出明确引导（hook 保持静默退出，不注入）
    guidance = config_guidance(cfg)
    if guidance:
        log(guidance)
        return

    # 多模态主模型场景（skip_when_multimodal=true 或 VISION_SKIP_MULTIMODAL=1）：
    # 主模型能直接看到原图，跳过视觉 API 识别与注入，让图片走原生通道——
    # 避免白耗 API 配额，也避免注入的低质量文本描述干扰模型直接看图。
    if cfg.get("skip_when_multimodal") or os.environ.get("VISION_SKIP_MULTIMODAL") == "1":
        log("skip: multimodal model configured, %d image(s) left to native channel" % len(resolved))
        return

    # 4) 体积保护 + 数量上限
    kept = []
    oversized = 0
    for mime, data_uri in resolved:
        est = len(data_uri) * 3 // 4  # data URI 长度 ≈ base64 长度 → 字节数估算
        if est > cfg.get("max_image_bytes", 10 * 1024 * 1024):
            log("image too large: ~%d bytes" % est)
            oversized += 1
            continue
        kept.append((mime, data_uri))
    total_in = len(kept)
    max_images = cfg.get("max_images", 4)
    if len(kept) > max_images:
        log("batch truncated: %d -> %d" % (len(kept), max_images))
        kept = kept[:max_images]
    if not kept:
        # 不静默丢图：明确告知用户有多少张因超限未识别及处理方式
        if oversized:
            limit = cfg.get("max_image_bytes", 10 * 1024 * 1024)
            shown = ("%dMB" % (limit // (1024 * 1024))) if limit >= 1024 * 1024 else ("%dKB" % (limit // 1024))
            msg = ("[Vision result] （%d 张图片超出大小限制（%s）未识别。"
                   "可压缩图片，或调大 config 的 max_image_bytes 后重发）" % (oversized, shown))
            print(json.dumps({"additionalContext": msg}, ensure_ascii=False))
            log("all images oversized, informed user")
        return

    # 5) 路由：单图用默认 provider；超过阈值整批用 batch_provider
    threshold = cfg.get("batch_threshold", 3)
    if len(kept) > threshold:
        chain = [cfg.get("batch_provider", "mimo"), cfg.get("fallback_provider", "mimo")]
    else:
        chain = [cfg.get("provider", "agnes"), cfg.get("fallback_provider", "mimo")]
    forced = os.environ.get("VISION_PROVIDER")
    if forced:
        chain = [forced]
    chain = list(dict.fromkeys(p for p in chain if p))
    log("routing: %d image(s), chain=%s" % (len(kept), chain))

    question = payload.get("prompt") or (target_texts[-1] if target_texts else None)
    total_cap = cfg.get("total_max_chars", 8000)
    # 预算均分：多图时每张分到 total_cap/张数 的注入空间（下限 200 字符），
    # 避免前几张图吃光预算、后面的图被整体截没。
    per_cap = min(cfg.get("per_image_max_chars", 2000),
                  max(200, total_cap // max(len(kept), 1)))

    full_parts = []    # 每张图的完整识别文本（不截断，用于落盘/全量注入）
    inject_parts = []  # 注入用的截断版
    used = set()
    multi = len(kept) > 1
    for i, (mime, data_uri) in enumerate(kept, 1):
        q = ("图%d。%s" % (i, question)) if multi else question
        ok = False
        for prov in chain:
            desc = call_vision(cfg, data_uri, q, prov)
            if desc:
                used.add(prov)
                full_parts.append(("图%d: " % i) + desc if multi else desc)
                d = desc[:per_cap]
                if len(desc) > per_cap:
                    d += "…(截断)"
                inject_parts.append(("图%d: " % i) + d if multi else d)
                ok = True
                break
        if not ok:
            full_parts.append("图%d: (识别失败)" % i)
            inject_parts.append("图%d: (识别失败)" % i)
            log("all providers failed for image %d" % i)

    successes = [p for p in inject_parts if not p.endswith("(识别失败)")]
    if not successes:
        log("no vision result at all")
        return

    # 至少一张识别成功才记账：本次识别的附件标记为"已识别"，后续事件（如
    # UserPromptSubmit + PreToolUse 双触发）不会重复注入同一批图。
    if identified:
        mark_identified(state, session_id, identified)
        save_state(state)
        log("state updated: %d attachment(s) marked" % len(identified))

    # 完整优先注入：识别结果全程完整保留（full_text 不截断）。
    # 未超 total_cap → 全量注入；超限 → 完整结果落盘 results/ 目录，
    # 注入截断版并给出文件路径，需要全量时可读取——识别从不丢信息。
    full_text = "\n".join(full_parts)
    if oversized:
        full_text += "\n（%d 张图片超出大小限制未识别）" % oversized
    if len(full_text) <= total_cap:
        result = full_text
    else:
        result = "\n".join(inject_parts)
        result = result[:total_cap]
        # 落盘提示追加在截断之后，保证可见（识别从不丢信息）
        try:
            res_dir = os.path.join(_HERE, "results")
            os.makedirs(res_dir, exist_ok=True)
            path = os.path.join(res_dir, "%s_%s.md" % (session_id or "session",
                                                       time.strftime("%Y%m%d_%H%M%S")))
            with open(path, "w", encoding="utf-8") as f:
                f.write(full_text)
            result += ("\n（识别已完成，但内容较长，完整结果已存至 %s，"
                       "需要完整内容时可读取该文件）" % path)
        except Exception as e:
            log("save full result failed: %s" % e)
    if total_in > len(kept):
        result += "\n（共 %d 张图，已识别前 %d 张）" % (total_in, len(kept))

    # 版本自检：有新版 → 注入末尾附提示（同版本不重复提醒；失败/无本地版本静默）
    try:
        latest = check_update(cfg, state)
        if latest and state.get("last_notified_version") != latest:
            state["last_notified_version"] = latest
            result += "\n⚠️ 检测到新版本 %s（当前 %s），回复“更新”即可自动更新" % (
                latest, local_version())
            log("update available: %s -> %s" % (local_version(), latest))
            save_state(state)
    except Exception:
        pass

    log("vision ok: %d image(s), %d chars, providers=%s" % (len(kept), len(result), ",".join(sorted(used))))
    print(json.dumps({"additionalContext": "[Vision result] " + result}, ensure_ascii=False))


def collect_files(folder, files):
    """收集待识别图片文件（目录递归 + 显式文件列表，去重保序）。"""
    exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
    result = []
    if folder:
        for root, _, names in os.walk(folder):
            for n in sorted(names):
                if os.path.splitext(n)[1].lower() in exts:
                    result.append(os.path.join(root, n))
    if files:
        result.extend(os.path.abspath(f) for f in files if os.path.isfile(f))
    seen, out = set(), []
    for f in result:
        rp = os.path.realpath(f)
        if rp not in seen:
            seen.add(rp)
            out.append(f)
    return out


def mime_for(path):
    return {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp"}.get(
        os.path.splitext(path)[1].lower().lstrip("."), "image/png")


def file_to_data_uri(path, mime):
    import base64
    with open(path, "rb") as f:
        raw = f.read()
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))


def run_folder_mode(cfg, files, question, out_path, chain, per_cap, plain=False):
    """批量识别：逐张调用（串行），结果写文件或打印。返回 None（写文件）或文本。

    plain=True（单文件、无 --out）时输出纯描述文本，便于模型直接读取（主动调用模式）。
    """
    lines, ok_count = [], 0
    for i, path in enumerate(files, 1):
        name = os.path.basename(path)
        mime = mime_for(path)
        try:
            data_uri = file_to_data_uri(path, mime)
        except Exception as e:
            lines.append("%d. **%s** — (读取失败: %s)" % (i, name, e))
            continue
        if len(data_uri) * 3 // 4 > cfg.get("max_image_bytes", 10 * 1024 * 1024):
            lines.append("%d. **%s** — (超出大小限制)" % (i, name))
            continue
        desc = None
        for prov in chain:
            desc = call_vision(cfg, data_uri, question, prov)
            if desc:
                break
        if desc:
            ok_count += 1
            if plain:
                lines.append(desc[:per_cap])
            else:
                lines.append("%d. **%s** — %s" % (i, name, desc[:per_cap]))
        else:
            lines.append("%d. **%s** — (识别失败)" % (i, name))
        log("folder mode: %d/%d done (ok=%d)" % (i, len(files), ok_count))
    text = "\n".join(lines)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("# 批量识图结果（共 %d 张，成功 %d 张）\n\n%s\n" % (len(files), ok_count, text))
        log("folder mode done: %d files -> %s (ok %d)" % (len(files), out_path, ok_count))
        return None
    return text


def main_cli(argv):
    import argparse
    ap = argparse.ArgumentParser(
        description="DeepSeek Vision Helper 批量识图（路由与 hook 一致）。"
                    "支持模型主动调用：python vision_hook.py --files <图片> --question <问题>")
    ap.add_argument("--folder", help="扫描并识别目录下所有图片（递归）")
    ap.add_argument("--files", nargs="*", help="指定图片文件列表")
    ap.add_argument("--out", help="结果写入文件（推荐；不写则打印到 stdout）")
    ap.add_argument("--provider", help="强制使用某 provider（如 agnes/zhipu）")
    ap.add_argument("--max", type=int, default=0, help="最多识别张数（0=不限）")
    ap.add_argument("--question", default=None, help="识别问题（默认用 config 的 default_question）")
    args = ap.parse_args(argv)
    if not args.folder and not args.files:
        ap.error("需要 --folder 或 --files")
    cfg = load_config()
    global _LOG_MAX_BYTES
    _LOG_MAX_BYTES = cfg.get("log_max_bytes", 1024 * 1024)
    guidance = config_guidance(cfg)
    if guidance:
        print(guidance)
        sys.exit(1)
    files = collect_files(args.folder, args.files)
    if args.max > 0:
        files = files[:args.max]
    if not files:
        print("未找到图片文件")
        return
    # 路由与 hook 一致：超过 batch_threshold 张走 batch_provider，失败降级 fallback_provider
    threshold = cfg.get("batch_threshold", 3)
    if args.provider:
        chain = [args.provider]
    elif len(files) > threshold:
        chain = [cfg.get("batch_provider", "mimo"), cfg.get("fallback_provider", "mimo")]
    else:
        chain = [cfg.get("provider", "agnes"), cfg.get("fallback_provider", "mimo")]
    chain = list(dict.fromkeys(p for p in chain if p))
    log("folder mode: %d file(s), chain=%s" % (len(files), chain))
    question = args.question or cfg.get("default_question")
    # 单文件且不落盘：输出纯描述文本（模型主动调用模式）
    plain = len(files) == 1 and not args.out
    text = run_folder_mode(cfg, files, question, args.out, chain,
                           cfg.get("per_image_max_chars", 2000), plain=plain)
    if text is not None:
        print(text)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main_cli(sys.argv[1:])
    else:
        main()
