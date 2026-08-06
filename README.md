# DeepSeek Vision Helper

给纯文本模型（DeepSeek 等）装上"眼睛"：粘贴图片 → 自动识别 → 以 `[Vision result]` 注入对话上下文，模型即可直接回答图片问题。

> **读者指南**
> - **AI 代理**，想自主安装 → 读第 1 节
> - **人类用户**，想手动安装 → 读第 2 节
> - **了解原理 / 调参 / 排查** → 读第 3~5 节

---

## 1. AI 自主安装指南

按顺序执行；第 0 步不满足时**停止并告知用户原因**。

### 第 0 步：环境检查

1. **必须是 ZCode**（或兼容客户端）——依赖其 hooks 事件与 `~/.zcode/cli/artifacts/` 附件目录。非 ZCode 环境只能装 skill（第 1 步），hook 无法工作，如实告知。
2. **Python 3 可用**：`python3 --version`，记录解释器绝对路径（后续命令用它）。
3. **视觉 API key**（唯一需要用户参与的点）：
   - 已有可复用配置（`~/.zcode/vision-hook/config.json` 含非 `YOUR_*` 的 key）→ 复用，跳过第 2 步的 key 配置
   - 否则引导用户提供：**首选 Agnes**（apihub.agnes-ai.com，免费）；国内用户可用智谱 bigmodel.cn 免费 GLM-4.6V-Flash
   - 用户拒绝 → 停止安装（无 key 无法识别）

### 第 1 步：安装 skill

```bash
mkdir -p ~/.zcode/skills
cp -r skills/deepseek-vision-helper ~/.zcode/skills/
```

- 跨工具共享（Claude/Codex 兼容）：装到 `~/.agents/skills/deepseek-vision-helper/`
- **完成标准**：`SKILL.md` 存在，frontmatter `name: deepseek-vision-helper`

### 第 2 步：安装 hook + 配置 key

```bash
mkdir -p ~/.zcode/vision-hook
cp hook/vision_hook.py ~/.zcode/vision-hook/
cp hook/config.example.json ~/.zcode/vision-hook/config.json
```

编辑 `config.json`：至少一个 provider 的 `api_key` 从 `YOUR_*` 换成真实 key，并把 `provider` 设为该 provider。

**或用环境变量**（优先级更高，不改配置文件）：

```bash
export VISION_API_KEY_AGNES=你的key
# 命名规则:VISION_API_KEY_<PROVIDER 大写、连字符转下划线>,如 VISION_API_KEY_MIMO_DIRECT
```

**完成标准**：`python3 -m json.tool config.json` 通过；至少一个 key 非 `YOUR_*`，或已设环境变量。

### 第 3 步：注册 hooks

编辑 `~/.zcode/cli/config.json`，在 `hooks` 下**合并**（不覆盖已有条目）两个事件：

```json
{
  "UserPromptSubmit": [
    { "matcher": ".*", "hooks": [
      { "type": "process", "command": "/usr/bin/python3",
        "args": ["/home/<用户名>/.zcode/vision-hook/vision_hook.py"], "timeoutMs": 300000 } ] }
  ],
  "PreToolUse": [
    { "matcher": ".*", "hooks": [
      { "type": "process", "command": "/usr/bin/python3",
        "args": ["/home/<用户名>/.zcode/vision-hook/vision_hook.py"], "timeoutMs": 300000 } ] }
  ]
}
```

- `command` 用第 0 步的 Python **绝对路径**；`args` 用 vision_hook.py **绝对路径**（不留 `~`）
- Windows PowerShell 用 `Copy-Item` 等对应命令，路径分隔符 `/` 或 `\` 均可
- 确保 `hooks.enabled: true`

**完成标准**：JSON 合法；两个事件存在。

### 第 4 步：初始化识别状态

防止首次运行把历史附件当新图误识别（无历史附件时输出 0 属正常）：

```bash
python3 - <<'EOF'
import json, os
base = os.path.expanduser("~/.zcode/cli/artifacts")
state, n = {}, 0
for sess in os.listdir(base):
    d = os.path.join(base, sess)
    if not os.path.isdir(d): continue
    for fn in os.listdir(d):
        if fn.startswith("prompt-attachment-upload") and fn.endswith(".txt"):
            state.setdefault(sess, {})[fn] = os.path.getmtime(os.path.join(d, fn)); n += 1
