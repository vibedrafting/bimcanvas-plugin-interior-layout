"""interior-layout plugin 业务逻辑(纯函数,无 ctx / HTTP 依赖)。

Server 业务下沉:逐条复刻自 BIMCanvas Server `Controllers/SemanticPlanController.cs`(~670 行),
把 indoor-layout domain 知识从平台基座撤回 plugin。

设计纪律:
- 纯函数:输入 dict / list / str,输出 dict / list / str,或 raise BusinessError。
- 不依赖 ctx / aiohttp / Server URL —— 工具体负责 IO,本模块只做业务判定。
- 字段名 PascalCase:与旧 SemanticPlanController 落盘格式保持一致(Entries / ZoneId / Tag /
  PlanType / Content / Timestamp / ReferenceAnalysisTag;LegacyEmbedded 在 doc 的 "referenceAnalysis" 键)。
  双轨期内旧 controller 仍可能读写同一文件,字段名漂移会破坏兼容。

文件格式契约:
- semantic_plan.json = {"Entries": [<entry>...], "referenceAnalysis"?: <legacy embedded>}
- reference_analysis.json = [<entry>...]  (顶层数组)
"""

from __future__ import annotations

import functools
import re
from datetime import datetime, timezone
from typing import Any

# ============================================================
# 业务常量(复刻 SemanticPlanController 静态字段)
# ============================================================

ALLOWED_SEMANTIC_PLAN_TAGS = (
    "spatial-skeleton",
    "strategic-plan",
    "multi-plan-overview",
    "construction-brief",
)

# spatial-skeleton / multi-plan-overview 全局只在 canonical 出现,不允许变体覆盖。
CANONICAL_ONLY_TAGS = ("spatial-skeleton", "multi-plan-overview")

PLAN_TYPE_DERIVED = "derived"
PLAN_TYPE_REFERENCE = "reference"

# canonical load:只认 construction-brief 作为生效图纸。
CANONICAL_EFFECTIVE_TAG = "construction-brief"

# merge view effectiveTag 优先级:construction-brief → strategic-plan → multi-plan-overview → spatial-skeleton。
MERGE_EFFECTIVE_PRIORITY = (
    "construction-brief",
    "strategic-plan",
    "multi-plan-overview",
    "spatial-skeleton",
)

_REFERENCE_ANALYSIS_TAG_NUMBER = re.compile(r"^v([1-9][0-9]*)$")


class BusinessError(Exception):
    """业务校验失败。工具体捕获后转 {is_error: True} 返回给 LLM。"""


# ============================================================
# 通用校验(复刻 IsDesignZoneId / NormalizePlanType / EnsureSafeVariantId 等)
# ============================================================

def is_design_zone_id(zone_id: str | None) -> bool:
    """semantic_plan / reference_analysis 只归属设计区,不归子分区(dz_ 前缀)。

    复刻 C#:`!string.IsNullOrWhiteSpace(zoneId) && !zoneId.StartsWith("dz_", OrdinalIgnoreCase)`。
    """
    if not zone_id or not zone_id.strip():
        return False
    return not zone_id.lower().startswith("dz_")


def normalize_plan_type(plan_type: str | None) -> str | None:
    """planType 白名单规范化:derived / reference(大小写不敏感),否则 None。

    复刻 C# NormalizePlanType:不 trim 比较(IsNullOrWhiteSpace 把纯空白当 None)。
    """
    if plan_type is None or not plan_type.strip():
        return None
    if plan_type.lower() == PLAN_TYPE_DERIVED:
        return PLAN_TYPE_DERIVED
    if plan_type.lower() == PLAN_TYPE_REFERENCE:
        return PLAN_TYPE_REFERENCE
    return None


def normalize_reference_analysis_tag(tag: str | None) -> str | None:
    """复刻 C# NormalizeReferenceAnalysisTag:空白 → None,否则 trim。"""
    if tag is None or not tag.strip():
        return None
    return tag.strip()


def ensure_safe_variant_id(variant_id: str) -> None:
    """复刻 ModuleFileTopologyService.EnsureSafeVariantId:仅允许字母/数字/下划线/连字符。"""
    if not variant_id or not variant_id.strip():
        raise BusinessError("variantId 不能为空")
    for ch in variant_id:
        if not (ch.isalnum() or ch in "-_"):
            raise BusinessError(
                f"variantId 包含非法字符 '{ch}',仅允许字母/数字/下划线/连字符"
            )


