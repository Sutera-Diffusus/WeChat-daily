"""Generate a body-free, offline user-profile calibration prototype."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Mapping


VALUE_DIMENSIONS = (
    ("action", "行动 / 决策"),
    ("risk", "风险与机会"),
    ("project", "项目相关"),
    ("people", "重要人物"),
    ("knowledge", "知识增量"),
    ("timeliness", "时效"),
    ("credibility", "可信度"),
    ("fun", "轻松有趣"),
    ("cost", "阅读成本"),
)

ALGORITHM_VERSION = "user-profile-calibration-v3"
SAMPLE_DATE = "2026-08-25"

_DIMENSION_KEYWORDS = {
    "action": ("决定", "选择", "计划", "操作", "购买", "注册", "退课", "方案", "建议", "安排"),
    "risk": ("风险", "封号", "限制", "故障", "问题", "失败", "机会", "安全", "损失"),
    "project": ("项目", "开发", "代码", "编程", "工具", "模型", "API", "课程", "研究"),
    "people": ("同学", "老师", "朋友", "师兄", "师姐", "导师", "家人", "客户", "同事", "联系人"),
    "knowledge": ("分析", "解释", "原因", "观点", "知识", "讨论", "区别", "体验"),
    "timeliness": ("今天", "现在", "尽快", "时间", "最新", "截止", "小时", "近期"),
    "credibility": ("核验", "证据", "是否", "可能", "猜测", "未知", "未明确"),
    "fun": ("有趣", "玩笑", "调侃", "梗", "娱乐", "哈哈"),
    "cost": ("简短", "零散", "复杂", "流程", "长", "阅读"),
}

_TOPIC_FAMILY_KEYWORDS = (
    ("education", ("选课", "课程", "学分", "学院")),
    ("account_access", ("注册", "登录", "账号", "权限", "密码", "验证", "邮件")),
    ("subscription_quota", ("订阅", "会员", "额度", "限额", "价格", "成本", "续费", "到期", "充值", "重置", "plus", "美元", "高消耗")),
    ("performance", ("缓慢", "速度", "性能", "耐用", "算力", "tps", "风扇")),
    ("development", ("开发", "自研", "agent", "代码", "开源", "上线", "配置", "约束", "工具")),
    ("comparison", ("优于", "对比", "体验", "模型", "codex", "kimi", "grok", "claude")),
    ("design", ("时间线", "鱼骨图", "圆桌", "展示")),
)

_IMPORTANCE_RANK = {"high": 3, "medium": 2, "low": 1, "unknown": 0}


def _rows(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, list):
        for row in value:
            if isinstance(row, Mapping):
                yield row


def _infer_dimensions(text: str) -> list[str]:
    found = [key for key, words in _DIMENSION_KEYWORDS.items() if any(word.lower() in text.lower() for word in words)]
    return found[:4] or ["knowledge"]


def _topic_family(text: str) -> str:
    lowered = text.lower()
    for family, words in _TOPIC_FAMILY_KEYWORDS:
        if any(word in lowered for word in words):
            return family
    return "other"


def _bigrams(text: str) -> set[str]:
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", text.lower())
    return {normalized[index : index + 2] for index in range(max(0, len(normalized) - 1))}


def _similarity(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    a = _bigrams(f"{left['title']} {left['summary']}")
    b = _bigrams(f"{right['title']} {right['summary']}")
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def _representative_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Thin dominant topic families while preserving diverse, higher-value examples."""
    unique_cards: list[dict[str, Any]] = []
    signatures: set[str] = set()
    for card in cards:
        signature = re.sub(r"\s+", "", f"{card['title']}|{card['summary']}".lower())
        if signature in signatures:
            continue
        signatures.add(signature)
        unique_cards.append(card)

    groups: dict[str, list[dict[str, Any]]] = {}
    for card in unique_cards:
        groups.setdefault(str(card["topic_family"]), []).append(card)

    selected_ids: set[str] = set()
    for rows in groups.values():
        limit = min(len(rows), math.ceil(math.sqrt(len(rows))) + (1 if len(rows) > 1 else 0))
        remaining = list(rows)
        chosen: list[dict[str, Any]] = []
        while remaining and len(chosen) < limit:
            best = max(
                remaining,
                key=lambda row: (
                    _IMPORTANCE_RANK.get(str(row["importance"]), 0) * 2
                    - (max((_similarity(row, prior) for prior in chosen), default=0.0) * 3),
                    -int(row["source_order"]),
                ),
            )
            chosen.append(best)
            remaining.remove(best)
        selected_ids.update(str(row["card_id"]) for row in chosen)
    return [card for card in cards if str(card["card_id"]) in selected_ids]


