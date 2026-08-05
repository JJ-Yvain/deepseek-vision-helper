# DeepSeek Vision Helper

给纯文本模型（DeepSeek 等）装上"眼睛"：粘贴/拖入图片提交消息时，自动调用视觉模型识别，
把结果以 `[Vision result] ...` 注入对话上下文，纯文本模型即可直接回答图片相关问题。
配套的 `deepseek-vision-helper` skill 指导模型把注入的识别结果当作"看到"的图片内容。

> **本仓库面向 AI 代理自主安装。** 下面的"AI 自主安装指南"是确定性指令：任何 AI 代理
> （ZCode / Claude / Codex 等）按步骤执行即可完成安装与自检，无需人工干预
> （唯一的用户参与点是提供视觉 API key，见步骤 0）。

---

## 面向 AI 代理：自主安装指南（Autonomous Installation）

如果你是 AI 代理，请严格按以下步骤执行。每一步都有明确的动作、目标路径和完成标准；
步骤 0 不满足时**停止并告知用户原因**，不要继续。

### 第 0 步：环境检查（前置条件，全部满足才继续）

1. **目标环境是 ZCode**（或其兼容客户端）。本项目依赖 ZCode 的
   `UserPromptSubmit` / `PreToolUse` hook 事件、`~/.zcode/cli/artifacts/` 附件目录、
   `~/.zcode/cli/config.json` 配置。若目标不是 ZCode 系工具，只能安装 skill 部分
   （步骤 1），hook 部分无法工作——如实告知用户。
2. **Python 3 可用**：执行 `python3 --version`（Windows 下可能是 `python --version`），
   记录可用的解释器路径，后续所有命令用它。
3. **视觉 API key**（唯一需要用户参与的点）：
   - 先检查目标机器是否已有可复用的配置：`~/.zcode/vision-hook/config.json` 存在且
     `providers` 里有真实 key（非 `YOUR_` 开头）→ 复用，跳过步骤 2 的 key 配置。
   - 否则**引导用户**提供 key：首选 **Agnes**（apihub.agnes-ai.com，agnes-2.5-flash，
     免费聚合后端，推荐）；**国内用户**也可用智谱开放平台（bigmodel.cn）免费注册
     GLM-4.6V-Flash（免费额度），或小米 MiMo（platform.xiaomimimo.com）。
     告诉用户"安装过程中只需要这一步需要你提供 API key"，拿到 key 后继续。
   - 若用户拒绝提供 key，**停止安装**并解释：没有 key 视觉识别无法工作，skill 单独
     安装没有意义。

### 第 1 步：安装 skill（纯文件复制）

把本仓库 `skills/deepseek-vision-helper/` 整个目录复制到目标机器的 skill 发现根目录。
按目标工具的规范选择优先级（ZCode 用户级 > 跨工具共享 > 工作区级）：

| 目标 | 安装位置 |
|---|---|
| ZCode 用户级（推荐，所有项目生效） | `~/.zcode/skills/deepseek-vision-helper/` |
| 跨工具共享（Claude/Codex/Cursor 兼容） | `~/.agents/skills/deepseek-vision-helper/` |
| ZCode 工作区级（仅该项目） | `<项目根>/.zcode/skills/deepseek-vision-helper/` |

命令示例（以用户级为例）：

```bash
mkdir -p ~/.zcode/skills
cp -r skills/deepseek-vision-helper ~/.zcode/skills/
```

**完成标准**：`~/.zcode/skills/deepseek-vision-helper/SKILL.md` 存在，且文件头
frontmatter 的 `name:` 字段为 `deepseek-vision-helper`。

### 第 2 步：安装 hook 脚本

```bash
mkdir -p ~/.zcode/vision-hook
cp hook/vision_hook.py ~/.zcode/vision-hook/
cp hook/config.example.json ~/.zcode/vision-hook/config.json
```

