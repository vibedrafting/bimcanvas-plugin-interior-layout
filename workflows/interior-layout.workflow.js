export const meta = {
  name: 'interior-layout-scene1',
  description: '场景①：无参考·单分区·最优方案 —— GEN骨架→N候选→多维评审→择优→精修→翻指针',
  phases: [
    { title: 'GEN骨架', detail: 'generator 写父 DESIGN.md 空间骨架 + 分区(subZones/语义功能带/功能分歧自动代决)' },
    { title: '候选生成', detail: 'N 个 generator 并行：战略+简报+落位+Layer1机检' },
    { title: '选拔评审', detail: '每候选×每维度并行 critic → judge 择优' },
    { title: '精修', detail: 'critic+judge 循环（≤精修档，首轮达标即收，不改方向）' },
    { title: '采纳', detail: 'generator Edit 父 DESIGN.md adopted=胜者 + 决策日志' },
  ],
}

// ── args（由 L0 主控喂入；与 BIMCANVAS.md 路由层共享契约）──
const zoneId = args && args.zoneId
if (!zoneId) throw new Error('args.zoneId 必填（场景①：单设计区 id）')
const N = Math.max(1, (args && args.n) || 3)
const refineLevel = (args && args.refineLevel != null) ? args.refineLevel : 1
const userRequest = (args && args.originalUserRequest) || ''
// 评审维度不在编排层硬编码（原则5：领域知识不进 workflow）。优先 args 注入；否则在 GEN 骨架后由 agent 读
// design_evaluation「维度选取参照」+ 房间类型 运行时确定（见下方 dimensions 推导）。
let dimensions = (args && args.dimensions && args.dimensions.length) ? args.dimensions : null

// ── 结构化输出 schema（critic/judge 出 schema，代码按字段分支）──
const CRITIC_SCHEMA = {
  type: 'object',
  required: ['dimension', 'score', 'layer1Fail', 'directionRespecting', 'findings'],
  properties: {
    dimension: { type: 'string' },
    score: { type: 'number', description: '0–100；Layer1 不过记不及格' },
    layer1Fail: { type: 'boolean', description: '工程合规（几何/通行/功能完整）是否有硬伤' },
    directionRespecting: { type: 'boolean', description: '本维度改进建议整体是否在方案既定方向内' },
    findings: { type: 'array', items: { type: 'string' }, description: '接地的问题/亮点，带证据' },
    suggestions: { type: 'array', items: { type: 'string' }, description: '改进建议（注明 strategy/placement 级）' },
  },
}
const JUDGE_SELECT_SCHEMA = {
  type: 'object',
  required: ['winner', 'rankedSlugs', 'rationale'],
  properties: {
    winner: { type: 'string' },
    rankedSlugs: { type: 'array', items: { type: 'object', required: ['slug', 'score'], properties: { slug: { type: 'string' }, score: { type: 'number' } } } },
    rationale: { type: 'string' },
  },
}
const JUDGE_REFINE_SCHEMA = {
  type: 'object',
  required: ['passed'],
  properties: {
    passed: { type: 'boolean' },
    rootCause: { type: 'string', enum: ['strategy', 'placement', 'none'] },
    reviseInstruction: { type: 'string' },
    failedDimensions: { type: 'array', items: { type: 'string' } },
  },
}

const dir = (zoneId, slug) => `schemes/${zoneId}/${slug}`

// ── 1. GEN 骨架（单脑，写父 {zoneId}/DESIGN.md）──
phase('GEN骨架')
await agent(
  `任务=skeleton。为设计区 ${zoneId} 分析当前户型（边界/门窗/通道/禁区，可调 get_zone_boundaries），` +
  `按 generator.md skeleton 规程产出**与设计方向无关、所有候选共享**的客观事实层：` +
  `① 写 schemes/${zoneId}/DESIGN.md 的「## 空间骨架（客观几何·冻结）」节；` +
  `② 分区(zoning)：空间预演判定是否需物理分割→需则写 schemes/zones.json 的 subZones 并对新子 zone 取 passage→无论是否分割都输出语义功能带→功能方案分歧以 [自动代决] 显式记录。` +
  `不放家具、不写战略（战略/简报/落位是 candidate 步的事）。原始诉求：${userRequest}`,
  { agentType: 'generator', label: `skeleton:${zoneId}`, phase: 'GEN骨架' }
)

// ── 评审维度运行时确定（原则5：维度知识在 design_evaluation 知识层，不在编排层）──
if (!dimensions) {
  const dimSel = await agent(
    `任务=选维度。读项目 references/design_evaluation.md 的「维度选取参照」表与设计区 ${zoneId} 的房间类型，返回该区适用的设计品质评审维度名数组（取自五维框架，2–5 个）。`,
    { agentType: 'critic', label: 'select-dims', phase: 'GEN骨架', schema: { type: 'object', required: ['dimensions'], properties: { dimensions: { type: 'array', items: { type: 'string' }, minItems: 1 } } } }
  )
  dimensions = (dimSel && dimSel.dimensions && dimSel.dimensions.length) ? dimSel.dimensions : null
}
if (!dimensions || !dimensions.length) throw new Error('未能确定评审维度（design_evaluation 维度选取失败）')
log(`评审维度：${dimensions.join('、')}`)

