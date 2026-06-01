# interior-layout

> BIMCanvas 室内家具布置 domain plugin。
>
> **架构过渡中**:正从「BIMCANVAS.md 提示词软编排」迁移到「确定性 Workflow 编排 LLM subagent」。
> 当前已上线 **query / edit / 场景①(无参考·单设计区·最优方案)**;参考分析 / 多方案 / 多分区 / relocation **暂不支持**(旧软编排已退役、新 workflow 待上线)。
>
> **Status**:Phase 1 内部 reference plugin,与 [BIMCanvas](https://github.com/vibedrafting/bimcanvas) 平台基座共同迭代;计划 Phase 2 作为**首个开源 reference plugin** 公开。

---

## 它做什么

把"用户对室内家具布置的自然语言请求"翻译成"几何合法、风格一致的 `modules.json` 布置方案":

- **输入**:`.bcp` 项目(baseline 户型 + 既有 schemes)+ 用户自然语言指令
- **输出**:`schemes/{zoneId}/{slug}/[{leaf}/]modules.json`(家具布置,纯指针式平级模型)+ `schemes/{zoneId}/{slug}/DESIGN.md`(设计意图合同)
- **边界**:只做"放哪 / 为什么 / 怎么放";几何 / 碰撞 / 边界由平台基座 `mcp__canvas__validate_layout` 统一验证(委派本插件 `validators/` 校验脚本)

## 安装与启用

> Phase 2 公开后,任意 BIMCanvas 用户走以下流程;当前 Phase 1 阶段需已拿到仓库访问权。

1. 在 BIMCanvas Web → 设置页 → 插件管理 → **[+ 安装新插件]**
2. 粘贴 `https://github.com/vibedrafting/bimcanvas-plugin-interior-layout` → 确认
3. 安装列表出现 `interior-layout [未信任]` → 点 **[信任并激活]** → 二次确认 → `ExecutablePluginProbe` 通过
4. 提示重启 → 重启后 active plugin = `interior-layout`

平台对插件采用 **install / trust 两阶段隔离**:安装时只做静态校验、绝不执行任何 plugin 代码;信任阶段才会调用一次 `register(builder)` dry-run。详见主仓库 [`docs/plugin-security-model.md`](https://github.com/vibedrafting/bimcanvas/blob/main/docs/plugin-security-model.md)。

## 能力一览

| 层 | 内容 |
|---|---|
| `BIMCANVAS.md` | 主控 Agent 提示词 —— 业务路由 + query/edit 内联 + 场景① workflow 调起 + 未迁移场景诚实降级 |
| `workflows/` | `interior-layout.workflow.js` —— 场景①确定性编排(GEN 骨架 → N 候选 → 多维评审 → 择优 → 精修 → 翻指针) |
| `agents/` | 3 个 workflow 角色 agent:`generator`(单脑生成,opus) / `critic`(单维度评审,haiku) / `judge`(合成仲裁,opus) |
| `validators/` | `interior-layout.py` —— 平台 `validate_layout` 委派的几何 / 碰撞 / 边界校验脚本 |
| `mcp_tools/` | 1 个 domain MCP 工具 `get_zone_boundaries`(`interior-layout` 命名空间) |
| `projectMount/modules/` | `module_library.json`(家具决策规则)+ ~30 个 SVG 家具资源,打开项目时按 active plugin 物化到项目 `modules/` |
| `projectMount/references/` | 6 份 Markdown 运行时设计规则模板(详见下),打开项目时物化到项目 `references/` |

## 业务路由

主控 Agent 按自然语言指令路由:

| 任务类型 | 触发关键词 | 处理 |
|---|---|---|
| chat | 你好、谢谢、你能做什么 | 直接简短回应 |
| query | 统计、查看、列出、有多少 | 主控**直接**读文件聚合(`load_artifact` + `Glob` + `Read`),不上 workflow |
| edit | 移动、删除、旋转、调整 + 明确目标 | 主控**直接**改 `modules.json` + `validate_layout`,不上 workflow |
| generate · 场景① | 布置 / 设计 / 生成,且**无参考 + 单设计区 + 非多方案** | 调起 `Workflow` 工具跑 `workflows/interior-layout.workflow.js` |
| 参考分析 / multi-plan / 多分区 / relocation | 参考图布局 / 多给几种 / 多个区 / 换个位置 | **暂不支持**——如实告知用户「正在迁移至 workflow,当前仅场景①可用」,禁走旧链路 |

## 场景① workflow

`workflows/interior-layout.workflow.js`(参数化主入口),主控以 `args` 调起:

```jsonc
{ "scenario": "single-zone-optimal", "zoneId": "<设计区 id>", "n": 3, "refineLevel": 1, "originalUserRequest": "<用户原话>" }
```

内部流程(全自动、无中途交互):GEN 骨架(generator)→ N 候选并行(generator,各自落位 + Layer1 机检)→ 每候选×每维度并行评审(critic)→ 择优(judge)→ 精修循环 ≤ refineLevel(critic+judge+generator,首轮达标即收、不改方向)→ 采纳(generator 去 `_` 前缀转正 + Edit 父 `DESIGN.md` 的 `adopted` + 写决策日志)。

## MCP 工具(`interior-layout` 命名空间)

| 工具 | 用途 |
|---|---|
| `get_zone_boundaries` | 读取设计区与叶子分区边界语义(把 zone 多边形拆成 wall / passage / door / window 段;参数:`zoneIds`) |

调用名规则:`mcp__interior-layout__get_zone_boundaries`。

> **已退役(指针模型 + workflow 重构)**:`save/load_semantic_plan`、`save/load_reference_analysis` 四个工具已删除——设计意图改落 `DESIGN.md`(普通 `Read`/`Write`/`Edit`),不再用 semantic_plan / reference_analysis 的 JSON 合同。

## 依赖的平台 MCP 工具(`canvas` 命名空间,平台基座提供)

| 工具 | 用途 |
|---|---|
| `mcp__canvas__load_artifact` | 通用只读 artifact(`modules` / `zones` / domain kinds);按 `path` 读单区(裸 zoneId 自动解析 adopted 指针)或留空聚合 |
| `mcp__canvas__validate_layout` | 几何 / 碰撞 / 边界验证(委派 active plugin 的 `validators/` 脚本) |
| `mcp__canvas__request_background_screenshot` | 画布截图(critic 接地看真实布局) |
| `mcp__canvas__analyze_image` | 图像分析 |

> `modules.json` 由 Agent 用 `Write` / `Edit` 工具直接编辑(保留外层 `schemeMetadata.summary` 字段),无专用写入 MCP 工具。
> 变体目录由 Agent 用 `Write` 直接建、用 `Bash mv` 转正/翻指针——平台不再提供 `register_variant` / `list_variants` MCP 工具(列方案 = `Glob schemes/{zoneId}/*/`,生效 = 读父 `DESIGN.md` 的 `adopted`)。

## 指针模型(数据布局)

```
schemes/{zoneId}/DESIGN.md              # 分区父:frontmatter adopted:{slug} = 当前生效方案指针
schemes/{zoneId}/{slug}/DESIGN.md       # 每个方案一份(平级,无 variants/ 层)
schemes/{zoneId}/{slug}/[{leaf}/]modules.json   # 该方案几何(叶子级)
```

`_` 前缀 slug = 隐藏候选;采纳 = 翻父 `adopted` 指针(零复制 / 零删除 / 可逆)。**没有固定 canonical `schemes/{zoneId}/modules.json`**。

## 项目级 references(运行时设计规则)

`projectMount/references/*.md` 在打开项目时按 active plugin 物化到项目 `references/...`,workflow 的 generator / critic 按需读取:

| 文件 | 用途 |
|---|---|
| `design_principles.md` | 通用设计原则(跨房间通用约束 + 自改图边界 + 闭合施工预检) |
| `design_evaluation.md` | 设计评价框架(五维设计目标 + 两层评价,critic/judge 的 rubric) |
| `bedroom.md` | 卧室策略 |
| `bathroom.md` | 卫生间策略 |
| `livingroom.md` | 客餐厅策略 |
| `optional-furniture-rules.md` | 可选家具规则 |

## 兼容性与平台依赖

| 字段 | 值 |
|---|---|
| `schemaVersion` | 1 |
| `compatibility.bimcanvas` | `^1.0.0` |
| `mcpNamespace` | `interior-layout` |

## 本地开发

按 BIMCanvas 标准 plugin 沙盒模式(参考 [`vibedrafting/bimcanvas-plugin-template`](https://github.com/vibedrafting/bimcanvas-plugin-template)):

1. 克隆本仓库
2. 在仓库根创建 / 软链 `.dev-home/plugins/interior-layout/` 指向本仓库根
3. 配置环境变量:
   - Linux / macOS:`export BIMCANVAS_HOME="$(pwd)/.dev-home"`
   - Windows PowerShell:`$env:BIMCANVAS_HOME = "$PWD\.dev-home"`
4. 在 BIMCanvas 主仓库执行 `dotnet run --project BIMCanvas.Server`

**改 references / `module_library.json` 后生效路径**:`projectMount/` 下是模板,打开项目时按 active plugin 物化到项目;改完对**新打开的项目**生效,运行时项目内的副本可由用户按项目微调。

**调试 MCP 工具**:`mcp_tools/interior-layout.py` 暴露 `register(builder)`,严格遵守 —— 不读 `builder.context` 字段、不做 `isinstance` 断言;一切副作用挪到 tool handler 内运行。

## 目录纯净纪律

仓库根**绝不**放置以下文件 —— `StaticPluginValidator` 会在安装阶段直接拒绝整个 plugin:

- `CLAUDE.md` / `settings.local.json` / `.claude/` / `.bimcanvas/`

`.gitignore` 已预禁这些路径,只要不主动绕过不会触发。

## 状态与开源计划

- **Phase 1(现状)**:内部 reference plugin,与 BIMCanvas 平台基座共同迭代;受访问控制,无公开安装入口
- **Phase 2(计划)**:作为**首个开源 reference plugin** 公开,采用 Apache-2.0 许可证

## License

Phase 2 公开时采用 **Apache-2.0**(与 BIMCanvas 主仓库一致)。Phase 1 阶段以仓库访问控制为准。
