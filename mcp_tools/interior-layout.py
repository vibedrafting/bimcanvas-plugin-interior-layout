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
