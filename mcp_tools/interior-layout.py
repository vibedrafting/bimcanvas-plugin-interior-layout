"""interior-layout plugin MCP 工具入口。

4 个 interior-layout 专属工具,通过 `register(builder)` 范式注册:
- save_semantic_plan / load_semantic_plan (语义方案标签管理)
- save_reference_analysis / load_reference_analysis (参考分析快照管理)

**业务下沉(Server 业务下沉与契约重订)**:
indoor-layout domain 业务(tag 白名单 / canonical-only / planType 启发式 / effectiveTag 优先级 /
merge view / next reference tag / LegacyEmbedded 兼容)原嵌在 BIMCanvas Server 的
`SemanticPlanController.cs`(~670 行)。现已撤回 plugin:
- 业务判定在 `lib/business.py`(纯函数,无 ctx/HTTP)
- 工具体只做 IO:调 business 校验 + 调 Server **通用 artifact 端点**(scene-agnostic)
  (GET `/api/scheme/artifacts/{kind}?path=` 精确读 / POST `/api/scheme/artifacts/{kind}` 写)
- Server 不再持有任何 indoor-layout 业务,只做通用文件 IO + baseline/computed 只读 gate

数据落点(按物理 zone 组织):
- canonical:schemes/{zoneId}/semantic_plan.json | reference_analysis.json
- variant:schemes/{zoneId}/variants/{variantId}/semantic_plan.json
- path 子段由工具体拼装(zoneId 或 zoneId/variants/{variantId}),Server 按 path 落 schemes/{path}/。

文件格式与旧 SemanticPlanController 保持一致(PascalCase Entries/Tag/PlanType/...),
双轨期内两套实现读写同一文件不冲突。
"""

from __future__ import annotations

import importlib.util as _importlib_util
import json
from pathlib import Path as _Path
from typing import Any

import aiohttp

from bimcanvas_plugin_sdk import McpServerBuilder


def _load_business_module() -> Any:
    """按路径加载 lib/business.py(唯一模块名,避免与其他 plugin 的 lib 冲突)。

    plugin 入口本身由平台 importlib.spec_from_file_location 加载,无 package 上下文,
    故业务模块也用 importlib 按路径加载,不污染 sys.path。
    """
    biz_path = _Path(__file__).resolve().parent / "lib" / "business.py"
    spec = _importlib_util.spec_from_file_location("interior_layout_business", biz_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载业务模块: {biz_path}")
    module = _importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


biz = _load_business_module()


# ============================================================
# 返回值 helper
# ============================================================

def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def _error_struct(status: str, message: str, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"status": status, "message": message}
    data.update(extra)
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": data,
        "is_error": True,
    }


# ============================================================
# Server 通用 artifact 端点 IO helper
# ============================================================

async def _load_artifact(ctx: Any, scene_id: str, kind: str,
                         path: str) -> tuple[int, Any, str]:
    """GET 精确读单文件 schemes/{path}/{kind}.json(scene-agnostic)。

    返回 (status, parsed_json_or_none, raw_text)。连接失败 status=-1。
    scene_id 形参保留兼容调用方,回退后不进 URL(数据按物理 zone 组织)。
    """
    url = f"{ctx.server_url}/api/scheme/artifacts/{kind}"
    try:
        async with ctx.session.get(url, params={"path": path}) as resp:
            raw = await resp.text()
            if resp.status == 200:
                try:
                    return 200, json.loads(raw), raw
                except json.JSONDecodeError:
                    return 200, None, raw
            return resp.status, None, raw
    except aiohttp.ClientError as e:
        return -1, None, f"无法连接 Server: {e}"


async def _save_artifact(ctx: Any, scene_id: str, kind: str, path: str,
                         content: Any) -> tuple[bool, str | None]:
    """POST 写单文件 schemes/{path}/{kind}.json(scene-agnostic)。返回 (ok, error_text)。

    scene_id 形参保留兼容调用方,回退后不进 URL。
    """
    url = f"{ctx.server_url}/api/scheme/artifacts/{kind}"
    try:
        async with ctx.session.post(url, json={"path": path, "content": content}) as resp:
            if resp.status == 200:
                return True, None
            return False, await resp.text()
    except aiohttp.ClientError as e:
        return False, f"无法连接 Server: {e}"


