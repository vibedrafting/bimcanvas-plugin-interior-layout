# interior-layout

> BIMCanvas 室内家具布置 domain plugin。覆盖**参考分析 → 规划 → 分区 → 布置**全工作流,
> 提供 6 份运行时设计规则、家具模块库与 5 个 MCP 工具。
>
> **Status**:Phase 1 内部 reference plugin,与 [BIMCanvas](https://github.com/vibedrafting/bimcanvas) 平台基座共同迭代;
> 计划 Phase 2 作为**首个开源 reference plugin** 公开。

---

## 它做什么

把"用户对室内家具布置的自然语言请求"翻译成"几何合法、风格一致的 `modules.json` 布置方案":

- **输入**:`.bcp` 项目(baseline 户型 + 既有 schemes)+ 用户自然语言指令(可附参考图)
- **输出**:`schemes/{sceneId}/.../modules.json`(家具布置)、`semantic_plan.json`(可施工合同)、`reference_analysis.json`(参考图分析快照)
- **边界**:只做"放哪/为什么/怎么放";几何 / 碰撞 / 边界由平台基座 `mcp__canvas__validate_layout` 统一验证

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
| `BIMCANVAS.md` | 主控 Agent 提示词(339 行) —— 五类任务路由 + generate 三种语义判定 |
| `agents/` | 3 个 SubAgent:`layout-agent`(多分区并行) / `variant-design-agent`(multi-plan 单变体) / `module-relocation-agent`(模块替代位置探索) |
| `skills/` | 6 个 Skill:`query-workflow` / `edit-workflow` / `generate-reference-analysis` / `generate-planning` / `generate-placement` / `generate-zoning` |
| `mcp_tools/` | 4 个 MCP 工具(`interior-layout` 命名空间),业务实现在 `lib/business.py`(详见下) |
| `projectMount/modules/` | `module_library.json`(864 行家具决策规则)+ ~30 个 SVG 家具资源,bind-time 物化到项目 `modules/` |
| `projectMount/references/` | 6 份 Markdown 运行时设计规则模板(详见下),bind-time 物化到项目 `references/` |

## 设计工作流

主控 Agent 按自然语言指令路由到对应能力:

| 任务类型 | 触发关键词 | 链路 |
|---|---|---|
| chat | 你好、谢谢、你能做什么 | 直接简短回应 |
| query | 统计、查看、列出、有多少 | 加载 `query-workflow`(只读) |
| edit | 移动、删除、旋转、调整 | 加载 `edit-workflow`(单一修改) |
| relocation | 更好的位置、换个位置、再给几个方案 | 派发 `module-relocation-agent` → clone-then-modify 写变体 |
| generate | 布置、设计、生成、还原、按这张图、手绘、草图 | 进入 generate 语义判定(见下) |

**generate 语义判定**(主控决定走哪条路径):

| 语义 | 触发 | 链路 |
|---|---|---|
| `derived` | 无参考图 / 用户要求主动设计 / 图片仅提供现场信息 | 单分区:`generate-planning` (free) · 多分区:并行派 `layout-agent` |
| `reference-informed-derived` | 用户要参考"感觉/风格/思路/氛围/灵感",图片不作图纸原文 | 同上,图片仅作上下文 |
| `reference-analysis` | 用户明确要参考图片中的"布局/摆位/墙面关系/朝向" | 先 `generate-reference-analysis` 提取约束包 → 按 `relevance` 等级(unrelated / style_only / partially_related / structurally_related)路由到 derived 或 constrained planning |

## SubAgent 调度边界

| Agent | 仅当 | 禁用场景 |
|---|---|---|
| `layout-agent` | 同一轮多分区(≥2 个 zone)派发包字段齐全(`batchId` / `batchZoneIds` / `currentZoneId` 等 8 字段) | 单分区任务 / 仅 placement / 仅 query/edit |
| `variant-design-agent` | multi-plan 模式 canonical `multi-plan-overview` 已就位 + `variantContext` 四字段齐(direction / narrative / anchorSeed / avoidance) | 普通 generate / 单方案设计 |
| `module-relocation-agent` | 主控识别出"目标模块需重新定位"+ 设计区粒度派发包齐(`targetModuleIds` 等) | 普通 placement / 多模块批量布置 |

每个 SubAgent **任务入场第一步强制校验派发包字段**;不满足立即停止,不调用 Skill,不调 MCP,不写入文件。

## MCP 工具(`interior-layout` 命名空间)

| 工具 | 用途 |
|---|---|
| `save_semantic_plan` | 提交规划子阶段(2.1 spatial-skeleton / 2.2 strategic-plan / 2.3 construction-brief / multi-plan-overview)语义方案标签 |
| `load_semantic_plan` | 加载当前设计区生效语义方案(可施工图纸,非完整历史) |
| `save_reference_analysis` | 提交完整参考分析快照(v1 客观 / v2 差异 / v3 用户确认 / v4+ 修订) |
| `load_reference_analysis` | 加载当前设计区参考分析(默认最新标签,可选 `tag` 读取指定版本) |
| `validate_layout` | 几何 / 碰撞 / 边界验证(几何下沉:Server `/api/modules/normalize` + `/api/validation/layout`) |
| `get_zone_boundaries` | 读取设计区与叶子分区边界语义(参数:`zoneId` 或 `zoneIds`) |

调用名规则:`mcp__interior-layout__<tool>`。

> 变体目录创建已由平台工具 `mcp__canvas__register_variant`(三种 mode:`blank` / `clone-from-canonical` / `clone-from-variant`)统一承担,本 plugin 不再提供 `clone_scheme_to_variant`。

## 项目级 references(运行时设计规则)

`projectMount/references/*.md` 在 scene bind-time 物化到项目 `references/...`,Agent 在 planning / placement 阶段按需读取:

| 文件 | 行数 | 用途 |
|---|---|---|
| `design_principles.md` | 105 | 通用设计原则(跨房间通用约束) |
| `design_evaluation.md` | 150 | 设计评价框架(品质复核基准) |
| `bedroom.md` | 326 | 卧室策略 |
| `bathroom.md` | 160 | 卫生间策略 |
| `livingroom.md` | 119 | 客餐厅策略 |
| `optional-furniture-rules.md` | 38 | 可选家具规则 |

## 兼容性与平台依赖

| 字段 | 值 |
|---|---|
| `schemaVersion` | 1 |
| `compatibility.bimcanvas` | `^1.0.0` |
| `maturity` | beta |
| `referenceStability` | semver-tracked |
| `defaultSceneIdPattern` | `interior-layout-{n}` |
| `mcpNamespace` | `interior-layout` |

**必需的平台 MCP 工具**(`canvas` 命名空间,平台基座必须提供):

- `mcp__canvas__register_variant` — 变体目录注册(申请制,三种 mode:`blank` / `clone-from-canonical` / `clone-from-variant`)
- `mcp__canvas__list_variants` — 列出指定设计区下所有变体
- `mcp__canvas__analyze_image` — 图像分析
- `mcp__canvas__request_background_screenshot` — 画布截图
- `mcp__canvas__list_project_scenes` — 跨 scene 元数据
- `mcp__canvas__load_scene_artifact` — 跨 scene 只读叠加

> `modules.json` 由 Agent 用 `Write` / `Edit` 工具直接编辑(保留外层 `schemeMetadata.summary` 字段),不再有专用写入 MCP 工具。

**业务自包(Server 业务下沉)**:本 plugin 4 个工具(`save/load_semantic_plan`、`save/load_reference_analysis`)的全部 domain 业务
—— tag 白名单、canonical-only 约束、planType 启发式判定、effectiveTag 优先级、merge view 合并、reference tag 算法、LegacyEmbedded 兼容 ——
实现在 `mcp_tools/lib/business.py`(纯函数)+ `mcp_tools/interior-layout.py`(工具体),
通过平台**通用 artifact 端点**(`POST /api/scheme/artifacts/{artifactKind}` 写 + `GET ...?path=` 读)落盘。
本 plugin 写的 artifactKinds:`semantic_plan` / `reference_analysis`。
BIMCanvas Server 端不含任何 indoor-layout 业务。通用 IO 契约见主仓库 [`docs/plugin-architecture.md`](https://github.com/vibedrafting/BIMCanvas/blob/master/docs/plugin-architecture.md) §7.1。

## 本地开发

按 BIMCanvas 标准 plugin 沙盒模式(参考 [`vibedrafting/bimcanvas-plugin-template`](https://github.com/vibedrafting/bimcanvas-plugin-template) 的 `.dev-home/plugins/my-plugin/README.md`):

1. 克隆本仓库
2. 在仓库根创建/软链 `.dev-home/plugins/interior-layout/` 指向本仓库根
3. 配置环境变量:
   - Linux / macOS:`export BIMCANVAS_HOME="$(pwd)/.dev-home"`
   - Windows PowerShell:`$env:BIMCANVAS_HOME = "$PWD\.dev-home"`
4. 在 BIMCanvas 主仓库执行 `dotnet run --project BIMCanvas.Server`

**改 references 后生效路径**:`projectMount/references/*.md` 是模板,只在 scene bind-time 物化。改完需对**新项目**或**重新绑定 scene** 才生效;已绑定的旧项目内运行时 references 不受影响(防止 R10 静默覆盖,详见主仓库 `docs/plugin-architecture.md`)。

**改 `module_library.json` 后生效路径**:`projectMount/modules/module_library.json` 同上,影响**新项目**的家具决策规则。

**调试 MCP 工具**:`mcp_tools/interior-layout.py` 暴露 `register(builder)`,严格遵守两条硬约束 —— 不读 `builder.context` 字段、不做 `isinstance` 断言;一切副作用挪到 tool handler 内运行。domain 业务判定在 `mcp_tools/lib/business.py`(纯函数,无 ctx / HTTP 依赖,可独立 import 调试)。

## 目录纯净纪律

仓库根**绝不**放置以下文件 —— `StaticPluginValidator` 会在安装阶段直接拒绝整个 plugin:

- `CLAUDE.md` / `settings.local.json` / `.claude/` / `.bimcanvas/`

`.gitignore` 已预禁这些路径,只要不主动绕过不会触发。

## 状态与开源计划

- **Phase 1(现状)**:内部 reference plugin,与 BIMCanvas 平台基座 `vibedrafting/bimcanvas` 共同迭代;受访问控制,无公开安装入口
- **Phase 2(计划)**:作为**首个开源 reference plugin** 公开,采用 Apache-2.0 许可证;任何 BIMCanvas 用户届时可通过 Web 设置页一键安装

## License

Phase 2 公开时采用 **Apache-2.0**(与 BIMCanvas 主仓库一致)。Phase 1 阶段以仓库访问控制为准。
