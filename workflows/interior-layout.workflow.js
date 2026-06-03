export const meta = {
  name: 'interior-layout-scene1',
  description: '场景①：设计引擎重做 —— 七步流（感知→规划推演→多方案→落地→评审→裁决→精修）',
  phases: [
    { title: '感知', detail: '战略分析 ∥ 空间理解，定调诉求与空间骨架' },
    { title: '规划推演', detail: '分区思维 ∥ 顺序思维，双轴出方案草稿' },
    { title: '多方案生成', detail: '消化双草稿，发散 N 个方向变体（含多样性护栏）' },
    { title: '多方案落地', detail: 'N 路并行落地：注册变体 + 施工简报 + 施工 + validate' },
    { title: '多维评审', detail: '每变体 5 单维 + 1 通用评审，与落地 pipeline 重叠' },
    { title: '裁决', detail: '聚合评审选最优，采纳胜者（翻指针 + 去 _ 前缀）' },
    { title: '精修', detail: '对采纳方案做固定 1 轮精修' },
  ],
}

// ── 入口 args 契约 ────────────────────────────────────────────────
const designZoneId = args?.designZoneId               // 必填，单段或多段 path，如 rz_3 或 rz_6/dz_1
if (!designZoneId) throw new Error('args.designZoneId 必填')
const userRequest  = args?.originalUserRequest || ''
const REFINE_ROUND = 1                                 // 固定 1 轮（锁死，不开 args）
// 评审维度：由 L0 路由层经 args 注入的不透明字符串列表（维度本体属知识层 design_evaluation）；
// 本脚本只迭代、不内嵌任何维度语义。缺省则只跑通用维（不臆造维度）。
const DIMS = Array.isArray(args?.dimensions) ? args.dimensions : []
const GENERAL = '__general__'

function clampN(raw){ const n = Math.max(1, raw || 3); return Math.min(n, 4) }   // 软上限 4，默认 3
let N = clampN(args?.n)

// ── 结构化输出 schema（4 个）─────────────────────────────────────
const OVERVIEW_SCHEMA = {  // Step3 返回
  type: 'object', required: ['variants', 'proposedN'],
  properties: {
    proposedN: { type: 'number' },                     // 驱动自适应收敛
    variants: { type: 'array', items: { type: 'object', required: ['slug', 'direction', 'anchorSeedType'],
      properties: {
        slug: { type: 'string' },
        direction: { type: 'string' },
        narrative: { type: 'string' },
        anchorSeed: { type: 'string' },
        avoidance: { type: 'string' },
        anchorSeedType: { type: 'string', enum: ['single-furniture', 'combination', 'spatial-strategy'] },
      } } },
    excluded: { type: 'array', items: { type: 'object',
      properties: { candidate: { type: 'string' }, reason: { type: 'string' } } } },
  },
}
const CRITIC_SCHEMA = {  // Step5 单维 + 通用共用，dimension 区分；Layer1.5 用 generalChecks
  type: 'object', required: ['dimension', 'score', 'layer1Fail', 'directionRespecting', 'findings'],
  properties: {
    dimension: { type: 'string' },                     // 维度之一 或 '__general__'
    score: { type: 'number' },                         // 0–100；Layer1 硬伤直接不及格
    layer1Fail: { type: 'boolean' },                   // 工程合规硬伤（validate 已过仍兜底）
    directionRespecting: { type: 'boolean' },          // 建议是否在既定方向内（防裁判把"换方向"当扣分）
    findings: { type: 'array', items: { type: 'string' } },     // 接地问题/亮点，带证据
    suggestions: { type: 'array', items: { type: 'string' } },  // 每条标 strategy级/placement级
    generalChecks: { type: 'object',                            // 仅 __general__ 填
      properties: { againstWall: { type: 'boolean' }, adjacentGap: { type: 'boolean' }, alignment: { type: 'boolean' } } },
  },
}
const JUDGE_SELECT_SCHEMA = {  // Step6 裁决
  type: 'object', required: ['winner', 'rankedSlugs', 'rationale'],
  properties: {
    winner: { type: 'string' },
    rankedSlugs: { type: 'array', items: { type: 'object', required: ['slug', 'score'],
      properties: { slug: { type: 'string' }, score: { type: 'number' }, oneLineReason: { type: 'string' } } } },
    rationale: { type: 'string' },                     // 判据来自知识层，不在 prompt 复述
  },
}
const JUDGE_REFINE_SCHEMA = {  // Step7 精修判决（optimization 返回）
  type: 'object', required: ['passed'],
  properties: {
    passed: { type: 'boolean' },
    rootCause: { type: 'string', enum: ['strategy', 'placement', 'none'] },  // 决定 fix 改哪层
    reviseInstruction: { type: 'string' },
    failedDimensions: { type: 'array', items: { type: 'string' } },
  },
}

