# interior-layout — 室内布置设计引擎

> BIMCanvas 首个 domain 插件：把"用户对室内家具布置的自然语言请求"，变成几何合法、风格一致、可对比可回溯的设计方案。
>
> **本文讲这个领域引擎本身——它怎么思考一次室内设计、背后的设计取舍是什么。** 平台/插件通用机制（安装信任两阶段、安全模型、manifest 字段、指针模型、MCP 契约、目录纯净纪律、本地沙盒）见主仓库 [`docs/Arch_Plugin.md`](https://github.com/vibedrafting/bimcanvas/blob/main/docs/Arch_Plugin.md)，不在此复述；通用的 workflow 编排方法论见 [`docs/Arch_Workflow.md`](https://github.com/vibedrafting/bimcanvas/blob/main/docs/Arch_Workflow.md)。
>
> **Status**：Phase 1 内部 reference plugin，与 [BIMCanvas](https://github.com/vibedrafting/bimcanvas) 平台基座共同迭代；计划 Phase 2 作为**首个开源 reference plugin** 公开（Apache-2.0）。

---

## 1. 设计哲学

一次室内布置，本质是**在硬约束（户型/动线/采光/禁区）下做有审美取舍的空间决策**。这件事有两个特征决定了本引擎的形态：

- **它是多解的**。同一个卧室，"床靠窗采光优先"和"床靠墙储物优先"是两个都成立的方向，没有客观最优。
- **它的"好坏"无法标量化**。给布局打 83 分、85 分是自欺——致命缺陷（少了梳妆台）会被其他维度的高分平均稀释掉。

由此定下核心信条：**AI 出多个已自检的方案 + 事实对比表，终选交给用户**。引擎不假装"替你选出最优"——它保证每个方案几何合法、风格自洽、并诚实列出彼此差异（主家具靠哪面墙、储物延米、可选家具有无），由用户看着可视化布局拍板。

> 这不是能力不足的妥协，而是实测结论：早期版本曾让 AI 做裁决（judge 择优），结果是裁决环节自证循环、用低分维度淹没致命缺陷。**移除 AI 裁决、回归"人定终选"后，质量反而更稳。** 详见 §6。

## 2. 当前能力

| | 任务 |
|---|---|
| **支持** | chat · query（统计/查看/列出）· edit（移动/删除/旋转）· **场景①：无参考 · 单设计区 · 多方案设计** |
| **暂不支持** | 参考分析 · 多分区 · relocation（对应 workflow 待建；主控会如实告知，不走旧链路） |

- **输入**：`.bcp` 项目（baseline 户型 + 既有 schemes）+ 自然语言指令
- **输出**：`schemes/{zoneId}/{slug}/[{leaf}/]modules.json`（布置几何）+ 同级 `DESIGN.md`（设计意图合同）
- **边界**：引擎只决策"放哪 / 为什么 / 怎么放"；几何 / 碰撞 / 边界由平台 `validate_layout` 委派本插件 `validators/` 校验

轻任务（chat/query/edit）主控直接处理，不上 workflow——没有扇出价值的流程不值得编排开销。设计任务（场景①）才拉起五段流。

## 3. 设计引擎：场景① 五段流

把一次设计拆成五个**认知阶段**，每段对应设计师真实工作中的一个思维断点。主控吐 `Workflow` 工具拉起 `workflows/interior-layout.workflow.js`：

```jsonc
{ "designZoneId": "<设计区 id>", "originalUserRequest": "<用户原话>", "n": 3 }
// n = 候选方案数，1–4，默认 3
```

| 段 | 认知任务 | 角色 |
|----|---------|------|
| **1 感知** | 听懂用户要什么（定调）+ 独立读懂这个空间（动线 / 纵深 / 采光 / 潜力与风险）——先有空间理解，才谈布置 | `perception-agent` |
| **2 规划推演** | 用两种设计师都会用的思路并行推演：**分区思维**（先切功能带）∥ **顺序思维**（从最重要的家具依次锁墙）。双思维互补，避免单一视角的盲区 | `zoning-design` ∥ `sequential-design` |
| **3 多方案** | 把双草稿收割成 N 个**方向**——每个方向只锁一条硬锚点（如"床靠西墙"），其余决策权完整留给落地。差异在方向层，不在配置层（为什么见 §6） | `multi-plan-agent` |
| **4 落地** | N 路并行，每路独立完成：施工简报 → 按图施工 → 落位自检 → `validate_layout` → **识图自评**（截图问 4-6 个聚焦小问，如"床头柜与床是否贴邻"）→ 自优化。外部视觉视角与内部设计上下文在同一会话闭环 | `placement-agent` ×N + `verify-agent` 核验 |
| **5 对比** | 纯脚本机械拼对比表（家具靠墙 / 储物延米 / 可选家具有无 + 实质雷同标注），零 LLM。**终选 = 用户在 Web 端点「采纳」**（翻父 `DESIGN.md` 的 `adopted` 指针） | `design-scribe` |

每段的中间产物都限长——更短的上游材料让下游 agent 的注意力集中在真正的约束上，每个传递的字段都是"上游写一次、下游读一次"的双向成本。

## 4. 设计知识体系（引擎的护城河）

工作流是骨架，**设计质量真正的来源是知识层**。同一份知识被"怎么落"（落地）和"落得对不对"（自检）共读，规则的分级天然就是自检的 rubric。

### 散文知识：`skills/load-design-knowledge/references/*.md`

> 这 6 份 references 现随 `load-design-knowledge` skill 发布（`skills/load-design-knowledge/references/`），不再项目挂载；`load-design-knowledge` 是它们的**唯一加载入口**，按 stage 增量注入，调用方只按概念引用、不出现文件名。

| 文件 | 答什么 |
|---|---|
| `spatial_design.md` | 怎么读空间（动线/纵深/采光/安静度）+ 怎么组织空间（分区法则·锚内凹角/留白/隐私梯度） |
| `furniture_placement.md` | 家具放置通用法则：墙面归属/朝向/通道/门段/顶角/填满有效段/家具依赖 + 模块间冲突仲裁 |
| `bedroom.md` / `bathroom.md` / `livingroom.md` | 房间级设计策略（按 roomType 选一份，只装房型特异） |
| `design_evaluation.md` | 设计评价框架：五维设计目标 + 两层评价 + 储物充分性/合格底线 |

> 施工方法（闭合预检/修正阶梯/自改图边界/可选家具收束）= placement-procedure skill；非 references。

### 结构化知识：`projectMount/modules/module_library.json`

每个家具携带 `morphology`（怎么变形）/ `topology_rules`（必须怎么放）/ `relation_rules`（与谁配合），规则分三级：

- **【必须】** → Layer 1 硬拦截（违反即不合法）
- **【建议】** → Layer 2 质量扣分
- **【提示】** → 倾向参考

知识按需分级加载（`load-design-knowledge` skill，按 stage（感知/规划推演/多方案/落地）× roomType 增量加载）——不是一次灌满，而是按当前阶段只取需要的那层。

## 5. 构成速查

| 资产 | 内容 |
|---|---|
| `agents/`（7） | `perception` · `zoning-design` · `sequential-design` · `multi-plan`（opus）· `placement` · `verify-agent` · `design-scribe`（除 multi-plan 外均 haiku） |
| `workflows/` | `interior-layout.workflow.js`（五段编排主入口） |
| `mcp_tools/`（3，`interior-layout` 命名空间） | `get_zone_boundaries`（zone 边界段语义 wall/passage/door/window）· `register_variant`（建变体目录骨架）· `adopt_variant`（采纳收口 + 翻指针） |
| `validators/` | `interior-layout.py`（平台 `validate_layout` 委派的几何/碰撞/边界校验脚本） |
| `skills/` | `load-design-knowledge`（references 唯一加载入口·阶段感知；references/ 子目录随其发布） |

> 设计意图统一落 `DESIGN.md`（普通 `Read`/`Write`/`Edit`）；旧的 `semantic_plan` / `reference_analysis` JSON 合同及对应 4 个 MCP 工具已退役删除。落地额外依赖平台 `canvas` 命名空间工具：`load_artifact` / `validate_layout` / `canvas_vision` / `create_job` · `complete_job`。

## 6. 关键设计决策（提炼自实测）

三条决策都是踩坑换来的，对任何"LLM 做空间/设计决策"的系统都通用：

1. **差异化在方向层，不在配置层。** 曾尝试把每面墙的家具归属预先锁死（配置层差异化）——结果剥夺了落地者的全局重判权，一句虚构的方向叙事（"西墙留白"）变成铁律，压死了本该出现的家具，质量崩溃。改为每方案只锁 ≤1 条硬锚点、其余全局自由后，质量恢复。**给 LLM 的硬约束越少越精准，越能发挥；过度预设会把噪音固化成规则。**

2. **粗粒度 Agent，不细拆工序。** 曾把流程拆给 12 个专职 agent（生成/评审×2/裁决/优化…），实测比单会话流程慢 3 倍且质量无增益——每个新会话付一次冷启动税，认知在 agent 间序列化转手时有损。**串行认知链不拆，只有并行扇出（N 个方案同时落地）才值得拆。** 一个连贯认知单元 = 一个 agent 会话："设计一个方案"是一个单元，"评审刚设计的方案"不是——它该留在同一会话内自检。

3. **AI 不做终选，识图自评内置于落地。** 独立的评审/裁决 agent 会自证循环（把施工简报的辩护词当事实）。正确形态是把视觉自检收进落地会话本身：布置完、机器校验通过后，同一 agent 调识图服务问聚焦小问、逐条处置并强制记录。外部视觉视角 + 内部设计上下文同会话闭环，比另起一个无上下文的评审者可靠。

> 更完整的方法论（五层架构、确定性控制流的四种手段、六条实测教训）见主仓库 [`docs/Arch_Workflow.md`](https://github.com/vibedrafting/bimcanvas/blob/main/docs/Arch_Workflow.md)——那是平台层的通用提炼，本节是它在室内布置 domain 的落地。

## 7. 开发与维护

- **改 workflow / agents / `BIMCANVAS.md`**：直接改对应文件，重启 Agent 生效。
- **改 references / `module_library.json`**：改 `projectMount/` 模板，对**新打开的项目**生效。
- ⚠️ **改 workflow / agents 时，同步更新本 README 的 §2/§3/§5**——本 README 曾因落后于工作流重构而严重失真（描述过早已删除的 agent 与"AI 择优"形态）。改流程必改本文。
- **MCP 工具 `register(builder)` 约束**：不读 `builder.context` 字段、不做 `isinstance` 断言，副作用一律挪到 tool handler 内。
- 安装 / 信任 / 沙盒 / 安全模型 / 指针模型 / manifest 字段 / 目录纯净纪律 → 见主仓库 [`docs/Arch_Plugin.md`](https://github.com/vibedrafting/bimcanvas/blob/main/docs/Arch_Plugin.md)。

## License

Phase 2 公开时采用 **Apache-2.0**（与 BIMCanvas 主仓库一致）。Phase 1 阶段以仓库访问控制为准。