async def _read_reference_entries(ctx: Any, scene_id: str, zone_id: str) -> list[dict[str, Any]]:
    """读 reference_analysis 历史 entries;空时 fallback 到 semantic_plan canonical 的 LegacyEmbedded。

    复刻 ReadReferenceAnalysisEntries。reference_analysis 无 variant 概念,path 永远是 canonical zoneId。
    """
    status, entries, _ = await _load_artifact(ctx, scene_id, "reference_analysis", zone_id)
    if status == 200 and isinstance(entries, list):
        return entries

    s2, doc, _ = await _load_artifact(ctx, scene_id, "semantic_plan", zone_id)
    if s2 == 200 and isinstance(doc, dict):
        return biz.legacy_embedded_to_entries(doc)
    return []


async def _strip_canonical_legacy_embedded(ctx: Any, scene_id: str, zone_id: str) -> None:
    """清 canonical semantic_plan 的 LegacyEmbedded 字段(复刻 RemoveLegacyEmbeddedReferenceAnalysisAsync)。"""
    status, doc, _ = await _load_artifact(ctx, scene_id, "semantic_plan", zone_id)
    if status == 200 and isinstance(doc, dict) and biz.strip_legacy_embedded(doc):
        await _save_artifact(ctx, scene_id, "semantic_plan", zone_id, doc)


def _semantic_subpath(zone_id: str, variant_id: str | None) -> str:
    """canonical → zoneId;variant → zoneId/variants/{variantId}。"""
    if variant_id:
        return f"{zone_id}/variants/{variant_id}"
    return zone_id


