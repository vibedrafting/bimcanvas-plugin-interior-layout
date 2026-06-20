---
name: load-design-knowledge
description: |
  设计知识统一加载器。**references 的唯一加载入口**——你据当前设计阶段把恰好够用的
  设计参考文件读进上下文，不多读（省上下文）、不少读（缺判据）。**无入参**：你本就知道
  自己在哪个阶段、设计什么房型，照下表自取，无需任何文件名（文件清单只在本 Skill 维护）。
  references 随本 Skill 目录发布（`references/` 子目录），非项目挂载。
allowed-tools: Read
---

# 加载设计知识（阶段感知 · 房型感知）

本 Skill 只做一件事：**按你当前所处的设计阶段 + 房间类型，Read 对应的设计参考文件**。它不解释、不裁剪、不补充——规则本体在被读取的 `.md` 里。**它是 references 的唯一加载入口**：别的 skill/agent 不直接 Read references、不写参考文件名，只调用本 Skill 并告知阶段。

## 无入参 —— 自助路由

本 Skill **不收类型化入参**（它是上下文增强，不是带签名的函数）。你本就知道当前阶段（`感知` / `规划推演` / `多方案` / `落地`）与房间类型——直接照下表自取对应 references 即可。

## 阶段 → 读哪些（**按阶段只读够用的，不要全量**）

文件都在**本 Skill 同目录的 `references/` 子目录**（路径 = 本 skill 根 + `/references/<file>`）。`{roomType}.md` 按你判断的房型选最接近的 `bedroom.md` / `bathroom.md` / `livingroom.md`（主卧/次卧/卧室 → bedroom，主卫/客卫/卫生间 → bathroom，客厅/起居/客餐厅 → livingroom；拿不准读最接近那个）。

| stage | Read 入 | 为什么这样切 |
|-------|--------|------------|
| **感知**（读空间 / 定调） | `references/spatial_design.md` + `references/design_evaluation.md` | 用品质维度作空间阅读判据；此阶段**房型中立、不放家具** → 不读放置法则 / 房型范式 / 模块库 |
| **规划推演**（分区 + 顺序） | `references/spatial_design.md` + `references/furniture_placement.md` + `references/{roomType}.md` | 组织空间 + 通用放置法则 + 房型选墙范式 |
| **多方案**（差异化生成） | `references/furniture_placement.md` + `references/{roomType}.md` + `references/design_evaluation.md` | 主家具清单/法则 + 合格底线判据 |
| **落地**（施工 + 自评 + 自优化） | `references/spatial_design.md` + `references/furniture_placement.md` + `references/{roomType}.md` + `references/design_evaluation.md` + **`projectMount/modules/module_library.json`** | 落地需全量：法则 + 房型 + 物本体 + 识图维度 |

> `module_library.json` 例外：它是家具物本体、被 validators/Web 等非 AI 方共用，**留在项目 `projectMount/modules/`**，不在本 Skill 目录；落地阶段从项目路径读它。

## 纪律

- **只读、不加工**：把原文留给调用方的 agent 使用，不在本 Skill 内总结/改写/推理。
- **渐进累积**：主控为脑下，一次设计会顺序经历多个阶段（感知→规划→多方案）——**前一阶段已读过的文件不必重读**，按阶段增量补读即可。这正是分阶段加载的意义：每阶段上下文只装该阶段够用的判据，不一次塞满。
- **无类型化入参**：旧 `L1/L2/L3` 参数、以及更早的 `stage`/`roomType` 形参均已去除——本 Skill 是自助索引（你按当前阶段/房型自取），不做参数校验（caller 传 `主卧`/`master-bedroom` 等与枚举不匹配反而失效，去参即根除）。
