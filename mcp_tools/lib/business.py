"""interior-layout plugin 业务逻辑(纯函数,无 ctx / HTTP 依赖)。

> **退役说明(指针模型 + workflow 重构)**:原本本模块承载 `save/load_semantic_plan`、
> `save/load_reference_analysis` 四个工具的全部 domain 业务(tag 白名单 / canonical-only /
> planType 启发式 / effectiveTag 优先级 / merge view / reference tag 算法 / LegacyEmbedded 兼容)。
> 这四个工具已随指针模型退役——设计意图改落 `DESIGN.md`(普通 Read/Write/Edit),不再用
> semantic_plan / reference_analysis 的 JSON 合同。相关业务函数已整体删除。
>
> 当前仅保留 `get_zone_boundaries` 工具所需的边界段格式化(把 Server 返回的 ZoneBoundaryData
> 渲染成 AI 友好的按墙面分组文本)。纯函数,无 ctx / HTTP 依赖。
"""

from __future__ import annotations

import json
import re
from typing import Any


def _segment_direction_label(dx: float, dy: float) -> str:
    """根据方向向量返回方位标签:东/南/西/北/斜边"""
    if abs(dx) < 1e-3 and abs(dy) < 1e-3:
        return "斜边"
    if abs(dx) < 1e-3:
        return "东墙" if dy > 0 else "西墙"
    if abs(dy) < 1e-3:
        return "南墙" if dx > 0 else "北墙"
    return "斜边"


def _segment_length(start: list, end: list) -> int:
    """计算段长度(毫米,取整)"""
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    return round((dx * dx + dy * dy) ** 0.5)


def format_zone_boundaries(data: list[dict[str, Any]]) -> str:
    """将 ZoneBoundaryData 列表格式化为按墙面分组的 AI 友好文本。"""
    if not data:
        return "没有找到 zone 边界数据"

    all_zone_lines: list[str] = []
    for zone_data in data:
        zone_id = zone_data.get("zoneId", "?")
        segments = zone_data.get("segments", [])
        if not segments:
            all_zone_lines.append(f"=== {zone_id} 边界语义 (0 面墙) ===")
            all_zone_lines.append("")
            continue

        seg_infos = []
        for seg in segments:
            start = seg.get("start", [0, 0])
            end = seg.get("end", [0, 0])
            dx = end[0] - start[0]
            dy = end[1] - start[1]
            label = _segment_direction_label(dx, dy)
            length = _segment_length(start, end)
            seg_infos.append({
                "seg": seg,
                "label": label,
                "length": length,
                "start": start,
                "end": end,
            })

        walls: list[list[dict]] = []
        current_wall: list[dict] = [seg_infos[0]]
        for i in range(1, len(seg_infos)):
            if seg_infos[i]["label"] != seg_infos[i - 1]["label"]:
                walls.append(current_wall)
                current_wall = [seg_infos[i]]
            else:
                current_wall.append(seg_infos[i])
        walls.append(current_wall)

        label_counts: dict[str, int] = {}
        for wall in walls:
            lbl = wall[0]["label"]
            label_counts[lbl] = label_counts.get(lbl, 0) + 1

        all_zone_lines.append(f"=== {zone_id} 边界语义 ({len(walls)} 面墙) ===")
        all_zone_lines.append("")

        label_index: dict[str, int] = {}
        for wall in walls:
            lbl = wall[0]["label"]
            total_length = sum(s["length"] for s in wall)
            wall_length = sum(s["length"] for s in wall if s["seg"].get("type") == "wall")

            if label_counts[lbl] > 1:
                idx = label_index.get(lbl, 0) + 1
                label_index[lbl] = idx
                wall_name = f"{lbl}{chr(0x2080 + idx)}"
            else:
                wall_name = lbl

            all_wall = all(s["seg"].get("type") == "wall" for s in wall)
            all_passage = all(s["seg"].get("type") == "passage" for s in wall)
            if all_passage:
                adj = wall[0]["seg"].get("adjacent", "")
                summary = f"通道(→{adj})" if adj else "通道"
            elif all_wall:
                summary = "完整实墙"
            else:
                summary = f"实墙 {wall_length}mm"

            all_zone_lines.append(f"{wall_name} | 总长 {total_length}mm | {summary}")

            for s in wall:
                seg = s["seg"]
                seg_type = seg.get("type", "?")
                seg_id = seg.get("id")
                start = s["start"]
                end = s["end"]
                length = s["length"]
                if seg_type == "wall":
                    all_zone_lines.append(f"  wall {length}mm [{start[0]},{start[1]}]→[{end[0]},{end[1]}]")
                else:
                    id_part = f"({seg_id})" if seg_id else ""
                    adj = seg.get("adjacent")
                    adj_part = f"→{adj}" if adj and seg_type == "passage" else ""
                    all_zone_lines.append(
                        f"  {seg_type}{id_part}{adj_part} {length}mm [{start[0]},{start[1]}]→[{end[0]},{end[1]}]"
                    )

            all_zone_lines.append("")

    return "\n".join(all_zone_lines)


