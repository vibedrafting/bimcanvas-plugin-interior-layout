---
name: whole-house-zoning
description: |
  场景③多区「全屋核 + 拆区」方法（仅 M2，由主控自跑、不扇出——全屋统一理解需推理连贯）。
  读全屋 baseline（rooms / openings / room_zones）建立全屋级理解（公私分区 / 整屋动线主轴 /
  风格统一），把全屋拆成一组设计区（MVP：一可设计房间 rz_* = 一设计区），并产出全屋协调，
  由主控写项目级 schemes/DESIGN.md（单一写者）。本方法不做单区设计（那是 single-zone-design SOP）。
allowed-tools: Read, Glob, Write, Edit, Skill, mcp__interior-layout__get_zone_boundaries
---

# 全屋核 + 拆区（M2，主控自跑）

本方法在多区设计模式（M2）由**主控亲自执行**（不扇出）：全屋是一个需要连贯理解的整体，拆区与协调必须在单一上下文里一次想清，才能给各设计区下一致的约束。产出 `designZones[]` + 全屋协调，**由主控按本方法写项目级 `schemes/DESIGN.md`**。

IMPORTANT: 必须用工具调用 API 调用 MCP 工具，禁止输出 `<mcp__xxx>...</mcp__xxx>` 文本。

## 纪律

- **【必须】**默认中文思考与产出。
- **【必须】**全屋拓扑/相邻判断靠**坐标事实**，不靠识图：当前运行时（deepseek）无 vision，`canvas_vision` 返回不可信——`Read baseline/rooms.json` + `baseline/openings.json` + `computed/room_zones.json` 用坐标判房间相邻与门洞归属，**禁**以识图或"无门窗→外立面"臆断。
- **【必须】**不改 `baseline/`；项目级 `schemes/DESIGN.md` 由主控写、是**纯协调文档**（无 frontmatter `adopted`，非 Server 托管方案文件）。

## 入场动作

1. `Read` 当前项目 `README.md`（意图与材料定位）+ 用户 `originalUserRequest`（全屋诉求）。
2. `Read baseline/rooms.json`（房间 id/name/type/boundary）、`computed/room_zones.json`（可设计区 rz_* + tags/optionalTags）、`baseline/openings.json`（门窗，定共享门洞）。
3. 必要时 `get_zone_boundaries` 取候选设计区边界核对。

## Step1 · 全屋核（统一理解）

从坐标事实建立全屋级判断：
- **公私分区**：按 room type 分公共（客厅/餐厅/厨房…）与私密（主卧/次卧/卫…），定隐私梯度。
- **整屋动线主轴**：由入户门 + 房间门洞（openings）推主通道走向，标各房间入口归属。
- **全屋风格基调**：从用户诉求提炼统一风格关键词（各区共享，避免并行设计各跑各的风格）。诉求未指定则给一个中性默认基调并标 `[自动代决]`。

## Step2 · 拆区（产 designZones[]）

- **MVP 拆区规则**：全屋每个可设计房间 `rz_*`（`computed/room_zones.json` 中 type=room/designable 且 visible）= 一个设计区。大空间内再切 subZone 留二期，本期不做。
- 每个设计区产一项：`{ designZoneId, tags, zoneRequest }`——`tags` 取该 rz_* 的功能标签；`zoneRequest` = 该区在全屋诉求下的角色诉求（如"主卧:睡眠+大收纳+梳妆"）。
- 用户诉求只涉及部分房间时，只拆用户要设计的那些区（不替用户全屋铺开，除非诉求是"全屋/整屋"）。

## Step3 · 写项目级 `schemes/DESIGN.md`（主控单一写者，契约①）

按以下结构写项目级 `schemes/DESIGN.md`（纯协调、无 frontmatter）：

```
# 全屋协调
## 全屋诉求          # originalUserRequest 摘要
## 全屋风格基调      # 统一风格关键词（各区共享，M1/M2 规划均须遵循）
## 设计区清单        # 每项:designZoneId / tags / 该区角色诉求
## 共享边界与相邻    # 共享门洞、公私分区、动线主轴（事前协调，替代缝合 verify）
## 决策日志          # [自动代决] 条目
```

> 各设计区的 zone-design-agent 会在设计期 `Read` 本文件、据此对齐风格与共享边界（D4/D12 事前协调）。本文件写全、写准，是多区一致性的唯一保障。

## 返回（交主控吐全屋 workflow）

返回 `designZones[]`（每项 `{designZoneId, tags, zoneRequest}`）。主控据此吐全屋 workflow（`args.designZones`）。

## 边界

- 本方法**不做单区设计**（感知/规划/多方案是 `single-zone-design` SOP 的事）。
- 不做坐标级落地、不翻 `adopted`。