// ── 路径 helper（内联）──────────────────────────────────────────
const parentDesign = `schemes/${designZoneId}/DESIGN.md`
const hiddenDesign = slug => `schemes/${designZoneId}/_${slug}/DESIGN.md`
const adoptedDesign = slug => `schemes/${designZoneId}/${slug}/DESIGN.md`

// ── 写盘 helper：把 markdown 节块串行交给 design-scribe（单写者，无并发同文件）──
async function writeSections(path, blocks, phaseName){
  const sections = blocks.filter(Boolean)
  if (!sections.length) return
  const packed = sections.map((b, i) => `<<<SECTION ${i}>>>\n${b}`).join('\n\n')
  await agent(
    `把下列 section 块写入文件。机械 upsert，不改内容、不加评语。\n\n` +
    `<file_path>${path}</file_path>\n<sections>\n${packed}\n</sections>`,
    { agentType: 'design-scribe', label: `scribe:${path}`, ...(phaseName ? { phase: phaseName } : {}) },
  )
}

// ── 结构化返回 → markdown 块（纯机械拼接 agent 产出的文本，无业务判断）──
function overviewBlock(ov){
  const lines = (ov?.variants || []).map(v =>
    `- **${v.slug}**（${v.anchorSeedType}）：${v.direction || ''}\n  - 叙事：${v.narrative || ''}\n  - 锚点：${v.anchorSeed || ''}\n  - 规避：${v.avoidance || ''}`)
  const exc = (ov?.excluded || []).map(e => `- 排除「${e.candidate}」：${e.reason || ''}`)
  return `## 多方案战略层概述\n\n${lines.join('\n')}` +
    (exc.length ? `\n\n### 自动排除\n${exc.join('\n')}` : '')
}
function reviewBlock(slug, reviews){
  const items = (reviews || []).map(r =>
    `- **${r.dimension}**：score=${r.score}` +
    `${r.layer1Fail ? ' ⚠layer1Fail' : ''}${r.directionRespecting === false ? ' [非既定方向建议]' : ''}\n` +
    `  - findings：${(r.findings || []).join('；') || '无'}\n` +
    `  - suggestions：${(r.suggestions || []).join('；') || '无'}` +
    (r.generalChecks ? `\n  - generalChecks：${JSON.stringify(r.generalChecks)}` : ''))
  return `## 评审结论\n\n${items.join('\n')}`
}
function verdictBlock(v){
  const ranked = (v?.rankedSlugs || []).map(r => `- ${r.slug}：score=${r.score}${r.oneLineReason ? `（${r.oneLineReason}）` : ''}`)
  return `## 最终裁决\n\n- 胜者：**${v?.winner || ''}**\n\n### 排名\n${ranked.join('\n')}\n\n### 裁决理由\n${v?.rationale || ''}`
}

