export const meta = {
  name: 'interior-layout-scene1',
  description: '场景①：多方案设计 —— 感知→规划推演→多方案→集成落地（施工+识图自评+自优化）→方案对比，终选由用户在 Web 端执行',
  phases: [
    { title: '感知', detail: '战略定调 + 空间骨架（单分身）' },
    { title: '规划推演', detail: '分区思维 ∥ 顺序思维，双轴出方案草稿' },
    { title: '多方案生成', detail: '消化双草稿，发散 N 个方向变体（含多样性护栏）' },
    { title: '方案落地', detail: 'N 路并行集成落地：施工简报 + 施工 + validate + 识图自评 + 自优化；每方案独立闸门核验' },
    { title: '方案对比', detail: '脚本机械拼方案对比表（含实质雷同标注），交用户 Web 端终选' },
  ],
}

// ── 入口 args 契约 ────────────────────────────────────────────────
const designZoneId = args?.designZoneId               // 必填，单段或多段 path，如 rz_3 或 rz_6/dz_1
if (!designZoneId) throw new Error('args.designZoneId 必填')
const userRequest  = args?.originalUserRequest || ''
// 评审维度判据不再经 args 注入——placement 集成自评后从知识层（Skill-L2 的 design_evaluation）自取。
function clampN(raw){ const n = Math.max(1, raw || 3); return Math.min(n, 4) }   // 软上限 4，默认 3

// ── 结构化输出 schema ───────────────────────────────────────────
const PERCEPTION_SCHEMA = {  // Step1 感知（合并 战略定调 + 空间骨架）：两节文本，每字段首字符须为 markdown 标题
  type: 'object', required: ['strategySec', 'spaceSec'],
  properties: {
    strategySec: { type: 'string' },                   // 「## 用户诉求 + 项目基础信息」节全文
    spaceSec: { type: 'string' },                      // 「## 设计区空间骨架」节全文
  },
}
// 【契约·三处同名钉死】Step3 变体字段短名（direction/narrative/anchorSeed/avoidance/expectedWalls）
// 必须三处一致：① 本 OVERVIEW_SCHEMA 属性名 ② multi-plan-agent.md 产出字段名 ③ 下方 vcOf 读取的 v.* 短名。
// vcOf 负责把短名映射回 placement 用的长名（variantDirection 等），勿在 agent 侧改回长名。
// 【差异化在方向层，不在配置层】每变体只锁 anchorSeed（≤1 条硬锚点：单家具/组合关系/空间策略三类型之一，
// 类型语义由 agent 按房型策略判定），其余决策交落地分身全局重判。
// 【expectedWalls 只查重、不锁定】实测教训：两个锚点类型不同（床=西墙 vs 北区收纳带）在同一户型收敛到同一物理
// 布局——锚点签名查不出这种"方向异、落点同"。expectedWalls=该方向预期主家具墙面归属（严格"家具:墙名"格式），
// 仅作产前查重键，**不进 vcOf、不传 placement**（不复活 wallPlan 配置锁定，落地重判权不变）。见 diverseEnough。
const OVERVIEW_SCHEMA = {  // Step3 返回：每变体一个设计方向 + 唯一硬锚点 + 预期布局查重键
  type: 'object', required: ['variants', 'proposedN'],
  properties: {
    proposedN: { type: 'number' },                     // = 去重后实质不同的可行方向数（不强凑、不重复）
    variants: { type: 'array', items: { type: 'object', required: ['slug', 'direction', 'anchorSeed', 'expectedWalls'],
      properties: {
        slug: { type: 'string' },
        direction: { type: 'string' },                 // 设计方向核心句（禁写具体家具配置）
        narrative: { type: 'string' },                 // 本方向为何值得探索（WHY 输入，非约束）
        anchorSeed: { type: 'string' },                // 唯一硬锚点（≤1 条）——落地唯一硬约束
        avoidance: { type: 'string' },                 // 反模式提示（区分兄弟变体的设计哲学，非家具禁止清单）
        expectedWalls: { type: 'string' },             // 预期主家具墙面归属（严格"家具:墙名|家具:墙名"，禁尺寸/段位修饰/附属件）——仅查重
      } } },
    excluded: { type: 'array', items: { type: 'object',
      properties: { candidate: { type: 'string' }, reason: { type: 'string' } } } },
  },
}
// 【契约·三处同名钉死】Step4 返回字段须与 ① 本 PLACEMENT_SCHEMA ② placement-agent.md「Step H」③ 下方 comparisonBlock 读取一致。
const PLACEMENT_SCHEMA = {  // Step4 集成落地返回：结果 + 对比表数据（脚本只排版/比对，不解释内容——房型中立）
  type: 'object', required: ['ok'],
  properties: {
    ok: { type: 'boolean' },                           // false = 认输/失败（report 写明原因，可不给 factsheet）；true 时必须给完整 factsheet
    factsheet: { type: 'object', required: ['mainFurnitureWalls', 'furnitureList', 'storageRunMm', 'optionalFurniture', 'selfCheckSummary', 'validateSummary'],
      properties: {
        mainFurnitureWalls: { type: 'string' },        // 主家具墙面归属签名（严格"家具:墙名|家具:墙名"，禁尺寸/段位修饰/附属件）——跨方案雷同比对键
        furnitureList: { type: 'string' },             // 一行家具清单（对照 tags；缺省的可选家具也列出）
        storageRunMm: { type: 'number' },              // 贴墙收纳总延米
        optionalFurniture: { type: 'string' },         // 每件可选家具：已布置(位置) / 置换布置 / 省略(坐标级理由)
        selfCheckSummary: { type: 'string' },          // 识图自评结论 + 处置摘要
        validateSummary: { type: 'string' },           // 最终 validate 结果
      } },
    report: { type: 'string' },
  },
}
// ── 编排层确定性后置核验 schema（verify-agent 只报事实，控制流在脚本）─────
const VALIDATE_GATE_SCHEMA = {  // 每方案落地后独立 validate 闸门（不信 agent 自报）
  type: 'object', required: ['fileModuleCount', 'validateModuleCount', 'e013'],
  properties: {
    fileModuleCount: { type: 'number' },               // 方案叶子 modules.json 实际模块数（读文件数）
    validateModuleCount: { type: 'number' },           // validate_layout 解析到的模块数
    e013: { type: 'boolean' },                          // 是否报 E013_INVALID_MODULE_FILE_PATH（路径错）
    reason: { type: 'string' },
  },
}