// ── 2. 生成 N 候选（并行，各自落位 + Layer1 机检）──
phase('候选生成')
// 候选默认 _ 前缀隐藏（§3.4）：建目录即隐藏（Web 不主动显示），胜者在采纳步去 _ 转正
const slugs = Array.from({ length: N }, (_, i) => `_cand-${String.fromCharCode(97 + i)}`)
await parallel(slugs.map((slug, i) => () =>
  agent(
    `任务=candidate。设计区 ${zoneId}，方案 slug=${slug}（多候选探索第 ${i + 1}/${N} 个，请采取与其它候选明显不同的合理设计方向/锚点）。` +
    `读时叠加父骨架与项目配置，写 ${dir(zoneId, slug)}/DESIGN.md 的「## 战略」(含 design_evaluation 五维设计目标)与「## 施工简报」节；` +
    `据简报落位 ${dir(zoneId, slug)}/[{leaf}/]modules.json（家具尺寸取自 module_library），每次写后 validate_layout 直到 Layer1 通过。` +
    `原始诉求：${userRequest}`,
    { agentType: 'generator', label: `candidate:${slug}`, phase: '候选生成' }
  )
))

// ── 3. 选拔评审（N>1）：每候选 × 每维度并行 critic → judge 择优 ──
let winner = slugs[0]
if (N > 1) {
  phase('选拔评审')
  const candReviews = await parallel(slugs.map(slug => () =>
    parallel(dimensions.map(dim => () =>
      agent(
        `任务=评审。维度=${dim}。对 ${dir(zoneId, slug)} 打分：读叠 DESIGN.md、Read modules、request_background_screenshot 看真实截图、按 design_evaluation 该维度判据 + module_library 三级规则。`,
        { agentType: 'critic', label: `critic:${slug}:${dim}`, phase: '选拔评审', schema: CRITIC_SCHEMA }
      )
    )).then(reviews => ({ slug, reviews: reviews.filter(Boolean) }))
  ))
  const verdict = await agent(
    `任务=selection。按 judge.md 的选拔规则，从以下各候选的多维评审中裁决最优：\n${JSON.stringify(candReviews.filter(Boolean), null, 2)}`,
    { agentType: 'judge', label: 'judge:select', phase: '选拔评审', schema: JUDGE_SELECT_SCHEMA }
  )
  if (verdict && verdict.winner) winner = verdict.winner
  log(`选拔胜者：${winner}${verdict ? '（' + verdict.rationale + '）' : ''}`)
} else {
  log('N=1，跳过选拔评审')
}

// ── 4. 精修 winner（loop ≤ refineLevel，首轮达标即收，不改方向）──
phase('精修')
for (let round = 0; round < refineLevel; round++) {
  const reviews = (await parallel(dimensions.map(dim => () =>
    agent(
      `任务=评审。维度=${dim}。对 ${dir(zoneId, winner)} 打分（看真实截图+modules+DESIGN.md）。`,
      { agentType: 'critic', label: `refine-critic:${winner}:${dim}:r${round + 1}`, phase: '精修', schema: CRITIC_SCHEMA }
    )
  ))).filter(Boolean)

  const j = await agent(
    `任务=refine。按 judge.md 的精修规则，判定 ${dir(zoneId, winner)} 是否达标并给出修订判决：\n${JSON.stringify(reviews, null, 2)}`,
    { agentType: 'judge', label: `judge:refine:r${round + 1}`, phase: '精修', schema: JUDGE_REFINE_SCHEMA }
  )
  if (!j || j.passed) { log(`精修第 ${round + 1} 轮达标即收`); break }

  await agent(
    `任务=refine。方案 ${dir(zoneId, winner)}。根因=${j.rootCause}；修订指令：${j.reviseInstruction}。只在既定方向内打磨；` +
    `strategy→Edit 战略/简报节再改 modules，placement→只调坐标/尺寸/朝向；改后重验失分维度：${(j.failedDimensions || []).join('、')}。`,
    { agentType: 'generator', label: `refine-gen:${winner}:r${round + 1}`, phase: '精修' }
  )
}

// ── 5. 采纳：转正（去 _ 前缀）+ 翻指针（agent 执行，脚本无文件系统权限）──
phase('采纳')
const winnerVisible = winner.startsWith('_') ? winner.slice(1) : winner
await agent(
  `任务=adopt（采纳收尾：转正 + 翻指针）。胜者候选=${winner}（_ 前缀隐藏）。\n` +
  `1) 转正：用 Bash 去 _ 前缀使胜者在 Web 可见：mv "schemes/${zoneId}/${winner}" "schemes/${zoneId}/${winnerVisible}"（目标已存在则停下报错、勿覆盖）。\n` +
  `2) 翻指针：Edit schemes/${zoneId}/DESIGN.md frontmatter 设 adopted: ${winnerVisible}（无则新增），不动正文其它节。\n` +
  `3) 决策日志：正文追加/更新「## 决策日志」一条：场景①自动择优，胜者=${winnerVisible}（候选 ${winner} 转正），评审维度=${dimensions.join('、')}，精修档=${refineLevel}。\n` +
  `落选候选保持 _ 前缀隐藏、不动。`,
  { agentType: 'generator', label: `adopt:${winnerVisible}`, phase: '采纳' }
)

return { scenario: 'single-zone-optimal', zoneId, winner: winnerVisible, hiddenCandidates: slugs.filter(s => s !== winner), dimensions, refineLevel }
