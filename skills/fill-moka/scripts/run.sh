#!/usr/bin/env bash
# run.sh · fill-moka 编排：抓字段 → Jev 批量映射 → 写入表单 → 输出 JSON 报告
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPACE="" URL="" INSTANCE="${CAMPUS_INSTANCE:-}" DRY=0 KEEP="" PAGE=""
while [ $# -gt 0 ]; do case "$1" in
  --space) SPACE="$2"; shift 2;;
  --url) URL="$2"; shift 2;;
  --instance) INSTANCE="$2"; shift 2;;
  --page) PAGE="$2"; shift 2;;          # 表单所在标签（p1/p2…）；不给就自动找地址里带这个职位 id 的标签
  --dry-run) DRY=1; shift;;
  --keep) KEEP="$2"; shift 2;;          # 调试：把 fields/plan/applied 三个中间文件留到这个目录（含个人信息，别放进仓库）
  *) echo "未知参数 $1" >&2; exit 2;; esac; done
[ -n "$SPACE" ] && [ -n "$URL" ] && [ -n "$INSTANCE" ] || { echo '用法: run.sh --space <ego空间> --url <申请页链接> [--instance DIR] [--page pN] [--dry-run] [--keep DIR]' >&2; exit 2; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
T0=$(date +%s)

# 问题库 → JSON（id/question/aliases/answer），供映射用
uv run --with pyyaml python3 -c "
import yaml,json,sys
d=yaml.safe_load(open('$INSTANCE/question-bank.yaml'))
items=d.get('items') if isinstance(d,dict) else d
out=[{'id':e.get('id'),'question':e.get('question',''),'aliases':e.get('aliases') or [],'answer':str(e.get('answer') or ''),'kind':e.get('kind') or 'value','category':e.get('category') or '其他'} for e in items if e.get('id')]
# profile.md 里的基础信息（姓名/性别/电话/邮箱等）没进问题库，这里补成候选条目，否则最常见的字段反而映射不上
import re
prof=open('$INSTANCE/profile.md',encoding='utf-8').read()
def pick(pat):
    m=re.search(pat,prof)
    return m.group(1).strip() if m else ''
base=[('profile-name','姓名',pick(r'姓名[:：]\s*([^ /,，\n]+)')),
      ('profile-phone','手机号码',pick(r'电话[:：]\s*\+?86?\s*(1[3-9]\d{9})')),
      ('profile-email','邮箱',pick(r'邮箱[:：]\s*([\w.+-]+@[\w.-]+)')),
      ('profile-gender','性别',pick(r'性别[:：]\s*([男女])')),
      ('profile-github','GitHub/个人主页',pick(r'GitHub[:：]\s*(\S+)'))]
have={e['id'] for e in out}|{e['question'] for e in out}
out += [{'id':i,'question':q,'aliases':[],'answer':v,'kind':'value','category':'身份' if i in ('profile-name','profile-gender') else '联系'} for i,q,v in base if v and q not in have]
json.dump(out,open('$TMP/qb.json','w'),ensure_ascii=False)
print(len(out),file=sys.stderr)
" 2>/dev/null || { echo '读问题库失败' >&2; exit 1; }

RESUME="$(ls "$INSTANCE"/materials/resume-cn.pdf 2>/dev/null || true)"
python3 - "$HERE/scrape.mjs" "$TMP/scrape.mjs" "$SPACE" "$URL" "$RESUME" "$PAGE" <<'PY'
import sys
import os
src,dst,space,url,resume=sys.argv[1:6]; page=sys.argv[6]
t=open(os.path.join(os.path.dirname(src),'lib.mjs')).read()+open(src).read()   # ego 的 nodejs 只吃单文件，公共函数拼在前面
t=t.replace('__FM_SPACE__',space).replace('__FM_URL__',url).replace('__FM_RESUME__',resume).replace('__FM_PAGE__',page)
open(dst,'w').write(t)
PY
ego-browser nodejs < "$TMP/scrape.mjs" 2>&1 | grep '^FM_JSON ' | sed 's/^FM_JSON //' > "$TMP/fields.json"
[ -s "$TMP/fields.json" ] || { echo '{"error":"抓取字段失败：ego 没有输出，检查空间归属与链接"}'; exit 1; }
if grep -q '"error"' "$TMP/fields.json"; then cat "$TMP/fields.json"; exit 1; fi

# Jev 走 AI SDK（Node），依赖装在缓存目录，避免污染仓库；首次会自动装
NODE_DIR="$HOME/.cache/campus-apply/node"
if [ ! -d "$NODE_DIR/node_modules/ai" ]; then
  mkdir -p "$NODE_DIR" && (cd "$NODE_DIR" && npm init -y >/dev/null 2>&1; npm i ai@latest >/dev/null 2>&1) || true
fi
KEY="$(security find-generic-password -s campus-apply-vercel-gateway -w 2>/dev/null || true)"
# ESM 的 import 不认 NODE_PATH，只能把脚本放到装了依赖的目录里跑
cp "$HERE/map-fields.mjs" "$NODE_DIR/map-fields.mjs"
(cd "$NODE_DIR" && AI_GATEWAY_API_KEY="$KEY" FM_QB="$TMP/qb.json" node map-fields.mjs) < "$TMP/fields.json" > "$TMP/plan.json" 2>"$TMP/map.err" \
  || echo "{\"plan\":[],\"degraded\":true,\"reason\":\"映射脚本异常：$(tail -1 "$TMP/map.err" | cut -c1-120 | tr -d '\"')\"}" > "$TMP/plan.json"

PAGE_USED="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('page') or 'p1')" "$TMP/fields.json")"
python3 - "$HERE/apply.mjs" "$TMP/apply.mjs" "$SPACE" "$TMP/plan.json" "$DRY" "$PAGE_USED" <<'PY'
import sys
import os
src,dst,space,plan,dry=sys.argv[1:6]; page=sys.argv[6]
t=open(os.path.join(os.path.dirname(src),'lib.mjs')).read()+open(src).read()
t=t.replace('__FM_SPACE__',space).replace('__FM_PLAN__',plan).replace('__FM_DRY__',dry).replace('__FM_PAGE__',page)
open(dst,'w').write(t)
PY
ego-browser nodejs < "$TMP/apply.mjs" 2>&1 | grep '^FM_JSON ' | sed 's/^FM_JSON //' > "$TMP/applied.json"
[ -s "$TMP/applied.json" ] || echo '{"done":[],"failed":[]}' > "$TMP/applied.json"

