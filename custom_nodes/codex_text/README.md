# Codex · 文本生成

通过本机 `codex exec` 生成文案、分镜和画面提示词，输出标准 `STRING`。默认 **`gpt-5.6-sol` + `xhigh`（极高）**，模型名和推理强度均可在节点中配置，不修改全局 Codex 默认值。

## 使用

在 ComfyUI 工作流列表打开：

- **Codex · 01 文本生成**：输入主题或原文，预览正文并保存 `.txt`。
- **Codex · 02 分镜 JSON**：输入剧情，按示例 Schema 生成 `title` 和 `shots`，预览并保存 `.json`。

两个示例都通过原生 `Preview as Text` / `Save Text` 节点展示和保存结果，文件位于 `ComfyUI/output/Codex/`。也可以搜索 **Codex · 文本生成** 手动添加节点。

| 参数 | 用途 |
| --- | --- |
| `prompt` | 用户任务或上游传入的文本 |
| `model` | 可编辑的 Codex 模型标识，默认 `gpt-5.6-sol` |
| `reasoning_effort` | 默认 `xhigh`；可选 `low`、`medium`、`high`、`xhigh`、`max`、`ultra`，需所选模型支持 |
| `timeout_seconds` | 等待上限，默认 900 秒 |
| `instructions` | 写作角色、风格和输出要求，可留空 |
| `output_schema` | 可选 JSON Schema；留空输出普通文本，填写后传给 CLI 的 `--output-schema` |
| `reuse_result` | 默认关闭；开启后复用相同输入的成功结果，便于视频工作流续跑 |
| `generation_id` | 默认 0；启用复用后，修改编号生成新版本，不是模型随机种子 |

连接生图或生视频：将目标节点的 `prompt` 控件转换为输入，再连接本节点的 `text`。建议先让 Codex 只输出该镜头的画面提示词。分镜 JSON 需要先提取具体镜头字段，不能直接把整份分镜当作单个镜头的提示词。

默认每次执行节点都会发起新的 Codex 调用，不自动重试。开启 `reuse_result` 后，相同提示词、模型、推理强度、写作要求、Schema 和 `generation_id` 会复用 `ComfyUI/user/codex_text/cache/` 中的成功结果。也可保存已确认文案并用原生 Text 节点传递。超时或在 ComfyUI 中取消时会结束本次 CLI 进程组；已经发生的云端用量不会撤回。

## 运行环境

- 2026-09-19 已将 npm 全局 Codex CLI 从 `0.153.4` 升级到 `0.155.0`，使用该版提供的 `--ignore-user-config` 等参数。
- 复用同一用户的 Codex 登录状态，凭据不写入工作流。登录失效时在终端执行 `codex login`，用 `codex login status` 查看状态。
- 每次调用使用独立临时目录、只读沙箱和非交互模式，忽略用户 `config.toml`，不加载项目 AGENTS.md；关闭 shell、联网搜索、Apps、插件、Hooks 和多智能体等功能，并要求模型只生成文本。账号的组织管理策略仍可能影响调用。
- 忽略用户配置也意味着本节点不沿用全局自定义模型供应商、MCP 或配置 profile；当前接入面向已登录的标准 Codex 服务。
- CLI 在本机执行，Sol 推理通过网络完成，使用当前登录账号的额度。
- 可用 `CODEX_CLI_PATH` 环境变量指定 CLI 可执行文件；默认在 PATH 和本机常见 npm/Node 安装目录中查找。该路径不开放为工作流参数。
- Python 使用现有 `ComfyUI/.venv`，无需安装额外 Python 依赖；节点源码位于 `ComfyUI/custom_nodes/codex_text`。

JSON Schema 约束交给 Codex/模型服务处理，节点检查返回内容可解析为 JSON；无效或不受支持的 Schema、模型和推理档位会直接报错，不会静默切换模型。示例是镜头规划，不生成实际字幕时间轴或音频对齐结果。

## 实测记录

2026-09-19，经用户明确要求，通过 ComfyUI `/prompt` 实际执行上述两个示例，均使用 `gpt-5.6-sol` + `xhigh`，未修改默认模型或推理强度：

- 纯文本：16.13 秒完成，返回 236 字符，预览与保存的 TXT 完全一致。
- 分镜 JSON：34.18 秒完成，返回 3 个镜头；字段、镜头序号、每镜计划 5 秒及非空解说/画面/视频提示词检查通过，预览与保存的 JSON 内容一致。

两次任务均为 `success`，输出保存在 `ComfyUI/output/Codex/verification/20260919-004117/`。详见 [实测报告](../../../artifacts/comfyui-codex-text/live-20260919-004117/report.json)；同目录保留提交的工作流与 ComfyUI 执行历史。此次只验证正常文本/JSON 生成、预览与落盘，没有额外调用图片或视频模型，也未覆盖超时、取消和登录失效等异常场景。

参考：[Codex 非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)、[CLI 配置](https://learn.chatgpt.com/docs/config-file/config-reference)、[GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol)。