// ── prompt builder（薄拼接：只塞 id / 维度 / 上游 return，不含业务判断）──
const base = `设计区 designZoneId=${designZoneId}。`
function landPrompt(slug, vc){
  return `${base}\n你负责落地变体 slug=${slug}。本变体方向上下文（variantContext，来自多方案概述，逐字遵守、不得改方向）：\n${JSON.stringify(vc, null, 2)}\n` +
    `按你的职责完成该变体完整落地（注册变体 + 按需 zones.json + 施工简报 + 施工 modules.json + validate），产物写入你自己的 _${slug}/ 私有文件。`
}
function criticPrompt(slug, dim, designPath){
  return `${base}\n你评审变体 slug=${slug} 的【单一维度】 dimension=「${dim}」。该变体产物位于：${designPath}（及其叶子 modules.json）。` +
    `只评这一个维度，按你的职责返回结构化评审；判据自行从知识层取，不在此复述。`
}
function judgePrompt(candidates){
  const ctx = candidates.map(c => `- slug=${c.slug}（评审 ${c.reviews.length} 份）`).join('\n')
  return `${base}\n原始用户诉求：${userRequest || '（见父 DESIGN.md 战略简报）'}\n候选变体：\n${ctx}\n` +
    `读各候选评审结论（${candidates.map(c => `_${c.slug}/DESIGN.md`).join('、')}）+ 父 DESIGN.md 用户喜好，选出最优并调 adopt_variant 采纳；返回结构化判决。`
}
function judgeDegeneratePrompt(slug){
  return `${base}\n仅有唯一候选变体 slug=${slug}（N=1 退化路径，无需选拔），直接调 adopt_variant 采纳它；返回结构化判决，winner=${slug}。`
}
function refinePrompt(slug){
  return `${base}\n对已采纳的最优方案 slug=${slug}（已转正，路径 ${slug}/）做固定 ${REFINE_ROUND} 轮精修：` +
    `读其最新评审结论 → 提取可优化项 → 修复（不改方向；几何级可自动、语义级记 [自动改图建议]）→ validate。` +
    `返回结构化精修判决（passed / rootCause / reviseInstruction / failedDimensions）。`
}

// ═══════════════ 七步编排 ═══════════════

// Step1 感知：战略分析 ∥ 空间理解
phase('感知')
const [strategySec, spaceSec] = await parallel([
  () => agent(`${base}\n原始用户诉求：${userRequest}\n你是 Step1 战略分析分身，按你的职责产出「用户诉求 + 项目基础信息」节并 return。`,
    { agentType: 'strategy-analysis-agent', label: 'sense:strategy', phase: '感知' }),
  () => agent(`${base}\n你是 Step1 空间理解分身，独立理解当前户型，产出「设计区空间骨架」节并 return。`,
    { agentType: 'space-understanding-agent', label: 'sense:space', phase: '感知' }),
])
await writeSections(parentDesign, [strategySec, spaceSec], '感知')

// Step2 规划推演：分区思维 ∥ 顺序思维
phase('规划推演')
const [zoningSec, seqSec] = await parallel([
  () => agent(`${base}\n你是 Step2 分区思维分身，承接空间骨架产出「方案草稿 · 分区思维」子段并 return。`,
    { agentType: 'zoning-design-agent', label: 'plan:zoning', phase: '规划推演' }),
  () => agent(`${base}\n你是 Step2 顺序思维分身，承接空间骨架产出「方案草稿 · 顺序思维」子段并 return。`,
    { agentType: 'sequential-design-agent', label: 'plan:sequential', phase: '规划推演' }),
])
await writeSections(parentDesign, ['## 方案草稿', zoningSec, seqSec], '规划推演')

// Step3 多方案生成：单脑 + N 自适应 + 多样性护栏（不过则有上限重出）
phase('多方案生成')
async function genOverview(retryNote){
  return agent(
    `${base}\n你是 Step3 多方案生成分身，消化双草稿发散方向变体，返回结构化 overview（含 proposedN、每变体 anchorSeedType）。` +
    `${retryNote || ''}`,
    { agentType: 'multi-plan-agent', schema: OVERVIEW_SCHEMA, label: 'multiplan', phase: '多方案生成' })
}
// 红线15：多样性护栏必须在【落地集】上校验。先收敛 N → slice → 在 sliced 集上验类型≥2，
// 否则 variants 数 > N 时（如 proposedN>4 被 clamp 到 4）护栏在全集通过、落地的前 N 个却可能全同类型。
function pickN(ov){ return Math.min(clampN(args?.n || ov?.proposedN), 4) }
function chooseVariants(ov){ return (ov?.variants || []).slice(0, pickN(ov)) }
function diverseEnough(vs){ return new Set(vs.map(v => v.anchorSeedType)).size >= 2 }

