---
name: load-design-knowledge
description: |
  设计知识分级加载器。按 level(L1/L2/L3) + roomType(bedroom/bathroom/livingroom)
  把工程合规 / 设计品质 / 设计倾向三级 references 读进上下文。
  纯加载器：本 Skill 不含任何设计方法论，方法论本体在被读取的 references 文件内。
allowed-tools: Read
---

# 加载设计知识

本 Skill 只做一件事：**把指定层级的设计 references 文件读进上下文**。它不解释、不裁剪、不补充任何设计规则——规则本体在被读取的 `.md` / `.json` 文件里。

## 入参

调用时在 args 中给出：

- `level`：`L1` | `L2` | `L3`
- `roomType`：`bedroom` | `bathroom` | `livingroom`（决定读哪份房间策略文件）

## 加载清单（按 level 读取，路径相对当前项目目录）

所有文件用 `Read` 工具逐个读入。**高层级包含低层级的全部文件**。

### L1 — 工程合规

- `references/design_principles.md`
- `references/{roomType}.md` —— 即 `references/bedroom.md` / `references/bathroom.md` / `references/livingroom.md` 之一
- `references/optional-furniture-rules.md`
- `modules/module_library.json`

### L2 — 设计品质（= L1 全部 + 下列）

- `references/design_evaluation.md`

### L3 — 设计倾向（= L2 全部 + 下列）

- 地方 / 集团设计标准文件（当前为空）

> L3 当前无对应文件：若目录下不存在地方/集团标准文件，跳过、不报错，按 L2 结果继续。

## 完成

读完对应层级的全部文件即结束。**不要**在本 Skill 内对读到的内容做总结、改写或推理——把原文留给调用方的 agent 使用。