if n:
    json.dump(state, open(os.path.expanduser("~/.zcode/vision-hook/vision_hook_state.json"), "w", encoding="utf-8"), ensure_ascii=False)
print("initialized %d attachment(s)" % n)
EOF
```

### 第 5 步：自检

| 检查 | 命令 | 预期 |
|---|---|---|
| 无图静默 | `echo '{"hook_event_name":"UserPromptSubmit","session_id":"x","transcript_path":"/tmp/none","prompt":"hi"}' \| python3 ~/.zcode/vision-hook/vision_hook.py; echo $?` | 无输出、exit 0 |
| 日志触发 | `tail -3 ~/.zcode/vision-hook/vision_hook.log` | 含 `hook fired` |
| 贴图注入 | ZCode 里粘贴图片发送 | 上下文出现 `[Vision result]` |

> 事件配置在会话启动时加载，改配置后需**重启 ZCode 客户端**再测第 3 项。

---

## 2. 人类安装简版

1. `skills/deepseek-vision-helper/` → `~/.zcode/skills/`；`hook/` 目录 → `~/.zcode/vision-hook/`，`config.example.json` 改名 `config.json` 填 key（推荐 Agnes；国内可注册智谱）
2. 注册 hooks（JSON 模板见 1.3），`hooks.enabled: true`
3. 重启客户端，贴图测试

---

## 3. 使用

### 贴图即用

粘贴图片发送即可。识别**完整优先**：未超注入上限时全量注入；超限时完整结果自动落盘 `results/` 目录并在注入中给出文件路径（识别从不丢信息）。图片超过 `max_image_bytes` 时会明确告知，不会静默丢弃。

### 批量识别（几十张图）

```bash
python3 vision_hook.py --folder "图片目录" --out results.md   # 递归扫描
python3 vision_hook.py --files a.png b.png --out results.md   # 指定文件
```

结果写文件、不占对话上下文；支持 png/jpg/jpeg/webp/gif/bmp。

### 模型主动调用（无 hook 环境）

单图输出纯文本描述，任何 Agent 可当普通命令调用：

```bash
python3 vision_hook.py --files 图.png --question "用中文描述这张图片"
```

给目标环境的指令模板：

```markdown
## 图片识别
模型不具备识图能力时,遇到图片不要用 Read,改用:
python3 /path/to/vision_hook.py --files "<图片路径>" --question "用中文描述这张图片"
对每张图片依次执行,拿到全部文字描述后再回复。这是强制规则。
```

---

## 4. 故障排查

| 症状 | 定位与修复 |
|---|---|
| 日志无 `hook fired` | hook 未注册：检查 `hooks.enabled: true`、事件名大小写、matcher；改配置后是否重启 |
| 日志有 `未配置可用的 API key` | 所有 key 都是 `YOUR_*` 或为空：填 key 或设 `VISION_API_KEY_<PROVIDER>` 环境变量 |
| `no image found` | 无新落盘附件：确认贴图后 `~/.zcode/cli/artifacts/<会话>/prompt-attachment-upload-*.txt` 存在；若文件在但未识别，检查 `vision_hook_state.json` 已记账（同图只识别一次，正常） |
| `vision api failed (HTTP xxx)` | 401 = key 无效；1305/429/5xx = 平台过载，自动重试；网络问题检查代理 |
| `provider missing in config` | `provider`/`batch_provider`/`fallback_provider` 指向了未填 key 的 provider |
| skill 不触发 | `SKILL.md` 的 description 必须含 `[Vision result]`；skill 位于发现根目录 |
| 注入出现"完整结果已存至" | 识别完整但超注入上限：读取提示中的文件获取全量，无需重发 |

---

## 5. 设计与配置参考

### 工作原理

```
粘贴图片 + 提问 → UserPromptSubmit / PreToolUse Hook → 检测新落盘附件 → 调视觉 API → 结果注入上下文 → 文本模型回答
```

- **双事件**：UserPromptSubmit + PreToolUse 任一触发都会尝试取图；state 记账保证同一批图只注入一次；无图时静默跳过（实测约 93ms）
- **取图 = state 增量附件监控**：ZCode 的 hook transcript 只含纯文本（UserPromptSubmit 仅 prompt、PreToolUse 为空），图片 part 不会出现在 transcript 里——粘贴的图片附件落盘到 `~/.zcode/cli/artifacts/<会话>/prompt-attachment-upload-*.txt`，脚本对比 `vision_hook_state.json` 只识别新落盘的附件，这是唯一可靠通道
- **注入**：stdout 输出 `{"additionalContext": "[Vision result] ..."}`；失败静默（exit 0）不影响对话
- **纯事实识别**：请求带 system prompt 将视觉后端约束为"图片识别工具"——只输出图片中客观存在的内容（文字提取、界面/图表/场景描述），**不包含**模型自己的分析、建议、总结、猜测或反问；一切判断由主模型完成

### 自动路由

| 场景 | 行为 |
|---|---|
| 1 ~ `batch_threshold` 张 | `provider`（默认 agnes，免费，约 8~20s/张） |
| 单张失败 | 自动降级 `fallback_provider` 重试 |
| 超过阈值 | 整批改用 `batch_provider`（质量高、避开免费限流） |
| 手动强制 | `VISION_PROVIDER=mimo` 环境变量 |

批量串行执行，最坏耗时 ≈ 张数 × 单张耗时。

### 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `provider` / `batch_provider` / `fallback_provider` | agnes / mimo / mimo | 常规 / 批量 / 降级后端 |
| `batch_threshold` | 3 | 超过此张数视为批量 |
| `max_images` | 4 | 单次最多识别张数 |
| `per_image_max_chars` / `total_max_chars` | 2000 / 8000 | 注入长度上限（完整优先，超限落盘见"使用"） |
| `max_image_bytes` | 10485760 | 单张大小上限（超出明确告知） |
| `timeout_seconds` | 90 | 单次 API 超时 |
| `max_tokens` | 4000 | 识别输出 token 上限 |
| `log_max_bytes` | 1048576 | 日志轮转阈值（归档为 `.log.1`，保留最近两段） |
| `skip_when_multimodal` | false | 主模型原生多模态时设 true：跳过识别注入，图片走原生通道（或 `VISION_SKIP_MULTIMODAL=1`） |

### Provider

| provider | 后端 | 说明 |
|---|---|---|
| `agnes` | agnes-2.5-flash（apihub.agnes-ai.com） | **推荐**：免费聚合后端 |
| `zhipu` | 免费 GLM-4.6V-Flash（bigmodel.cn） | 国内用户可注册；免费、快；有免费限流 |
| `mimo` | 小米 MiMo-V2.5（经 opencode Go 网关） | 质量高但较慢；消耗套餐配额。👉 [通过推荐链接使用 opencode Go](https://opencode.ai/go?ref=RKEAQV3NAW) |
| `mimo-direct` | 小米官方 API（api.xiaomimimo.com） | 备用；需 platform.xiaomimimo.com 的 key |

OpenAI 兼容 `/chat/completions`，可自行添加任意提供商。`VISION_CONFIG=/path/config.json` 指定配置文件（测试用）。`mimo-v2.5` 支持图片，`mimo-v2.5-pro` 不支持。

### 目录结构

```
deepseek-vision-helper/
├── skills/deepseek-vision-helper/SKILL.md   # skill：指导模型使用注入的识别结果
└── hook/
    ├── vision_hook.py                       # Hook 脚本（Python 3，仅标准库）
    └── config.example.json                  # 配置示例（复制为 config.json 填 key）
```

运行时自动生成：`vision_hook_state.json`（已识别记账）、`vision_hook.log`（调试日志）、`results/`（超限时的完整识别结果）——均不含密钥。

### 安全提醒

- API key 只存本机 `config.json` 或环境变量；仓库 `.gitignore` 已排除 config/state/log/results
- 本仓库只含 `config.example.json` 占位配置，无任何真实密钥
- key 曾在聊天/日志泄露过 → 到对应平台控制台重置

---

## 许可证

MIT License，见 [LICENSE](LICENSE)。
