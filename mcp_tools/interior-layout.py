"""interior-layout plugin MCP 工具入口。

当前仅 1 个 domain 工具,通过 `register(builder)` 范式注册:
- get_zone_boundaries (读取 zone 边界段语义)

**退役说明(指针模型 + workflow 重构)**:
原本本插件含 4 个工具——`save/load_semantic_plan`、`save/load_reference_analysis`——承载语义方案 /
参考分析的标签管理与合并视图。指针模型上线后,设计意图改落 `DESIGN.md`(普通 Read/Write/Edit),
不再用 semantic_plan / reference_analysis 的 JSON 合同,这 4 个工具及其 `lib/business.py` 业务逻辑
已整体删除。几何 / 碰撞 / 边界校验走平台 `mcp__canvas__validate_layout`(委派本插件 `validators/`)。

设计纪律(SDK register 范式):不读 `builder.context` 字段、不做 `isinstance` 断言;
一切副作用挪到 tool handler 内运行。domain 业务判定在 `lib/business.py`(纯函数,无 ctx / HTTP)。
"""

from __future__ import annotations

import importlib.util as _importlib_util
import json
import os
import shutil
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


# ============================================================
# 文件落盘 helper(副作用,handler 专用;纯逻辑在 lib/business.py)
# ============================================================

def _write_text(path: str, content: str) -> None:
    """UTF-8(无 BOM)写文本,newline="" 不做 \\n→\\r\\n 转换,保字节级一致。"""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