然后编辑 `~/.zcode/vision-hook/config.json`，把 `providers` 里你准备使用的 provider
的 `api_key` 从 `YOUR_XXX_API_KEY` 替换为步骤 0 拿到的真实 key（**至少填 `agnes`**
（推荐），`zhipu`（国内可用）`mimo`/`mimo-direct` 可选；不用的 provider 可留占位符）。
同时建议把 `provider` 字段设为已填 key 的那个（如 `"provider": "agnes"`）。

**也可以不改 config.json，改用环境变量**（优先级更高，适合不想在配置文件里写 key 的场景）：

```bash
export VISION_API_KEY_AGNES=你的agnes key
# 国内用户: export VISION_API_KEY_ZHIPU=你的智谱key
# 或 export VISION_API_KEY_MIMO=... / VISION_API_KEY_MIMO_DIRECT=...
```

环境变量命名规则：`VISION_API_KEY_<PROVIDER 大写、连字符转下划线>`（如
`mimo-direct` → `VISION_API_KEY_MIMO_DIRECT`）。设置后重启 ZCode 客户端或新终端生效。

**完成标准**：`config.json` 是合法 JSON（`python3 -m json.tool config.json` 不报错），
且至少一个 provider 的 `api_key` 不是 `YOUR_` 开头，**或**已设置对应环境变量。

### 第 3 步：注册 hooks（ZCode 配置）

编辑 `~/.zcode/cli/config.json`，操作其 `hooks` 字段：

1. 若 `hooks` 不存在或 `hooks.enabled` 不为 `true`：设置为
   `"hooks": { "enabled": true, "timeoutMs": 300000, "events": {} }`。
2. **合并（不是覆盖）**：保留用户已有的 `events.*` 条目。确保 `events` 下存在
   `UserPromptSubmit` 和 `PreToolUse` 两个数组，每个数组内追加一个匹配所有工具的
   hook 条目（注意 `process` 类型的字段只能有 `command`/`args`/`timeoutMs`）：

```json
{
  "UserPromptSubmit": [
    {
      "matcher": ".*",
      "hooks": [
        {
          "type": "process",
          "command": "/usr/bin/python3",
          "args": ["/home/<用户名>/.zcode/vision-hook/vision_hook.py"],
          "timeoutMs": 300000
        }
      ]
    }
  ],
  "PreToolUse": [
    {
      "matcher": ".*",
      "hooks": [
        {
          "type": "process",
          "command": "/usr/bin/python3",
          "args": ["/home/<用户名>/.zcode/vision-hook/vision_hook.py"],
          "timeoutMs": 300000
        }
      ]
    }
  ]
}
```

`command` 用步骤 0 探测到的 Python 解释器**绝对路径**（如 `/usr/bin/python3` 或
`C:/.../python.exe`）；`args` 数组内用 `vision_hook.py` 的**绝对路径**
（如 `C:/Users/<用户名>/.zcode/vision-hook/vision_hook.py`），不要把 `~` 留在路径里。

> 跨平台提示：以上命令为 bash 语法。AI 代理在 Windows PowerShell 环境下执行时，
> 用 `Copy-Item -Recurse`、`mkdir -Force` 等对应命令；路径分隔符用 `\` 或 `/` 均可
> （Python 和 JSON 都接受正斜杠）。

**完成标准**：`python3 -m json.tool ~/.zcode/cli/config.json` 通过；`hooks.enabled`
为 `true`；两个事件都存在。

### 第 4 步：初始化识别状态（防止误识别历史附件）

hook 通过对比"已识别附件记账"（`vision_hook_state.json`）来发现新贴的图。首次安装时
记账为空，若目标机器 `~/.zcode/cli/artifacts/` 下已存在历史附件，会把旧图当新图识别。
执行以下初始化，把现有附件全部标记为已识别：

```bash
python3 - <<'EOF'
import json, os, time
base = os.path.expanduser("~/.zcode/cli/artifacts")
state = {}
n = 0
for sess in os.listdir(base):
    d = os.path.join(base, sess)
    if not os.path.isdir(d):
        continue
    for fn in os.listdir(d):
        if fn.startswith("prompt-attachment-upload") and fn.endswith(".txt"):
            state.setdefault(sess, {})[fn] = os.path.getmtime(os.path.join(d, fn))
            n += 1