def build_sample_manifest(reconstruction: Mapping[str, Any], source_name: str) -> dict[str, Any]:
    """Project V7 semantic content lines into a body-free calibration manifest."""
    review = reconstruction.get("review")
    if not isinstance(review, Mapping):
        raise ValueError("reconstruction.review is missing")

    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for unit in _rows(review.get("sample_units")):
        extraction = unit.get("content_line_extraction")
        if not isinstance(extraction, Mapping) or extraction.get("status") != "complete":
            continue
        for line in _rows(unit.get("content_line_candidates")):
            title = str(line.get("title") or "").strip()
            summary = str(line.get("summary_candidate") or "").strip()
            line_ref = str(line.get("content_line_ref") or "").strip()
            if not title or not line_ref or line_ref in seen:
                continue
            seen.add(line_ref)
            text = f"{title} {summary}"
            cards.append(
                {
                    "card_id": line_ref,
                    "unit_ref": str(unit.get("unit_ref") or ""),
                    "chat_ref": str(unit.get("chat_ref") or ""),
                    "title": title,
                    "summary": summary,
                    "date": SAMPLE_DATE,
                    "importance": str(line.get("importance_candidate") or "unknown"),
                    "claim_type": str(line.get("claim_type") or "unknown"),
                    "value_dimension_candidates": _infer_dimensions(text),
                    "topic_family": _topic_family(text),
                    "source_order": len(cards),
                }
            )

    if len(cards) < 2:
        raise ValueError("at least two semantic content-line cards are required")
    source_card_count = len(cards)
    cards = _representative_cards(cards)
    for card in cards:
        card.pop("source_order", None)
    dimension_card_counts = {
        key: sum(key in card["value_dimension_candidates"] for card in cards) for key, _ in VALUE_DIMENSIONS
    }
    sample_id = "|".join(
        (ALGORITHM_VERSION, source_name, SAMPLE_DATE, str(len(cards)), cards[0]["card_id"], cards[-1]["card_id"])
    )
    return {
        "schema_version": "user_profile_calibration_samples_v1",
        "algorithm_version": ALGORITHM_VERSION,
        "body_free": True,
        "contains_original_chat_body": False,
        "source": source_name,
        "date_coverage": [SAMPLE_DATE],
        "coverage_notice": f"当前样本仅来自 {SAMPLE_DATE}，结果只能视为初步画像。",
        "sample_id": sample_id,
        "dimensions": [{"key": key, "label": label} for key, label in VALUE_DIMENSIONS],
        "dimension_card_counts": dimension_card_counts,
        "source_card_count": source_card_count,
        "collapsed_card_count": source_card_count - len(cards),
        "sample_count": len(cards),
        "card_count": len(cards),
        "maximum_comparisons": len(cards) // 2,
        "cards": cards,
    }


def _html(manifest: Mapping[str, Any]) -> str:
    payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    payload = payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return _HTML_TEMPLATE.replace("__MANIFEST_JSON__", payload)


def generate_calibration_site(source: Path, output_dir: Path) -> dict[str, Path]:
    reconstruction = json.loads(source.read_text(encoding="utf-8"))
    manifest = build_sample_manifest(reconstruction, source.as_posix())
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "sample_manifest.json"
    index_path = output_dir / "index.html"
    readme_path = output_dir / "README.md"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    index_path.write_text(_html(manifest), encoding="utf-8")
    readme_path.write_text(
        _README.format(
            source_card_count=manifest["source_card_count"],
            card_count=manifest["card_count"],
            max_comparisons=manifest["maximum_comparisons"],
        ),
        encoding="utf-8",
    )
    return {"index": index_path, "manifest": manifest_path, "readme": readme_path}


