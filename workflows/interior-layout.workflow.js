export const meta = {
  name: 'interior-layout-scene1',
  description: '场景①：设计引擎重做 —— 七步流（感知→规划推演→多方案→落地→评审→裁决→精修）',
  phases: [
    { title: '感知', detail: '战略分析 ∥ 空间理解，定调诉求与空间骨架' },
    { title: '规划推演', detail: '分区思维 ∥ 顺序思维，双轴出方案草稿' },
    { title: '多方案生成', detail: '消化双草稿，发散 N 个方向变体（含多样性护栏）' },
    { title: '多方案落地', detail: 'N 路并行落地：注册变体 + 施工简报 + 施工 + validate' },
    { title: '多维评审', detail: '每变体多单维 + 1 通用评审（只报明显问题，无则通过），与落地 pipeline 重叠' },
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
// 【契约·三处同名钉死】Step3 变体字段短名（direction/narrative/anchorSeed/avoidance/anchorSeedType）
// 必须三处一致：① 本 OVERVIEW_SCHEMA 属性名 ② multi-plan-agent.md 产出字段名 ③ 下方 vcOf 读取的 v.* 短名。
// vcOf 负责把短名映射回 placement 用的长名（variantDirection 等），勿在 agent 侧改回长名。
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
const CRITIC_SCHEMA = {  // Step5 单维 + 通用共用，dimension 区分；去打分：只报该维明显问题，无问题=直接通过
  type: 'object', required: ['dimension', 'hasIssue', 'layer1Fail', 'directionRespecting', 'issues'],
  properties: {
    dimension: { type: 'string' },                     // 维度之一 或 '__general__'
    hasIssue: { type: 'boolean' },                     // 该维是否发现明显问题；false = 直接通过（不强行凑优点）
    layer1Fail: { type: 'boolean' },                   // 工程合规硬伤（validate 已过仍兜底）
    directionRespecting: { type: 'boolean' },          // 评审是否在既定方向内（防裁判把"换方向"当缺陷）
    issues: { type: 'array', items: { type: 'object', required: ['desc', 'severity'],   // 只列明显问题，无则空数组
      properties: {
        desc: { type: 'string' },                                // 明显问题描述
        evidence: { type: 'string' },                            // 坐标/截图证据
        severity: { type: 'string', enum: ['硬违规', '明显', '轻微'] },  // 硬违规=layer1Fail/【必须】级✗
      } } },
    generalChecks: { type: 'object',                            // 仅 __general__ 填
      properties: { againstWall: { type: 'boolean' }, adjacentGap: { type: 'boolean' }, alignment: { type: 'boolean' } } },
  },
}
const JUDGE_SELECT_SCHEMA = {  // Step6 裁决：去打分，缺陷最少/最轻者胜（硬违规优先于明显数）
  type: 'object', required: ['winner', 'ranking', 'rationale'],
  properties: {
    winner: { type: 'string' },
    ranking: { type: 'array', items: { type: 'object', required: ['slug', 'defects'],   // 按缺陷少→多排序
      properties: {
        slug: { type: 'string' },
        defects: { type: 'array', items: { type: 'object',          // 该变体未化解缺陷清单（空数组=无缺陷）
          properties: { dim: { type: 'string' }, desc: { type: 'string' }, severity: { type: 'string', enum: ['硬违规', '明显', '轻微'] } } } },
        oneLineReason: { type: 'string' },
      } } },
    rationale: { type: 'string' },                     // 文字客观优缺点对比 + 缺陷最少判定；判据来自知识层，不复述
  },
}
const JUDGE_REFINE_SCHEMA = {  // Step7 精修判决（optimization 返回）
  type: 'object', required: ['passed'],
  properties: {
    passed: { type: 'boolean' },
    layer1Fail: { type: 'boolean' },                   // 工程合规硬伤兜底（如 E013/0 模块=路径错），与 passed 互斥语义
    rootCause: { type: 'string', enum: ['strategy', 'placement', 'none'] },  // 决定 fix 改哪层
    reviseInstruction: { type: 'string' },
    failedDimensions: { type: 'array', items: { type: 'string' } },
    optimizationRecord: { type: 'string' },            // R5：「## 优化记录」节文本，交 workflow 经 design-scribe upsert（不自写 DESIGN.md）
  },
}
// ── 编排层确定性后置核验 schema（verify-agent 只报事实，控制流在脚本）─────
const ADOPT_VERIFY_SCHEMA = {  // Step6 后采纳收口核验
  type: 'object', required: ['adoptedSlug', 'promoted'],
  properties: {
    adoptedSlug: { type: 'string' },                   // 回读父 DESIGN.md frontmatter 实际 adopted（未采纳填空串）
    promoted: { type: 'boolean' },                     // 转正目录（无 _ 前缀）是否真实存在
    repaired: { type: 'boolean' },                     // 本次是否由 verify-agent 补调了 adopt_variant
  },
}
const VALIDATE_GATE_SCHEMA = {  // Step7 后独立 validate 闸门
  type: 'object', required: ['fileModuleCount', 'validateModuleCount', 'e013'],
  properties: {
    fileModuleCount: { type: 'number' },               // 采纳叶子 modules.json 实际模块数（读文件数）
    validateModuleCount: { type: 'number' },           // validate_layout 解析到的模块数
    e013: { type: 'boolean' },                          // 是否报 E013_INVALID_MODULE_FILE_PATH（路径错）
    reason: { type: 'string' },
  },
}

// ── 路径 helper（内联）──────────────────────────────────────────
const parentDesign = `schemes/${designZoneId}/DESIGN.md`
const hiddenDesign = slug => `schemes/${designZoneId}/_${slug}/DESIGN.md`
const adoptedDesign = slug => `schemes/${designZoneId}/${slug}/DESIGN.md`

// ── 节块净化（P-4）：剥 agent return 夹带的散文/英文前言与包裹围栏，再交 scribe ──
// 仅做"剥到首个 markdown 标题 + 去整体包裹围栏"这类机械清理，不改节内文字（不违逐字红线）。
function sanitizeSection(s){
  if (!s) return s
  let t = String(s).trim()
  const fence = t.match(/^```[a-zA-Z]*\n([\s\S]*?)\n```$/)   // 整体被 ```lang … ``` 包裹 → 剥壳
  if (fence) t = fence[1].trim()
  const h = t.search(/^#{1,6}\s/m)                           // 首个 markdown 标题位置
  if (h > 0) t = t.slice(h)                                  // 标题前的散文/英文前言剥掉；无标题(-1)/标题在首则不动
  return t.trim()
}

// ── 写盘 helper：把 markdown 节块串行交给 design-scribe（单写者，无并发同文件）──
async function writeSections(path, blocks, phaseName){
  const sections = blocks.filter(Boolean).map(sanitizeSection).filter(Boolean)
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
  const items = (reviews || []).map(r => {
    const head = `- **${r.dimension}**：${r.hasIssue ? '发现问题' : '通过'}` +
      `${r.layer1Fail ? ' ⚠layer1Fail' : ''}${r.directionRespecting === false ? ' [非既定方向建议]' : ''}`
    const issues = (r.issues || []).length
      ? '\n' + r.issues.map(i => `  - [${i.severity || '明显'}] ${i.desc}${i.evidence ? `（${i.evidence}）` : ''}`).join('\n')
      : '\n  - 无明显问题'
    const gc = (r.dimension === GENERAL && r.generalChecks && Object.keys(r.generalChecks).length)
      ? `\n  - generalChecks：${JSON.stringify(r.generalChecks)}` : ''   // 仅 __general__ 维渲染，空对象 {} 也滤掉
    return head + issues + gc
  })
  return `## 评审结论\n\n${items.join('\n')}`
}
function verdictBlock(v){
  const ranked = (v?.ranking || []).map(r => {
    const defects = (r.defects || []).length
      ? r.defects.map(d => `[${d.severity || '明显'}]${d.dim ? `${d.dim}:` : ''}${d.desc}`).join('；')
      : '无缺陷'
    return `- ${r.slug}：${defects}${r.oneLineReason ? `（${r.oneLineReason}）` : ''}`
  })
  return `## 最终裁决\n\n- 胜者：**${v?.winner || ''}**\n\n### 各方案缺陷\n${ranked.join('\n')}\n\n### 裁决理由\n${v?.rationale || ''}`
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
function judgePrompt(candidates, excluded){
  const ctx = candidates.map(c => `- slug=${c.slug}（评审 ${c.reviews.length} 份）`).join('\n')
  const exc = (excluded && excluded.length)
    ? `\n【已被采纳闸门打回、不得再选】：${excluded.join('、')}（这些方案无法转正/采纳，从候选中剔除）。` : ''
  return `${base}\n原始用户诉求：${userRequest || '（见父 DESIGN.md 战略简报）'}\n候选变体：\n${ctx}${exc}\n` +
    `读各候选评审结论（${candidates.map(c => hiddenDesign(c.slug)).join('、')}）+ 父 ${parentDesign} 用户喜好，选出最优并调 adopt_variant 采纳；返回结构化判决。`
}
function judgeDegeneratePrompt(slug){
  return `${base}\n仅有唯一候选变体 slug=${slug}（N=1 退化路径，无需选拔），直接调 adopt_variant 采纳它；返回结构化判决，winner=${slug}。`
}
function refinePrompt(slug){
  return `${base}\n对已采纳的最优方案 slug=${slug}（已转正，路径 schemes/${designZoneId}/${slug}/）做固定 ${REFINE_ROUND} 轮精修：` +
    `入场先 Glob/Read 实际 modules.json 路径（单叶子 ${slug}/modules.json 或多叶子 ${slug}/{leaf}/modules.json，不凭拼），` +
    `读其最新评审结论 → 提取可优化项 → 修复（不改方向；几何级可自动、语义级记 [自动改图建议]）→ validate。` +
    `不要自写 DESIGN.md：把「## 优化记录」节文本放进返回的 optimizationRecord 字段，由编排层写盘。` +
    `返回结构化精修判决（passed / layer1Fail / rootCause / reviseInstruction / failedDimensions / optimizationRecord）。`
}
// 采纳收口核验（verify-agent，只报事实）：探测转正态 → 未满足补调 adopt_variant → 回读校验
function adoptVerifyPrompt(slug){
  return `${base}\n你是采纳收口核验分身（只报事实，不做设计判断、不决定重挑/跳过）。胜者 slug=${slug}。\n` +
    `① Glob schemes/${designZoneId}/ 探测：转正目录「${slug}」（无 _ 前缀）是否存在、隐藏目录「_${slug}」是否仍在；Read 父 DESIGN.md（${parentDesign}）首部 frontmatter 取 adopted。\n` +
    `② 若未真转正（转正目录缺失 或 adopted≠${slug}）：以 function-calling 调 adopt_variant({ designZoneId:"${designZoneId}", winnerSlug:"${slug}" }) 补做（幂等可重入）。\n` +
    `③ 回读校验：再次确认转正目录存在 + 父 DESIGN.md frontmatter adopted 的真实值。\n` +
    `返回 { adoptedSlug:回读到的真实 adopted（无则空串）, promoted:转正目录是否存在, repaired:本次是否补调过 adopt_variant }。`
}
// 独立 validate 闸门（verify-agent，只报事实，不自判 passed）
function validateGatePrompt(slug){
  return `${base}\n你是精修后置 validate 闸门分身（只报事实，不自判 passed/通过、不决定重试/跳过）。采纳方案 slug=${slug}。\n` +
    `① Glob/Read 解析采纳叶子真实路径与叶子 zoneIds：有 schemes/${designZoneId}/${slug}/zones.json → 多叶子，取其声明的叶子集；无 → 单叶子，路径 ${slug}/modules.json、zoneId=${designZoneId}。\n` +
    `② Read 各采纳叶子 modules.json，数其 modules 数组实际长度之和 = fileModuleCount。\n` +
    `③ 调 validate_layout({ zoneIds:[采纳叶子 zoneIds] })，取其解析到的模块数 = validateModuleCount；若返回 E013_INVALID_MODULE_FILE_PATH 则 e013=true。\n` +
    `返回 { fileModuleCount, validateModuleCount, e013, reason:一句话说明 }。最终是否通过由编排层判定，你只给原始数字与 e013。`
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
  overview = re; chosen = reChosen                       // N-9：无条件放行第二轮（最新重出版本），保留单次重试边界
  if (!(reChosen.length <= 1 || diverseEnough(reChosen)))
    log('重出后落地集类型仍 < 2，仍放行第二轮（避免死循环），由裁决阶段兜底')
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
if (!degenerate && DIMS.length === 0) log('⚠ 未注入评审维度（args.dimensions 缺），Step5 将仅跑通用维——多维评审失效')   // N-11

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

// Step6 裁决 + 采纳收口（R1）：judge 选最优（仍内部调 adopt_variant），但其 prose 自述不作数；
// 编排层用零领域 verify-agent 独立探测→补做 adopt→回读校验。采纳失败则打回裁决、排除坏胜者重挑 1 次。
phase('裁决')
let winnerSlug = null
let adoptOk = false
const excluded = []
for (let attempt = 0; attempt < 2 && !adoptOk; attempt++) {
  const pool = valid.filter(c => !excluded.includes(c.slug))
  if (!pool.length) break
  const verdict = await agent(
    degenerate ? judgeDegeneratePrompt(pool[0].slug) : judgePrompt(pool, excluded),
    { agentType: 'judge-agent', schema: JUDGE_SELECT_SCHEMA, label: `judge:select${attempt ? '-retry' : ''}`, phase: '裁决' })
  winnerSlug = verdict?.winner || pool[0].slug
  await writeSections(parentDesign, [verdictBlock(verdict)], '裁决')   // 正文节 workflow 写；frontmatter 由 adopt_variant 写（二者串行不并发）
  // 采纳确定性后置核验（verify-agent 只报事实，控制流在脚本）
  const adopt = await agent(adoptVerifyPrompt(winnerSlug),
    { agentType: 'verify-agent', schema: ADOPT_VERIFY_SCHEMA, label: `adopt:${winnerSlug}`, phase: '裁决' })
  if (adopt && adopt.adoptedSlug === winnerSlug && adopt.promoted) {
    adoptOk = true
    if (adopt.repaired) log(`采纳由编排层补做生效（judge 未真转正，verify-agent 补调 adopt）：${winnerSlug}`)
  } else {
    log(`采纳收口失败（${winnerSlug}：adoptedSlug=${adopt?.adoptedSlug ?? 'null'}, promoted=${adopt?.promoted}），打回裁决排除该胜者重挑`)
    excluded.push(winnerSlug)
  }
}
if (!adoptOk) throw new Error(`采纳收口失败：重挑后仍无有效可采纳胜者（已排除 ${excluded.join('、') || '无'}）`)

// Step7 精修：固定 1 轮（采纳方案已转正）
phase('精修')
const refine = await agent(refinePrompt(winnerSlug),
  { agentType: 'optimization-agent', schema: JUDGE_REFINE_SCHEMA, label: `refine:${winnerSlug}`, phase: '精修' })
// optimization 仅 Edit 采纳叶子 modules.json（单叶子 {slug}/modules.json 或多叶子 {slug}/{leaf}/modules.json，入场自行 Glob 解析）；
// R5：「优化记录」节由 optimization 经 schema 返回、workflow 经 design-scribe upsert，保评审节不被全量重建压成占位。
if (refine?.optimizationRecord) await writeSections(adoptedDesign(winnerSlug), [refine.optimizationRecord], '精修')

// R2 独立后置 validate 闸门：不信 optimization 自报 passed，verify-agent 独立重跑 validate 比对模块数；最终布尔在脚本算。
const gate = await agent(validateGatePrompt(winnerSlug),
  { agentType: 'verify-agent', schema: VALIDATE_GATE_SCHEMA, label: `gate:${winnerSlug}`, phase: '精修' })
const layer1Pass = !!gate && !gate.e013 && gate.validateModuleCount === gate.fileModuleCount && gate.fileModuleCount > 0
const refinePassed = !!(refine?.passed) && !refine?.layer1Fail && layer1Pass
if (!refinePassed)
  log(`精修后置闸门未过（如实汇报、不重试）：refine.passed=${refine?.passed}, refine.layer1Fail=${refine?.layer1Fail}, e013=${gate?.e013}, validateCount=${gate?.validateModuleCount}, fileCount=${gate?.fileModuleCount}`)

return {
  ok: true,
  designZoneId,
  winner: winnerSlug,
  candidates: slugs,
  degenerate,
  refinePassed,
}