if n:
    tmp = os.path.expanduser("~/.zcode/vision-hook/vision_hook_state.json")
    json.dump(state, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
print("initialized %d attachment(s) in state" % n)
EOF
```

（若 artifacts 目录不存在或为空，输出 `initialized 0 attachment(s)` 也属正常。）

**完成标准**：命令无报错；`~/.zcode/vision-hook/vision_hook_state.json` 存在
（或输出为 0 时也可不存在）。

### 第 5 步：自检（必须全部通过）

```bash
# 自检 1：无图场景应无输出、退出码 0
echo '{"hook_event_name":"UserPromptSubmit","session_id":"x","transcript_path":"/tmp/nonexist.jsonl","prompt":"hi"}' \
  | python3 ~/.zcode/vision-hook/vision_hook.py
echo "exit=$?"
# 预期：无输出、exit=0
```

```bash
# 自检 2：日志出现 hook fired（脚本会自动写日志）
tail -3 ~/.zcode/vision-hook/vision_hook.log
# 预期：最近一行包含 "hook fired"
```

```bash
# 自检 3（可选，需贴图）：告知用户在 ZCode 里粘贴一张图片发送
# 预期：模型上下文中出现 [Vision result] 开头的识别结果
```

**注意**：ZCode 的 hook 事件配置在会话启动时加载。步骤 3 修改配置后，需要用户
**重启 ZCode 客户端**（或开新会话）后 `PreToolUse` 事件才生效；`UserPromptSubmit`
同理。自检 3 请在重启后进行。

若任一自检失败，按下方"故障排查"定位，不要跳过。

---

## 故障排查（按症状定位）

| 症状 | 定位与修复 |
|---|---|
| 日志无 `hook fired` | hook 没被调用。检查 `~/.zcode/cli/config.json` 的 `hooks.enabled: true`、事件名是否为 `UserPromptSubmit`/`PreToolUse`（大小写敏感）、`matcher` 是否合法；改配置后是否重启过客户端 |
| `未配置可用的 API key`（日志/CLI 输出） | 没有任何 provider 的 key 可用（都是 `YOUR_` 占位符或空）。按"步骤 2"填 key，或设置 `VISION_API_KEY_<PROVIDER>` 环境变量 |
| `no image found` | 未检测到新贴的附件。确认贴图后 `~/.zcode/cli/artifacts/<会话>/prompt-attachment-upload-*.txt` 已落盘；若文件存在但未识别，检查 `vision_hook_state.json` 是否已记录（同图只识别一次，正常） |
| `vision api failed (HTTP xxx)` | 视觉 API 调用失败。401 = key 无效（回步骤 2 检查）；1305/429/5xx = 平台过载，稍后自动重试；网络不通则检查代理 |
| `provider missing in config` | `config.json` 的 `providers` 缺少路由使用的 provider。把 `provider`/`batch_provider`/`fallback_provider` 指向已填 key 的 provider |
| skill 不触发 | `SKILL.md` 的 frontmatter `description` 必须包含 `[Vision result]` 触发词；确认 skill 位于发现根目录（步骤 1 的表格） |

---

## 人工安装简版（给人类用户）

1. **装 skill**：把 `skills/deepseek-vision-helper/` 复制到 `~/.zcode/skills/`。
2. **装 hook**：把 `hook/` 目录复制到 `~/.zcode/vision-hook/`，
   `config.example.json` 改名为 `config.json` 并填入你的 API key
   （推荐 Agnes；国内用户可注册智谱 bigmodel.cn 免费 GLM-4.6V-Flash）。
3. **注册 hooks**：在 `~/.zcode/cli/config.json` 的 `hooks` 字段加入
   `UserPromptSubmit` + `PreToolUse` 两个事件（JSON 模板见"步骤 3"），
   确保 `hooks.enabled: true`。
4. **重启 ZCode 客户端**。
5. **贴图测试**：粘贴一张图片发送，上下文应出现 `[Vision result] ...`。

---

## 工作原理

```
粘贴图片 + 提问 → UserPromptSubmit / PreToolUse Hook → 检测新落盘附件 → 调视觉 API → 结果注入上下文 → 文本模型回答
```

- **Hook 事件**：`UserPromptSubmit` + `PreToolUse` 双事件（任一触发都会尝试取图；
  state 记账保证同一批图只注入一次；无图时静默跳过，实测开销约 95ms）
- **取图（state 增量附件监控）**：粘贴的图片附件会落盘到
  `~/.zcode/cli/artifacts/<会话>/prompt-attachment-upload-*.txt`（内容即 data URI）。
  脚本每次运行对比 `vision_hook_state.json`（同目录自动生成）中的已识别记录，
  只识别**新落盘**的附件。不依赖 transcript：ZCode 的 hook transcript 只含纯文本
  （UserPromptSubmit 仅 prompt、PreToolUse 为空），图片 part 不会出现在 transcript 里，
  附件增量监控是贴图识别的唯一可靠通道。
- **注入**：stdout 输出 `{"additionalContext": "[Vision result] ..."}`
- **失败静默**：无图 / 接口报错 → 空输出（exit 0），不影响对话

## 目录结构

```
deepseek-vision-helper/
├── skills/
│   └── deepseek-vision-helper/
│       └── SKILL.md          # ZCode skill：指导模型使用注入的识别结果
└── hook/
    ├── vision_hook.py        # Hook 脚本（Python 3，仅标准库）
    └── config.example.json   # 配置示例：复制为 config.json 后填入自己的 API key
```

> `vision_hook_state.json` 与 `vision_hook.log` 在脚本运行时自动生成于 hook 目录
> （已识别附件记账 / 调试日志，均不含密钥，勿提交仓库）。

## 自动路由与配置

| 场景 | 行为 |
|---|---|
| 1 ~ 3 张图（`batch_threshold`） | 用 `provider`（默认 agnes / agnes-2.5-flash，免费，约 8~20s/张） |
| 单张失败（报错/超时/限流） | 自动降级 `fallback_provider` 重试该图 |
| 超过阈值张数 | 整批改用 `batch_provider`（质量高、避开免费限流） |
| 手动强制 | 环境变量 `VISION_PROVIDER=mimo` 强制只用某 provider |

配置项（`config.json`，改动即时生效）：

| 键 | 默认 | 说明 |
|---|---|---|
| `provider` | `agnes` | 常规后端 |
| `batch_provider` | `mimo` | 批量后端（超过阈值时） |
| `fallback_provider` | `mimo` | 常规/批量失败后的降级后端 |
| `batch_threshold` | 3 | 超过此张数视为批量 |
| `max_images` | 4 | 单次最多识别张数（超出部分注入时注明） |
| `per_image_max_chars` / `total_max_chars` | 2000 / 8000 | 单张/总注入长度上限。默认已按"完整优先"设置；未超上限时识别结果**全量注入**，超限时完整结果自动落盘 `results/` 目录并在注入中给出文件路径（识别从不丢信息） |
| `max_image_bytes` | 10485760 | 单张图片大小上限（超出跳过并明确告知，不静默） |
| `timeout_seconds` | 90 | 单次 API 调用超时 |
| `log_max_bytes` | 1048576 | 运行日志轮转阈值（超过后归档为 `vision_hook.log.1`，保留最近两段） |
| `skip_when_multimodal` | false | 主模型是多模态（原生能看图）时设为 true：跳过视觉 API 识别与注入，图片走原生通道（也可用环境变量 `VISION_SKIP_MULTIMODAL=1`） |
| `max_tokens` | 4000 | 识别结果 token 上限（越大识别越完整，响应越慢） |

Provider 说明（OpenAI 兼容 `/chat/completions`，可自行添加任意提供商）：

| provider | 后端 | 说明 |
|---|---|---|
| `agnes` | agnes-2.5-flash（apihub.agnes-ai.com） | **推荐**：免费聚合后端 |
| `zhipu` | 免费 GLM-4.6V-Flash（bigmodel.cn） | 国内用户可注册；免费、快；有免费限流 |
| `mimo` | 小米 MiMo-V2.5（经 opencode Go 网关） | 质量高但较慢；消耗套餐配额 |
| `mimo-direct` | 小米官方 API（api.xiaomimimo.com） | 备用；需 platform.xiaomimimo.com 的 key |

注意：`mimo-v2.5` 才支持图片，`mimo-v2.5-pro` 不支持。批量行为：每张图独立调用、
**串行**执行（避免撞免费并发限流），结果合并注入（`图1: ... 图2: ...`）。
批量最坏耗时 ≈ 张数 × 单张耗时，hook 超时已放宽到 5 分钟。
`VISION_CONFIG=/path/to/config.json` 可指定配置文件，`VISION_PROVIDER=xxx` 可强制
provider，均用于测试。

## 模型主动调用模式（无 hook 环境）

在**不支持 hook 事件**的环境（其他 Agent、纯 CLI 场景）里，模型可以主动调用脚本识图——
脚本以普通命令形式运行，输出纯文本描述，无注入、无 hooks 依赖：

```bash
# 单图 + 问题：输出纯描述文本（无序号前缀，便于模型直接读取）
python3 vision_hook.py --files screenshot.png --question "用中文描述这张图片，提取所有文字"

# 多图 / 批量：结果落盘后按需读取
python3 vision_hook.py --files a.png b.png --out results.md
```

给目标环境（如 CLAUDE.md / 系统指令）的指令模板：

```markdown
## 图片识别

如果模型不具备原生识图能力，遇到图片时不要用 Read 工具，改用：

python3 /path/to/vision_hook.py --files "<图片路径>" --question "用中文描述这张图片，提取所有文字"

对每张图片依次执行，拿到所有文字描述后再回复。这是强制规则。
```

## 批量识别文件夹（数十张图）

交互式贴图适合 1~5 张。**几十张图请用命令行批量模式**，结果写文件、不占对话上下文：

交互式贴图适合 1~5 张。**几十张图请用命令行批量模式**，结果写文件、不占对话上下文：

```bash
python3 vision_hook.py --folder "图片目录" --out "图片目录/results.md"   # 递归扫描
python3 vision_hook.py --files a.png b.png c.png --out results.md        # 指定文件
python3 vision_hook.py --folder "图片目录" --provider mimo --max 20 --out results.md
```

- 路由与 hook 完全一致：≤3 张走 `provider`，>3 张走 `batch_provider`，失败自动降级
- 支持 png/jpg/jpeg/webp/gif/bmp；串行执行，每张独立调用
- `--out` 建议必填：结果落盘后模型按需读取，避免把几十段描述灌进上下文

## 安全提醒

- API key 只存放在本机 `~/.zcode/vision-hook/config.json`（仓库已通过 `.gitignore`
  排除 `config.json`、`vision_hook_state.json`、`*.log`）；不要在聊天中明文发送、
  不要提交到任何仓库。
- 本仓库只包含 `config.example.json` 占位配置，无任何真实密钥。
- 若 key 曾在聊天/日志中泄露过，建议到对应平台控制台重置。

## 许可证

MIT License，见 [LICENSE](LICENSE)。
