"""interior-layout 布局校验器（被平台 PluginValidatorRuntime 子进程调用）。

包A · 2026-05-27 决议：validation 的"合理性判断"是 domain 代码 → 整套 SchemeValidator
（E001–E014）+ facing normalize 从主仓 C#（BIMCanvas.Core / BIMCanvas.Server）下沉到本脚本。
平台只提供几何原语（bimcanvas_plugin_sdk.geometry，shapely）、调用机制、稳定端点与回写。

P3 · §2.8 整合（2026-06-03）：拓扑解析**只留 C# 一份**（ModuleFileTopologyService）。
本验证器**删掉整个自建拓扑层**，降级为纯几何/语义检查器，改为消费 C# 经 stdin 注入的"已解析视图"：
  - request["resolvedLeaves"]  = [{leafZoneId, modulesPath(相对schemes,posix), designZoneId, isContainer}]
      → 告诉本脚本"哪个文件 = 哪个叶子 zoneId"，不再自建拓扑 / 不再 flatten / 不再读 DESIGN.md。
  - request["zoneGeometry"]     = {designZones:[...], exclusionZones:[...]}（仅 validate 注入，几何唯一来源）。
  - request["pathIssues"]       = [{code, zoneId, actualPath, expectedPath, moduleCount}]（E013/E014，C# 已判好）。

入口：`run(request) -> result`
  request = {mode, projectPath, zoneIds?, variantId?, resolvedLeaves, zoneGeometry?, pathIssues}
  result  = {report: {...冻结报文...}, writeback: [{path, wrapper}, ...]}
  - normalize → report 为 ModuleNormalizationReport 形态
  - validate  → report 为 SchemeValidationReport 形态（内部先 normalize 回写、再校验）
  writeback 由平台经 ModulesWriterService 落盘（脚本只决策不写文件）。

行为对齐主仓（指挥部已放宽硬线为"功能等价 + 用户手测"）：
- 几何（E001–E005）走 geometry.within_tolerant / overlap_info，镜像 CollisionDetector；
- 阈值 / 噪声地板 / 方位 / message 模板 / 诊断顺序 / AABB 预检门，逐条照搬 C#。
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from typing import Optional

from bimcanvas_plugin_sdk import geometry

# ── 常量（镜像 C#）──────────────────────────────────────────────
ZONE_EXCLUSION = 0   # ZoneType.Exclusion
ZONE_ROOM = 1        # ZoneType.Room
ZONE_DESIGNABLE = 2  # ZoneType.Designable

ERROR_THRESHOLD_MM = 10.0      # SchemeValidator.ErrorThresholdMm（穿透深度 > 此值为 error）
BOUNDS_TOL_MM = 0.001          # ValidationController.BoundsCoordinateToleranceMm
WITHIN_TOLERANCE_MM = 10.0     # CollisionDetector.IsWithinTolerant 默认容差

# DiagnosticCodes（与 C# BIMCanvas.Core.Validation.DiagnosticCodes 逐字一致）
E_OUT_OF_BOUNDS = "E001_OUT_OF_BOUNDS"
E_WALL_OVERLAP = "E002_WALL_OVERLAP"
E_COLUMN_OVERLAP = "E003_COLUMN_OVERLAP"
E_EXCLUSION_OVERLAP = "E004_EXCLUSION_OVERLAP"
E_MODULE_OVERLAP = "E005_MODULE_OVERLAP"
E_MISSING_BOUNDS = "E006_MISSING_BOUNDS"
E_INVALID_FACING_SEMANTIC = "E007_INVALID_FACING_SEMANTIC"
E_MISSING_FACING_VALUE = "E008_MISSING_FACING_VALUE"
E_INVALID_FACING_VALUE = "E009_INVALID_FACING_VALUE"
E_INVALID_MODULE_ID = "E011_INVALID_MODULE_ID"
E_INVALID_BOUNDS = "E012_INVALID_BOUNDS"
E_INVALID_MODULE_FILE_PATH = "E013_INVALID_MODULE_FILE_PATH"
E_DUPLICATE_ZONE_MODULE_FILES = "E014_DUPLICATE_ZONE_MODULE_FILES"
E_REGION_UNREACHABLE = "E015_REGION_UNREACHABLE"  # 连通性硬闸：门/子区被家具封死=不可达

REACH_MIN_PASSAGE_MM = 600.0       # 可达底线（次通道/到子区·次门）；主通道 900 属 Layer2 软、不入硬闸
REACH_AREA_FLOOR_MM2 = 100000.0    # 连通块显著性地板（0.1m²），滤 buffer 碎片/噪声

# ZoneType 兼容：注入数据走整数（C# 业务 enum 整数序列化）；旧 on-disk 数据可能用 camelCase 字符串。
# 两种表示都兼容，避免 zoneGeometry / 历史文件混用时静默漏判。
_ZONE_TYPE_ALIASES = {
    "exclusion": ZONE_EXCLUSION,
    "room": ZONE_ROOM,
    "designable": ZONE_DESIGNABLE,
}


def _zone_type(z: dict):
    """规整 zone.type → 整数(0/1/2)或 None。兼容整数与 camelCase 字符串两种序列化。"""
    t = z.get("type")
    if isinstance(t, str):
        return _ZONE_TYPE_ALIASES.get(t.strip().lower())
    if isinstance(t, bool):
        return None
    if isinstance(t, int):
        return t if t in (ZONE_EXCLUSION, ZONE_ROOM, ZONE_DESIGNABLE) else None
    return None


# ── 入口 ────────────────────────────────────────────────────────
def run(request: dict) -> dict:
    mode = request.get("mode")
    project_path = request.get("projectPath")
    zone_ids = request.get("zoneIds") or None
    variant_id = request.get("variantId") or None

    if not project_path:
        raise ValueError("缺少 projectPath")
    if variant_id and not zone_ids:
        raise ValueError("variantId 非空时必须显式指定 zoneIds，不允许全分区扫描变体")

    target_raw = set(zone_ids) if zone_ids else None

    if mode == "normalize":
        return _run_normalize(request, project_path)
    if mode == "validate":
        return _run_validate(request, project_path, target_raw)
    raise ValueError(f"未知 mode: {mode}")


# ── normalize（镜像 ModuleNormalizationService.NormalizeModules）──
def _run_normalize(request: dict, project_path: str) -> dict:
    t0 = time.perf_counter()
    diagnostics: list[dict] = []
    normalized_count = 0
    total_modules = 0
    writeback: list[dict] = []

    # resolvedLeaves 已由 C# 按 zoneIds/variantId 解析好（含变体）；本脚本只读、归一、回写。
    for abs_path, zone_id in _iter_resolved_files(project_path, request.get("resolvedLeaves")):
        wrapper = _read_modules_wrapper(abs_path)  # 仅认 wrapper，裸数组抛错
        if wrapper is None:
            continue
        modules = wrapper["modules"]
        diags, n = _normalize_facings(modules)
        diagnostics.extend(diags)
        normalized_count += n
        total_modules += len(modules)
        writeback.append(_writeback_entry(project_path, abs_path, wrapper))

    elapsed = int((time.perf_counter() - t0) * 1000)
    report = {
        "isValid": _count(diagnostics, "error") == 0,
        "totalModules": total_modules,
        "normalizedCount": normalized_count,
        "errorCount": _count(diagnostics, "error"),
        "warningCount": _count(diagnostics, "warning"),
        "diagnostics": diagnostics,
        "elapsedMs": elapsed,
    }
    return {"report": report, "writeback": writeback}


# ── validate（镜像 ValidationController.ValidateLayout 全链路）────
def _run_validate(request: dict, project_path: str, target_raw: Optional[set]) -> dict:
    t0 = time.perf_counter()

    # 几何唯一来源 = C# 注入的 zoneGeometry（叉口-1）；建筑/库仍本地读（与拓扑无关）。
    zg = request.get("zoneGeometry") or {}
    design_zones = zg.get("designZones") or []
    exclusion_zones = zg.get("exclusionZones") or []
    walls, columns = _load_architecture(project_path)
    openings = _load_openings(project_path)
    library_ids, circulation_exempt, overlap_exempt = _load_library(project_path)

    all_diags: list[dict] = []

    # 1) 先 normalize（写回 + 收集 E007/E008/E009），并保留各文件 modules 供后续校验
    writeback: list[dict] = []
    loaded: list[tuple[str, list[dict]]] = []  # (zoneId, modules)
    for abs_path, zone_id in _iter_resolved_files(project_path, request.get("resolvedLeaves")):
        wrapper = _read_modules_wrapper(abs_path)
        if wrapper is None:
            continue
        modules = wrapper["modules"]
        diags, _ = _normalize_facings(modules)
        all_diags.extend(diags)
        for m in modules:
            if m.get("zoneId") is None:
                m["zoneId"] = zone_id
        loaded.append((zone_id, modules))
        writeback.append(_writeback_entry(project_path, abs_path, wrapper))

    # 2) 结构层：路径问题（E013/E014）直接并入 C# 传来的 pathIssues + bounds 结构预检（E006/E012）
    all_diags.extend(_path_issue_diags(request.get("pathIssues")))

    valid_modules: list[dict] = []
    skipped = 0
    for _zone_id, modules in loaded:
        for m in modules:
            err = _bounds_structure_error(m)
            if err is not None:
                code, detail = err
                all_diags.append(_diag(
                    code, "error",
                    f"模块 {m.get('id', '')} ({_name(m)}) 的 bounds 结构非法：{detail}",
                    m.get("id", ""), _name_or_none(m)))
                skipped += 1
            else:
                valid_modules.append(m)

    # 3) facing 兜底（E008/E009）
    all_diags.extend(_validate_module_facings(valid_modules))
    # 4) moduleId 查库（E011，warning）
    all_diags.extend(_validate_module_ids(valid_modules, library_ids))
    # 5) 几何校验（E001–E005）
    all_diags.extend(_validate_scheme(valid_modules, design_zones, exclusion_zones,
                                      walls, columns, target_raw, overlap_exempt))
    # 6) 连通性硬闸（E015）：门/窗开口源点-汇点可达——锚最大 free 块，开口两两互达
    all_diags.extend(_validate_reachability(valid_modules, design_zones,
                                            openings, circulation_exempt, target_raw))

    total_modules = len(valid_modules) + skipped
    elapsed = int((time.perf_counter() - t0) * 1000)
    report = {
        "isValid": _count(all_diags, "error") == 0,
        "totalModules": total_modules,
        "errorCount": _count(all_diags, "error"),
        "warningCount": _count(all_diags, "warning"),
        "diagnostics": all_diags,
        "elapsedMs": elapsed,
    }
    return {"report": report, "writeback": writeback}


# ── 注入数据消费（P3 §2.8：取代自建拓扑层）─────────────────────
def _iter_resolved_files(project_path: str, resolved_leaves):
    """从 C# 注入的 resolvedLeaves 取 (abs_path, leafZoneId)。

    modulesPath 相对 schemes、posix（/ 分隔）；文件不存在则跳过（叶子无 modules.json 不算错）。
    resolvedLeaves 已在 C# 按 zoneIds/variantId 过滤，本脚本不再自行选文件。
    """
    schemes_path = os.path.join(project_path, "schemes")
    for rl in resolved_leaves or []:
        rel = rl.get("modulesPath")
        zone_id = rl.get("leafZoneId")
        if not rel or not zone_id:
            continue
        abs_path = os.path.join(schemes_path, *[s for s in rel.split("/") if s])
        if not os.path.exists(abs_path):
            continue
        yield abs_path, zone_id


def _path_issue_diags(path_issues) -> list[dict]:
    """E013/E014：C# 已解析好的结构化 pathIssues → 诊断。

    code 为全码（"E013_*"/"E014_*"，与本脚本常量逐字一致），直接透传；
    中文 message 在此本地生成（镜像旧 _path_issues 模板，消费 actualPath/expectedPath/moduleCount）。
    """
    out: list[dict] = []
    for pi in path_issues or []:
        code = pi.get("code")
        zone_id = pi.get("zoneId")
        actual = pi.get("actualPath")
        expected = pi.get("expectedPath")
        mc = pi.get("moduleCount")
        count_text = f"{mc} 个模块" if isinstance(mc, int) else "模块数未知"
        if code == E_INVALID_MODULE_FILE_PATH:
            out.append(_diag(
                E_INVALID_MODULE_FILE_PATH, "error",
                f"模块文件路径错误：{actual} 不应作为分区 {zone_id} 的 modules.json；"
                f"期望路径：{expected}；文件内 {count_text}。该文件中的模块已跳过布局验证",
                zone_id, None, actual, "moduleFile"))
        elif code == E_DUPLICATE_ZONE_MODULE_FILES:
            out.append(_diag(
                E_DUPLICATE_ZONE_MODULE_FILES, "error",
                f"分区 {zone_id} 存在多个 modules.json：{actual}；"
                f"规范路径：{expected}；请保留规范路径并人工合并/删除错误路径",
                zone_id, None, actual, "moduleFile"))
    return out


# ── facing 规范化（镜像 ModuleNormalizationService.NormalizeFacings）─
def _normalize_facings(modules: list[dict]) -> tuple[list[dict], int]:
    diags: list[dict] = []
    normalized = 0
    for m in modules:
        if "items" not in m or m.get("items") is None:
            m["items"] = []
        facing = m.get("facing") or {}
        value = facing.get("value")
        semantic = facing.get("semantic")

        sv = geometry.semantic_to_vector(semantic) if _has_semantic(semantic) else None
        if sv is not None:
            m["facing"] = {"value": [sv[0], sv[1]], "semantic": None}
            normalized += 1
            continue
        if _has_semantic(semantic):
            diags.append(_diag(E_INVALID_FACING_SEMANTIC, "error",
                               f"模块 {m.get('id', '')} ({_name(m)}) 的 facing.semantic '{semantic}' 无效",
                               m.get("id", ""), _name_or_none(m)))
            continue
        if not _value_present(value):
            diags.append(_diag(E_MISSING_FACING_VALUE, "error",
                               f"模块 {m.get('id', '')} ({_name(m)}) 缺少 facing.value",
                               m.get("id", ""), _name_or_none(m)))
            continue
        norm = _normalize_value(value)
        if norm is None:
            diags.append(_diag(E_INVALID_FACING_VALUE, "error",
                               f"模块 {m.get('id', '')} ({_name(m)}) 的 facing.value 不是有效单位向量",
                               m.get("id", ""), _name_or_none(m)))
            continue
        if not _same_vector(value, norm):
            normalized += 1
        m["facing"] = {"value": [norm[0], norm[1]], "semantic": None}
    return diags, normalized


# ── facing 兜底（镜像 ValidationController.ValidateModuleFacings）──
def _validate_module_facings(modules: list[dict]) -> list[dict]:
    diags: list[dict] = []
    for m in modules:
        facing = m.get("facing") or {}
        value = facing.get("value")
        if not _value_present(value):
            diags.append(_diag(E_MISSING_FACING_VALUE, "error",
                               f"模块 {m.get('id', '')} ({_name(m)}) 缺少 facing.value；facing.semantic 无效或两者都缺失",
                               m.get("id", ""), _name_or_none(m)))
            continue
        if _normalize_value(value) is None:
            diags.append(_diag(E_INVALID_FACING_VALUE, "error",
                               f"模块 {m.get('id', '')} ({_name(m)}) 的 facing.value 不是有效单位向量",
                               m.get("id", ""), _name_or_none(m)))
    return diags


# ── moduleId 查库（镜像 ValidationController.ValidateModuleIds）───
def _validate_module_ids(modules: list[dict], library_ids: Optional[set]) -> list[dict]:
    if library_ids is None:
        return []  # 库不存在 → 降级，不报错
    diags: list[dict] = []
    for m in modules:
        mid = m.get("moduleId")
        if not mid:
            continue  # 缺 moduleId 由 Load 质检处理，这里跳过
        if mid.lower() not in library_ids:
            diags.append(_diag(E_INVALID_MODULE_ID, "warning",
                               f"模块 {m.get('id', '')} ({_name(m)}) 的 moduleId '{mid}' 不在模块库中",
                               m.get("id", ""), _name_or_none(m)))
    return diags


# ── 几何校验（镜像 SchemeValidator.Validate）────────────────────
def _validate_scheme(modules: list[dict], design_zones: list[dict], exclusion_zones: list[dict],
                     walls: list[dict], columns: list[dict], target_raw: Optional[set],
                     overlap_exempt: set) -> list[dict]:
    diags: list[dict] = []

    # zoneCache：Room/Designable + (target None 或 id 命中)；boundary = computed ?? raw
    zone_cache = []
    for z in design_zones:
        if _zone_type(z) not in (ZONE_ROOM, ZONE_DESIGNABLE):
            continue
        if target_raw is not None and z.get("id") not in target_raw:
            continue
        b = z.get("computedBoundary") or z.get("rawBoundary")
        if b is not None:
            zone_cache.append((z, b))

    # exclusionCache：Exclusion；boundary = raw ?? computed
    excl_cache = []
    for z in exclusion_zones:
        if _zone_type(z) != ZONE_EXCLUSION:
            continue
        b = z.get("rawBoundary") or z.get("computedBoundary")
        if b is not None:
            excl_cache.append((z, b))

    wall_cache = [w for w in walls if w.get("polygon") is not None]
    col_cache = [c for c in columns if c.get("polygon") is not None]

    valid = [(m, m["bounds"]) for m in modules]  # bounds 已过结构预检

    for m, mb in valid:
        # Check 1: 在任一合法区域内（带 10mm 容差）；AABB 门控与 C# 一致
        in_any = False
        for _z, zb in zone_cache:
            if geometry.aabb_intersects(mb, zb) and geometry.within_tolerant(mb, zb, WITHIN_TOLERANCE_MM):
                in_any = True
                break
        if not in_any:
            diags.append(_diag(E_OUT_OF_BOUNDS, "error",
                               f"模块 {m.get('id', '')} ({_name(m)}) 不在任何设计区域内",
                               m.get("id", ""), _name_or_none(m)))

        # Check 2a/2b/2c: 墙 / 柱 / 禁区
        for w in wall_cache:
            _overlap_diag(diags, m, mb, w["polygon"], E_WALL_OVERLAP, w.get("id"), "wall",
                          f"模块 {m.get('id', '')} ({_name(m)}) 与墙体 {w.get('id')} 重叠")
        for c in col_cache:
            _overlap_diag(diags, m, mb, c["polygon"], E_COLUMN_OVERLAP, c.get("id"), "column",
                          f"模块 {m.get('id', '')} ({_name(m)}) 与柱子 {c.get('id')} 重叠")
        for z, zb in excl_cache:
            _overlap_diag(diags, m, mb, zb, E_EXCLUSION_OVERLAP, z.get("id"), "exclusion",
                          f"模块 {m.get('id', '')} ({_name(m)}) 与禁区 {z.get('id')} 重叠 ({z.get('reason', '')})")

    # Phase 3: 模块两两重叠（双向记录，方向互反）
    for i in range(len(valid)):
        ma, ba = valid[i]
        for j in range(i + 1, len(valid)):
            mb_, bb = valid[j]
            # overlay（地毯/椅子）合法叠放：椅塞桌下、毯压床下，豁免 E005；mounted（窗帘/淋浴屏）仍参与
            if _in_exempt(ma, overlap_exempt) or _in_exempt(mb_, overlap_exempt):
                continue
            if not geometry.aabb_intersects(ba, bb):
                continue
            info = geometry.overlap_info(ba, bb)
            if not info["has_overlap"]:
                continue
            severity = "error" if info["depth_mm"] > ERROR_THRESHOLD_MM else "warning"
            rev = _reverse_dir(info["direction"])
            diags.append(_diag(E_MODULE_OVERLAP, severity,
                               f"模块 {ma.get('id', '')} ({_name(ma)}) 与模块 {mb_.get('id', '')} ({_name(mb_)}) 重叠",
                               ma.get("id", ""), _name_or_none(ma),
                               mb_.get("id"), "module",
                               info["area_mm2"], info["depth_mm"], info["direction"]))
            diags.append(_diag(E_MODULE_OVERLAP, severity,
                               f"模块 {mb_.get('id', '')} ({_name(mb_)}) 与模块 {ma.get('id', '')} ({_name(ma)}) 重叠",
                               mb_.get("id", ""), _name_or_none(mb_),
                               ma.get("id"), "module",
                               info["area_mm2"], info["depth_mm"], rev))
    return diags


def _overlap_diag(diags: list[dict], m: dict, mb, obstacle, code: str,
                  conflict_id, conflict_type: str, message: str) -> None:
    if not geometry.aabb_intersects(mb, obstacle):
        return
    info = geometry.overlap_info(mb, obstacle)
    if not info["has_overlap"]:
        return
    severity = "error" if info["depth_mm"] > ERROR_THRESHOLD_MM else "warning"
    diags.append(_diag(code, severity, message,
                       m.get("id", ""), _name_or_none(m),
                       conflict_id, conflict_type,
                       info["area_mm2"], info["depth_mm"], info["direction"]))


# ── E015 连通性硬闸（北极星：填 validate 拓扑盲区，禁"床封死主卫"类灾难）──
def _validate_reachability(modules: list[dict], design_zones: list[dict],
                           openings: list[dict], circulation_exempt: set,
                           target_raw: Optional[set]) -> list[dict]:
    """门/窗开口源点-汇点可达校验（纯 shapely）。

    free = 设计区多边形 − union(solid 家具 footprint)；锚 = 腐蚀(-300)后最大的 base_n 连通块。
    无需识别主入口——一组开口两两互达 ⟺ 同属锚块。
      - 门（type=0）：strip 到锚 ≤300mm 否则报；<500=error / 500–600=warning。
      - 窗（type=1）：strip 邻接 free 不连锚块（被切进独立孤岛）→ error；被家具背靠盖住放过。
    mounted/overlay（窗帘/淋浴屏/地毯/椅子）不挖 free。家具可达汇点已移除（见文件尾历史）。
    shapely 不可用 / 几何异常 → 静默跳过（不阻断 validate；其余 E001–E014 仍承担）。
    """
    try:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
    except Exception as exc:  # noqa: BLE001 —— 连通性是增量硬闸，依赖缺失不得阻断既有校验
        print(f"[interior-layout] E015 跳过：shapely 不可用 ({exc})", file=sys.stderr, flush=True)
        return []

    def _poly(bounds):
        try:
            shell, holes = geometry._coerce_rings(bounds)
            if not shell or len(shell) < 3:
                return None
            p = Polygon(shell, holes or None)
            if not p.is_valid:
                p = p.buffer(0)
            return p if (not p.is_empty and p.area > 0) else None
        except Exception:  # noqa: BLE001
            return None

    def _pieces(geom) -> int:
        if geom is None or geom.is_empty:
            return 0
        geoms = getattr(geom, "geoms", None)
        items = list(geoms) if geoms is not None else [geom]
        return sum(1 for g in items if (not g.is_empty) and g.area > REACH_AREA_FLOOR_MM2)

    room_polys = []
    for z in design_zones:
        if _zone_type(z) not in (ZONE_ROOM, ZONE_DESIGNABLE):
            continue
        if target_raw is not None and z.get("id") not in target_raw:
            continue
        b = z.get("computedBoundary") or z.get("rawBoundary")
        p = _poly(b) if b is not None else None
        if p is not None:
            room_polys.append(p)
    if not room_polys:
        return []
    room = unary_union(room_polys)
    room_n = _pieces(room)
    if room_n == 0:
        return []

    # 通行障碍 = solid 家具 footprint。禁区(门扇开启区 ez_* 等)是「可走地面」——人就站那儿开门，
    # 不是通行屏障，不计入。实测：把 14 个禁区当障碍 → free 被错切 3 块、NE 翼缩小、腐蚀后 <地板被滤 → 漏判全封。
    module_polys = []  # (id, name, polygon) —— solid 家具：障碍 + 归因
    obstacle_polys = []
    for m in modules:
        if _in_exempt(m, circulation_exempt):
            continue  # mounted/overlay（窗帘/淋浴屏/地毯/椅子）：不挖 free、不当连通障碍
        p = _poly(m.get("bounds"))
        if p is not None:
            module_polys.append((m.get("id", ""), _name_or_none(m), p))
            obstacle_polys.append(p)

    try:
        free = room.difference(unary_union(obstacle_polys)) if obstacle_polys else room
    except Exception as exc:  # noqa: BLE001
        print(f"[interior-layout] E015 跳过：free 计算失败 ({exc})", file=sys.stderr, flush=True)
        return []

    def _piece_list(geom) -> list:
        if geom is None or geom.is_empty:
            return []
        geoms = getattr(geom, "geoms", None)
        items = list(geoms) if geoms is not None else [geom]
        sig = [g for g in items if (not g.is_empty) and g.area > REACH_AREA_FLOOR_MM2]
        return sorted(sig, key=lambda g: g.area, reverse=True)

    def _bbox_txt(g) -> str:
        minx, miny, maxx, maxy = g.bounds
        return f"X[{minx:.0f},{maxx:.0f}]·Y[{miny:.0f},{maxy:.0f}]（约 {g.area / 1e6:.1f}m²）"

    def _sealers_of(island, reachable) -> list:
        """卡在「孤岛」与「可达主区」之间(到两侧都近)的家具 = 封口元凶。
        用 max(到孤岛距离, 到主区距离) 衡量"夹在中间"——床卡喉时到两侧都近、值最小；
        岛内家具(床头柜/斗柜)到主区远、值大被排除。对腐蚀/膨胀造成的边距偏差鲁棒，
        补识图 v4 归错(怪床头柜)的精确定位。"""
        scored = []
        for mid, mname, mp in module_polys:
            try:
                d = max(mp.distance(island), mp.distance(reachable))
            except Exception:  # noqa: BLE001
                continue
            scored.append((d, mp.area, mid, mname))
        if not scored:
            return []
        scored.sort(key=lambda t: (t[0], -t[1]))  # 夹得最紧优先；并列大件优先
        thr = scored[0][0] + REACH_MIN_PASSAGE_MM  # 取与最佳同档(throat)的家具
        return [(mid, mname) for d, _a, mid, mname in scored if d <= thr]

    # 连通性判定建在「腐蚀后的 free」上 = 600mm 的人实际能站的地方。
    # 这同时治两个坑：① 床东缘恰好贴 NE 翼开口线时，原始 difference 把两区当"0 宽桥"仍连通、漏判封喉
    #   ——腐蚀经不起 0 宽连接、NE 翼被正确切出；② 贴墙 <600mm 细缝(没人走)在原始 free 里成假孤岛
    #   ——腐蚀直接抹掉、不再误报。这正是"≥600mm 可达"的本义。
    half = REACH_MIN_PASSAGE_MM / 2.0  # 300mm：600mm 通行的配置空间(腐蚀)半径
    try:
        reach = free.buffer(-half)
        room_reach = room.buffer(-half)
    except Exception as exc:  # noqa: BLE001
        print(f"[interior-layout] E015 跳过：腐蚀失败 ({exc})", file=sys.stderr, flush=True)
        return []

    reach_pieces = _piece_list(reach)
    base_n = max(1, len(_piece_list(room_reach)))  # 房间在 600mm 通行下的连通块数(建筑基线，吸收异形颈)
    if not reach_pieces:
        return []  # 房间在 600mm 下无任何立足点(异形/全被占)——连通性不强报，交由其它诊断

    # 锚点 = 最大的 base_n 块(无需识别主入口；一组开口两两互达 ⟺ 同属此主区)
    reachable = unary_union(reach_pieces[:base_n])
    reachable_dil = reachable.buffer(half)
    free_pieces = _piece_list(free)               # 未腐蚀连通块(供窗孤岛判 + 归因)
    main_free = unary_union([p for p in free_pieces if p.intersects(reachable)]) \
        if free_pieces else reachable

    # 严重度分级线：500mm 二次腐蚀主区，区分 "<500 真封死" 与 "500–600 紧口"
    seal_half = 500.0 / 2.0  # 250mm
    try:
        rp500 = _piece_list(free.buffer(-seal_half))
    except Exception:  # noqa: BLE001
        rp500 = []
    main500 = unary_union(rp500[:base_n]) if rp500 else None

    def _strict_ok(target) -> bool:
        # 轮廓邻接：存在 600mm 合规站位能够到 target ⟺ target 到主区 ≤300mm(+浮点容差)
        try:
            return target.distance(reachable) <= half + 1.0
        except Exception:  # noqa: BLE001
            return True  # 几何异常不强报

    def _grade(target) -> str:
        try:
            if main500 is not None and target.distance(main500) <= seal_half + 1.0:
                return "warning"  # 500–600mm 紧口
        except Exception:  # noqa: BLE001
            pass
        return "error"  # <500mm 真封死

    def _attribute(target):
        # 找 target 所在的非主 free 块 + 卡喉家具(复用 _sealers_of)
        try:
            tb = target.buffer(1.0)
            host = next((p for p in free_pieces
                         if p.intersects(tb) and not p.intersects(reachable)), None)
            sealers = _sealers_of(host if host is not None else target, reachable_dil)
        except Exception:  # noqa: BLE001
            sealers = []
        txt = "、".join(f"{sid}({snm or '?'})" for sid, snm in sealers) if sealers \
            else "（坐标复算贴该区边界的家具）"
        pid, pnm = sealers[0] if sealers else ("", None)
        return txt, pid, pnm

    diags = []

    # ── 门(严格)：必须能走到门口，否则相邻空间不可达 = 功能失效 ──
    for op in (openings or []):
        if op.get("type") != 0:
            continue
        strip = _opening_strip(op, room)
        if strip is None or _strict_ok(strip):
            continue
        sev = _grade(strip)
        txt, pid, pnm = _attribute(strip)
        tail = "（口宽 <500mm，真封死）" if sev == "error" else "（仅 500–600mm 紧口）"
        diags.append(_diag(
            E_REGION_UNREACHABLE, sev,
            f"门 {op.get('id', '')} 不可达 {_bbox_txt(strip)}：从主空间走不到门口{tail}"
            f"——卡喉家具：{txt}。把卡喉家具挪开 / 缩窄，给门口留 ≥600mm 通行。",
            pid, pnm))

    # ── 窗(宽松)：只要求不被切进独立孤岛；被家具背靠盖住属有意布置，放过 ──
    for op in (openings or []):
        if op.get("type") != 1:
            continue
        strip = _opening_strip(op, room)
        if strip is None:
            continue
        try:
            sf = strip.intersection(free)
            if sf.is_empty or sf.intersects(main_free):
                continue
        except Exception:  # noqa: BLE001
            continue
        txt, pid, pnm = _attribute(strip)
        diags.append(_diag(
            E_REGION_UNREACHABLE, "error",
            f"窗 {op.get('id', '')} 不可达 {_bbox_txt(strip)}：被家具围进走不进去的孤岛"
            f"——卡喉家具：{txt}。打通该区到主空间的 ≥600mm 通道。",
            pid, pnm))

    # ── 实体家具可达：已移除 ──
    # 旧严格判据 distance(footprint, reachable)≤300 要求每件家具四边都有 600mm 净空，
    # 误伤贴床小件(床头柜)与满铺隔断(淋浴屏)，逼 agent 规避 E015 删家具(金凤127 事故)。
    # 家具仍作障碍参与门/窗连通、仍参与 _sealers_of 归因；"家具必可达"诉求待用 facing 朝向面重做。

    return diags


# ── bounds 结构预检（镜像 GetBoundsStructureError）──────────────
def _bounds_structure_error(m: dict) -> Optional[tuple]:
    bounds = m.get("bounds")
    if bounds is None:
        return (E_MISSING_BOUNDS, "缺少 bounds 定义")
    shell, _holes = geometry._coerce_rings(bounds)
    verts = [(float(p[0]), float(p[1])) for p in shell]
    if len(verts) != 4:
        return (E_INVALID_BOUNDS, f"顶点数不符合模块规范（{len(verts)} 个，需要 4 个矩形顶点）")
    for x, y in verts:
        if math.isnan(x) or math.isnan(y) or math.isinf(x) or math.isinf(y):
            return (E_INVALID_BOUNDS, "包含非法坐标值（NaN 或 Infinity）")
    distinct = _count_distinct(verts)
    if distinct != 4:
        return (E_INVALID_BOUNDS, f"包含重复顶点，实际有效顶点数 {distinct} 个，需要 4 个互不重复的矩形顶点")
    if abs(_signed_area(verts)) <= BOUNDS_TOL_MM:
        return (E_INVALID_BOUNDS, "面积为 0，无法形成有效模块轮廓")
    return None


def _count_distinct(verts: list[tuple]) -> int:
    distinct: list[tuple] = []
    for v in verts:
        if not any(abs(v[0] - e[0]) <= BOUNDS_TOL_MM and abs(v[1] - e[1]) <= BOUNDS_TOL_MM
                   for e in distinct):
            distinct.append(v)
    return len(distinct)


def _signed_area(verts: list[tuple]) -> float:
    s = 0.0
    n = len(verts)
    for i in range(n):
        cur = verts[i]
        nxt = verts[(i + 1) % n]
        s += cur[0] * nxt[1] - nxt[0] * cur[1]
    return s / 2.0


# ── facing 值工具（镜像 Facing 结构语义）────────────────────────
def _has_semantic(semantic) -> bool:
    return isinstance(semantic, str) and semantic.strip() != ""


def _value_present(value) -> bool:
    return isinstance(value, (list, tuple)) and len(value) == 2 and value[0] is not None and value[1] is not None


def _normalize_value(value) -> Optional[tuple]:
    """TryGetNormalizedValue：有限且 length>=1e-10 → 归一向量，否则 None。"""
    if not _value_present(value):
        return None
    try:
        x = float(value[0]); y = float(value[1])
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isnan(y) or math.isinf(x) or math.isinf(y):
        return None
    length = math.hypot(x, y)
    if length < 1e-10:
        return None
    return (x / length, y / length)


def _same_vector(a, b) -> bool:
    try:
        return abs(float(a[0]) - b[0]) <= 1e-9 and abs(float(a[1]) - b[1]) <= 1e-9
    except (TypeError, ValueError, IndexError):
        return False


def _reverse_dir(d: Optional[str]) -> Optional[str]:
    return {"north": "south", "south": "north", "east": "west", "west": "east"}.get(d, d)


# ── 建筑 / 库 读取（镜像 ValidationController.Load*）────────────
def _load_architecture(project_path: str) -> tuple[list[dict], list[dict]]:
    arch = _read_json(os.path.join(project_path, "baseline", "architecture.json"))
    if not isinstance(arch, dict):
        return [], []
    return arch.get("walls") or [], arch.get("columns") or []


def _load_openings(project_path: str) -> list[dict]:
    """读 baseline/openings.json（门窗）；缺文件 → 空列表，静默降级。

    纯几何消费（line + facingDirection），不涉拓扑。type: 0=门 1=窗。
    """
    arr = _read_json(os.path.join(project_path, "baseline", "openings.json"))
    return arr if isinstance(arr, list) else []


def _load_library(project_path: str) -> tuple[Optional[set], set, set]:
    """读模块库 → (id 集, circulation_exempt, overlap_exempt)。

    physicality 枚举决定两类豁免（缺省 solid）：
      - circulation_exempt = mounted ∪ overlay → E015 不当通行障碍（窗帘/淋浴屏/地毯/椅子）；
      - overlap_exempt     = overlay           → E005 重叠豁免（地毯/椅子；mounted 仍参与，占墙面受保护）。
    id 集为 None 表示库缺失（降级，E011 不报）。
    """
    lib = _read_json(os.path.join(project_path, "modules", "module_library.json"))
    if not isinstance(lib, dict) or not isinstance(lib.get("modules"), list):
        return None, set(), set()
    ids, circ, ovl = set(), set(), set()
    for m in lib["modules"]:
        mid = m.get("id")
        if not mid:
            continue
        k = str(mid).lower()
        ids.add(k)
        ph = _physicality(m)
        if ph in ("mounted", "overlay"):
            circ.add(k)
        if ph == "overlay":
            ovl.add(k)
    return ids, circ, ovl


def _physicality(m: dict) -> str:
    """physicality 枚举 → 'solid'|'mounted'|'overlay'；缺省 / 非法值 = 'solid'。零兼容旧 physical。"""
    v = m.get("physicality")
    if isinstance(v, str) and v.strip().lower() in ("solid", "mounted", "overlay"):
        return v.strip().lower()
    return "solid"


def _in_exempt(m: dict, id_set: set) -> bool:
    """模块 moduleId 是否在给定豁免集。"""
    mid = m.get("moduleId")
    return bool(mid) and str(mid).lower() in id_set


def _opening_strip(op: dict, room):
    """开口室内侧 threshold 薄带：line 沿 facingDirection 向室内挤出 ~300mm，再 ∩ room。

    返回 shapely 几何或 None（shapely 缺失 / 数据不全 / 裁剪后为空）。
    """
    try:
        from shapely.geometry import Polygon
    except Exception:  # noqa: BLE001
        return None
    line = op.get("line")
    fd = op.get("facingDirection")
    if not line or len(line) != 2 or not fd or len(fd) != 2:
        return None
    try:
        x1, y1 = float(line[0][0]), float(line[0][1])
        x2, y2 = float(line[1][0]), float(line[1][1])
        fx, fy = float(fd[0]), float(fd[1])
    except (TypeError, ValueError, IndexError):
        return None
    n = math.hypot(fx, fy)
    if n < 1e-9:
        return None
    depth = REACH_MIN_PASSAGE_MM / 2.0  # 300mm
    fx, fy = fx / n * depth, fy / n * depth
    try:
        poly = Polygon([(x1, y1), (x2, y2), (x2 + fx, y2 + fy), (x1 + fx, y1 + fy)])
        if not poly.is_valid:
            poly = poly.buffer(0)
        s = poly.intersection(room)
        return s if (not s.is_empty and s.area > 0) else None
    except Exception:  # noqa: BLE001
        return None


# ── modules 文件读写 ────────────────────────────────────────────
def _read_modules_wrapper(abs_path: str) -> Optional[dict]:
    """仅认 wrapper {schemeMetadata, modules}；裸数组抛错（镜像 ModulesReaderService）。"""
    if not os.path.exists(abs_path):
        return None
    with open(abs_path, "r", encoding="utf-8-sig") as f:
        raw = f.read()
    if not raw.strip():
        return {"schemeMetadata": {"summary": ""}, "modules": []}
    token = json.loads(raw)
    if isinstance(token, list):
        raise ValueError(f"modules.json 是裸数组格式，已不再支持，请先运行迁移脚本：{abs_path}")
    if not isinstance(token, dict):
        raise ValueError(f"modules.json 既不是 wrapper 也不是数组：{abs_path}")
    token.setdefault("schemeMetadata", {"summary": ""})
    if token.get("schemeMetadata") is None:
        token["schemeMetadata"] = {"summary": ""}
    token.setdefault("modules", [])
    if token.get("modules") is None:
        token["modules"] = []
    return token


def _writeback_entry(project_path: str, abs_path: str, wrapper: dict) -> dict:
    """构造回写条目：path（相对 project，posix）+ 保留 schemeMetadata 的 wrapper。

    清理运行时字段 zoneId（镜像 PersistModules 写前置 null）；平台经 ModulesWriterService 落盘。
    """
    out_modules = []
    for m in wrapper["modules"]:
        mm = dict(m)
        mm["zoneId"] = None
        out_modules.append(mm)
    rel = os.path.relpath(abs_path, project_path).replace("\\", "/")
    return {
        "path": rel,
        "wrapper": {
            "schemeMetadata": wrapper.get("schemeMetadata") or {"summary": ""},
            "modules": out_modules,
        },
    }


# ── 通用工具 ────────────────────────────────────────────────────
def _read_json(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _name(m: dict) -> str:
    mn = m.get("moduleName")
    return mn if mn is not None else "未命名"


def _name_or_none(m: dict) -> Optional[str]:
    return m.get("moduleName")


def _count(diags: list[dict], severity: str) -> int:
    return sum(1 for d in diags if d.get("severity") == severity)


def _diag(code: str, severity: str, message: str, module_id: str,
          module_name: Optional[str], conflict_id: Optional[str] = None,
          conflict_type: Optional[str] = None, overlap_area: Optional[float] = None,
          penetration_depth: Optional[float] = None, penetration_dir: Optional[str] = None) -> dict:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "moduleId": module_id,
        "moduleName": module_name,
        "conflictId": conflict_id,
        "conflictType": conflict_type,
        "overlapAreaMm2": overlap_area,
        "penetrationDepthMm": penetration_depth,
        "penetrationDirection": penetration_dir,
    }
