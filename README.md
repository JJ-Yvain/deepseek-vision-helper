# DeepSeek Vision Helper

给纯文本模型（DeepSeek 等）装上"眼睛"：粘贴/拖入图片提交消息时，自动调用视觉模型识别，
把结果以 `[Vision result] ...` 注入对话上下文，纯文本模型即可直接回答图片相关问题。

配套的 `deepseek-vision-helper` skill 会指导模型把注入的识别结果当作"看到"的图片内容来回答，
不再出现"我看不到图片"的情况。

> 本项目面向 [ZCode](https://github.com)（及兼容 `~/.agents/` 规范的工具）：识别脚本依赖
> ZCode 的 transcript / artifacts 机制取图，skill 遵循 ZCode skills 规范。

## 工作原理

```
粘贴图片 + 提问 → UserPromptSubmit / PreToolUse Hook → 检测新落盘附件 → 调视觉 API → 结果注入上下文 → 文本模型回答
```

- **Hook 事件**：`UserPromptSubmit` + `PreToolUse` 双事件（任一触发都会尝试取图；state 记账
  保证同一批图只注入一次；无图时静默跳过，开销约 0.2 秒）
- **取图（state 增量附件监控）**：粘贴的图片附件会落盘到
  `~/.zcode/cli/artifacts/<会话>/prompt-attachment-upload-*.txt`（内容即 data URI）。
  脚本每次运行对比 `vision_hook_state.json`（同目录自动生成）中的已识别记录，
  只识别**新落盘**的附件。之所以不依赖 transcript：ZCode 的 hook transcript 只含纯文本
  （UserPromptSubmit 仅 prompt、PreToolUse 为空），图片 part 永远不会出现在 transcript 里，
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

## 安装

### 1. 安装 skill

把 `skills/deepseek-vision-helper/` 复制到任意 skill 发现根目录：

| 范围 | 路径 | 说明 |
|---|---|---|
| ZCode 用户级 | `~/.zcode/skills/deepseek-vision-helper/` | 所有 workspace 生效 |
| 跨工具共享 | `~/.agents/skills/deepseek-vision-helper/` | Claude / Codex / Cursor 兼容 |
| ZCode 工作区级 | `<repo>/.zcode/skills/deepseek-vision-helper/` | 仅该项目生效 |

### 2. 安装 hook

```bash
# 复制 hook 目录到任意位置（示例：~/.zcode/vision-hook/）
cp -r hook ~/.zcode/vision-hook

# 生成自己的配置并填入 API key
cp ~/.zcode/vision-hook/config.example.json ~/.zcode/vision-hook/config.json
# 编辑 config.json：在 providers 中填入你的 key
```

### 3. 注册 hook（ZCode）

编辑 `~/.zcode/cli/config.json`（或工作区 `.zcode/config.json`），在 `hooks` 字段加入
（建议 `UserPromptSubmit` + `PreToolUse` 双事件挂载，双保险）：

```json
{
  "enabled": true,
  "timeoutMs": 300000,
  "events": {
    "UserPromptSubmit": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "process",
            "command": "/path/to/python3",
            "args": ["/path/to/vision_hook.py"],
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
            "command": "/path/to/python3",
            "args": ["/path/to/vision_hook.py"],
            "timeoutMs": 300000
          }
        ]
      }
    ]
  }
}
```

> 配置文件的 hook 需要 `hooks.enabled: true` 才会执行。事件配置在会话启动时加载，
> 新增/修改事件后需重启客户端生效。

### 4. 验证

```bash
# 无图场景：应无输出、退出码 0
echo '{"hook_event_name":"UserPromptSubmit","session_id":"x","transcript_path":"不存在的文件","prompt":"hi"}' \
  | python3 vision_hook.py
```

然后在 ZCode 里粘贴一张图片发送，日志中出现 `hook fired` 且上下文注入
`[Vision result] ...` 即成功。

## 自动路由

| 场景 | 行为 |
|---|---|
| 1 ~ 3 张图（`batch_threshold`） | 用 `provider`（默认 zhipu / 免费 GLM-4.6V-Flash，约 8s/张） |
| 单张失败（报错/超时/限流） | 自动降级 `fallback_provider` 重试该图 |
| 超过阈值张数 | 整批改用 `batch_provider`（质量高、避开免费限流） |
| 手动强制 | 环境变量 `VISION_PROVIDER=mimo` 强制只用某 provider |

配置项（`config.json`，改动即时生效）：

| 键 | 默认 | 说明 |
|---|---|---|
| `provider` | `zhipu` | 常规后端 |
| `batch_provider` | `mimo` | 批量后端（超过阈值时） |
| `fallback_provider` | `mimo` | 常规/批量失败后的降级后端 |
| `batch_threshold` | 3 | 超过此张数视为批量 |
| `max_images` | 4 | 单次最多识别张数（超出部分注入时注明） |
| `per_image_max_chars` / `total_max_chars` | 800 / 4000 | 单张/总注入长度上限 |
| `max_image_bytes` | 10485760 | 单张图片大小上限（超出跳过） |
| `timeout_seconds` | 90 | 单次 API 调用超时 |
| `max_tokens` | 1500 | 识别结果 token 上限 |

批量行为：每张图独立调用、**串行**执行（避免撞免费并发限流），结果合并注入
（`图1: ... 图2: ...`）。批量最坏耗时 ≈ 张数 × 单张耗时，hook 超时建议放宽到 5 分钟。
嫌慢可调低 `max_images` 或把 `batch_provider` 设为免费后端。

Provider 说明（OpenAI 兼容 `/chat/completions`，可自行添加任意提供商）：

| provider | 后端 | 说明 |
|---|---|---|
| `zhipu` | 免费 GLM-4.6V-Flash（bigmodel.cn） | 免费、快；有免费限流 |
| `agnes` | agnes-2.5-flash（apihub.agnes-ai.com） | 免费聚合后端 |
| `mimo` | 小米 MiMo-V2.5（经 opencode Go 网关） | 质量高但较慢；消耗套餐配额 |
| `mimo-direct` | 小米官方 API（api.xiaomimimo.com） | 备用；需 platform.xiaomimimo.com 的 key |

注意：`mimo-v2.5` 才支持图片，`mimo-v2.5-pro` 不支持。若某网关出现 HTTP 500
问题，切换其他 provider 即可（`VISION_CONFIG=/path/to/config.json` 可指定配置文件，
`VISION_PROVIDER=xxx` 可强制 provider，均用于测试）。

## 批量识别文件夹（数十张图）

交互式贴图适合 1~5 张。**几十张图请用命令行批量模式**，结果写文件、不占对话上下文：

```bash
# 扫描目录下所有图片（递归），结果写入 results.md
python3 vision_hook.py --folder "图片目录" --out "图片目录/results.md"

# 指定文件列表
python3 vision_hook.py --files a.png b.png c.png --out results.md

# 强制某 provider / 限量
python3 vision_hook.py --folder "图片目录" --provider mimo --max 20 --out results.md
```

- 路由与 hook 完全一致：≤3 张走 `provider`，>3 张走 `batch_provider`，失败自动降级
- 支持 png/jpg/jpeg/webp/gif/bmp；串行执行，每张独立调用
- `--out` 建议必填：结果落盘后模型按需读取，避免把几十段描述灌进上下文

## 故障排查

1. **完全不生效**：查看 `vision_hook.log`（脚本同目录）是否有 `hook fired`；没有说明
   hook 没被调用（检查 `hooks.enabled: true` 与事件名/匹配器，新增事件后需重启客户端）。
2. **`no image found`**：没有检测到新落盘的图片附件。确认贴图后附件会写入
   `~/.zcode/cli/artifacts/<会话>/prompt-attachment-upload-*.txt`；若文件存在但未识别，
   检查 `vision_hook_state.json` 是否已记录该文件（同一张图只识别一次，属正常行为）。
3. **`vision api failed`**：看具体错误；`1305` 是平台过载，稍后重试即可；401 是 key 无效。
4. **skill 不触发**：确认 `SKILL.md` frontmatter 的 `description` 中存在
   `[Vision result]` 触发词，且 skill 位于发现根目录（见上文安装表）。

## 安全提醒

- API key 只存放在本机 `config.json`（已被 `.gitignore` 排除）；不要在聊天中明文发送、
  不要提交到任何仓库。
- 若 key 曾在聊天/日志中泄露过，建议到对应平台控制台重置。
- 本仓库只包含 `config.example.json` 占位配置，无任何真实密钥。

## 许可证

MIT License，见 [LICENSE](LICENSE)。