// ── 路径 helper（内联）──────────────────────────────────────────
const parentDesign = `schemes/${designZoneId}/DESIGN.md`
// 方案目录可见无 _ 前缀：{slug}/DESIGN.md 由 placement 自写（含自检与优化记录），编排层不再写方案级文件

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
// scribeChain：scribe 全部挂链后台执行——同文件写盘保持串行（链式），但不阻塞主链关键路径；
// 在依赖盘上数据的步骤前（或 workflow return 前）须 await scribeChain 收口。
let scribeChain = Promise.resolve()
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
    (v.expectedWalls ? `  - 预期布局：${v.expectedWalls}\n` : '') +
    (v.avoidance ? `  - 避免：${v.avoidance}\n` : '') +
    `  - 叙事：${v.narrative || ''}`)
  const exc = (ov?.excluded || []).map(e => `- 排除「${e.candidate}」：${e.reason || ''}`)
  return `## 多方案战略层概述\n\n${lines.join('\n')}` +
    (exc.length ? `\n\n### 自动排除\n${exc.join('\n')}` : '')
}
// 方案对比表（纯机械排版 agent 产出的 factsheet 字段 + 不透明字符串雷同比对，零业务判断——房型中立）
// wallsKey 复用 normWalls 归一化（剥括号修饰/空白）——实测教训："西墙南段(3600mm)"与"西墙(全段4850mm)"
// 这类自由文本修饰让精确比对失效；格式主防在 agent 侧（严格"家具:墙名"），此处归一化兜底。
function wallsKey(f){ return normWalls(f?.mainFurnitureWalls) }
function findDuplicates(items){   // items: [{slug, facts}]，返回 [[slugA, slugB], ...]
  const pairs = []
  for (let i = 0; i < items.length; i++) for (let j = i + 1; j < items.length; j++) {
    const a = wallsKey(items[i].facts), b = wallsKey(items[j].facts)
    if (a && a === b) pairs.push([items[i].slug, items[j].slug])
  }
  return pairs
}
function comparisonBlock(items, chosenVariants, dup){
  const vmeta = {}
  for (const v of chosenVariants || []) vmeta[v.slug] = v
  const rows = [
    ['方向', s => vmeta[s]?.direction || ''],
    ['锚点', s => vmeta[s]?.anchorSeed || ''],
    ['主家具墙面', (s, f) => f.mainFurnitureWalls || ''],
    ['家具清单', (s, f) => f.furnitureList || ''],
    ['收纳延米(mm)', (s, f) => String(f.storageRunMm ?? '')],
    ['可选家具', (s, f) => f.optionalFurniture || ''],
    ['自检/识图', (s, f) => f.selfCheckSummary || ''],
    ['validate', (s, f) => f.validateSummary || ''],
    ['独立闸门', (s, f, it) => it.layer1Pass ? '通过' : '⚠未过'],
  ]
  const head = `| 项 | ${items.map(it => `**${it.slug}**`).join(' | ')} |`
  const sep = `|----|${items.map(() => '----').join('|')}|`
  const body = rows.map(([name, get]) =>
    `| ${name} | ${items.map(it => String(get(it.slug, it.facts || {}, it)).replace(/\|/g, '／').replace(/\n/g, ' ')).join(' | ')} |`).join('\n')
  const dupNote = (dup && dup.length)
    ? `\n\n> ⚠ 实质雷同标注：${dup.map(p => `「${p[0]}」与「${p[1]}」主家具布局相同`).join('；')}——叙事不同不构成两个方案。`
    : ''
  return `## 方案对比\n\n${head}\n${sep}\n${body}${dupNote}\n\n> 请在画布中查看各方案，对照本表点击「采纳」选定；落选方案可自行删除。`
}

