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
const GENERAL = '通用品质'      // Layer 1.5（靠墙/间隙/对齐/空间利用）
const DESIGN_Q = '设计品质'     // Layer 2（一个分身整体覆盖全部注入维）

function clampN(raw){ const n = Math.max(1, raw || 3); return Math.min(n, 4) }   // 软上限 4，默认 3
let N = clampN(args?.n)

// ── 结构化输出 schema（4 个）─────────────────────────────────────
// 【契约·三处同名钉死】Step3 变体字段短名（direction/narrative/anchorSeed/avoidance）
// 必须三处一致：① 本 OVERVIEW_SCHEMA 属性名 ② multi-plan-agent.md 产出字段名 ③ 下方 vcOf 读取的 v.* 短名。
// vcOf 负责把短名映射回 placement 用的长名（variantDirection 等），勿在 agent 侧改回长名。
// 【差异化在方向层，不在配置层】每变体只锁 anchorSeed（≤1 条硬锚点：单家具/组合关系/空间策略三类型之一，
// 类型语义由 agent 按房型策略判定），其余决策交落地分身全局重判。本脚本只比对不透明 anchorSeed 签名做
// 产前弱护栏去重（抽象层查重能力有限），防雷同主防线 = judge 产后对各变体 modules.json 的事实查重。见 diverseEnough。
const OVERVIEW_SCHEMA = {  // Step3 返回：每变体一个设计方向 + 唯一硬锚点
  type: 'object', required: ['variants', 'proposedN'],
  properties: {
    proposedN: { type: 'number' },                     // = 去重后实质不同的可行方向数（不强凑、不重复）
    variants: { type: 'array', items: { type: 'object', required: ['slug', 'direction', 'anchorSeed'],
      properties: {
        slug: { type: 'string' },
        direction: { type: 'string' },                 // 设计方向核心句（禁写具体家具配置）
        narrative: { type: 'string' },                 // 本方向为何值得探索（WHY 输入，非约束）
        anchorSeed: { type: 'string' },                // 唯一硬锚点（≤1 条）——相异性查重键 + 落地唯一硬约束
        avoidance: { type: 'string' },                 // 反模式提示（区分兄弟变体的设计哲学，非家具禁止清单）
      } } },
    excluded: { type: 'array', items: { type: 'object',
      properties: { candidate: { type: 'string' }, reason: { type: 'string' } } } },
  },
}
const CRITIC_SCHEMA = {  // Step5 评审：每变体 2 份——设计品质(整体覆盖全维) + 通用品质(Layer1.5)；去打分：只报明显问题
  type: 'object', required: ['dimension', 'hasIssue', 'layer1Fail', 'directionRespecting', 'issues'],
  properties: {
    dimension: { type: 'string' },                     // '设计品质' 或 '通用品质'
    hasIssue: { type: 'boolean' },                     // 是否发现明显问题；false = 直接通过（不强行凑优点）
    layer1Fail: { type: 'boolean' },                   // 工程合规硬伤（validate 已过仍兜底）
    directionRespecting: { type: 'boolean' },          // 评审是否在既定方向内（防裁判把"换方向"当缺陷）
    issues: { type: 'array', items: { type: 'object', required: ['desc', 'severity'],   // 只列明显问题，无则空数组
      properties: {
        dim: { type: 'string' },                                 // 该问题所属子维（设计品质:动线/空间意图/…；通用品质:靠墙/间隙/对齐/空间利用）
        desc: { type: 'string' },                                // 明显问题描述
        evidence: { type: 'string' },                            // 坐标/截图证据
        severity: { type: 'string', enum: ['硬违规', '明显', '轻微'] },  // 硬违规=layer1Fail/【必须】级✗
      } } },
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
// 仅做"剥到标题 + 去整体包裹围栏"这类机械清理，不改节内文字（不违逐字红线）。
// expectedAnchor（可选）：期望节标题关键词——剥离到首个【包含该关键词的标题行】，跳过 agent 夹带的
// 带标题草稿（如"### Analysis Summary"中间稿，实测曾泄漏进父 DESIGN.md）；无匹配则回退首标题行为。
function sanitizeSection(s, expectedAnchor){
  if (!s) return s
  let t = String(s).trim()
  const fence = t.match(/^```[a-zA-Z]*\n([\s\S]*?)\n```$/)   // 整体被 ```lang … ``` 包裹 → 剥壳
  if (fence) t = fence[1].trim()
  if (expectedAnchor) {
    const re = new RegExp(`^#{1,6}\\s.*${expectedAnchor.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'm')
    const m = t.match(re)
    if (m) return t.slice(t.indexOf(m[0])).trim()            // 命中期望锚 → 从该标题起取
  }
  const h = t.search(/^#{1,6}\s/m)                           // 首个 markdown 标题位置
  if (h > 0) t = t.slice(h)                                  // 标题前的散文/英文前言剥掉；无标题(-1)/标题在首则不动
  return t.trim()
}

// ── 写盘 helper：把 markdown 节块串行交给 design-scribe（单写者，无并发同文件）──
// anchors（可选）：与 blocks 等长的期望节标题关键词数组（纯标题块如 '## 方案草稿' 传 null）。
async function writeSections(path, blocks, phaseName, anchors){
  const sections = blocks
    .map((b, i) => b ? sanitizeSection(b, anchors && anchors[i]) : b)   // 按原索引对齐 anchors
    .filter(Boolean)
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
    `- **${v.slug}**：${v.direction || ''}\n  - 锚点：${v.anchorSeed || ''}\n` +
    (v.avoidance ? `  - 避免：${v.avoidance}\n` : '') +
    `  - 叙事：${v.narrative || ''}`)
  const exc = (ov?.excluded || []).map(e => `- 排除「${e.candidate}」：${e.reason || ''}`)
  return `## 多方案战略层概述\n\n${lines.join('\n')}` +
    (exc.length ? `\n\n### 自动排除\n${exc.join('\n')}` : '')
}
function reviewBlock(slug, reviews){
  const items = (reviews || []).map(r => {
    const head = `- **${r.dimension}**：${r.hasIssue ? '发现问题' : '通过'}` +
      `${r.layer1Fail ? ' ⚠layer1Fail' : ''}${r.directionRespecting === false ? ' [非既定方向建议]' : ''}`
    const issues = (r.issues || []).length
      ? '\n' + r.issues.map(i => `  - [${i.severity || '明显'}]${i.dim ? ` ${i.dim}:` : ''} ${i.desc}${i.evidence ? `（${i.evidence}）` : ''}`).join('\n')
      : '\n  - 无明显问题'
    return head + issues
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
  return `${base}\n你负责落地变体 slug=${slug}。本变体方向上下文（variantContext，来自多方案概述）：\n${JSON.stringify(vc, null, 2)}\n` +
    `约束力分级：variantAnchorSeed 是唯一硬约束（必须兑现；几何上不成立则走认输路径上报，不强行施工）；` +
    `variantDirection / variantNarrative 是方向参考（帮助你决策的 WHY 输入，不是合同条款，其中的描述性语句不得当禁令）；` +
    `variantAvoidance 是反模式提示。其余决策（主家具选墙、是否 L 形、可选家具位置等）由你按房间策略全局判断。\n` +
    `按你的职责完成该变体完整落地（注册变体 + 按需 zones.json + 施工简报 + 施工 modules.json + 落位自检 + validate），产物写入你自己的 _${slug}/ 私有文件。`
}
function designQualityPrompt(slug, dims, designPath){
  return `${base}\n你做变体 slug=${slug} 的【Layer 2 设计品质·整体评审】 dimension=「${DESIGN_Q}」。该变体产物位于：${designPath}（及其叶子 modules.json）。` +
    `一次性整体覆盖这些设计维度：${(dims && dims.length) ? dims.join('、') : '（未注入）'}——逐维找明显问题，每个 issue 标 \`dim\`=所属维度，无问题该维不报。判据自行从知识层取，不在此复述。`
}
function generalQualityPrompt(slug, designPath){
  return `${base}\n你做变体 slug=${slug} 的【Layer 1.5 通用品质】 dimension=「${GENERAL}」。该变体产物位于：${designPath}（及其叶子 modules.json）。` +
    `只判通用品质：靠墙完整性 / 相邻空隙 / 对齐 / 空间利用（有无大块墙段或区域既无家具又无成立的留白豁免——定性、不设阈值）；每个 issue 标 \`dim\`。判据见知识层「Layer 1.5 通用品质」，不在此复述。`
}
function judgePrompt(candidates, excluded){
  const ctx = candidates.map(c => `- slug=${c.slug}（评审 ${c.reviews.length} 份）`).join('\n')
  const exc = (excluded && excluded.length)
    ? `\n【已被采纳闸门打回、不得再选】：${excluded.join('、')}（这些方案无法转正/采纳，从候选中剔除）。` : ''
  return `${base}\n原始用户诉求：${userRequest || '（见父 DESIGN.md 战略简报）'}\n候选变体：\n${ctx}${exc}\n` +
    `先读各候选叶子 modules.json（_{slug}/ 或 _{slug}/{leaf}/ 下）与父 ${parentDesign}「设计区空间骨架」节，按你的提示词建横向事实台账（含实质雷同判定）；` +
    `再读各候选评审结论（${candidates.map(c => hiddenDesign(c.slug)).join('、')}）+ 父 DESIGN.md 用户喜好，选出最优并调 adopt_variant 采纳；返回结构化判决。`
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
await writeSections(parentDesign, [strategySec, spaceSec], '感知', ['用户诉求', '空间骨架'])

// Step2 规划推演：分区思维 ∥ 顺序思维
phase('规划推演')
const [zoningSec, seqSec] = await parallel([
  () => agent(`${base}\n你是 Step2 分区思维分身，承接空间骨架产出「方案草稿 · 分区思维」子段并 return。`,
    { agentType: 'zoning-design-agent', label: 'plan:zoning', phase: '规划推演' }),
  () => agent(`${base}\n你是 Step2 顺序思维分身，承接空间骨架产出「方案草稿 · 顺序思维」子段并 return。`,
    { agentType: 'sequential-design-agent', label: 'plan:sequential', phase: '规划推演' }),
])
await writeSections(parentDesign, ['## 方案草稿', zoningSec, seqSec], '规划推演', [null, '方案草稿', '方案草稿'])

// Step3 多方案生成：单脑 + N 自适应 + 多样性护栏（不过则有上限重出）
phase('多方案生成')
async function genOverview(retryNote){
  return agent(
    `${base}\n你是 Step3 多方案生成分身，消化双草稿组织出 N 个【方向层变体】，返回结构化 overview（含 proposedN）。每变体含：direction（设计方向核心句，禁写具体家具配置）/ narrative（本方向为何值得探索）/ anchorSeed（本变体唯一硬锚点，最多 1 条，三类型与填法见你的提示词）/ avoidance（反模式提示，可选）。各变体 anchorSeed 必须两两不同（同锚点=同方案）；proposedN = 去重后实质不同的可行方向数，不足 3 不强凑、不重复。` +
    `${retryNote || ''}`,
    { agentType: 'multi-plan-agent', schema: OVERVIEW_SCHEMA, label: 'multiplan', phase: '多方案生成' })
}
// 红线15：相异性护栏必须在【落地集】上校验。先收敛 N → slice → 在 sliced 集上验 anchorSeed 两两不同，
// 否则 variants 数 > N 时护栏在全集通过、落地的前 N 个却可能雷同。
function pickN(ov){ return Math.min(clampN(args?.n || ov?.proposedN), 4) }
function chooseVariants(ov){ return (ov?.variants || []).slice(0, pickN(ov)) }
// 产前弱护栏：anchorSeed（唯一硬锚点）两两不同；同 anchorSeed=同一方案，不论叙事。
// 抽象层查重能力有限（叙事不同而落地相同的雷同在此查不出），防雷同主防线 = judge 产后事实查重。
// 【房型中立】只比对不透明签名（归一化去空白），不内嵌任何家具/墙名——卧室/卫生间/客厅通用。
function layoutKey(v){ return (v.anchorSeed || '').replace(/\s+/g, '') }
function diverseEnough(vs){ return new Set(vs.map(layoutKey)).size === vs.length }

let overview = await genOverview()
let chosen = chooseVariants(overview)
if (chosen.length > 1 && !diverseEnough(chosen)) {
  log('相异性护栏未过（落地集 anchorSeed 有雷同），要求去重重出 1 次')
  const re = await genOverview('上一轮入选变体中有 anchorSeed（唯一硬锚点）雷同者——它们是同一方案。请让各变体锚点两两不同（换锚点类型或换锚定对象），或减少变体数（只报实质不同的方向，不补不重复）后重出。')
  const reChosen = chooseVariants(re)
  overview = re; chosen = reChosen                       // 无条件放行第二轮（最新重出版本），保留单次重试边界
  if (!(reChosen.length <= 1 || diverseEnough(reChosen)))
    log('重出后落地集仍有雷同，仍放行第二轮（避免死循环），由裁决阶段兜底')
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
if (!degenerate && DIMS.length === 0) log('⚠ 未注入评审维度（args.dimensions 缺），Step5 设计品质维跳过、仅跑通用品质')   // N-11

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
    ...(DIMS.length ? [() => agent(designQualityPrompt(slug, DIMS, hiddenDesign(slug)),
      { agentType: 'design-review-agent', schema: CRITIC_SCHEMA, label: `review:${slug}:设计品质`, phase: '多维评审' })] : []),
    () => agent(generalQualityPrompt(slug, hiddenDesign(slug)),
      { agentType: 'general-review-agent', schema: CRITIC_SCHEMA, label: `review:${slug}:通用品质`, phase: '多维评审' }),
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
if (refine?.optimizationRecord) await writeSections(adoptedDesign(winnerSlug), [refine.optimizationRecord], '精修', ['优化记录'])

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
