---
name: single-zone-design
description: |
  单设计区完整设计阶段（感知→规划→多方案）的唯一权威流程 SOP。被主控（M1 单区，
  可交互）或 zone-design-agent（M2 多区，静默）加载执行——两处共用本 SOP，设计行为同源。
  Step0 读项目级全屋协调（若有）→ Step1 感知（perception-method）→ Step2 规划双思维
  （zoning-thinking + sequential-thinking）→ Step3 多方案（multi-variant-diversity），
  逐步加载方法 Skill 供能、按 design-doc-upsert 写本区父 DESIGN.md，返回 variants[]+四段。
  本 SOP 只管设计阶段，不做坐标级落地（落地由调用方接 fanout）。
allowed-tools: Read, Glob, Write, Edit, Skill, mcp__interior-layout__get_zone_boundaries, mcp__canvas__canvas_vision
---

# 单设计区设计流程 SOP（感知→规划→多方案）

本 SOP 是**单设计区设计阶段的唯一权威流程定义**。无论你是 **M1 可交互主控** 还是 **M2 静默 zone-design-agent**，都加载并严格执行本 SOP——设计逻辑只此一份，保证单区/多区设计行为一致。

IMPORTANT: 必须用工具调用 API（function calling）调用 MCP 工具，禁止输出 `<mcp__xxx>...</mcp__xxx>` 文本。

## 执行模式（交互策略，唯一与执行者相关的参数）

调用方会告知**执行模式**：
- **interactive（M1，主控自跑）**：关键战略选择点（路径取舍 / 锚点歧义 / 诉求与户型矛盾）**可**按需 `AskUserQuestion` 征询用户（SEAM）；默认不暂停、按推荐方向继续并标 `[自动代决] <决策+理由>`。
- **silent（M2，zone-design-agent 扇出）**：**全程静默,禁 `AskUserQuestion`**；所有策略点一律自动代决并标 `[自动代决]`，不静默吞掉。

> 除交互策略外，两模式的设计步骤、判据、产出**完全相同**。各方法 Skill（perception-method 等）已内置「调用方为可交互主控则可 SEAM，否则自动代决」逻辑——把本执行模式如实传达即可。

## 入场参数

调用方给出：`designZoneId`（设计区节点 path）、`tags`（功能标签，可空）、`zoneRequest`/`originalUserRequest`（本区诉求原文）、执行模式。

## Step0 · 读全屋协调约束（若存在）

`Read` 项目级 `schemes/DESIGN.md`（全屋协调,主控在 M2 全屋核阶段写；M1 standalone 时可能不存在）：
- **存在**：提取「全屋风格基调」「本设计区相邻/共享边界」「该区角色诉求」，作为本区设计的**硬性输入**——风格须遵循全屋基调，规划须尊重共享边界（D4 事前协调）。把采纳的约束在 Step1 strategySec 的 `### 战略取舍与存疑` 里留痕。
- **不存在**：standalone，本区独立设计，不臆造全屋约束。

> WHY：跨区一致性本期不做事后缝合核验，全靠各区在设计期读同一份全屋协调、自觉对齐（风格/动线/共享门洞）。读不到就 standalone，但读到必遵守。

## Step1 · 感知（产 strategySec + spaceSec）

1. `Skill` 加载 `perception-method`（感知执行程序）+ 它要求的 `load-design-knowledge`（按【感知】阶段取 references）。
2. 按 perception-method 完成 §1 战略定调 + §2 空间骨架，产出 `strategySec`（`## 用户诉求 + 项目基础信息`）与 `spaceSec`（`## 设计区空间骨架`）两节。
3. `Skill` 加载 `design-doc-upsert`，把两节幂等写入本区父 `schemes/{designZoneId}/DESIGN.md`。

## Step2 · 规划推演（双思维，产 zoningSec + seqSec）

1. `Skill` 加载 `zoning-thinking` + `sequential-thinking`。
2. 分别产出 `### 方案草稿 · 分区思维`（zoningSec）、`### 方案草稿 · 顺序思维`（seqSec）两子节（只记墙面归属/相邻，不写坐标）。
3. 按 `design-doc-upsert` 写父「## 方案草稿」节（含上述两子节）。

## Step3 · 多方案（产 variants[]）

1. `Skill` 加载 `multi-variant-diversity`。
2. 收割双草稿，产出 `variants[]`（每项 `slug` / `direction` / `narrative` / `anchorSeed` / `avoidance` / `expectedWalls`）+ `proposedN` + `excluded[]`；**自查各 `expectedWalls` 归一化后两两不同**（雷同合并、减数），落地集 ≤4。
3. 按 `design-doc-upsert` 写父「## 多方案战略层概述」节。

## 返回（设计阶段产物，交调用方接落地）

按以下结构返回（= `ZONE_DESIGN_SCHEMA`）：

```
{ designZoneId, ok:true,
  variants: [{slug, direction, narrative, anchorSeed, avoidance, expectedWalls}],
  strategySec, spaceSec, zoningSec, seqSec }
```

- `variants` + 四段 = 下游 `interior-layout-fanout` 落地脚本的入参契约，直接对接。
- 设计阶段无法产出有效方案（户型/诉求矛盾不可解）时,返回 `{ designZoneId, ok:false, reason:"一句话原因" }`,**不强行凑方案**。

## 边界与红线

- **本 SOP 只做设计阶段（Step1-3）**：不做坐标级落地、不调 `register_variant`/`validate_layout`、不翻 `adopted` 指针。落地由调用方负责（M1 主控吐 fanout / M2 全屋 workflow 调 fanout）。
- 写盘只写本区 `schemes/{designZoneId}/` 下文件，**不碰其它设计区、不碰项目级 `schemes/DESIGN.md`**（项目级是主控单一写者）。
- 失败即失败、如实 `ok:false`，禁补救凑成功。