// ── prompt builder（薄拼接：只塞 id / 上游 return，不含业务判断）──
const base = `设计区 designZoneId=${designZoneId}。`
function landPrompt(slug, vc){
  return `${base}\n你负责落地变体 slug=${slug}。本变体方向上下文（variantContext，来自多方案概述）：\n${JSON.stringify(vc, null, 2)}\n` +
    `约束力分级：variantAnchorSeed 是唯一硬约束（必须兑现；几何上不成立则走认输路径返回 ok:false，不强行施工）；` +
    `variantDirection / variantNarrative 是方向参考（帮助你决策的 WHY 输入，不是合同条款，其中的描述性语句不得当禁令）；` +
    `variantAvoidance 是反模式提示。其余决策（主家具选墙、是否 L 形、可选家具位置等）由你按房间策略全局判断。\n` +
    `按你的职责完成该变体完整集成落地（注册可见变体 + 按需 zones.json + 施工简报 + 施工 modules.json + 落位自检 + validate + 识图自评 + 自优化 + 自检与优化记录），` +
    `产物写入 ${slug}/ 方案目录，最后按 schema 返回 ok + factsheet。${upstreamSenses}\n\n${zoningSec || ''}\n\n${seqSec || ''}`
}
// 独立 validate 闸门（verify-agent，只报事实，不自判 passed）
function validateGatePrompt(slug){
  return `${base}\n你是落地后置 validate 闸门分身（只报事实，不自判 passed/通过、不决定重试/跳过）。方案 slug=${slug}。\n` +
    `① Glob/Read 解析方案叶子真实路径与叶子 zoneIds：有 schemes/${designZoneId}/${slug}/zones.json → 多叶子，取其声明的叶子集；无 → 单叶子，路径 ${slug}/modules.json、zoneId=${designZoneId}。\n` +
    `② Read 各叶子 modules.json，数其 modules 数组实际长度之和 = fileModuleCount。\n` +
    `③ 调 validate_layout({ zoneIds:[方案叶子 zoneIds], variantId:"${slug}" })——**必须传 variantId**（方案未采纳，缺 variantId 会按 adopted 路径解析到 0 模块，实测误报）；取其解析到的模块数 = validateModuleCount；若返回 E013_INVALID_MODULE_FILE_PATH 则 e013=true。\n` +
    `返回 { fileModuleCount, validateModuleCount, e013, reason:一句话说明 }。最终是否通过由编排层判定，你只给原始数字与 e013。`
}

// ═══════════════ 五段编排 ═══════════════