_README = """# 低摩擦用户画像校准原型

直接双击 `index.html`。页面离线运行，选择记录和画像只保存在当前浏览器的 `localStorage`，不会联网。

## 样本边界

- 上游共有 {source_card_count} 张内容线卡片；按主题代表性压缩后，本轮使用 {card_count} 张。
- 样本只来自 V7 的 `reconstruction.json`，日期仅为 **2026-08-25**。
- 卡片只包含语义标题、摘要、importance、claim_type、日期与卡片引用，不包含原始聊天正文或密钥；语义摘要可能保留上游产物中的姓名或匿名标识。
- 当前结果属于单日初步画像，不能视为长期偏好或跨日期验证。

## 交互与画像规则

- 每张卡在一轮中最多出现一次，因此本批样本最多比较 {max_comparisons} 组；证据提前充分时会更早结束。
- 系统优先补足证据较少的价值维度，并避开同一交流段及文字高度相近的配对。
- 支持偏向左侧、偏向右侧、都重要、都不重要、无法判断和跳过。
- 原因标签属于显式选择；卡片维度带来的权重来自明确取舍；证据不足时显示未知。
- 推断采用保守门槛：同一维度至少需要三次相关选择、方向强度达到阈值且一致性不低于 65%；置信度只显示低或中。
- 用户可纠正、停用或删除每条结论，也可撤销上一题、重置和导出 JSON。
- 点击和停留时间不参与画像。

## 重新生成

```powershell
python -m wechat_bridge.user_profile_calibration
```
"""


_HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'self' 'unsafe-inline' data:; connect-src 'none'; img-src 'self' data:">
<title>低摩擦用户画像校准</title>
<style>
:root{color-scheme:light;--ink:#18201d;--muted:#66736d;--paper:#f5f3ec;--card:#fff;--line:#d8ddd8;--green:#1e6750;--pale:#e8f2ed;--warn:#8b5b18}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif}.wrap{max-width:1120px;margin:auto;padding:28px 18px 60px}h1,h2,h3{line-height:1.25}.hero,.panel{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px;margin-bottom:16px}.hero h1{margin:0 0 8px}.notice{color:var(--warn);font-weight:650}.meta,.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.choice{border:1px solid var(--line);border-radius:13px;padding:18px;min-height:220px;background:#fff}.choice h3{margin-top:0}.pill{display:inline-block;padding:3px 8px;border-radius:99px;background:#eef1ee;color:#52605a;margin:2px;font-size:12px}.actions,.toolbar,.reasons{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}button,select{border:1px solid #b9c4be;border-radius:9px;background:#fff;padding:9px 12px;color:var(--ink);cursor:pointer}button.primary{background:var(--green);color:#fff;border-color:var(--green)}button:hover{border-color:var(--green)}label.reason{border:1px solid var(--line);border-radius:99px;padding:5px 9px;background:#fafafa}.profile-row{border-top:1px solid var(--line);padding:14px 0}.profile-head{display:flex;justify-content:space-between;gap:12px}.confidence{font-variant-numeric:tabular-nums}.source-explicit{color:var(--green)}.source-inferred{color:#315c83}.source-unknown{color:var(--muted)}.evidence{font-size:13px;color:var(--muted);word-break:break-all}.empty{padding:20px;background:#fafafa;border-radius:10px;color:var(--muted)}@media(max-width:760px){.grid{grid-template-columns:1fr}.choice{min-height:0}.wrap{padding:12px}.hero,.panel{padding:16px}}
</style></head><body><main class="wrap">
<section class="hero"><h1>低摩擦用户画像校准</h1><p>每次比较两条内容。没有合适答案可以跳过，原因也可以不选。</p><p><strong>这一轮最多比较 <span id="maximum"></span> 组，每张卡只出现一次；证据充分时会提前结束。</strong></p><p class="notice">当前样本仅来自 2026-08-25，形成的画像只适用于这批内容附近的判断。</p><p class="meta">全程离线；数据只保存在这个浏览器中。点击和停留时间不会参与判断。</p></section>
<section class="panel" id="question"><div class="profile-head"><h2>这两条内容，哪条更值得你看？</h2><span class="muted" id="progress"></span></div><div class="grid"><article class="choice" id="left"></article><article class="choice" id="right"></article></div>
<div class="actions"><button class="primary" data-outcome="left">左边更重要</button><button class="primary" data-outcome="right">右边更重要</button><button data-outcome="both">都重要</button><button data-outcome="neither">都不重要</button><button data-outcome="unknown">无法判断</button><button data-outcome="skip">跳过</button></div>
<details><summary>可选：这次取舍主要考虑什么</summary><div class="reasons" id="reasons"></div></details></section>
<section class="panel"><div class="profile-head"><h2>初步画像</h2><span class="muted" id="readiness"></span></div><p class="muted">所有变化都只来自你的明确选择：原因标签是直接证据，内容取舍是候选维度证据；证据不足时保留未知。任何结论都可以停用或删除。</p><div id="profile"></div></section>
<section class="panel toolbar"><button id="undo">撤销上一题</button><button id="reset">重置</button><button id="export">导出 JSON</button><span class="muted" id="saved"></span></section>
</main><script id="manifest" type="application/json">__MANIFEST_JSON__</script><script>
const manifest=JSON.parse(document.getElementById('manifest').textContent);const KEY='wechat-user-profile-calibration-v3',LEGACY_KEYS=['wechat-user-profile-calibration-v2','wechat-user-profile-calibration-v1'];
const labels=Object.fromEntries(manifest.dimensions.map(x=>[x.key,x.label]));
let loadNotice='',state=load(),pair=null;
function fresh(){return{version:3,sampleId:manifest.sample_id,answers:[],disabled:{},deleted:{},createdAt:new Date().toISOString()}}
function load(){try{let raw=localStorage.getItem(KEY),legacy=!raw&&LEGACY_KEYS.map(k=>localStorage.getItem(k)).find(Boolean);if(!raw&&!legacy)return fresh();let parsed=JSON.parse(raw||legacy);if(parsed.version!==3||parsed.sampleId!==manifest.sample_id){loadNotice='样本或算法已经更新，旧记录未混入本轮，已开始新的校准。';return fresh()}return Object.assign(fresh(),parsed)}catch(e){loadNotice='旧记录无法读取，已开始新的校准。';return fresh()}}
function save(){state.updatedAt=new Date().toISOString();localStorage.setItem(KEY,JSON.stringify(state));document.getElementById('saved').textContent='已保存到本地浏览器'}
function stats(){let exposure={},reason={},sys={};manifest.dimensions.forEach(d=>{reason[d.key]={pos:0,neg:0,e:[]};sys[d.key]={score:0,n:0,pos:0,neg:0,e:[]}});state.answers.forEach(a=>{[a.left,a.right].forEach(id=>exposure[id]=(exposure[id]||0)+1);let chosen=[],rejected=[];if(a.outcome==='left'){chosen=[a.left];rejected=[a.right]}if(a.outcome==='right'){chosen=[a.right];rejected=[a.left]}if(a.outcome==='both')chosen=[a.left,a.right];if(a.outcome==='neither')rejected=[a.left,a.right];(a.reasons||[]).forEach(k=>{if(chosen.length){reason[k].pos++;reason[k].e.push(a.id)}if(rejected.length&&!chosen.length){reason[k].neg++;reason[k].e.push(a.id)}});chosen.forEach(id=>card(id).value_dimension_candidates.forEach(k=>{sys[k].score+=a.outcome==='both'?.4:.6;sys[k].n++;sys[k].pos++;sys[k].e.push(a.id)}));rejected.forEach(id=>card(id).value_dimension_candidates.forEach(k=>{sys[k].score-=a.outcome==='neither'?.4:.35;sys[k].n++;sys[k].neg++;sys[k].e.push(a.id)}))});return{exposure,reason,sys}}
function card(id){return manifest.cards.find(c=>c.card_id===id)}
function usedPair(a,b){return state.answers.some(x=>(x.left===a&&x.right===b)||(x.left===b&&x.right===a))}
function titleOverlap(a,b){let grams=x=>{x=String(x).toLowerCase().replace(/[^a-z0-9\u4e00-\u9fff]/g,'');let s=new Set();for(let i=0;i<x.length-1;i++)s.add(x.slice(i,i+2));return s},x=grams(a),y=grams(b);if(!x.size||!y.size)return 0;let common=[...x].filter(k=>y.has(k)).length;return common/Math.min(x.size,y.size)}
function coverageGaps(){let tested=Object.fromEntries(manifest.dimensions.map(d=>[d.key,0]));state.answers.forEach(a=>{let dims=new Set([...card(a.left).value_dimension_candidates,...card(a.right).value_dimension_candidates]);dims.forEach(k=>tested[k]++)});return new Set(manifest.dimensions.filter(d=>tested[d.key]<Math.min(3,manifest.dimension_card_counts[d.key]||0)).map(d=>d.key))}
function nextPair(){const s=stats(),cards=manifest.cards,unresolved=coverageGaps();if(!unresolved.size)return null;let best=null,bestScore=-1e9;for(let i=0;i<cards.length;i++)for(let j=i+1;j<cards.length;j++){let a=cards[i],b=cards[j];if((s.exposure[a.card_id]||0)>0||(s.exposure[b.card_id]||0)>0)continue;if(usedPair(a.card_id,b.card_id))continue;let union=[...new Set([...a.value_dimension_candidates,...b.value_dimension_candidates])],open=union.filter(k=>unresolved.has(k));if(!open.length)continue;let gap=open.reduce((n,k)=>n+1/Math.max(1,manifest.dimension_card_counts[k]||1),0);let diff=open.filter(k=>a.value_dimension_candidates.includes(k)!==b.value_dimension_candidates.includes(k)).length;let score=gap*8+diff*1.5-(a.unit_ref===b.unit_ref?4:0)-titleOverlap(a.title,b.title)*6;if(score>bestScore){bestScore=score;best=[a,b]}}return best}
function renderCard(el,c){el.innerHTML=`<h3>${esc(c.title)}</h3><p>${esc(c.summary||'当前只有标题级语义信息。')}</p><p>${c.value_dimension_candidates.map(k=>`<span class="pill">${esc(labels[k])}</span>`).join('')}</p><p class="meta">${esc(c.date)} · importance: ${esc(c.importance)} · claim_type: ${esc(c.claim_type)} · 卡片引用 ${esc(c.card_id)}</p>`}
function renderQuestion(){pair=nextPair();if(!pair){let complete=!coverageGaps().size,msg=complete?'可覆盖的价值维度已经完成校准。':'剩余内容不能增加新的有效信息，已提前停止。';document.getElementById('question').innerHTML=`<h2>本轮校准已完成</h2><p class="muted">${msg} 你可以查看画像或导出 JSON。</p>`;return}renderCard(document.getElementById('left'),pair[0]);renderCard(document.getElementById('right'),pair[1]);document.getElementById('progress').textContent=`已完成 ${state.answers.length} / 最多 ${manifest.maximum_comparisons} 组，可随时停止`;document.querySelectorAll('#reasons input').forEach(x=>x.checked=false)}
function profileFor(k,s){let r=s.reason[k],explicit=r.pos+r.neg;if(explicit){let dir=r.pos>=r.neg?'你可能会把它作为加分理由':'你可能会把它作为降低优先级的理由';return{source:'原因选择',sourceClass:'explicit',text:explicit<2?'有一条明确选择与此有关，暂不概括稳定倾向':dir,confidence:explicit>=3?'中':'低',count:explicit,e:[...new Set(r.e)]}}let x=s.sys[k],cons=x.n?Math.max(x.pos,x.neg)/x.n:0;if(x.n>=3&&Math.abs(x.score)>=1.2&&cons>=.65)return{source:'取舍归纳',sourceClass:'inferred',text:x.score>0?'在这批内容中，你可能更愿意优先看涉及这一维度的卡片':'在这批内容中，这一维度暂未提高你的阅读优先级',confidence:x.n>=6&&cons>=.75?'中':'低',count:x.n,e:[...new Set(x.e)]};return{source:'证据不足',sourceClass:'unknown',text:'目前不概括这一维度的偏好',confidence:'低',count:x.n,e:[...new Set(x.e)]}}
function renderProfile(){let s=stats(),html='';manifest.dimensions.forEach(d=>{if(state.deleted[d.key])return;let p=profileFor(d.key,s),off=!!state.disabled[d.key];html+=`<div class="profile-row" data-dim="${d.key}"><div class="profile-head"><strong>${esc(d.label)}</strong><span class="source-${p.sourceClass}">${esc(p.source)} · 置信度：${esc(p.confidence)}</span></div><p>${off?'此结论已停用':esc(p.text)}</p><p class="evidence">证据次数：${p.count}；适用范围：仅 2026-08-25 内容样本；选择记录引用：${p.e.length?p.e.slice(-6).join('、'):'无'}</p><div class="toolbar"><button data-disable="${d.key}">${off?'启用':'停用'}</button><button data-delete="${d.key}">删除</button></div></div>`});document.getElementById('profile').innerHTML=html||'<div class="empty">画像结论均已删除。重置后可恢复。</div>';let covered=manifest.dimensions.filter(d=>profileFor(d.key,s).source!=='证据不足').length;let uncertain=manifest.dimensions.length-covered;document.getElementById('readiness').textContent=uncertain<=3?'多数维度已有初步证据，仍只代表单日样本':'仍有较多未知，可继续比较，也可以现在结束';bindProfile()}
function bindProfile(){document.querySelectorAll('[data-disable]').forEach(b=>b.onclick=()=>{let k=b.dataset.disable;state.disabled[k]=!state.disabled[k];save();renderProfile()});document.querySelectorAll('[data-delete]').forEach(b=>b.onclick=()=>{state.deleted[b.dataset.delete]=true;save();renderProfile()})}
function answer(outcome){let reasons=[...document.querySelectorAll('#reasons input:checked')].map(x=>x.value),signal=['skip','unknown'].includes(outcome)?'explicit_non_preference':'explicit_choice';state.answers.push({id:'choice-'+String(state.answers.length+1).padStart(3,'0'),left:pair[0].card_id,right:pair[1].card_id,outcome,reasons,at:new Date().toISOString(),signal});save();renderQuestion();renderProfile()}
function exportData(){let s=stats(),profile={},conclusion_status={};manifest.dimensions.forEach(d=>{let deleted=!!state.deleted[d.key],disabled=!!state.disabled[d.key];conclusion_status[d.key]={disabled,deleted};if(!deleted)profile[d.key]={label:d.label,...profileFor(d.key,s),disabled,deleted:false}});let data={schema_version:'user_profile_calibration_export_v1',algorithm_version:manifest.algorithm_version,exported_at:new Date().toISOString(),privacy:{storage:'browser_localStorage',network_used:false,behavioral_inference:false},sample_scope:{dates:manifest.date_coverage,card_count:manifest.card_count,notice:manifest.coverage_notice},answers:state.answers,conclusion_status,profile};let blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='user-profile-calibration.json';a.click();URL.revokeObjectURL(a.href)}
function esc(x){return String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
document.getElementById('maximum').textContent=manifest.maximum_comparisons;document.getElementById('reasons').innerHTML=manifest.dimensions.map(d=>`<label class="reason"><input type="checkbox" value="${d.key}"> ${esc(d.label)}</label>`).join('');document.querySelectorAll('[data-outcome]').forEach(b=>b.onclick=()=>answer(b.dataset.outcome));document.getElementById('undo').onclick=()=>{state.answers.pop();save();renderQuestion();renderProfile()};document.getElementById('reset').onclick=()=>{if(confirm('清除本浏览器中的全部校准记录？')){state=fresh();save();renderQuestion();renderProfile()}};document.getElementById('export').onclick=exportData;renderQuestion();renderProfile();if(loadNotice)document.getElementById('saved').textContent=loadNotice;
</script></body></html>'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        dest="input",
        type=Path,
        default=Path("output/conversation-reconstruction-semantic-evaluation-v7-20260904/reconstruction.json"),
    )
    parser.add_argument("--output", type=Path, default=Path("output/user-profile-calibration-test-20260905"))
    args = parser.parse_args()
    paths = generate_calibration_site(args.input, args.output)
    print(json.dumps({key: str(path) for key, path in paths.items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