def register(builder: McpServerBuilder) -> None:
    """interior-layout plugin 注册入口。"""
    ctx = builder.context

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

    # ---------- register_variant ----------
    @builder.tool(
        "register_variant",
        "创建方案变体目录骨架:在 schemes/{designZoneId}/ 下建 [_]{slug}/ + DESIGN.md(正文骨架,"
        "含 summary 一句话) + 按 leafCount 建叶子 modules.json(0/1=单 modules.json;"
        ">1=建 zones.json 占位 + dz_1..n/modules.json)。返回 leafPaths。",
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": ["designZoneId", "slug", "leafCount"],
            "properties": {
                "designZoneId": {"type": "string", "description": "设计区节点 path(如 rz_3)"},
                "slug": {"type": "string", "description": "变体 slug,仅 [a-z0-9-]、长度 ≤30"},
                "visible": {
                    "type": "boolean",
                    "default": False,
                    "description": "false→_{slug} 隐藏候选(场景①默认);true→{slug} 可见",
                },
                "leafCount": {
                    "type": "integer",
                    "description": "0/1=不建叶子(单 modules.json);>1=建 zones.json 占位 + dz_1..n/modules.json",
                },
                "summary": {"type": "string", "default": ""},
                "overwrite": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    )
    async def register_variant(args: dict[str, Any]) -> dict[str, Any]:
        """建方案变体目录骨架(直接文件系统,根 = ctx.project_path)。"""
        project_path = getattr(ctx, "project_path", None)
        if not project_path:
            return _error("当前无加载项目(project_path 为空),无法创建变体目录")

        design_zone_id = args["designZoneId"]
        slug = args["slug"]
        visible = bool(args.get("visible", False))
        leaf_count = int(args["leafCount"])
        summary = args.get("summary", "") or ""
        overwrite = bool(args.get("overwrite", False))

        if not biz.is_safe_slug(slug):
            return _error(f"slug 非法 '{slug}':仅允许 [a-z0-9-]、长度 1..30")

        dz_root = os.path.join(project_path, "schemes", design_zone_id)
        if not os.path.isdir(dz_root):
            return _error(f"设计区不存在: {design_zone_id}")

        dir_name = slug if visible else f"_{slug}"
        variant_root = os.path.join(dz_root, dir_name)
        if os.path.exists(variant_root):
            if not overwrite:
                return _error(f"already-exists: {dir_name}")
            shutil.rmtree(variant_root)
        os.makedirs(variant_root, exist_ok=True)

        # 变体级 DESIGN.md:无 frontmatter,正文骨架(裁决 B)
        _write_text(
            os.path.join(variant_root, "DESIGN.md"),
            biz.build_variant_design_md(summary),
        )

        leaf_paths: dict[str, str] = {}
        if leaf_count > 1:
            leaf_ids = [f"dz_{i}" for i in range(1, leaf_count + 1)]
            # 几何待填占位 zones.json:扁平叶子数组(裁决 A1)
            _write_text(
                os.path.join(variant_root, "zones.json"),
                biz.build_zones_skeleton(leaf_ids),
            )
            for leaf_id in leaf_ids:
                leaf_dir = os.path.join(variant_root, leaf_id)
                os.makedirs(leaf_dir, exist_ok=True)
                mpath = os.path.join(leaf_dir, "modules.json")
                _write_text(mpath, biz.build_modules_skeleton(summary))
                leaf_paths[leaf_id] = mpath
        else:
            mpath = os.path.join(variant_root, "modules.json")
            _write_text(mpath, biz.build_modules_skeleton(summary))
            leaf_paths[design_zone_id] = mpath

        return _text(
            json.dumps(
                {
                    "slug": slug,
                    "dirName": dir_name,
                    "variantRoot": variant_root,
                    "leafPaths": leaf_paths,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    # ---------- reveal_variant ----------
    @builder.tool(
        "reveal_variant",
        "揭示候选可见:把隐藏候选 _{slug} 去前缀转正为 {slug}(rename),使其进入 Web 采纳轮播。"
        "与 adopt_variant 的区别:**只转正可见、不翻 adopted 指针**(终选仍归用户)。"
        "幂等:已可见则直接成功;两者皆不存在报错。落地成功收尾时由 placement 调用——"
        "认输/失败的候选不调用 → 留 _ 隐藏、不进轮播(fail-safe)。",
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": ["designZoneId", "slug"],
            "properties": {
                "designZoneId": {"type": "string", "description": "设计区节点 path(如 rz_3)"},
                "slug": {"type": "string", "description": "候选 slug,可带或不带 _ 前缀"},
            },
            "additionalProperties": False,
        },
    )
    async def reveal_variant(args: dict[str, Any]) -> dict[str, Any]:
        """把隐藏候选 _{slug} 转正为可见 {slug}(纯文件 rename,不翻 adopted 指针)。

        register_variant 同为纯文件操作(无 Server 调用),Web 在流程收尾 reload 时按目录名
        识别可见候选;本工具镜像其做法,只做 _ 前缀的 rename,故无需走 Server。
        """
        project_path = getattr(ctx, "project_path", None)
        if not project_path:
            return _error("当前无加载项目(project_path 为空),无法揭示候选")

        design_zone_id = args["designZoneId"]
        raw = args["slug"]
        slug = raw[1:] if raw.startswith("_") else raw
        if not biz.is_safe_slug(slug):
            return _error(f"slug 非法 '{raw}':去前缀后须 [a-z0-9-]、长度 1..30")

        dz_root = os.path.join(project_path, "schemes", design_zone_id)
        hidden = os.path.join(dz_root, f"_{slug}")
        visible = os.path.join(dz_root, slug)
        if os.path.isdir(visible):
            return _text(json.dumps(
                {"slug": slug, "dirName": slug, "revealed": False, "note": "已可见"},
                ensure_ascii=False))
        if not os.path.isdir(hidden):
            return _error(f"候选目录不存在: _{slug} / {slug}")
        os.rename(hidden, visible)
        return _text(json.dumps(
            {"slug": slug, "dirName": slug, "revealed": True}, ensure_ascii=False))

    # ---------- hide_variant ----------
    @builder.tool(
        "hide_variant",
        "隐藏可见方案:把可见候选 {slug} 加 _ 前缀改名为 _{slug}(rename),使其退出 Web 采纳轮播"
        "(保留数据、可被 reveal_variant 复原)。reveal_variant 的**反向**操作:只隐藏、不删数据、不翻 adopted 指针。"
        "幂等:已隐藏则直接成功;两者皆不存在报错。**禁隐藏已采纳的生效方案**(adopted 指向的 slug,隐藏会让指针悬空)。"
        "用途:用户/主控事后剔除不想要的可见候选(无需彻底删除)。",
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": ["designZoneId", "slug"],
            "properties": {
                "designZoneId": {"type": "string", "description": "设计区节点 path(如 rz_3)"},
                "slug": {"type": "string", "description": "候选 slug,可带或不带 _ 前缀"},
            },
            "additionalProperties": False,
        },
    )
    async def hide_variant(args: dict[str, Any]) -> dict[str, Any]:
        """把可见候选 {slug} 改为隐藏 _{slug}(纯文件 rename,不翻 adopted、不删数据)。reveal_variant 的反向。"""
        project_path = getattr(ctx, "project_path", None)
        if not project_path:
            return _error("当前无加载项目(project_path 为空),无法隐藏候选")

        design_zone_id = args["designZoneId"]
        raw = args["slug"]
        slug = raw[1:] if raw.startswith("_") else raw
        if not biz.is_safe_slug(slug):
            return _error(f"slug 非法 '{raw}':去前缀后须 [a-z0-9-]、长度 1..30")

        dz_root = os.path.join(project_path, "schemes", design_zone_id)
        visible = os.path.join(dz_root, slug)
        hidden = os.path.join(dz_root, f"_{slug}")
        if os.path.isdir(hidden) and not os.path.isdir(visible):
            return _text(json.dumps(
                {"slug": slug, "dirName": f"_{slug}", "hidden": False, "note": "已隐藏"},
                ensure_ascii=False))
        if not os.path.isdir(visible):
            return _error(f"可见候选目录不存在: {slug}")
        # 护栏:禁隐藏已采纳的生效方案(adopted 指向它则隐藏会让指针悬空)
        try:
            import re as _re
            with open(os.path.join(dz_root, "DESIGN.md"), encoding="utf-8") as _f:
                _m = _re.search(r"(?m)^adopted:\s*(\S+)\s*$", _f.read())
            if _m:
                _ad = _m.group(1).strip().strip('"').strip("'")
                _ad = _ad[1:] if _ad.startswith("_") else _ad
                if _ad == slug:
                    return _error(
                        f"不能隐藏已采纳的生效方案 '{slug}'(父 DESIGN.md adopted 指向它);"
                        f"请先在画布改采纳别的方案后再隐藏")
        except OSError:
            pass
        os.rename(visible, hidden)
        return _text(json.dumps(
            {"slug": slug, "dirName": f"_{slug}", "hidden": True}, ensure_ascii=False))

    # ---------- adopt_variant ----------
    @builder.tool(
        "adopt_variant",
        "采纳收口:胜者目录去 _ 前缀转正(rename,目标存在则报错不覆盖) + 父设计区 "
        "DESIGN.md frontmatter 写 adopted: {slug}。落选保持 _ 隐藏。",
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": ["designZoneId", "winnerSlug"],
            "properties": {
                "designZoneId": {"type": "string", "description": "设计区节点 path(如 rz_3)"},
                "winnerSlug": {"type": "string", "description": "胜者 slug,可带或不带 _ 前缀"},
            },
            "additionalProperties": False,
        },
    )
    async def adopt_variant(args: dict[str, Any]) -> dict[str, Any]:
        """采纳收口:转正(_ 去前缀)+ 翻父 DESIGN.md adopted 指针,统一由 C#
        POST /api/scheme/variant/adopt 唯一落盘——含 SignalR 广播(前端自动 reload)、
        空方案非空校验、R3 写 gate、designZone 并发锁。本 MCP 仅只读解析磁盘真实目录名后透传,
        不再自写盘(故无需 Python 字节级对齐 C# frontmatter)。"""
        project_path = getattr(ctx, "project_path", None)
        if not project_path:
            return _error("当前无加载项目(project_path 为空),无法采纳")

        design_zone_id = args["designZoneId"]
        winner = args["winnerSlug"]
        promoted = winner[1:] if winner.startswith("_") else winner
        if not biz.is_safe_slug(promoted):
            return _error(f"winnerSlug 非法 '{winner}':去前缀后须 [a-z0-9-]、长度 1..30")

        # 只读解析磁盘真实目录名:场景①候选默认隐藏 _{slug}。C# 端点按传入名定位目录、
        # 不会自动改试 _ 前缀(先按名找、后处理转正),故须传真实存在的名字;_ 去前缀转正 +
        # 翻指针仍由 C# 做,这里只 isdir 探测、不写盘。
        dz_root = os.path.join(project_path, "schemes", design_zone_id)
        if os.path.isdir(os.path.join(dz_root, f"_{promoted}")):
            variant_slug = f"_{promoted}"
        elif os.path.isdir(os.path.join(dz_root, promoted)):
            variant_slug = promoted
        else:
            return _error(f"方案目录不存在: {winner}")

        # 照抄 get_zone_boundaries 的 Server 调用范式(MCP 调 Server,Server 为唯一写盘真理源)。
        try:
            async with ctx.session.post(
                f"{ctx.server_url}/api/scheme/variant/adopt",
                json={"designZoneId": design_zone_id, "variantSlug": variant_slug},
            ) as resp:
                if resp.status != 200:
                    try:
                        error_data = await resp.json()
                        error_msg = (
                            error_data.get("error")
                            or error_data.get("message")
                            or f"HTTP {resp.status}"
                        )
                    except Exception:
                        error_msg = await resp.text()
                    return _error(f"采纳失败: {error_msg}")
                data = await resp.json()
                return _text(json.dumps(data, ensure_ascii=False, indent=2))
        except aiohttp.ClientError as e:
            return _error(f"无法连接 Server: {e}")