let overview = await genOverview()
let chosen = chooseVariants(overview)
if (chosen.length > 1 && !diverseEnough(chosen)) {
  log('多样性护栏未过（落地集 anchorSeedType 类型 < 2），要求重出 1 次')
  const re = await genOverview('上一轮入选（系统只采用前 N 个）变体的 anchorSeedType 类型不足 2 种，请保证类型至少跨 2 种重出。')
  const reChosen = chooseVariants(re)
  if (reChosen.length <= 1 || diverseEnough(reChosen)) { overview = re; chosen = reChosen }
  else log('重出后落地集类型仍 < 2，按现状放行（避免死循环），由裁决阶段兜底')
}
await writeSections(parentDesign, [overviewBlock({ ...overview, variants: chosen })], '多方案生成')

// 落地集（= 收敛后 slice，护栏已在其上校验/兜底）
const variants = chosen
N = variants.length
const vcOf = {}
for (const v of variants) vcOf[v.slug] = {
  variantDirection: v.direction, variantNarrative: v.narrative,
  variantAnchorSeed: v.anchorSeed, variantAvoidance: v.avoidance,
}
const slugs = variants.map(v => v.slug)
if (!slugs.length) return { ok: false, reason: 'Step3 未产出任何变体' }
const degenerate = N === 1 || slugs.length === 1
log(`N=${slugs.length}（${degenerate ? 'N=1 退化' : '常规'}）；变体：${slugs.join('、')}`)

// Step4 多方案落地 ↔ Step5 多维评审（pipeline 重叠：每 slug 落地 resolve 即扇出其评审）
phase('多方案落地')
const reviewed = await parallel(slugs.map(slug => async () => {
  try {
    await agent(landPrompt(slug, vcOf[slug]), { agentType: 'placement-agent', label: `land:${slug}`, phase: '多方案落地' })
  } catch (e) {
    log(`slug ${slug} 落地失败/认输，跳过其评审`)
    return { slug, reviews: [], failed: true }
  }
  if (degenerate) return { slug, reviews: [] }   // N=1 退化：跳过多维评审选拔
  const reviews = await parallel([
    ...DIMS.map(dim => () => agent(criticPrompt(slug, dim, hiddenDesign(slug)),
      { agentType: 'review-agent', schema: CRITIC_SCHEMA, label: `review:${slug}:${dim}`, phase: '多维评审' })),
    () => agent(criticPrompt(slug, GENERAL, hiddenDesign(slug)),
      { agentType: 'review-agent', schema: CRITIC_SCHEMA, label: `review:${slug}:general`, phase: '多维评审' }),
  ])
  const ok = reviews.filter(Boolean)
  // 每 slug 评审落点 = 各自 _{slug}/DESIGN.md（不同文件，跨 slug 不竞态）
  await writeSections(hiddenDesign(slug), [reviewBlock(slug, ok)], '多维评审')
  return { slug, reviews: ok }
}))

const valid = reviewed.filter(r => r && !r.failed)
if (!valid.length) return { ok: false, reason: '全部候选落地失败（不强宣成功）' }   // 对齐 D14 验证闸门

// Step6 裁决：聚合评审选最优 + 采纳（judge 内部调 adopt_variant）
phase('裁决')
const verdict = await agent(
  degenerate ? judgeDegeneratePrompt(valid[0].slug) : judgePrompt(valid),
  { agentType: 'judge-agent', schema: JUDGE_SELECT_SCHEMA, label: 'judge:select', phase: '裁决' })
const winnerSlug = verdict?.winner || valid[0].slug
await writeSections(parentDesign, [verdictBlock(verdict)], '裁决')   // 正文节 workflow 写；frontmatter 由 adopt_variant 写（二者串行不并发）

// Step7 精修：固定 1 轮（采纳方案已转正，路径 {slug}/）
phase('精修')
const refine = await agent(refinePrompt(winnerSlug),
  { agentType: 'optimization-agent', schema: JUDGE_REFINE_SCHEMA, label: `refine:${winnerSlug}`, phase: '精修' })
// optimization 自写 {slug}/{leaf}/modules.json + {slug}/DESIGN.md「优化记录」节（私有，无竞态）

return {
  ok: true,
  designZoneId,
  winner: winnerSlug,
  candidates: slugs,
  degenerate,
  refinePassed: refine?.passed ?? null,
}