# ============================================================
# 变体目录骨架 / 指针翻转纯函数(register_variant / adopt_variant)
#
# 纪律:本模块全是纯函数(无 ctx / HTTP / 磁盘 I/O);文件落盘 / 目录 rename
# 等副作用一律由 interior-layout.py 的 tool handler 执行。
# ============================================================

# slug 字符集:[a-z0-9-] 且 1..30(对齐蓝图 §5.3 + 平台 EnsureSafeVariantId)
_SLUG_RE = re.compile(r"^[a-z0-9-]{1,30}$")


def is_safe_slug(slug: str) -> bool:
    """校验 slug 字符集:仅 [a-z0-9-]、长度 1..30。"""
    return bool(slug) and _SLUG_RE.match(slug) is not None


def build_variant_design_md(summary: str) -> str:
    """变体级 DESIGN.md 正文骨架(裁决 B:无 frontmatter)。

    依总纲领 §3.4——方案级 {zoneId}/{slug}/DESIGN.md 方向/锚点/战略/简报全在正文,
    不写任何 frontmatter;summary 单一来源是 modules.json 的 schemeMetadata.summary,
    此处仅把它作为正文一句话陈述,不另立 frontmatter 真理源。
    """
    intent = summary.strip() if summary else ""
    lines = ["# 方案设计说明", ""]
    if intent:
        lines.append(intent)
        lines.append("")
    return "\n".join(lines)


def build_modules_skeleton(summary: str) -> str:
    """叶子 modules.json 骨架:{schemeMetadata:{summary}, modules:[]}。

    键名 camelCase,对齐 C# ModulesWrapper 落盘形态。
    """
    return json.dumps(
        {"schemeMetadata": {"summary": summary}, "modules": []},
        ensure_ascii=False,
        indent=2,
    )


def build_zones_skeleton(leaf_ids: list[str]) -> str:
    """per-scheme zones.json 占位骨架(裁决 A1 + 扁平叶子数组)。

    顶层是扁平 JSON 数组 [{id,...},...](非 subZones 包裹),对齐 P1
    ModuleFileTopologyService.RegisterSchemeLeaves 的 ReadJson<List<Zone>> + foreach。
    rawBoundary:null 占位,几何由 Step4② 方案落地 Agent 后续 Edit 补齐
    (EnumerateSchemeLeaves 只读 id 枚举叶子、不需几何;几何到 validate 才用)。
    """
    zones = [
        {
            "id": leaf_id,
            "name": "",
            "type": "designable",
            "rawBoundary": None,
            "tags": [],
            "optionalTags": [],
        }
        for leaf_id in leaf_ids
    ]
    return json.dumps(zones, ensure_ascii=False, indent=2)


def _split_frontmatter_and_body(text: str) -> tuple[str | None, str]:
    """复刻 C# SchemeDesignDocService.SplitFrontmatterAndBody。

    返回 (frontmatter|None, body)。首行须为 `---` 且向下能找到闭合 `---`,
    否则视为无 frontmatter、body 为原始文本(不归一化,与 C# 早退分支对称)。
    """
    body = text or ""
    if not text:
        return None, body
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, body
    close = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close = i
            break
    if close < 0:
        return None, body
    frontmatter = "\n".join(lines[1:close])
    body = "\n".join(lines[close + 1:])
    return frontmatter, body


def write_adopted_frontmatter(existing_text: str, slug: str) -> str:
    """复刻 C# SchemeDesignDocService.WriteAdoptedSlug 的字节级输出(watch W1)。

    只定位替换 frontmatter 中的 `adopted:` 行(大小写不敏感),其余行(含空行/他字段)
    与正文原样保留;无 adopted 行则首行插入。空文件 → `---\\nadopted: {slug}\\n---\\n`。
    输出须与 C# 字节级一致,否则 Server ReadAdoptedSlug 的 YAML 反序列化读不到指针。
    """
    frontmatter, body = _split_frontmatter_and_body(existing_text or "")
    frontmatter_lines: list[str] = []
    adopted_written = False
    if frontmatter:
        for raw_line in frontmatter.split("\n"):
            line = raw_line.rstrip("\r")
            if line.lstrip().lower().startswith("adopted:"):
                frontmatter_lines.append(f"adopted: {slug}")
                adopted_written = True
            else:
                frontmatter_lines.append(line)
    if not adopted_written:
        frontmatter_lines.insert(0, f"adopted: {slug}")

    parts = ["---\n"]
    for line in frontmatter_lines:
        parts.append(line + "\n")
    parts.append("---\n")
    if body:
        parts.append("\n")
        parts.append(body.lstrip("\n"))
    return "".join(parts)