def validate_save_semantic_plan(zone_id: str, tag: str, plan_type: str | None,
                                variant_id: str | None) -> str:
    """save_semantic_plan 的全部前置校验,返回规范化后的 planType;失败 raise BusinessError。

    复刻 SemanticPlanController.SaveSemanticPlan 校验链(顺序一致):
      1. IsDesignZoneId
      2. NormalizePlanType
      3. tag 白名单
      4. variantId charset(非空时)
      5. canonical-only tag 不允许写变体路径
    """
    if not is_design_zone_id(zone_id):
        raise BusinessError("semantic_plan 只归属于设计区,不归属于子分区。请传入父设计区 zoneId。")

    normalized_plan_type = normalize_plan_type(plan_type)
    if normalized_plan_type is None:
        raise BusinessError("planType 必须是 derived 或 reference")

    if not tag or not tag.strip() or tag not in ALLOWED_SEMANTIC_PLAN_TAGS:
        allowed = ", ".join(ALLOWED_SEMANTIC_PLAN_TAGS)
        raise BusinessError(f"非法 tag: {tag or '(空)'}。合法值:{allowed}")

    if variant_id:
        ensure_safe_variant_id(variant_id)

    if tag in CANONICAL_ONLY_TAGS and variant_id:
        raise BusinessError(
            f"tag={tag} 是 canonical 单 owner(spatial-skeleton / multi-plan-overview 全局只在 canonical 出现),"
            "不能写入变体路径。请省略 variantId。"
        )

    return normalized_plan_type


# ============================================================
# planType 解析(复刻 TryResolvePlanType)
# ============================================================

def try_resolve_plan_type(entries: list[dict[str, Any]]) -> tuple[bool, str | None]:
    """复刻 TryResolvePlanType:返回 (ok, planType)。

    1. 收集所有 entry 规范化 planType,去重:
       - 唯一 → 返回它
       - 多个 → (False, None)(ambiguous)
    2. 全空时启发式:
       - 含 construction-brief → derived
       - 最新 tag(Ordinal)== strategic-plan 且某 entry 内容含 "识别方案" → reference
       - 否则 (False, None)
    """
    normalized: list[str] = []
    for e in entries:
        nt = normalize_plan_type(e.get("PlanType"))
        if nt and nt not in normalized:
            normalized.append(nt)

    if len(normalized) == 1:
        return True, normalized[0]
    if len(normalized) > 1:
        return False, None

    has_construction_brief = any(e.get("Tag") == "construction-brief" for e in entries)
    if has_construction_brief:
        return True, PLAN_TYPE_DERIVED

    tags = sorted((e.get("Tag") or "") for e in entries)  # Ordinal
    latest_tag = tags[-1] if tags else None
    if latest_tag == "strategic-plan":
        has_reference_title = any(
            e.get("Content") and "识别方案" in e["Content"] for e in entries
        )
        if has_reference_title:
            return True, PLAN_TYPE_REFERENCE

    return False, None


# ============================================================
# entry 维护 + effectiveTag 解析
# ============================================================

def build_semantic_plan_entry(zone_id: str, tag: str, plan_type: str, content: str,
                              reference_analysis_tag: str | None) -> dict[str, Any]:
    """构造 semantic_plan entry(PascalCase,复刻 SemanticPlanEntry)。"""
    return {
        "ZoneId": zone_id,
        "Tag": tag,
        "PlanType": plan_type,
        "Content": content,
        "Timestamp": utc_now_iso(),
        "ReferenceAnalysisTag": normalize_reference_analysis_tag(reference_analysis_tag),
    }


def upsert_entry(entries: list[dict[str, Any]], new_entry: dict[str, Any]) -> list[dict[str, Any]]:
    """同 Tag 替换 + append + 按 Tag(Ordinal)排序。复刻 RemoveAll + Add + Sort。"""
    tag = new_entry.get("Tag")
    kept = [e for e in entries if e.get("Tag") != tag]
    kept.append(new_entry)
    kept.sort(key=lambda e: e.get("Tag") or "")
    return kept


