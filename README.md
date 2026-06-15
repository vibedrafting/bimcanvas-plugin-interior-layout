# interior-layout（室内布置 domain plugin）

> BIMCanvas 首个 domain 插件：把自然语言的室内家具布置请求，变成几何合法、风格一致的设计方案。
>
> **本文只讲本插件独有的部分。** 平台/插件通用机制（安装信任两阶段、安全模型、manifest 字段、指针模型、MCP 契约、目录纯净纪律、本地沙盒）见主仓库 [`docs/Arch_Plugin.md`](https://github.com/vibedrafting/bimcanvas/blob/main/docs/Arch_Plugin.md)，不在此复述。
>
> **Status**：Phase 1 内部 reference plugin，与 [BIMCanvas](https://github.com/vibedrafting/bimcanvas) 平台基座共同迭代；计划 Phase 2 作为**首个开源 reference plugin** 公开（Apache-2.0）。

---

## 1. 当前能力

用户视角"能让它干嘛"：

| | 任务 |
|---|---|
| **支持** | chat · query（统计/查看/列出）· edit（移动/删除/旋转）· **场景①：无参考 · 单设计区 · 多方案设计** |
| **暂不支持** | 参考分析 · 多分区 · relocation（对应 workflow 待建；主控会如实告知，不走旧链路） |

- **输入**：`.bcp` 项目（baseline 户型 + 既有 schemes）+ 自然语言指令
- **输出**：`schemes/{zoneId}/{slug}/[{leaf}/]modules.json`（布置）+ 同级 `DESIGN.md`（设计意图）
- **边界**：插件只决策"放哪 / 为什么 / 怎么放"；几何 / 碰撞 / 边界由平台 `mcp__canvas__validate_layout` 委派本插件 `validators/` 校验

## 2. 核心：场景① 五段流

本插件的灵魂。主控吐 `Workflow` 工具拉起 `workflows/interior-layout.workflow.js`：

```jsonc
{ "designZoneId": "<设计区 id>", "originalUserRequest": "<用户原话>", "n": 3 }
// n = 候选方案数，1–4，默认 3
```

五段（全自动、无中途交互）：

1. **感知** `perception-agent`：诉求定调 + 空间骨架（动线/纵深/采光）
2. **规划推演** `zoning-design` ∥ `sequential-design`（并行）：分区思维 × 顺序思维双草稿
3. **多方案** `multi-plan-agent`：N 个方向变体（差异在方向层，每变体 ≤1 条硬锚点）
4. **落地** `placement-agent` ×N 并行 + `verify-agent` 后置核验：施工 → 自检 → `validate_layout` → 识图自评 → 自优化 → factsheet
5. **对比** `design-scribe`：机械拼对比表（含雷同标注）

**AI 不做终选**——产出 N 个方案 + 对比表，**用户在 Web 端点「采纳」**（采纳 = 翻父 `DESIGN.md` 的 `adopted` 指针）。

## 3. 构成（本插件独有资产）

| 资产 | 内容 |
|---|---|
| `agents/`（7） | `perception` · `zoning-design` · `sequential-design` · `multi-plan`(opus) · `placement` · `verify-agent` · `design-scribe`（除 multi-plan 外均 haiku） |
| `workflows/` | `interior-layout.workflow.js`（五段编排） |
| `mcp_tools/`（3，`interior-layout` 命名空间） | `get_zone_boundaries`（zone 边界段语义 wall/passage/door/window）· `register_variant`（建变体目录骨架）· `adopt_variant`（采纳收口 + 翻指针） |
| `validators/` | `interior-layout.py`（平台 `validate_layout` 委派的几何/碰撞/边界校验脚本） |
| `skills/` | `load-design-knowledge`（设计知识分级加载，L1/L2 × roomType） |
| `projectMount/references/`（6） | `design_principles` · `bedroom` · `bathroom` · `livingroom` · `optional-furniture-rules`（以上 L1）· `design_evaluation`（L2） |
| `projectMount/modules/` | `module_library.json`（家具决策规则）+ SVG 家具资源 |

`references/` 与 `modules/` 在打开项目时按 active plugin 物化到项目全局。

> 设计意图统一落 `DESIGN.md`（普通 `Read`/`Write`/`Edit`）；旧的 `semantic_plan` / `reference_analysis` JSON 合同及对应 4 个 MCP 工具已退役删除。

## 4. 开发与维护

- **改 workflow / agents / `BIMCANVAS.md`**：直接改对应文件，重启 Agent 生效。
- **改 references / `module_library.json`**：改 `projectMount/` 模板，对**新打开的项目**生效。
- ⚠️ **改 workflow / agents 时，同步更新本 README 的"当前能力 / 五段流 / 构成"**——本 README 曾因落后于工作流重构而严重失真（描述过已删除的 `generator/critic/judge` 与"单方案 judge 择优"）。改流程必改本文。
- **MCP 工具 `register(builder)` 约束**：不读 `builder.context` 字段、不做 `isinstance` 断言，副作用一律挪到 tool handler 内。
- 安装 / 信任 / 沙盒 / 安全模型 / 指针模型 / manifest 字段 / 目录纯净纪律 → 见主仓库 [`docs/Arch_Plugin.md`](https://github.com/vibedrafting/bimcanvas/blob/main/docs/Arch_Plugin.md)。

## License

Phase 2 公开时采用 **Apache-2.0**（与 BIMCanvas 主仓库一致）。Phase 1 阶段以仓库访问控制为准。