// Step1 感知：战略定调 + 空间骨架（单分身，schema 双节返回；scribe 后台挂链）
phase('感知')
const perception = await agent(
  `${base}\n原始用户诉求：${userRequest}\n你是 Step1 感知分身，按你的职责完成「战略定调 + 空间骨架」两章，按 schema 返回 strategySec / spaceSec 两节。`,
  { agentType: 'perception-agent', schema: PERCEPTION_SCHEMA, label: 'sense', phase: '感知' })
const strategySec = perception?.strategySec || ''
const spaceSec = perception?.spaceSec || ''
if (!spaceSec) return { ok: false, reason: 'Step1 感知未产出空间骨架' }
scribeChain = scribeChain.then(() => writeSections(parentDesign, [strategySec, spaceSec], '感知', ['用户诉求', '空间骨架']))

// Step2 规划推演：分区思维 ∥ 顺序思维（上游材料直传，免读盘）
phase('规划推演')
const upstreamSenses = `\n\n【上游材料·已附，免读父 DESIGN.md 对应节】\n\n${strategySec}\n\n${spaceSec}`
const [zoningSec, seqSec] = await parallel([
  () => agent(`${base}\n你是 Step2 分区思维分身，承接下方空间骨架产出「方案草稿 · 分区思维」子段并 return。${upstreamSenses}`,
    { agentType: 'zoning-design-agent', label: 'plan:zoning', phase: '规划推演' }),
  () => agent(`${base}\n你是 Step2 顺序思维分身，承接下方空间骨架产出「方案草稿 · 顺序思维」子段并 return。${upstreamSenses}`,
    { agentType: 'sequential-design-agent', label: 'plan:sequential', phase: '规划推演' }),
])
scribeChain = scribeChain.then(() => writeSections(parentDesign, ['## 方案草稿', zoningSec, seqSec], '规划推演', [null, '方案草稿', '方案草稿']))

// Step3 多方案生成：单脑 + N 自适应 + 多样性护栏（不过则有上限重出）
phase('多方案生成')
async function genOverview(retryNote){
  return agent(
    `${base}\n你是 Step3 多方案生成分身，消化下方双草稿组织出 N 个【方向层变体】，返回结构化 overview（含 proposedN）。每变体含：direction（设计方向核心句，禁写具体家具配置）/ narrative（本方向为何值得探索）/ anchorSeed（本变体唯一硬锚点，最多 1 条，三类型与填法见你的提示词）/ avoidance（反模式提示，可选）/ expectedWalls（该方向预期主家具墙面归属，严格"家具:墙名|家具:墙名"格式、禁尺寸/段位修饰/附属件——仅查重用）。**各变体 expectedWalls 必须两两不同**（锚点/叙事再不同，预期落点相同=同一方案，合并报 1 个）；proposedN = 按预期落点去重后实质不同的可行布局数，不足 3 不强凑、不重复。` +
    `${retryNote || ''}` +
    `${upstreamSenses}\n\n${zoningSec || ''}\n\n${seqSec || ''}`,
    { agentType: 'multi-plan-agent', schema: OVERVIEW_SCHEMA, label: 'multiplan', phase: '多方案生成' })
}
// 红线15：相异性护栏必须在【落地集】上校验。先收敛 N → slice → 在 sliced 集上验 anchorSeed 两两不同，
// 否则 variants 数 > N 时护栏在全集通过、落地的前 N 个却可能雷同。
function pickN(ov){ return Math.min(clampN(args?.n || ov?.proposedN), 4) }
function chooseVariants(ov){ return (ov?.variants || []).slice(0, pickN(ov)) }
// 产前护栏：expectedWalls（预期主家具墙面归属）两两不同；预期落点相同=同一方案，不论锚点/叙事如何包装。
// （实测教训：anchorSeed 签名查重失效——"床=西墙"与"北区收纳带"两个不同锚点收敛到同一物理布局。）
// 产后仍由 findDuplicates 比对 factsheet 实际落点兜底（落地可能偏离预期）。
// 【房型中立】只比对归一化不透明签名（去空白/剥括号修饰），不内嵌任何家具/墙名——卧室/卫生间/客厅通用。
function normWalls(s){ return String(s || '').replace(/（[^）]*）|\([^)]*\)/g, '').replace(/\s+/g, '') }
function layoutKey(v){ return normWalls(v.expectedWalls) }
function diverseEnough(vs){ return new Set(vs.map(layoutKey)).size === vs.length }

