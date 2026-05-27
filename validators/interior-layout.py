"""interior-layout 布局校验器（被平台 PluginValidatorRuntime 子进程调用）。

包A · 2026-05-27 决议：validation 的"合理性判断"是 domain 代码 → 整套 SchemeValidator
（E001–E014）+ facing normalize 从主仓 C#（BIMCanvas.Core / BIMCanvas.Server）下沉到本脚本。
平台只提供几何原语（bimcanvas_plugin_sdk.geometry，shapely）、调用机制、稳定端点与回写。

入口：`run(request) -> result`
  request = {mode: "normalize"|"validate", projectPath, zoneIds?: [..], variantId?: str}
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
import time
from typing import Any, Optional

from bimcanvas_plugin_sdk import geometry

# ── 常量（镜像 C#）──────────────────────────────────────────────
ZONE_EXCLUSION = 0   # ZoneType.Exclusion
ZONE_ROOM = 1        # ZoneType.Room
ZONE_DESIGNABLE = 2  # ZoneType.Designable

ERROR_THRESHOLD_MM = 10.0      # SchemeValidator.ErrorThresholdMm（穿透深度 > 此值为 error）
BOUNDS_TOL_MM = 0.001          # ValidationController.BoundsCoordinateToleranceMm
WITHIN_TOLERANCE_MM = 10.0     # CollisionDetector.IsWithinTolerant 默认容差

# DiagnosticCodes
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
        return _run_normalize(project_path, target_raw, variant_id)
    if mode == "validate":
        return _run_validate(project_path, target_raw, variant_id)
    raise ValueError(f"未知 mode: {mode}")


# ── normalize（镜像 ModuleNormalizationService.NormalizeModules）──
def _run_normalize(project_path: str, target_raw: Optional[set], variant_id: Optional[str]) -> dict:
    t0 = time.perf_counter()
    schemes_path = os.path.join(project_path, "schemes")
    topo = _build_topology(schemes_path)
    files = _canonical_files(topo, schemes_path, target_raw, variant_id)

    diagnostics: list[dict] = []
    normalized_count = 0
    total_modules = 0
    writeback: list[dict] = []

    for abs_path, zone_id in files:
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
def _run_validate(project_path: str, target_raw: Optional[set], variant_id: Optional[str]) -> dict:
    t0 = time.perf_counter()
    schemes_path = os.path.join(project_path, "schemes")
    topo = _build_topology(schemes_path)

    walls, columns = _load_architecture(project_path)
    design_zones, exclusion_zones = _load_zone_data(project_path, schemes_path)
    library_ids = _load_library_ids(project_path)

    all_diags: list[dict] = []

    # 1) 先 normalize（写回 + 收集 E007/E008/E009），并保留各文件 modules 供后续校验
    files = _canonical_files(topo, schemes_path, target_raw, variant_id)
    writeback: list[dict] = []
    loaded: list[tuple[str, list[dict]]] = []  # (zoneId, modules)
    for abs_path, zone_id in files:
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

    # 2) 结构层：路径问题（E013/E014）+ bounds 结构预检（E006/E012，剔除非法 bounds 模块）
    all_diags.extend(_path_issues(topo, schemes_path, target_raw))

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
                                      walls, columns, target_raw))

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
                     walls: list[dict], columns: list[dict], target_raw: Optional[set]) -> list[dict]:
    diags: list[dict] = []

    # zoneCache：Room/Designable + (target None 或 id 命中)；boundary = computed ?? raw
    zone_cache = []
    for z in design_zones:
        if z.get("type") not in (ZONE_ROOM, ZONE_DESIGNABLE):
            continue
        if target_raw is not None and z.get("id") not in target_raw:
            continue
        b = z.get("computedBoundary") or z.get("rawBoundary")
        if b is not None:
            zone_cache.append((z, b))

    # exclusionCache：Exclusion；boundary = raw ?? computed
    excl_cache = []
    for z in exclusion_zones:
        if z.get("type") != ZONE_EXCLUSION:
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


# ── 区域 / 建筑 / 库 读取（镜像 ValidationController.Load*）──────
def _load_architecture(project_path: str) -> tuple[list[dict], list[dict]]:
    arch = _read_json(os.path.join(project_path, "baseline", "architecture.json"))
    if not isinstance(arch, dict):
        return [], []
    return arch.get("walls") or [], arch.get("columns") or []


def _load_zone_data(project_path: str, schemes_path: str) -> tuple[list[dict], list[dict]]:
    design: list[dict] = []
    room_zones = _read_json(os.path.join(project_path, "computed", "room_zones.json"))
    if isinstance(room_zones, list):
        design.extend(room_zones)
    scheme_zones = _read_json(os.path.join(schemes_path, "zones.json"))
    if isinstance(scheme_zones, list):
        design.extend(_flatten_leaves(scheme_zones))
    exclusions = _read_json(os.path.join(project_path, "computed", "exclusions.json"))
    excl = exclusions if isinstance(exclusions, list) else []
    return design, excl


def _flatten_leaves(zones: list[dict]) -> list[dict]:
    out: list[dict] = []
    for z in zones:
        subs = z.get("subZones")
        if subs:
            out.extend(_flatten_leaves(subs))
        else:
            out.append(z)
    return out


def _load_library_ids(project_path: str) -> Optional[set]:
    lib = _read_json(os.path.join(project_path, "modules", "module_library.json"))
    if not isinstance(lib, dict) or not isinstance(lib.get("modules"), list):
        return None
    return {str(m.get("id", "")).lower() for m in lib["modules"] if m.get("id")}


# ── modules 文件读写 ────────────────────────────────────────────
def _read_modules_wrapper(abs_path: str) -> Optional[dict]:
    """仅认 wrapper {schemeMetadata, modules}；裸数组抛错（镜像 ModulesReaderService）。"""
    if not os.path.exists(abs_path):
        return None
    with open(abs_path, "r", encoding="utf-8") as f:
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


# ── 拓扑（镜像 ModuleFileTopologyService）───────────────────────
def _build_topology(schemes_path: str) -> dict:
    """返回 {canonical: {leafZoneId: [segments]}, leaves_by_container: {cid:[..]},
    containers: set, design_zone_ids: set, has_topology: bool}。"""
    empty = {"canonical": {}, "leaves_by_container": {}, "containers": set(),
             "design_zone_ids": set(), "has_topology": False}
    zones = _read_json(os.path.join(schemes_path, "zones.json"))
    if not isinstance(zones, list) or len(zones) == 0:
        return empty

    by_id = {}
    for z in zones:
        zid = z.get("id")
        if zid and zid not in by_id:
            by_id[zid] = z

    referenced: set = set()

    def collect_ref(zs: list[dict]) -> None:
        for z in zs:
            for sub in (z.get("subZones") or []):
                if sub.get("id"):
                    referenced.add(sub["id"])
                if sub.get("subZones"):
                    collect_ref([sub])
    collect_ref(zones)

    canonical: dict[str, list] = {}
    leaves_by_container: dict[str, list] = {}
    containers: set = set()
    design_zone_ids: set = set()

    def register(zone_ref: dict, segments: list, stack: set) -> list:
        zid = zone_ref.get("id")
        if not zid:
            return []
        full = by_id.get(zid, zone_ref)
        if zid in stack:
            return []
        stack.add(zid)
        try:
            subs = zone_ref.get("subZones") or full.get("subZones") or []
            if subs:
                containers.add(zid)
                leaf_ids: list = []
                for sub in subs:
                    if not sub.get("id"):
                        continue
                    leaf_ids.extend(register(sub, segments + [sub["id"]], stack))
                leaves_by_container[zid] = leaf_ids
                return leaf_ids
            if zid not in canonical:
                canonical[zid] = list(segments)
            return [zid]
        finally:
            stack.discard(zid)

    for z in zones:
        zid = z.get("id")
        if not zid or zid in referenced:
            continue
        design_zone_ids.add(zid)
        register(z, [zid], set())

    canonical["_unzoned"] = ["_unzoned"]
    return {
        "canonical": canonical,
        "leaves_by_container": leaves_by_container,
        "containers": containers,
        "design_zone_ids": design_zone_ids,
        "has_topology": True,
    }


def _expand_targets(topo: dict, target_raw: Optional[set]) -> Optional[set]:
    if not target_raw:
        return None
    result = set(target_raw)
    for zid in target_raw:
        for leaf in topo["leaves_by_container"].get(zid, []):
            result.add(leaf)
    return result


def _canonical_path(schemes_path: str, segments: list) -> str:
    return os.path.join(schemes_path, *segments, "modules.json")


def _canonical_files(topo: dict, schemes_path: str, target_raw: Optional[set],
                     variant_id: Optional[str]) -> list[tuple[str, str]]:
    if not topo["has_topology"]:
        return _legacy_files(schemes_path)
    target = _expand_targets(topo, target_raw)
    entries = []
    for zid, segments in topo["canonical"].items():
        if target is not None and zid not in target:
            continue
        entries.append((zid, segments))
    out: list[tuple[str, str]] = []
    seen = set()
    for zid, segments in entries:
        if variant_id:
            path = _swap_to_variant(schemes_path, zid, segments, variant_id)
        else:
            path = _canonical_path(schemes_path, segments)
        if os.path.exists(path):
            key = os.path.normcase(os.path.abspath(path))
            if key not in seen:
                seen.add(key)
                out.append((path, zid))
    return out


def _swap_to_variant(schemes_path: str, zone_id: str, segments: list, variant_id: str) -> str:
    """镜像 ModuleFileTopology.SwapToVariant（新协议优先，旧 sibling 兜底）。"""
    design_zone_id = segments[0] if segments else zone_id
    is_top_level_leaf = (design_zone_id == zone_id)
    if is_top_level_leaf:
        new_path = os.path.join(schemes_path, design_zone_id, "variants", variant_id, "modules.json")
    else:
        new_path = os.path.join(schemes_path, design_zone_id, "variants", variant_id, zone_id, "modules.json")
    if os.path.exists(new_path):
        return new_path
    # legacy sibling：schemes/{segments}/modules-{variantId}.json
    canonical_dir = os.path.dirname(_canonical_path(schemes_path, segments))
    return os.path.join(canonical_dir, f"modules-{variant_id}.json")


def _legacy_files(schemes_path: str) -> list[tuple[str, str]]:
    """无 zones.json 拓扑时的回退（镜像 FindLegacyModuleFiles）。"""
    out: list[tuple[str, str]] = []
    if not os.path.isdir(schemes_path):
        return out
    for root, _dirs, names in os.walk(schemes_path):
        if "modules.json" not in names:
            continue
        dirname = os.path.basename(root)
        low = dirname.lower()
        if low.startswith("rz_") or low.startswith("dz_") or low == "_unzoned":
            out.append((os.path.join(root, "modules.json"), dirname))
    if out:
        return out
    legacy = os.path.join(schemes_path, "modules.json")
    if os.path.exists(legacy):
        out.append((legacy, "legacy"))
    return out


# ── 路径问题 E013/E014（镜像 ModuleFileTopology.GetPathIssues）──
def _path_issues(topo: dict, schemes_path: str, target_raw: Optional[set]) -> list[dict]:
    if not topo["has_topology"] or not os.path.isdir(schemes_path):
        return []
    target = _expand_targets(topo, target_raw)
    canonical = topo["canonical"]
    canonical_abs = {zid: os.path.normcase(os.path.abspath(_canonical_path(schemes_path, seg)))
                     for zid, seg in canonical.items()}

    records: list[tuple[str, str, str]] = []  # (zoneId, absPath, relPath)
    for root, _dirs, names in os.walk(schemes_path):
        if "modules.json" not in names:
            continue
        abs_path = os.path.join(root, "modules.json")
        if "/variants/" in abs_path.replace("\\", "/").lower():
            continue
        rel = os.path.relpath(abs_path, schemes_path).replace("\\", "/")
        zone_id = "legacy" if rel == "modules.json" else os.path.basename(os.path.dirname(abs_path))
        if target is not None and zone_id not in target:
            continue
        records.append((zone_id, abs_path, rel))

    issues: list[dict] = []
    for zone_id, abs_path, rel in records:
        canon = canonical_abs.get(zone_id)
        if canon is not None and os.path.normcase(os.path.abspath(abs_path)) == canon:
            continue  # canonical，合法
        issues.append(_diag(E_INVALID_MODULE_FILE_PATH, "error",
                            f"模块文件路径错误：{rel} 不应作为分区 {zone_id} 的 modules.json；"
                            f"期望路径：{_expected_path(topo, schemes_path, zone_id)}；"
                            f"文件内 {_count_modules_text(abs_path)}。该文件中的模块已跳过布局验证",
                            zone_id, None, rel, "moduleFile"))

    # 同一 zoneId 多文件 → E014
    by_zone: dict[str, list[str]] = {}
    for zone_id, _abs, rel in records:
        by_zone.setdefault(zone_id, []).append(rel)
    for zone_id, rels in by_zone.items():
        if len(rels) <= 1:
            continue
        issues.append(_diag(E_DUPLICATE_ZONE_MODULE_FILES, "error",
                            f"分区 {zone_id} 存在多个 modules.json：{', '.join(rels)}；"
                            f"规范路径：{_expected_path(topo, schemes_path, zone_id)}；"
                            f"请保留规范路径并人工合并/删除错误路径",
                            zone_id, None, ", ".join(rels), "moduleFile"))
    return issues


def _expected_path(topo: dict, schemes_path: str, zone_id: str) -> str:
    canonical = topo["canonical"]
    if zone_id in canonical:
        return os.path.relpath(_canonical_path(schemes_path, canonical[zone_id]), schemes_path).replace("\\", "/")
    if zone_id in topo["containers"]:
        leaves = [os.path.relpath(_canonical_path(schemes_path, canonical[lid]), schemes_path).replace("\\", "/")
                  for lid in topo["leaves_by_container"].get(zone_id, []) if lid in canonical]
        if leaves:
            return "容器分区不承载 modules.json；请写入叶子分区：" + ", ".join(leaves)
        return "容器分区不承载 modules.json"
    return "zones.json 中未定义此叶子分区"


def _count_modules_text(abs_path: str) -> str:
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            token = json.load(f)
        if isinstance(token, dict) and isinstance(token.get("modules"), list):
            return f"{len(token['modules'])} 个模块"
    except Exception:  # noqa: BLE001
        pass
    return "模块数未知"


# ── 通用工具 ────────────────────────────────────────────────────
def _read_json(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
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
