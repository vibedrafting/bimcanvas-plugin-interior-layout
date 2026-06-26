export const meta = {
  name: 'interior-layout-whole-house',
  description: '场景③多区编排（主控为脑·M2）：接收主控全屋核产出的 designZones[]，按设计区 pipeline——外层扇出 zone-design-agent（各加载 single-zone-design SOP、静默、读项目级 schemes/DESIGN.md 全屋协调）完成设计阶段，内层 workflow() 嵌套复用 interior-layout-fanout 落地 N 方案；返回各区方案对比表交主控写盘、引导用户逐区采纳。本脚本不写设计盘、不做设计判断。',
  phases: [
    { title: '多区设计', detail: '各设计区并行：zone-design-agent 跑 感知→规划→多方案，返回 variants[]+四段' },
    { title: '多区落地', detail: '各区设计完即接 fanout（N 方案并行落地 + 独立 validate 闸门 + 对比表）' },
  ],
}

// ── 入口 args 契约（契约③）──────────────────────────────────────
// 【契约·钉死】designZones 由主控全屋核产出；fanoutScriptPath 由主控用「插件根 + /workflows/interior-layout-fanout.workflow.js」拼好直传（避免按名解析风险）。
// 健壮性归一：部分模型/代理把 args 序列化成 JSON 字符串而非对象（实测 deepseek 秒失败）。字符串则 JSON.parse + log 告警。
const a = (typeof args === 'string')
  ? (log('⚠ args 以字符串到达，已 JSON.parse 归一（producer 未按对象传参）'), JSON.parse(args))
  : (args || {})
const designZones = Array.isArray(a.designZones) ? a.designZones : []
if (!designZones.length) throw new Error('args.designZones 必填（非空，每项 {designZoneId, tags, zoneRequest}）')
const originalUserRequest = a.originalUserRequest || ''
const fanoutScriptPath = a.fanoutScriptPath
if (!fanoutScriptPath) throw new Error('args.fanoutScriptPath 必填（内层落地脚本 interior-layout-fanout 的绝对路径）')

// ── 结构化输出 schema（契约②，与 single-zone-design SOP 返回、zone-design-agent 一致）──
const ZONE_DESIGN_SCHEMA = {
  type: 'object', required: ['designZoneId', 'ok'],
  properties: {
    designZoneId: { type: 'string' },
    ok: { type: 'boolean' },                            // false = 认输/失败（reason 写明，可不给 variants）
    variants: { type: 'array', items: {                 // 落地集：= fanout 入参 variants 契约
      type: 'object', required: ['slug'],
      properties: {
        slug: { type: 'string' }, direction: { type: 'string' }, narrative: { type: 'string' },
        anchorSeed: { type: 'string' }, avoidance: { type: 'string' }, expectedWalls: { type: 'string' },
      } } },
    strategySec: { type: 'string' }, spaceSec: { type: 'string' },   // 四段上游材料，直传 fanout
    zoningSec: { type: 'string' }, seqSec: { type: 'string' },
    reason: { type: 'string' },
  },
}

// ── prompt builder（薄拼接：只塞 id / 诉求 / 静默标志，不含业务判断——业务在 SOP）──
function designPrompt(z) {
  return `设计区 designZoneId=${z.designZoneId}${z.tags ? `（tags=${Array.isArray(z.tags) ? z.tags.join(',') : z.tags}）` : ''}。\n` +
    `本区诉求：${z.zoneRequest || originalUserRequest}\n` +
    `执行模式=silent（静默）：禁 AskUserQuestion，所有策略点自动代决并标 [自动代决]。\n` +
    `按 zone-design-agent 职责完成本区设计阶段：加载 single-zone-design SOP → 先 Read 项目级 schemes/DESIGN.md 全屋协调约束并据此规划本区 → 感知→规划→多方案 → 写本区 schemes/${z.designZoneId}/DESIGN.md → 按 schema 返回 ok + variants[] + 四段。\n` +
    `不做落地、不调 register_variant/validate_layout、不翻 adopted；失败如实 ok:false + reason。`
}

log(`多区编排：${designZones.length} 个设计区——${designZones.map(z => z.designZoneId).join('、')}`)

// ═══════════════ 按设计区 pipeline（无 barrier：某区设计完即接落地，不等其它区）═══════════════
// stage1 设计阶段（zone-design-agent，静默）；stage2 落地（内层 workflow() 复用 fanout，零改）。
const results = await pipeline(
  designZones,
  // —— stage1：设计阶段 ——
  (z) => agent(designPrompt(z), {
    agentType: 'zone-design-agent', schema: ZONE_DESIGN_SCHEMA,
    label: `design:${z.designZoneId}`, phase: '多区设计',
  }),
  // —— stage2：落地（内层嵌套复用现有 fanout 脚本，零改）——
  (d, z) => {
    if (!d || !d.ok) {
      log(`${z.designZoneId} 设计失败/认输（如实保留，不落地）：${d?.reason || '无返回'}`)
      return { designZoneId: z.designZoneId, zoneFailed: true, reason: d?.reason || '设计阶段无返回' }
    }
    return workflow({ scriptPath: fanoutScriptPath }, {
      designZoneId: d.designZoneId, variants: d.variants,
      strategySec: d.strategySec, spaceSec: d.spaceSec, zoningSec: d.zoningSec, seqSec: d.seqSec,
    }).then(fr => ({ designZoneId: d.designZoneId, zoneFailed: false, fanout: fr }))
      .catch(e => {
        log(`${z.designZoneId} 落地 fanout 异常：${e?.message || e}`)
        return { designZoneId: z.designZoneId, zoneFailed: true, reason: `落地异常：${e?.message || e}` }
      })
  }
)

// ═══════════════ 汇总（交主控逐区写父 DESIGN.md 对比节 + 引导逐区采纳）═══════════════
phase('多区落地')
const zones = []
const failedZones = []
for (const r of results) {
  if (!r) continue                                       // pipeline 整链异常丢 null
  if (r.zoneFailed || !r.fanout || r.fanout.ok === false) {
    failedZones.push({ designZoneId: r.designZoneId, reason: r.reason || r.fanout?.reason || '落地失败' })
    continue
  }
  const fr = r.fanout
  zones.push({
    designZoneId: r.designZoneId,
    comparisonTableMd: fr.comparisonTableMd,             // 交主控写本区 schemes/{zoneId}/DESIGN.md「方案对比」节
    variants: fr.variants,                               // [{slug, layer1Pass}]
    landFailed: fr.failed,                               // 该区明确失败/认输的 slug
    duplicates: fr.duplicates,
  })
}

log(`多区完成：成功 ${zones.length} 区，失败 ${failedZones.length} 区`)
return {
  ok: zones.length > 0,
  zones,
  failedZones,
  note: '各成功区已产出方案并通过自评+独立 validate 闸门。请主控按 design-doc-upsert 把每区 comparisonTableMd 写入该区 schemes/{designZoneId}/DESIGN.md「方案对比」节，逐区引导用户在画布点「采纳」终选；failedZones 如实告知用户、可重跑。',
}