T1=$(date +%s)
[ -n "$KEEP" ] && mkdir -p "$KEEP" && cp "$TMP"/fields.json "$TMP"/plan.json "$TMP"/applied.json "$KEEP"/ 2>/dev/null
python3 - "$TMP" "$((T1-T0))" "$DRY" <<'PY'
import json,sys
from collections import OrderedDict
tmp,secs,dry=sys.argv[1],int(sys.argv[2]),sys.argv[3]=='1'
f=json.load(open(f'{tmp}/fields.json')); p=json.load(open(f'{tmp}/plan.json')); a=json.load(open(f'{tmp}/applied.json'))
plan=p.get('plan',[])
own=[r for r in plan if r.get('lane')!='records' and not (r.get('lane')=='edu' and not r.get('stage'))]   # 脚本负责判断的字段
def where(r): return ('%s·第%s行·' % (r['section'],r['row']) if r.get('row') else '')+r['label']
pending=[dict({'label':where(r)},**{k:r[k] for k in ('type','required','reason','hint') if r.get(k) not in (None,'')}) for r in own if r.get('action')=='none']
pending += a.get('failed',[])
# 分段经历（实习/项目/获奖…）与认不出阶段的教育行：脚本不碰，只按「分组·第几行」汇总空着的字段
groups=OrderedDict()
for r in plan:
    if r in own: continue
    g=groups.setdefault('%s·第%s行' % (r.get('section') or '未命名分组', r.get('row') or 1), {'已有值':0,'空着':[]})
    if r.get('hasValue'): g['已有值']+=1
    else: g['空着'].append(r['label']+('*' if r.get('required') else ''))
skipped_ctrl=[x['label'] for x in f.get('fields',[]) if x['type'] in ('file','checkbox')]
print(json.dumps({
 '总字段':len(f.get('fields',[])), '脚本已填':len(a.get('done',[])), '解析已带入且已核对':sum(1 for r in own if r.get('action')=='keep'),
 '待处理':len(pending), '分段经历字段(交 worker)':sum(1 for r in plan if r not in own),
 '简历已上传':f.get('uploaded',False), '简历备注':f.get('uploadNote') or None,
 '耗时秒':secs, '花费美元':p.get('cost',0), '映射降级':p.get('degraded',False), '降级原因':p.get('reason'), 'dry_run':dry,
 '提醒':p.get('notes',[]),
 '解析值与问题库不一致':p.get('mismatches',[]),
 '待处理明细':pending,
 '分段经历(带*为必填)':groups,
 '未处理的控件(上传/勾选)':skipped_ctrl, '已填明细':a.get('done',[]),
}, ensure_ascii=False, indent=1))
PY