def register(builder: McpServerBuilder) -> None:
    """interior-layout plugin 注册入口。"""
    ctx = builder.context

    # ---------- save_semantic_plan ----------
    @builder.tool(
        "save_semantic_plan",
        "保存语义方案标签。在规划阶段的每个子阶段（2.1/2.2/2.3）完成后调用，提交当前标签的语义方案。"
        "可选 variantId 用于写入变体路径；spatial-skeleton / multi-plan-overview 是 canonical 全局单 owner，禁止与 variantId 同时传入。",
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "zoneId": {
                    "type": "string",
                    "description": "目标 Zone ID，如 'rz_3'",
                },
                "tag": {
                    "type": "string",
                    "enum": ["spatial-skeleton", "strategic-plan", "multi-plan-overview", "construction-brief"],
                    "description": "语义方案标签：spatial-skeleton=空间骨架, strategic-plan=战略层方案, multi-plan-overview=多方案概述, construction-brief=完整施工简报",
                },
                "planType": {
                    "type": "string",
                    "enum": ["derived"],
                    "description": "图纸类型：当前正式流程统一为 derived；旧的 reference 仅用于识别历史数据。",
                },
                "content": {
                    "type": "string",
                    "description": "语义方案文本内容（markdown 格式）",
                },
                "referenceAnalysisTag": {
                    "type": "string",
                    "description": "可选。若当前方案消费了定稿 reference_analysis，记录对应的标签（如 v3 / v4）。",
                },
                "variantId": {
                    "type": "string",
                    "description": "可选。非空时写变体路径 schemes/{zoneId}/variants/{variantId}/semantic_plan.json；为空时写 canonical。"
                                   "**spatial-skeleton / multi-plan-overview 禁止传 variantId**（这两个 tag 全局只在 canonical 出现）。"
                                   "Phase 1 暂无调用方需要传入；预留给后续 multi-plan / variant-design-agent。",
                },
            },
            "required": ["zoneId", "tag", "planType", "content"],
            "additionalProperties": False,
        },
    )
    async def save_semantic_plan(args: dict[str, Any]) -> dict[str, Any]:
        zone_id = args["zoneId"]
        tag = args["tag"]
        content = args["content"]
        reference_analysis_tag = args.get("referenceAnalysisTag")
        variant_id = args.get("variantId")

        scene_id = ctx.active_scene

        # 业务校验(tag 白名单 / planType / variantId charset / canonical-only)
        try:
            normalized_plan_type = biz.validate_save_semantic_plan(zone_id, tag, args["planType"], variant_id)
        except biz.BusinessError as e:
            return _error(str(e))

        sub_path = _semantic_subpath(zone_id, variant_id)

        # 读现有 doc
        status, doc, raw = await _load_artifact(ctx, scene_id, "semantic_plan", sub_path)
        if status not in (200, 404):
            return _error(f"读取现有语义方案失败: {raw}")
        if status == 404 or not isinstance(doc, dict):
            doc = {"Entries": []}

        entries = doc.get("Entries") or []

        # 仅 canonical 清理 legacy embedded(变体目录从无 legacy 数据)
        if not variant_id:
            biz.strip_legacy_embedded(doc)

        entry = biz.build_semantic_plan_entry(zone_id, tag, normalized_plan_type, content, reference_analysis_tag)
        doc["Entries"] = biz.upsert_entry(entries, entry)

        ok, err = await _save_artifact(ctx, scene_id, "semantic_plan", sub_path, doc)
        if not ok:
            return _error(f"保存失败: {err}")

        ref_tag = entry.get("ReferenceAnalysisTag")
        suffix = f"（reference={ref_tag}）" if ref_tag else ""
        return _text(f"语义方案 {normalized_plan_type} {tag} 已保存{suffix}。继续下一阶段。")

    # ---------- load_semantic_plan ----------
    @builder.tool(
        "load_semantic_plan",
        "加载当前设计区的生效语义方案。返回当前可施工图纸，而不是完整历史。"
        "传 variantId 时返回 merge view（canonical 的 spatial-skeleton + 变体的 strategic-plan/construction-brief entries）。",
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "zoneId": {
                    "type": "string",
                    "description": "目标 Zone ID，如 'rz_3'",
                },
                "variantId": {
                    "type": "string",
                    "description": "可选。非空时返回 merge view（canonical 的 spatial-skeleton + 变体的 strategic-plan/construction-brief entries）；effectiveTag 落在变体的合同上。"
                                   "Phase 1 暂无调用方需要传入；预留给后续 multi-plan / variant-design-agent。",
                },
            },
            "required": ["zoneId"],
            "additionalProperties": False,
        },
    )
    async def load_semantic_plan(args: dict[str, Any]) -> dict[str, Any]:
        zone_id = args["zoneId"]
        variant_id = args.get("variantId")

        scene_id = ctx.active_scene

        if not biz.is_design_zone_id(zone_id):
            return _error("semantic_plan 只归属于设计区，不归属于子分区。请传入父设计区 zoneId。")

        # variantId 非空 → merge view 分支
        if variant_id:
            try:
                biz.ensure_safe_variant_id(variant_id)
            except biz.BusinessError as e:
                return _error(str(e))
            return await _load_semantic_plan_merge_view(scene_id, zone_id, variant_id)

        # canonical 分支
        status, doc, raw = await _load_artifact(ctx, scene_id, "semantic_plan", zone_id)
        if status == 404 or not isinstance(doc, dict):
            return _error_struct("missing", f"未找到 {zone_id} 的语义方案", zoneId=zone_id)
        if status != 200:
            return _error(f"加载失败: {raw}")

        entries = doc.get("Entries") or []
        if not entries:
            return _error_struct("missing", f"{zone_id} 的语义方案为空", zoneId=zone_id)

        ok, plan_type = biz.try_resolve_plan_type(entries)
        if not ok:
            return _error_struct(
                "ambiguous_legacy",
                f"{zone_id} 的旧语义方案无法自动判定 planType，请重新规划或由主控 Agent 介入确认。",
                zoneId=zone_id,
            )

        target = biz.resolve_canonical_target(entries)
        if target is None:
            if plan_type == biz.PLAN_TYPE_REFERENCE:
                return _error_struct(
                    "legacy_reference_requires_replan",
                    f"{zone_id} 当前仍是旧版 reference 工作流（缺少可施工的 construction-brief 自包含合同）。请重新执行规划。",
                    zoneId=zone_id,
                )
            return _error_struct(
                "missing",
                f"未找到 {zone_id} 的生效图纸 construction-brief",
                zoneId=zone_id,
            )

        data = {
            "status": "ok",
            "zoneId": target.get("ZoneId"),
            "planType": plan_type,
            "effectiveTag": target.get("Tag"),
            "content": target.get("Content"),
            "timestamp": target.get("Timestamp"),
            "referenceAnalysisTag": target.get("ReferenceAnalysisTag"),
        }
        return _semantic_plan_ok_result(data)

    async def _load_semantic_plan_merge_view(scene_id: str, zone_id: str,
                                             variant_id: str) -> dict[str, Any]:
        """复刻 LoadSemanticPlanMergeView:canonical.spatial-skeleton + 变体 entries。"""
        variant_path = f"{zone_id}/variants/{variant_id}"
        status_v, variant_doc, raw_v = await _load_artifact(ctx, scene_id, "semantic_plan", variant_path)
        if status_v == 404 or not isinstance(variant_doc, dict):
            return _error_struct(
                "missing",
                f"未找到变体语义方案 schemes/{zone_id}/variants/{variant_id}/semantic_plan.json",
                zoneId=zone_id, variantId=variant_id,
            )
        if status_v != 200:
            return _error(f"加载变体语义方案失败: {raw_v}")

        status_c, canonical_doc, _ = await _load_artifact(ctx, scene_id, "semantic_plan", zone_id)
        canonical_entries = (canonical_doc.get("Entries") if (status_c == 200 and isinstance(canonical_doc, dict)) else None) or []
        variant_entries = variant_doc.get("Entries") or []

        merged, canonical_skeleton = biz.merge_view(canonical_entries, variant_entries)
        if not merged:
            return _error_struct(
                "missing",
                f"变体 {variant_id} 的语义方案为空（且 canonical 未提供 spatial-skeleton）",
                zoneId=zone_id, variantId=variant_id,
            )

        target = biz.resolve_effective_entry(merged, biz.MERGE_EFFECTIVE_PRIORITY)
        if target is None:
            return _error_struct(
                "missing",
                f"变体 {variant_id} 未提供任何已知 tag 的语义方案",
                zoneId=zone_id, variantId=variant_id,
            )

        ok, plan_type = biz.try_resolve_plan_type(merged)
        if not ok:
            return _error_struct(
                "ambiguous_legacy",
                f"{zone_id}/variants/{variant_id} 的语义方案 planType 不一致，无法解析。",
                zoneId=zone_id, variantId=variant_id,
            )

        warning = None if canonical_skeleton is not None else "canonical spatial-skeleton missing"
        data = {
            "status": "ok",
            "zoneId": zone_id,
            "variantId": variant_id,
            "planType": plan_type,
            "effectiveTag": target.get("Tag"),
            "content": target.get("Content"),
            "timestamp": target.get("Timestamp"),
            "referenceAnalysisTag": target.get("ReferenceAnalysisTag"),
            "entries": [
                {
                    "zoneId": e.get("ZoneId"),
                    "tag": e.get("Tag"),
                    "planType": e.get("PlanType"),
                    "content": e.get("Content"),
                    "timestamp": e.get("Timestamp"),
                    "referenceAnalysisTag": e.get("ReferenceAnalysisTag"),
                }
                for e in merged
            ],
            "warning": warning,
        }
        return _semantic_plan_ok_result(data)

    def _semantic_plan_ok_result(data: dict[str, Any]) -> dict[str, Any]:
        """复刻原 load_semantic_plan 工具的 LLM 输出文本 + structuredContent。"""
        text_parts = [
            f"status: {data['status']}",
            f"zoneId: {data['zoneId']}",
            f"planType: {data['planType']}",
            f"effectiveTag: {data['effectiveTag']}",
            f"timestamp: {data['timestamp']}",
        ]
        if data.get("referenceAnalysisTag"):
            text_parts.append(f"referenceAnalysisTag: {data['referenceAnalysisTag']}")
        text_parts.append(f"\n{data['content']}")
        return {
            "content": [{"type": "text", "text": "\n".join(text_parts)}],
            "structuredContent": data,
        }

    # ---------- load_reference_analysis ----------
    @builder.tool(
        "load_reference_analysis",
        "加载当前设计区的参考分析。默认返回最新标签；可选 tag 参数读取指定标签。",
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "zoneId": {
                    "type": "string",
                    "description": "目标 Zone ID，如 'rz_3'",
                },
                "tag": {
                    "type": "string",
                    "description": "可选。指定参考分析标签，如 'v1'；不传则返回最新标签。",
                },
            },
            "required": ["zoneId"],
            "additionalProperties": False,
        },
    )
    async def load_reference_analysis(args: dict[str, Any]) -> dict[str, Any]:
        zone_id = args["zoneId"]
        tag = args.get("tag")

        scene_id = ctx.active_scene

        if not biz.is_design_zone_id(zone_id):
            return _error("reference_analysis 只归属于设计区，不归属于子分区。请传入父设计区 zoneId。")

        entries = await _read_reference_entries(ctx, scene_id, zone_id)
        if not entries:
            return _error_struct("missing", f"未找到 {zone_id} 的参考分析", zoneId=zone_id)

        target = biz.resolve_reference_target(entries, tag)
        if target is None:
            return _error_struct(
                "missing",
                f"未找到 {zone_id} 的参考分析 {tag}",
                zoneId=zone_id, tag=tag,
            )

        data = {
            "status": "ok",
            "zoneId": zone_id,
            "tag": target.get("Tag"),
            "sourceImageId": target.get("SourceImageId", ""),
            "content": target.get("Content"),
            "timestamp": target.get("Timestamp"),
        }
        text_parts = [
            f"status: {data['status']}",
            f"zoneId: {data['zoneId']}",
            f"tag: {data['tag']}",
            f"sourceImageId: {data.get('sourceImageId', '')}",
            f"timestamp: {data['timestamp']}",
            "",
            data["content"] or "",
        ]
        return {
            "content": [{"type": "text", "text": "\n".join(text_parts)}],
            "structuredContent": data,
        }

    # ---------- save_reference_analysis ----------
    @builder.tool(
        "save_reference_analysis",
        "保存完整参考分析快照。在参考图分析各阶段完成后调用，提交当前标签的完整 Markdown 分析内容。",
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "zoneId": {
                    "type": "string",
                    "description": "目标 Zone ID，如 'rz_3'",
                },
                "sourceImageId": {
                    "type": "string",
                    "description": "参考图附件 ID（可选）",
                },
                "content": {
                    "type": "string",
                    "description": "参考分析内容（Markdown 格式），必须是当前阶段的完整、自包含快照",
                },
            },
            "required": ["zoneId", "content"],
            "additionalProperties": False,
        },
    )
    async def save_reference_analysis(args: dict[str, Any]) -> dict[str, Any]:
        zone_id = args["zoneId"]
        content = args["content"]
        source_image_id = args.get("sourceImageId", "")

        scene_id = ctx.active_scene

        if not biz.is_design_zone_id(zone_id):
            return _error("reference_analysis 只归属于设计区，不归属于子分区。请传入父设计区 zoneId。")

        # 读历史(含 legacy embedded fallback)→ next tag → append + sort
        entries = await _read_reference_entries(ctx, scene_id, zone_id)
        next_tag = biz.next_reference_tag(entries)
        entries.append(biz.build_reference_entry(next_tag, source_image_id, content))
        biz.sort_reference_entries(entries)

        ok, err = await _save_artifact(ctx, scene_id, "reference_analysis", zone_id, entries)
        if not ok:
            return _error(f"保存失败: {err}")

        # 写完后清 canonical semantic_plan 的 LegacyEmbedded(自愈型,中断下次再触发)
        await _strip_canonical_legacy_embedded(ctx, scene_id, zone_id)

        return {
            "content": [{"type": "text", "text": f"参考分析结果已保存为 {next_tag}。"}],
            "structuredContent": {"saved": True, "zoneId": zone_id, "tag": next_tag},
        }

    # ---------- get_zone_boundaries ----------
    @builder.tool(
        "get_zone_boundaries",
        "获取 Zone 边界语义数据:将 zone 的多边形边界拆分为 wall/passage/door/window 段,"
        "帮助理解每条边的物理含义。子分区场景下区分实墙和通道。",
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "zoneIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选。指定要查询的 Zone ID 列表(如 [\"dz_1\", \"dz_2\"])。不传则返回所有叶子 zone 的边界段数据。",
                }
            },
            "additionalProperties": False,
        },
    )
    async def get_zone_boundaries(args: dict[str, Any]) -> dict[str, Any]:
        """获取 Zone 边界段语义数据"""
        zone_ids = args.get("zoneIds")
        body: dict[str, Any] = {}
        if zone_ids:
            body["zoneIds"] = zone_ids
        body = body or None

        try:
            async with ctx.session.post(
                f"{ctx.server_url}/api/validation/zone-boundaries", json=body
            ) as resp:
                if resp.status == 400:
                    return _error("错误: 没有加载的项目")
                if resp.status != 200:
                    try:
                        error_data = await resp.json()
                        error_msg = error_data.get("message", f"HTTP {resp.status}")
                    except Exception:
                        error_msg = await resp.text()
                    return _error(f"获取边界数据失败: {error_msg}")

                data = await resp.json()
                return _text(biz.format_zone_boundaries(data))

        except aiohttp.ClientError as e:
            return _error(f"无法连接 Server: {e}")