def resolve_canonical_target(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """canonical load:取最后一个 construction-brief(LastOrDefault)。"""
    target = None
    for e in entries:
        if e.get("Tag") == CANONICAL_EFFECTIVE_TAG:
            target = e
    return target


def resolve_effective_entry(entries: list[dict[str, Any]],
                            priority: tuple[str, ...]) -> dict[str, Any] | None:
    """按优先级解析 effective entry,每级取 LastOrDefault。复刻 merge view 的 effectiveTag 解析。"""
    for preferred in priority:
        target = None
        for e in entries:
            if e.get("Tag") == preferred:
                target = e
        if target is not None:
            return target
    return None


def merge_view(canonical_entries: list[dict[str, Any]],
               variant_entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """合并 = canonical.spatial-skeleton + 变体的非 spatial-skeleton entries,按 Tag(Ordinal)排序。

    复刻 LoadSemanticPlanMergeView 的硬约束合并。返回 (merged, canonical_skeleton)。
    """
    canonical_skeleton = None
    for e in canonical_entries:
        if e.get("Tag") == "spatial-skeleton":
            canonical_skeleton = e  # LastOrDefault

    merged: list[dict[str, Any]] = []
    if canonical_skeleton is not None:
        merged.append(canonical_skeleton)
    for e in variant_entries:
        # 防御性过滤:变体不应承载 spatial-skeleton,即便文件被手工写入也以 canonical 为准。
        if e.get("Tag") != "spatial-skeleton":
            merged.append(e)
    merged.sort(key=lambda e: e.get("Tag") or "")
    return merged, canonical_skeleton


# ============================================================
# reference_analysis tag 算法(复刻 GetNextReferenceAnalysisTag / CompareReferenceAnalysisTag)
# ============================================================

def _parse_ref_tag_number(tag: str | None) -> int | None:
    """复刻 TryParseReferenceAnalysisTagNumber:vN → N,否则 None。"""
    if not tag or not tag.strip():
        return None
    trimmed = tag.strip()
    if len(trimmed) < 2 or trimmed[0] != "v":
        return None
    try:
        return int(trimmed[1:])
    except ValueError:
        return None


def next_reference_tag(entries: list[dict[str, Any]]) -> str:
    """复刻 GetNextReferenceAnalysisTag:max(vN) + 1。"""
    max_number = 0
    for e in entries:
        n = _parse_ref_tag_number(e.get("Tag"))
        if n is not None and n > max_number:
            max_number = n
    return f"v{max_number + 1}"


def _compare_ref_tag(left: str | None, right: str | None) -> int:
    """复刻 CompareReferenceAnalysisTag:双方可解析数字 → 数字比;否则 OrdinalIgnoreCase 字符串比。"""
    ln = _parse_ref_tag_number(left)
    rn = _parse_ref_tag_number(right)
    if ln is not None and rn is not None:
        return (ln > rn) - (ln < rn)
    l = (left or "").lower()
    r = (right or "").lower()
    return (l > r) - (l < r)


def sort_reference_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 reference tag 比较器排序(就地排序后返回同一 list)。"""
    entries.sort(key=functools.cmp_to_key(lambda a, b: _compare_ref_tag(a.get("Tag"), b.get("Tag"))))
    return entries


def build_reference_entry(tag: str, source_image_id: str | None, content: str) -> dict[str, Any]:
    """构造 reference_analysis entry(PascalCase,复刻 ReferenceAnalysisEntry)。"""
    return {
        "Tag": tag,
        "SourceImageId": source_image_id or "",
        "Content": content,
        "Timestamp": utc_now_iso(),
    }


def resolve_reference_target(entries: list[dict[str, Any]], tag: str | None) -> dict[str, Any] | None:
    """复刻 LoadReferenceAnalysis 的 target 选择:tag 空 → 最新;非空 → LastOrDefault 匹配(OrdinalIgnoreCase)。"""
    if not entries:
        return None
    if not tag or not tag.strip():
        ordered = sorted(entries, key=functools.cmp_to_key(
            lambda a, b: _compare_ref_tag(a.get("Tag"), b.get("Tag"))))
        return ordered[-1]
    target = None
    needle = tag.strip().lower()
    for e in entries:
        if (e.get("Tag") or "").lower() == needle:
            target = e  # LastOrDefault
    return target


# ============================================================
# LegacyEmbedded 兼容(复刻 ReadReferenceAnalysisEntries 的 fallback + RemoveLegacy)
# ============================================================

def legacy_embedded_to_entries(semantic_doc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """从 semantic_plan canonical doc 的 LegacyEmbedded(referenceAnalysis 键)提取 v1 entry。

    复刻 ReadReferenceAnalysisEntries 末段:legacy null / content 空 → []。
    """
    if not semantic_doc:
        return []
    legacy = semantic_doc.get("referenceAnalysis")
    if not isinstance(legacy, dict):
        return []
    content = legacy.get("Content")
    if not content or not str(content).strip():
        return []
    timestamp = legacy.get("Timestamp")
    return [{
        "Tag": "v1",
        "SourceImageId": legacy.get("SourceImageId") or "",
        "Content": content,
        "Timestamp": timestamp if (timestamp and str(timestamp).strip()) else utc_now_iso(),
    }]


def strip_legacy_embedded(semantic_doc: dict[str, Any]) -> bool:
    """清 canonical doc 的 LegacyEmbedded 字段。返回是否发生了改动(需写回)。

    复刻 RemoveLegacyEmbeddedReferenceAnalysisAsync:仅 canonical 文件需要清理。
    """
    if "referenceAnalysis" in semantic_doc and semantic_doc.get("referenceAnalysis") is not None:
        del semantic_doc["referenceAnalysis"]
        return True
    return False


# ============================================================
# 杂项
# ============================================================

def utc_now_iso() -> str:
    """近似 C# DateTime.UtcNow.ToString("o"):ISO 8601 + Z。

    注:C# "o" 为 100ns(7 位小数)精度,本实现为 6 位微秒;timestamp 仅作记录,
    业务排序用 Tag 而非时间戳,差异不影响行为。
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
