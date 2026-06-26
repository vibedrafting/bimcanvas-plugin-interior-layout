export const meta = {
  name: 'interior-layout-fanout',
  description: '场景①扇出段（主控为脑）：接收主控产出的 variants[] + 上游材料，并行落地 N 方案（施工+识图自评+自优化）+ 独立 validate 闸门，机械拼对比表 return 给主控；本脚本不写盘、不做设计判断。',
  phases: [
    { title: '方案落地', detail: 'N 路并行集成落地：施工简报 + 施工 + validate + 识图自评 + 自优化；每方案独立闸门核验' },
    { title: '方案对比', detail: '脚本机械拼方案对比表（含实质雷同标注），return 给主控写父、交用户 Web 端终选' },
  ],
}

// ── 入口 args 契约（主控为脑：感知/规划/多方案在主控单上下文已完成，此处只做并行扇出）──
// 健壮性归一：部分模型/代理把 Workflow 的 args 序列化成 JSON 字符串而非对象（实测 deepseek 多次
// 秒失败 "args.designZoneId 必填"）。字符串则 JSON.parse 兜底，并 log 告警让契约漂移可见、不静默掩盖。
const a = (typeof args === 'string')
  ? (log('⚠ args 以字符串到达，已 JSON.parse 归一（producer 未按对象传参）'), JSON.parse(args))
  : (args || {})
const designZoneId = a.designZoneId                     // 必填，单段或多段 path，如 rz_3 或 rz_6/dz_1
if (!designZoneId) throw new Error('args.designZoneId 必填')
const variantsIn = Array.isArray(a.variants) ? a.variants : []
if (!variantsIn.length) throw new Error('args.variants 必填（主控产出的方向层变体集，非空）')
// 主控已做 N 自适应 + 上限 4；此处防御性 slice，避免 runaway 扇出
const variants = variantsIn.slice(0, 4)
// 上游材料由主控直传，供 placement 读，免读父 DESIGN.md
const strategySec = a.strategySec || ''
const spaceSec    = a.spaceSec || ''
const zoningSec   = a.zoningSec || ''
const seqSec      = a.seqSec || ''

// ── 结构化输出 schema ───────────────────────────────────────────
// 【契约·三处同名钉死】Step4 返回字段须与 ① 本 PLACEMENT_SCHEMA ② placement-procedure skill「Step H」③ comparisonBlock 读取一致。
const PLACEMENT_SCHEMA = {  // 集成落地返回：结果 + 对比表数据（脚本只排版/比对，不解释内容——房型中立）
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
// 编排层确定性后置核验：verify-agent 只报事实，控制流布尔在脚本算
const VALIDATE_GATE_SCHEMA = {
  type: 'object', required: ['fileModuleCount', 'validateModuleCount', 'e013'],
  properties: {
    fileModuleCount: { type: 'number' },               // 方案叶子 modules.json 实际模块数（读文件数）
    validateModuleCount: { type: 'number' },           // validate_layout 解析到的模块数
    e013: { type: 'boolean' },                          // 是否报 E013_INVALID_MODULE_FILE_PATH（路径错）
    reason: { type: 'string' },
  },
}

// ── 对比表 helper（纯机械排版 + 不透明字符串雷同比对，零业务判断——房型中立）──
// wallsKey 复用 normWalls 归一化（剥括号修饰/空白）——实测教训："西墙南段(3600mm)"与"西墙(全段4850mm)"
// 这类自由文本修饰让精确比对失效；格式主防在 placement 侧（严格"家具:墙名"），此处归一化兜底。
function normWalls(s){ return String(s || '').replace(/（[^）]*）|\([^)]*\)/g, '').replace(/\s+/g, '') }
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

// ── prompt builder（薄拼接：只塞 id / 上游材料，不含业务判断）──
const base = `设计区 designZoneId=${designZoneId}。`
const upstreamSenses = `\n\n【上游材料·主控直传，免读父 DESIGN.md 对应节】\n\n${strategySec}\n\n${spaceSec}`
function landPrompt(slug, vc){
  return `${base}\n你负责落地变体 slug=${slug}。本变体方向上下文（variantContext，来自主控多方案概述）：\n${JSON.stringify(vc, null, 2)}\n` +
    `约束力分级：variantAnchorSeed 是唯一硬约束（必须兑现；几何上不成立则走认输路径返回 ok:false，不强行施工）；` +
    `variantDirection / variantNarrative 是方向参考（帮助你决策的 WHY 输入，不是合同条款，其中的描述性语句不得当禁令）；` +
    `variantAvoidance 是反模式提示。其余决策（主家具选墙、是否 L 形、可选家具位置等）由你按房间策略全局判断。\n` +
    `按 placement-procedure 完成该变体完整集成落地（注册可见变体 + 按需 zones.json + 施工简报 + 施工 modules.json + 落位自检 + validate + 识图自评 + 自优化 + 自检与优化记录），` +
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

// 变体短名 → placement 用长名（vcOf 映射，勿在 placement 侧改回长名）
const vcOf = {}
for (const v of variants) vcOf[v.slug] = {
  variantDirection: v.direction, variantNarrative: v.narrative,
  variantAnchorSeed: v.anchorSeed, variantAvoidance: v.avoidance,
}
const slugs = variants.map(v => v.slug)
log(`扇出 N=${slugs.length}；变体：${slugs.join('、')}`)

// ═══════════════ 扇出两段 ═══════════════

// Step4 方案落地（集成：施工 + validate + 识图自评 + 自优化）：每方案落地完即跟独立 validate 闸门
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

// Step5 方案对比：脚本机械拼对比表（零 LLM），return 给主控写父，终选交用户 Web 端
phase('方案对比')
const valid = landed.filter(v => v && !v.failed)
if (!valid.length) return { ok: false, reason: '全部候选落地失败（不强宣成功）' }
const dup = findDuplicates(valid)
if (dup.length) log(`实质雷同标注：${dup.map(p => p.join('≈')).join('、')}`)

return {
  ok: true,
  designZoneId,
  variants: valid.map(v => ({ slug: v.slug, layer1Pass: v.layer1Pass })),
  failed: landed.filter(v => v?.failed).map(v => v.slug),
  duplicates: dup,
  comparisonTableMd: comparisonBlock(valid, variants, dup),   // 对比表 markdown，return 给主控写父「方案对比」节
  note: '方案已全部可见并通过自评自优化。请主控按 design-doc-upsert 把 comparisonTableMd 写入父 DESIGN.md「方案对比」节，并引导用户在画布中对照各方案点击「采纳」终选；落选方案可由用户自行删除。',
}