let overview = await genOverview()
let chosen = chooseVariants(overview)
if (chosen.length > 1 && !diverseEnough(chosen)) {
  log('相异性护栏未过（落地集 expectedWalls 预期布局有雷同），要求去重重出 1 次')
  const re = await genOverview('上一轮入选变体中有 expectedWalls（预期主家具墙面归属）雷同者——锚点/叙事再不同，预期落点相同就是同一方案。请合并雷同者并减少变体数（只报实质不同的布局，不补不重复），或给出预期落点真正不同的方向后重出。')
  const reChosen = chooseVariants(re)
  overview = re; chosen = reChosen                       // 无条件放行第二轮（最新重出版本），保留单次重试边界
  if (!(reChosen.length <= 1 || diverseEnough(reChosen)))
    log('重出后落地集仍有雷同，仍放行第二轮（避免死循环），由产后对比表雷同标注兜底')
}
scribeChain = scribeChain.then(() => writeSections(parentDesign, [overviewBlock({ ...overview, variants: chosen })], '多方案生成'))

// 落地集（= 收敛后 slice，护栏已在其上校验/兜底）
const variants = chosen
const vcOf = {}
for (const v of variants) vcOf[v.slug] = {
  variantDirection: v.direction, variantNarrative: v.narrative,
  variantAnchorSeed: v.anchorSeed, variantAvoidance: v.avoidance,
}
const slugs = variants.map(v => v.slug)
if (!slugs.length) return { ok: false, reason: 'Step3 未产出任何变体' }
log(`N=${slugs.length}；变体：${slugs.join('、')}`)

// Step4 方案落地（集成：施工 + validate + 识图自评 + 自优化）：每方案落地完即跟独立 validate 闸门（pipeline 重叠）
// 上游材料已直传进 landPrompt，placement 不读父 DESIGN.md——scribeChain 无需在此收口。
phase('方案落地')
const landed = await parallel(slugs.map(slug => async () => {
  let r = null
  try {
    r = await agent(landPrompt(slug, vcOf[slug]),
      { agentType: 'placement-agent', schema: PLACEMENT_SCHEMA, label: `land:${slug}`, phase: '方案落地' })
  } catch (e) {
    log(`slug ${slug} 落地异常`)
    return { slug, failed: true }
  }
  if (!r?.ok) { log(`slug ${slug} 落地失败/认输（如实保留，不计入对比表）`); return { slug, failed: true, report: r?.report } }
  // 独立后置 validate 闸门：不信 placement 自报，verify-agent 重跑 validate 比对模块数；布尔在脚本算。
  const gate = await agent(validateGatePrompt(slug),
    { agentType: 'verify-agent', schema: VALIDATE_GATE_SCHEMA, label: `gate:${slug}`, phase: '方案落地' })
  const layer1Pass = !!gate && !gate.e013 && gate.validateModuleCount === gate.fileModuleCount && gate.fileModuleCount > 0
  if (!layer1Pass) log(`slug ${slug} 独立闸门未过（如实标注）：e013=${gate?.e013}, validateCount=${gate?.validateModuleCount}, fileCount=${gate?.fileModuleCount}`)
  return { slug, facts: r.factsheet, layer1Pass }
}))

// Step5 方案对比：脚本机械拼对比表（零 LLM），终选交用户 Web 端
phase('方案对比')
const valid = landed.filter(v => v && !v.failed)
if (!valid.length) return { ok: false, reason: '全部候选落地失败（不强宣成功）' }
const dup = findDuplicates(valid)
if (dup.length) log(`实质雷同标注：${dup.map(p => p.join('≈')).join('、')}`)
scribeChain = scribeChain.then(() => writeSections(parentDesign, [comparisonBlock(valid, chosen, dup)], '方案对比'))
await scribeChain   // 收口全部后台写盘（含前三节与对比表），保证 return 时父 DESIGN.md 完整

return {
  ok: true,
  designZoneId,
  variants: valid.map(v => ({ slug: v.slug, layer1Pass: v.layer1Pass })),
  failed: landed.filter(v => v?.failed).map(v => v.slug),
  duplicates: dup,
  note: '方案已全部可见并通过自评自优化，请引导用户在画布中对照父 DESIGN.md「方案对比」表查看各方案并点击「采纳」终选；落选方案可由用户自行删除。',
}
