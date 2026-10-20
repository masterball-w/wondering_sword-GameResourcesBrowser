# -*- coding: utf-8 -*-
"""
逸剑风云决 · 资源浏览器 (Web 版)
双击运行 → 自动打开浏览器 http://127.0.0.1:8738
直接从 pak 读取资源, 支持分类浏览/搜索/预览(贴图·动效·音频·剧情文本)/导出
"""
import base64
import io
import json
import os
import pickle
import re
import struct
import sys
import threading
import urllib.parse
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tools"))
from assetdb import AssetDB

PAK = os.path.join(HERE, "Wandering_Sword-WindowsNoEditor.pak")
INDEX = os.path.join(HERE, "分类清单.json")
CACHE = os.path.join(HERE, "tools", "_catcache.pkl")
PORT = 8738

# ================================================================ 数据加载
print("正在加载 pak 与分类索引…", flush=True)
DB = AssetDB(PAK)

if os.path.exists(CACHE) and os.path.getmtime(CACHE) > os.path.getmtime(INDEX):
    with open(CACHE, "rb") as f:
        cat_list = pickle.load(f)
else:
    data = json.load(open(INDEX, encoding="utf-8"))
    cat_list = {c: [it["资产"] for it in items] for c, items in data.items()}
    with open(CACHE, "wb") as f:
        pickle.dump(cat_list, f)
    del data
DB.stem_cat = {}
for c, lst in cat_list.items():
    for s in lst:
        DB.stem_cat[s] = c
DB.cat_stems = cat_list

# /ParticleSystem/ 和 /Particles/ 下的资产强制归入 09_特效 (原分类误分到材质/纹理等)
_fx = "09_特效"
_fx_set = set(cat_list.get(_fx, []))
for _s in DB.groups:
    if "/ParticleSystem/" in _s or "/Particles/" in _s:
        if _s not in _fx_set:
            old_cat = DB.stem_cat.get(_s)
            if old_cat and old_cat in cat_list:
                try:
                    cat_list[old_cat].remove(_s)
                except ValueError:
                    pass
            DB.stem_cat[_s] = _fx
            _fx_set.add(_s)
cat_list[_fx] = sorted(_fx_set)
print(f"就绪: {len(DB.entries):,} 文件, {len(cat_list)} 类", flush=True)

LOCRES_PATHS = sorted(p for p in DB.entries if p.endswith(".locres"))

TREE_CACHE = {}
DT_CACHE = {}


def build_tree(cat: str):
    """分类内资产的目录树: [{name, n(直接), t(含子), c:[...]}]"""
    key = cat or "*"
    if key in TREE_CACHE:
        return TREE_CACHE[key]
    stems = DB.cat_stems.get(cat, []) if cat else DB.groups.keys()
    trie = {}
    for s in stems:
        node = trie
        for p in s.split("/")[:-1]:
            node = node.setdefault(p, {})
        node["$"] = node.get("$", 0) + 1

    def conv(d):
        out = []
        for k, v in d.items():
            if k == "$" or not isinstance(v, dict):
                continue
            kids = conv(v)
            total = v.get("$", 0) + sum(c["t"] for c in kids)
            out.append(dict(name=k, n=v.get("$", 0), t=total, c=kids))
        out.sort(key=lambda x: (-x["t"], x["name"]))
        return out

    result = conv(trie)
    TREE_CACHE[key] = result
    return result


def asset_type_of(stem: str) -> str:
    """粗略类型 (供列表图标): 由类名/路径/扩展"""
    if stem in DB.entries:
        ext = stem.rsplit(".", 1)[-1]
        if ext != "uasset":
            if ext == "locres":
                return "locres"
            return "file"
    cat = DB.stem_cat.get(stem, "")
    pkg = DB.package_of(stem)
    cls = DB.export_class(pkg)
    if cls in ("PaperFlipbook", "PaperSprite"):
        return cls
    if cls == "Texture2D":
        return "texture"
    if cls == "SoundWave":
        return "sound"
    if cls == "SoundCue":
        return "soundcue"
    if cls:
        return cls
    return cat[:6] if cat else "other"


# ================================================================ 前端页面
HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>逸剑风云决 · 资源浏览器</title>
<style>
:root{--bg:#14161a;--panel:#1d2026;--panel2:#22262e;--line:#2e333d;--txt:#d7dae0;--dim:#8b93a1;--acc:#5aa2ff;--ok:#67c23a}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px/1.5 "Segoe UI",Microsoft YaHei,sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden}
header{display:flex;align-items:center;gap:14px;padding:8px 14px;background:var(--panel);border-bottom:1px solid var(--line)}
header h1{font-size:16px;font-weight:600;white-space:nowrap}
header input{flex:1;max-width:520px;background:var(--panel2);border:1px solid var(--line);color:var(--txt);padding:7px 12px;border-radius:6px;outline:none}
header input:focus{border-color:var(--acc)}
#stats{color:var(--dim);font-size:12px;white-space:nowrap}
main{flex:1;display:flex;min-height:0}
#side{width:230px;overflow-y:auto;background:var(--panel);border-right:1px solid var(--line);padding:6px 0}
.cat{display:flex;justify-content:space-between;padding:6px 14px;cursor:pointer;color:var(--dim)}
.cat:hover{background:var(--panel2);color:var(--txt)}
.cat.on{background:var(--panel2);color:var(--acc);border-right:2px solid var(--acc)}
#dtree{width:250px;overflow:auto;background:var(--panel);border-right:1px solid var(--line);padding:4px 0;flex-shrink:0}
.dtnode{display:flex;align-items:center;gap:4px;padding:2px 6px;cursor:pointer;color:var(--dim);font-size:12px;white-space:nowrap}
.dtnode:hover{background:var(--panel2);color:var(--txt)}
.dtnode.on{background:var(--panel2);color:var(--acc)}
.dtnode .tw{width:14px;text-align:center;flex-shrink:0;cursor:pointer;color:#5a6272}
.dtnode .cnt{color:#5f6774;font-size:10px;margin-left:auto;padding-left:6px}
.dtnode .nm{overflow:hidden;text-overflow:ellipsis}
#crumb{color:var(--acc);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#crumb .seg{cursor:pointer}
#crumb .seg:hover{text-decoration:underline}
#mid{flex:1;display:flex;flex-direction:column;min-width:0;border-right:1px solid var(--line)}
#toolbar{display:flex;align-items:center;gap:8px;padding:6px 10px;border-bottom:1px solid var(--line);color:var(--dim);font-size:12px}
#list{flex:1;overflow-y:auto}
.item{display:flex;gap:8px;align-items:center;padding:5px 12px;cursor:pointer;border-bottom:1px solid #191c21}
.item:hover{background:var(--panel2)}
.item.on{background:var(--panel2);box-shadow:inset 2px 0 0 var(--acc)}
.tag{font-size:10px;padding:1px 6px;border-radius:4px;background:#2a2f38;color:var(--dim);flex-shrink:0}
.tag.tex{color:#f7b955}.tag.anim{color:#67c23a}.tag.snd{color:#f2827e}.tag.txt{color:#b48ef7}.tag.map{color:#5fd4c4}
.path{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px}
#right{width:46%;min-width:420px;display:flex;flex-direction:column;background:#181a1f}
#pv{flex:1;overflow:auto;padding:14px}
#pv h3{font-size:14px;margin-bottom:6px;word-break:break-all}
.meta{color:var(--dim);font-size:12px;margin-bottom:10px;word-break:break-all}
canvas{background:
 repeating-conic-gradient(#23262d 0 25%,#1b1e24 0 50%) 0 0/20px 20px;border:1px solid var(--line);max-width:100%}
button{background:var(--panel2);color:var(--txt);border:1px solid var(--line);padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px}
button:hover{border-color:var(--acc);color:var(--acc)}
.btns{display:flex;gap:8px;margin:10px 0;flex-wrap:wrap}
table.kv{border-collapse:collapse;width:100%;font-size:12px;margin-top:8px}
table.kv td{border:1px solid var(--line);padding:4px 8px;vertical-align:top}
table.kv td:first-child{color:var(--dim);width:160px;white-space:nowrap}
.dt td,.dt th{max-width:280px;overflow:hidden;text-overflow:ellipsis}
.lofilter{width:100%;margin-bottom:8px;background:var(--panel2);border:1px solid var(--line);color:var(--txt);padding:6px 10px;border-radius:6px}
#frames{display:flex;gap:4px;flex-wrap:wrap;margin:8px 0}
#frames canvas{width:64px;height:64px;object-fit:contain;image-rendering:pixelated;cursor:pointer;border:1px solid var(--line)}
.dim{color:var(--dim)}
audio{width:100%;margin-top:8px}
#loading{position:fixed;inset:0;background:rgba(10,12,16,.7);display:none;align-items:center;justify-content:center;z-index:9;font-size:15px;color:var(--acc)}
</style>
</head>
<body>
<header>
  <h1>逸剑风云决 · 资源浏览器</h1>
  <input id="q" placeholder="搜索资源路径 (支持中文, 回车)…">
  <div id="stats"></div>
</header>
<main>
  <div id="side"></div>
  <div id="dtree"></div>
  <div id="mid">
    <div id="toolbar"><span id="crumb"></span><button id="more" style="display:none">加载更多…</button></div>
    <div id="list"></div>
  </div>
  <div id="right"><div id="pv"><div class="dim">点击左侧资源进行预览</div></div></div>
</main>
<div id="loading">加载中…</div>
<script>
let CAT='',Q='',OFFSET=0,LIMIT=300,MARK={},DIR='';
const $=id=>document.getElementById(id);
async function api(u){const r=await fetch(u);if(!r.ok)throw new Error(await r.text());return r.json();}

async function boot(){
  const st=await api('/api/stats');
  $('stats').textContent=`${st.files} 文件 · ${st.groups} 资产 · 解压 ${st.size}`;
  const side=$('side');
  side.innerHTML='';
  const all=document.createElement('div');all.className='cat on';all.innerHTML=`<span>全部资源</span><span>${st.groups}</span>`;
  all.onclick=()=>{CAT='';Q='';DIR='';$('q').value='';load();loadTree();mark(all)};
  side.appendChild(all);
  for(const [c,n] of Object.entries(st.cats)){
    const d=document.createElement('div');d.className='cat';
    d.innerHTML=`<span>${c}</span><span>${n}</span>`;
    d.onclick=()=>{CAT=c;Q='';DIR='';$('q').value='';load();loadTree();mark(d)};
    side.appendChild(d);
  }
  load();loadTree();
  function mark(el){document.querySelectorAll('.cat').forEach(x=>x.classList.remove('on'));el.classList.add('on')}
}
function updateTreeSel(){
  document.querySelectorAll('#dtree .dtnode').forEach(el=>{
    el.classList.toggle('on', (el._path||'')===DIR);
  });
}

async function loadTree(){
  const r=await api('/api/tree?cat='+encodeURIComponent(CAT));
  const box=$('dtree');
  box.innerHTML='';
  const all=document.createElement('div');
  all.className='dtnode'+(DIR?'':' on');
  all.innerHTML=`<span class="tw"></span><span class="nm">全部</span><span class="cnt">${r.nodes.reduce((a,n)=>a+n.t,0)}</span>`;
  all.onclick=()=>{DIR='';OFFSET=0;load();updateTreeSel()};
  box.appendChild(all);
  r.nodes.forEach(n=>box.appendChild(mkNode(n,0,'')));
  function mkNode(n,lv,path){
    const full=path?path+'/'+n.name:n.name;
    const el=document.createElement('div');
    const has=n.c.length;
    el.className='dtnode'+(DIR===full?' on':'');
    el.style.paddingLeft=(6+lv*14)+'px';
    el.innerHTML=`<span class="tw">${has?'▸':'·'}</span><span class="nm" title="${full}">${n.name}</span><span class="cnt">${n.t}</span>`;
    el._node=n; el._lv=lv; el._path=full;
    let open=false, kids=null;
    const twEl=el.querySelector('.tw');
    const toggle=()=>{
      open=!open;
      twEl.textContent=open?'▾':'▸';
      if(open&&!kids){
        kids=document.createElement('div');
        n.c.forEach(ch=>kids.appendChild(mkNode(ch,lv+1,full)));
        el.after(kids);
        // 自动展开: 单子链(跳过 Wandering_Sword→Content 这类) 或前两级
        kids.childNodes.forEach(k=>{
          if(k._node&&(k._node.c.length===1||k._lv<=1)&&k.querySelector){
            const t=k.querySelector('.tw');
            if(t&&t.textContent==='▸') t.click();
          }
        });
      }else if(kids){
        kids.style.display=open?'':'none';
      }
    };
    twEl.onclick=(e)=>{e.stopPropagation();if(has)toggle()};
    el.onclick=()=>{DIR=full;OFFSET=0;load();updateTreeSel()};
    return el;
  }
}

async function load(){
  $('loading').style.display='flex';
  const r=await api(`/api/list?cat=${encodeURIComponent(CAT)}&q=${encodeURIComponent(Q)}&dir=${encodeURIComponent(DIR)}&offset=${OFFSET}&limit=${LIMIT}`);
  const list=$('list');
  if(OFFSET===0)list.innerHTML='';
  for(const it of r.items)addItem(it);
  const cbox=$('crumb');
  if(cbox){
    let h=`<span class="seg" data-d="">${r.cat||'全部'}</span>`;
    if(DIR){let acc='';
      DIR.split('/').forEach(seg=>{acc=acc?acc+'/'+seg:seg;
        h+=` / <span class="seg" data-d="${acc}">${seg}</span>`;});}
    h+=` · ${r.total.toLocaleString()} 项${Q?` · 搜"${Q}"`:''}`;
    cbox.innerHTML=h;
    cbox.querySelectorAll('.seg').forEach(s=>s.onclick=()=>{DIR=s.dataset.d;OFFSET=0;load();updateTreeSel()});
  }
  $('more').style.display=r.more?'':'none';
  MARK={};r.items.forEach(it=>MARK[it.p]=it.t);
  $('loading').style.display='none';
}
function addItem(it){
  const d=document.createElement('div');d.className='item';
  const tag=tagOf(it.t);
  d.innerHTML=`<span class="tag ${tag[1]}">${tag[0]}</span><span class="path" title="${it.p}">${it.p}</span>`;
  d.onclick=()=>{document.querySelectorAll('.item').forEach(x=>x.classList.remove('on'));d.classList.add('on');preview(it)};
  $('list').appendChild(d);
}
function tagOf(t){
  if(t==='texture'||t==='PaperSprite')return['贴图','tex'];
  if(t==='PaperFlipbook')return['动效','anim'];
  if(t==='sound'||t==='soundcue')return['音频','snd'];
  if(t==='locres')return['文本','txt'];
  if(t==='World')return['地图','map'];
  return[t.slice(0,6),''];
}
$('more').onclick=()=>{OFFSET+=LIMIT;load()};
$('q').addEventListener('keydown',e=>{if(e.key==='Enter'){Q=$('q').value.trim();OFFSET=0;load()}});

// ================================================== 预览
async function preview(it){
  $('loading').style.display='flex';
  try{
    const a=await api('/api/asset?path='+encodeURIComponent(it.p));
    render(it,a);
  }catch(e){
    $('pv').innerHTML=`<h3>${it.p}</h3><div class="meta">预览失败: ${e.message}</div>`+fileBtns(it);
  }
  $('loading').style.display='none';
}
function fileBtns(it){
  return `<div class="btns">
    <button onclick="zip('${it.p}')">导出原始文件 (zip)</button>
  </div>`;
}
function loc2(u){location.href=u;}
const enc0=encodeURIComponent;
function dl(p){location.href='/api/file?path='+encodeURIComponent(p+'.uasset');}
async function showSprite(a){
  const t=await fetchTex(a.texUrl);
  const off=document.createElement('canvas');off.width=t.w;off.height=t.h;
  drawTexture(off.getContext('2d'),t.buf,t.w,t.h,t.fmt);
  const cv=$('cv');cv.width=a.sw;cv.height=a.sh;
  cv.getContext('2d').drawImage(off,a.x,a.y,a.sw,a.sh,0,0,a.sw,a.sh);
  $('tinfo').innerHTML=`<b>${a.sw}×${a.sh}</b> ${a.note}`;
}
function zip(p){location.href='/api/export?path='+encodeURIComponent(p);}

async function render(it,a){
  const pv=$('pv');
  let h=`<h3>${it.p.split('/').pop()}</h3><div class="meta">${it.p}<br>${a.type}${a.note?' · '+a.note:''}</div>`;
  pv.innerHTML=h+fileBtns(it)+`<div id="pbody"></div>`;
  const body=$('pbody');
  const enc=encodeURIComponent;
  if(a.kind==='texture'){
    body.innerHTML=`<div class="btns"><button onclick="loc2('/api/png?path=${enc(it.p)}')">导出 PNG</button></div>
      <canvas id="cv"></canvas><div class="meta" id="tinfo"></div>`;
    await showTexture(a,'cv',`<b>${a.w}×${a.h}</b> ${a.fmt} · ${a.src}`);
  }else if(a.kind==='sprite'){
    body.innerHTML=`<div class="btns"><button onclick="loc2('/api/png?path=${enc(it.p)}')">导出 PNG</button></div>
      <canvas id="cv" style="image-rendering:pixelated"></canvas><div class="meta" id="tinfo"></div>`;
    await showSprite(a);
  }else if(a.kind==='animseq'){
    body.innerHTML=`<div class="btns">
      <button id="play">▶ 播放</button>
      <span class="dim">帧范围:</span>
      <input id="fstart" type="number" style="width:52px;background:var(--panel2);border:1px solid var(--line);color:var(--txt);padding:3px 6px;border-radius:4px" value="0">
      <span class="dim">~</span>
      <input id="fend" type="number" style="width:52px;background:var(--panel2);border:1px solid var(--line);color:var(--txt);padding:3px 6px;border-radius:4px" value="${a.frames.length-1}">
      <button id="fgif">导出 GIF</button>
      <span class="dim" id="fstat"></span></div>
      <canvas id="cv" style="image-rendering:pixelated"></canvas><div id="frames"></div><div class="meta" id="tinfo"></div>`;
    await showAnimSeq(a);
  }else if(a.kind==='flipbook'){
    body.innerHTML=`<div class="btns"><button id="play">▶ 播放</button>
      <button onclick="loc2('/api/gif?path=${enc(it.p)}')">导出 GIF 动画</button>
      <button id="expcur">导出当前帧 PNG</button>
      <button onclick="loc2('/api/frames?path=${enc(it.p)}')">导出全部帧 PNG (zip)</button>
      <span class="dim" id="fstat"></span></div>
      <canvas id="cv" style="image-rendering:pixelated"></canvas><div id="frames"></div><div class="meta" id="tinfo"></div>`;
    await showFlipbook(a);
  }else if(a.kind==='sound'){
    body.innerHTML=`<div class="btns"><button onclick="loc2('/api/ogg?path=${enc(it.p)}')">导出 OGG 音频</button></div>
      <div class="meta">${a.info}</div><audio controls src="/api/audio?path=${enc(it.p)}"></audio>`;
  }else if(a.kind==='datatable'){
    body.innerHTML=`<div class="meta">共 ${a.rows.length} 行 · 双击单元格复制</div>
      <div style="max-height:64vh;overflow:auto;border:1px solid var(--line)">
      <table class="dt" style="border-collapse:collapse;font-size:11px;white-space:nowrap">
      <thead style="position:sticky;top:0;background:var(--panel2)"><tr>
        <th style="padding:3px 8px;border:1px solid var(--line);color:var(--acc)">#行名</th>
        ${a.cols.map(c=>`<th style="padding:3px 8px;border:1px solid var(--line)">${esc(String(c))}</th>`).join('')}
      </tr></thead><tbody>
      ${a.rows.map(r=>`<tr>${r.map((c,i)=>`<td style="padding:2px 8px;border:1px solid var(--line);${i===0?'color:var(--ok)':''}" title="${esc(String(c))}" ondblclick="navigator.clipboard.writeText(String.raw\`${esc(String(c)).replace(/`/g,'')}\`)">${esc(fmtv(c))}</td>`).join('')}</tr>`).join('')}
      </tbody></table></div>`;
  }else if(a.kind==='particle'){
    body.innerHTML=`<div class="btns"><button id="pplay">▶ 播放</button><span class="dim" id="pstat"></span></div>
      <canvas id="pcv" style="width:100%;height:56vh;background:#050608;border:1px solid var(--line)"></canvas>
      <div class="meta">${a.note} · 模块: ${a.modules.slice(0,6).join(', ')}${a.modules.length>6?'…':''}</div>
      <div id="ptexs" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px"></div>`;
    showParticle(a);
  }else if(a.kind==='mesh'){
    body.innerHTML=`<div class="btns"><span class="dim" id="mstat"></span></div>
      <canvas id="cv3d" style="width:100%;height:60vh;background:#0d0f13;border:1px solid var(--line);cursor:grab"></canvas>
      <div class="meta">拖动旋转 · 滚轮缩放 · ${a.note} · LOD 顶点: ${a.lods.join(' / ')}</div>`;
    showMesh(a);
  }else if(a.kind==='locres'){
    showLocres(a, it.p);
  }else if(a.kind==='props'){
    body.innerHTML=`<table class="kv">${Object.entries(a.props).map(([k,v])=>`<tr><td>${k}</td><td>${esc(String(v))}</td></tr>`).join('')}</table>`;
  }
}
function esc(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function fmtv(v){
  if(v===null||v===undefined)return '';
  if(typeof v==='object'){
    try{let s=JSON.stringify(v);return s.length>60?s.slice(0,60)+'…':s;}catch(e){return String(v);}
  }
  const s=String(v);return s.length>80?s.slice(0,80)+'…':s;
}
async function showParticle(a){
  const cv=$('pcv');
  const dpr=window.devicePixelRatio||1;
  cv.width=cv.clientWidth*dpr;cv.height=cv.clientHeight*dpr;
  const ctx=cv.getContext('2d');ctx.scale(dpr,dpr);
  const W=cv.clientWidth,H=cv.clientHeight;
  // 加载纹理
  const imgs=await Promise.all(a.textures.slice(0,4).map(t=>
    new Promise(res=>{const im=new Image();
      im.onload=()=>res(im);im.onerror=()=>res(null);
      im.src='/api/tex?path='+encodeURIComponent(t);})));
  const valid=imgs.filter(Boolean);
  if(!valid.length){ctx.fillStyle='#666';ctx.fillText('纹理加载失败',20,30);return;}
  // 纹理缩略
  const pt=$('ptexs');
  valid.forEach((im,i)=>{
    const c=document.createElement('canvas');c.width=72;c.height=72;
    c.style.cssText='border:1px solid var(--line);background:#000';
    c.getContext('2d').drawImage(im,0,0,72,72);
    pt.appendChild(c);
  });
  // 粒子模拟
  const parts=[];
  const N=Math.min(80,20+a.emitters*10);
  let playing=false,raf=null,frame=0;
  function spawn(){
    const im=valid[Math.floor(Math.random()*valid.length)];
    // SubUV 图集: 随机取一个格子
    let sx=0,sy=0,sw=im.width,sh=im.height;
    const cell=Math.min(6,Math.floor(im.width/im.height)||1);
    if(cell>1){sw=im.width/cell;sh=im.height;
      sx=Math.floor(Math.random()*cell)*sw;}
    parts.push({im,sx,sy,sw,sh,
      x:W/2+(Math.random()-0.5)*W*0.5, y:H*0.75+(Math.random()-0.5)*H*0.25,
      vx:(Math.random()-0.5)*0.8, vy:-(0.4+Math.random()*1.6),
      rot:Math.random()*6.28, vr:(Math.random()-0.5)*0.04,
      size:30+Math.random()*70, life:1, decay:0.004+Math.random()*0.008,
      grow:1+Math.random()*0.015, cell, cellIdx:Math.floor(Math.random()*cell), cellSpd:0.02+Math.random()*0.04,
      hue:Math.random()*30});
  }
  for(let i=0;i<N*0.4;i++)spawn();
  function draw(){
    if(!playing)return;
    frame++;
    ctx.fillStyle='rgba(5,6,8,0.22)';
    ctx.fillRect(0,0,W,H);
    ctx.globalCompositeOperation='lighter';
    for(let i=parts.length-1;i>=0;i--){
      const p=parts[i];
      p.x+=p.vx;p.y+=p.vy;p.vy*=0.998;p.rot+=p.vr;
      p.size*=p.grow;p.life-=p.decay;
      if(p.life<=0||p.size<2||p.y<-p.size){parts.splice(i,1);continue;}
      // SubUV 动画
      if(p.cell>1){p.cellIdx=(p.cellIdx+p.cellSpd)%p.cell;
        p.sx=Math.floor(p.cellIdx)*p.sw;}
      const s=p.size*p.life+4;
      ctx.save();
      ctx.translate(p.x,p.y);ctx.rotate(p.rot);
      ctx.globalAlpha=Math.min(1,p.life)*0.85;
      ctx.drawImage(p.im,p.sx,p.sy,p.sw,p.sh,-s/2,-s/2,s,s);
      ctx.restore();
    }
    ctx.globalCompositeOperation='source-over';
    if(parts.length<N&&frame%2===0)spawn();
    $('pstat').textContent=`${parts.length} 粒子 · ${valid.length} 纹理 · 帧 ${frame}`;
    raf=requestAnimationFrame(draw);
  }
  function play(){
    playing=!playing;
    $('pplay').textContent=playing?'⏸ 暂停':'▶ 播放';
    if(playing)draw();else cancelAnimationFrame(raf);
  }
  $('pplay').onclick=play;
  play();
}

function showMesh(a){
  const cv=$('cv3d');
  const dpr=window.devicePixelRatio||1;
  cv.width=cv.clientWidth*dpr;cv.height=cv.clientHeight*dpr;
  const ctx=cv.getContext('2d');ctx.scale(dpr,dpr);
  const W=cv.clientWidth,H=cv.clientHeight;
  const pts=a.pts,N=a.n;
  const bb=a.bbox;
  const cx=(bb[0]+bb[3])/2,cy=(bb[1]+bb[4])/2,cz=(bb[2]+bb[5])/2;
  const r=Math.max(bb[3]-bb[0],bb[4]-bb[1],bb[5]-bb[2],1)/2;
  let rotX=-0.4,rotY=0.6,zoom=1,dist=3.2;
  let drag=null;
  function draw(){
    ctx.fillStyle='#0d0f13';ctx.fillRect(0,0,W,H);
    const cA=Math.cos(rotX),sA=Math.sin(rotX),cB=Math.cos(rotY),sB=Math.sin(rotY);
    const sc=Math.min(W,H)/(r*dist)*zoom;
    // 按深度排序绘制
    const proj=[];
    for(let i=0;i<N;i++){
      let x=(pts[i*3]-cx)/r,y=(pts[i*3+1]-cy)/r,z=(pts[i*3+2]-cz)/r;
      // UE Z 向上 → 屏幕 Y 翻转
      let y1=y*cA-z*sA, z1=y*sA+z*cA;
      let x2=x*cB-y1*sB, y2=x*sB+y1*cB;
      const depth=z1;
      const px=W/2+x2*sc, py=H/2-y2*sc;
      if(px>=-20&&px<W+20&&py>=-20&&py<H+20)proj.push([px,py,depth]);
    }
    proj.sort((p,q)=>p[2]-q[2]);
    const dzmin=proj.length?proj[0][2]:-1,dzmax=proj.length?proj[proj.length-1][2]:1;
    for(const [px,py,dz] of proj){
      const t=(dz-dzmin)/((dzmax-dzmin)||1);
      const s=1.6;
      ctx.fillStyle=`rgba(${100+t*140|0},${170+t*60|0},${220-t*60|0},${0.45+t*0.55})`;
      ctx.fillRect(px-s/2,py-s/2,s,s);
    }
    $('mstat').textContent=`${N} 顶点 · 旋转 ${(-rotY*57.3)|0}° 俯角 ${(rotX*57.3)|0}° · 缩放 ${zoom.toFixed(1)}x`;
  }
  cv.onmousedown=e=>{drag={x:e.offsetX,y:e.offsetY};cv.style.cursor='grabbing'};
  cv.onmousemove=e=>{if(!drag)return;rotY+=(e.offsetX-drag.x)*0.01;rotX+=(e.offsetY-drag.y)*0.01;drag={x:e.offsetX,y:e.offsetY};draw()};
  cv.onmouseup=cv.onmouseleave=()=>{drag=null;cv.style.cursor='grab'};
  cv.onwheel=e=>{e.preventDefault();zoom*=e.deltaY<0?1.1:0.9;zoom=Math.max(0.1,Math.min(20,zoom));draw()};
  let touch=null;
  cv.ontouchstart=e=>{touch={x:e.touches[0].clientX,y:e.touches[0].clientY}};
  cv.ontouchmove=e=>{if(!touch)return;rotY+=(e.touches[0].clientX-touch.x)*0.01;rotX+=(e.touches[0].clientY-touch.y)*0.01;touch={x:e.touches[0].clientX,y:e.touches[0].clientY};draw()};
  cv.ontouchend=()=>{touch=null};
  draw();
}

// ---------- 贴图解码 ----------
async function fetchTex(url){
  const r=await fetch(url);const meta={};
  r.headers.forEach((v,k)=>{if(k.startsWith('x-'))meta[k.slice(2)]=v});
  const buf=new Uint8Array(await r.arrayBuffer());
  return {buf,w:+meta.w,h:+meta.h,fmt:meta.fmt};
}
async function showTexture(a,cvId,infoHtml){
  const t=await fetchTex(a.texUrl);
  const cv=$(cvId);cv.width=t.w;cv.height=t.h;
  const ctx=cv.getContext('2d');
  drawTexture(ctx,t.buf,t.w,t.h,t.fmt);
  $('tinfo').innerHTML=infoHtml;
}
function drawTexture(ctx,buf,w,h,fmt){
  const img=ctx.createImageData(w,h);
  const px=img.data;
  if(fmt==='PF_B8G8R8A8'||fmt==='PF_R8G8B8A8'){
    const sw=fmt==='PF_B8G8R8A8';
    for(let i=0,j=0;i<w*h*4;i+=4,j+=4){
      if(sw){px[j]=buf[i+2];px[j+1]=buf[i+1];px[j+2]=buf[i];px[j+3]=buf[i+3];}
      else{px[j]=buf[i];px[j+1]=buf[i+1];px[j+2]=buf[i+2];px[j+3]=buf[i+3];}
    }
  }else if(fmt==='PF_DXT1'){dxt(buf,w,h,px,false,false);}
  else if(fmt==='PF_DXT3'){dxt(buf,w,h,px,true,false);}
  else if(fmt==='PF_DXT5'){dxt(buf,w,h,px,false,true);}
  else if(fmt==='PF_BC5'){bc5(buf,w,h,px);}
  else if(fmt==='PF_BC4'){bc4(buf,w,h,px);}
  else{ctx.fillStyle='#555';ctx.fillText('不支持的格式 '+fmt,10,20);return;}
  ctx.putImageData(img,0,0);
}
const c565=(c)=>{const r=c>>11&31,g=c>>5&63,b=c&31;return[(r<<3)|(r>>2),(g<<2)|(g>>4),(b<<3)|(b>>2)];};
function dxt(buf,w,h,px,dxt3,dxt5){
  const bw=(w+3)>>2,bh=(h+3)>>2,bs=8+(dxt3?8:0)+(dxt5?8:0);
  let o=0;
  for(let by=0;by<bh;by++)for(let bx=0;bx<bw;bx++,o+=bs){
    let al=[255,255,255,255];
    if(dxt3){for(let k=0;k<16;k++)al[k]=(buf[o+ (k>>1)]>>((k&1)*4)&15)*17;}
    else if(dxt5){
      const a0=buf[o],a1=buf[o+1];
      const bits=[];for(let k=0;k<6;k++){const b=buf[o+2+k];for(let t=0;t<8;t++)bits.push(b>>t&1);}
      const idx=[];for(let k=0;k<16;k++){let v=0;for(let t=0;t<3;t++)v|=bits[k*3+t]<<t;idx.push(v);}
      const lut=a0>a1?[a0,a1,(a0*6+a1)/7,(a0*5+a1*2)/7,(a0*4+a1*3)/7,(a0*3+a1*4)/7,(a0*2+a1*5)/7,(a0+a1*6)/7]
                    :[a0,a1,(a0*4+a1)/5,(a0*3+a1*2)/5,(a0*2+a1*3)/5,(a0+a1*4)/5,0,255];
      al=idx.map(i=>lut[i]|0);
    }
    const co=o+(dxt3||dxt5?8:0);
    const c0=buf[co]|buf[co+1]<<8,c1=buf[co+2]|buf[co+3]<<8;
    let p0=c565(c0),p1=c565(c1),p2,p3;
    if(c0>c1){p2=[(p0[0]*2+p1[0])/3,(p0[1]*2+p1[1])/3,(p0[2]*2+p1[2])/3];p3=[(p0[0]+p1[0]*2)/3,(p0[1]+p1[1]*2)/3,(p0[2]+p1[2]*2)/3];}
    else{p2=[(p0[0]+p1[0])/2,(p0[1]+p1[1])/2,(p0[2]+p1[2])/2];p3=[0,0,0];}
    const v32=buf[co+4]|buf[co+5]<<8|buf[co+6]<<16|buf[co+7]<<24;
    for(let py=0;py<4;py++)for(let px2=0;px2<4;px2++){
      const k=py*4+px2;
      const sel=v32>>>(k*2)&3;
      const col=[p0,p1,p2,p3][sel];
      const x=bx*4+px2,y=by*4+py;
      if(x<w&&y<h){const j=(y*w+x)*4;px[j]=col[0];px[j+1]=col[1];px[j+2]=col[2];px[j+3]=al[k];}
    }
  }
}
function bc4(buf,w,h,px){
  const bw=(w+3)>>2,bh=(h+3)>>2;let o=0;
  const dec=(o2)=>{
    const a0=buf[o2],a1=buf[o2+1];
    const bits=[];for(let k=0;k<6;k++){const b=buf[o2+2+k];for(let t=0;t<8;t++)bits.push(b>>t&1);}
    const out=[];for(let k=0;k<16;k++){let v=0;for(let t=0;t<3;t++)v|=bits[k*3+t]<<t;out.push(v);}
    const lut=a0>a1?[a0,a1,(a0*6+a1)/7,(a0*5+a1*2)/7,(a0*4+a1*3)/7,(a0*3+a1*4)/7,(a0*2+a1*5)/7,(a0+a1*6)/7]
                  :[a0,a1,(a0*4+a1)/5,(a0*3+a1*2)/5,(a0*2+a1*3)/5,(a0+a1*4)/5,0,255];
    return out.map(i=>lut[i]|0);
  };
  for(let by=0;by<bh;by++)for(let bx=0;bx<bw;bx++,o+=8){
    const R=dec(o);
    for(let py=0;py<4;py++)for(let px2=0;px2<4;px2++){
      const k=py*4+px2,x=bx*4+px2,y=by*4+py;
      if(x<w&&y<h){const j=(y*w+x)*4;px[j]=px[j+1]=px[j+2]=R[k];px[j+3]=255;}
    }
  }
}
function bc5(buf,w,h,px){
  const bw=(w+3)>>2,bh=(h+3)>>2;
  let o=0;
  const dec=(o2)=>{
    const a0=buf[o2],a1=buf[o2+1];
    const bits=[];for(let k=0;k<6;k++){const b=buf[o2+2+k];for(let t=0;t<8;t++)bits.push(b>>t&1);}
    const out=[];
    for(let k=0;k<16;k++){let v=0;for(let t=0;t<3;t++)v|=bits[k*3+t]<<t;out.push(v);}
    const lut=a0>a1?[a0,a1,(a0*6+a1)/7,(a0*5+a1*2)/7,(a0*4+a1*3)/7,(a0*3+a1*4)/7,(a0*2+a1*5)/7,(a0+a1*6)/7]
                  :[a0,a1,(a0*4+a1)/5,(a0*3+a1*2)/5,(a0*2+a1*3)/5,(a0+a1*4)/5,0,255];
    return out.map(i=>lut[i]|0);
  };
  for(let by=0;by<bh;by++)for(let bx=0;bx<bw;bx++,o+=16){
    const R=dec(o),G=dec(o+8);
    for(let py=0;py<4;py++)for(let px2=0;px2<4;px2++){
      const k=py*4+px2,x=bx*4+px2,y=by*4+py;
      if(x<w&&y<h){const j=(y*w+x)*4;px[j]=R[k];px[j+1]=G[k];px[j+2]=(R[k]+G[k])/2|0;px[j+3]=255;}
    }
  }
}

// ---------- 翻页动画 ----------
async function showAnimSeq(a){
  const t=await fetchTex(a.texUrl);
  const off=document.createElement('canvas');off.width=t.w;off.height=t.h;
  drawTexture(off.getContext('2d'),t.buf,t.w,t.h,t.fmt);
  $('tinfo').innerHTML=`图集 <b>${t.w}×${t.h}</b> ${t.fmt} · ${a.frames.length} 帧 · ${a.fps} FPS`;
  const cv=$('cv');const F=a.frames;
  const maxw=Math.max(...F.map(f=>f.w)),maxh=Math.max(...F.map(f=>f.h));
  cv.width=maxw*2;cv.height=maxh*2;
  cv.style.maxWidth=maxw*4+'px';
  const ctx=cv.getContext('2d');ctx.imageSmoothingEnabled=false;
  let cur=0,playing=false,timer=null;
  function range(){
    const s=Math.max(0,parseInt($('fstart').value)||0);
    const e=Math.min(F.length-1,parseInt($('fend').value)||F.length-1);
    return [s,e];
  }
  function draw(i){
    ctx.clearRect(0,0,cv.width,cv.height);
    const f=F[i];
    const sy=t.h-f.y-f.h;
    ctx.drawImage(off,f.x,sy,f.w,f.h,0,0,cv.width,cv.height);
    const [s,e]=range();
    $('fstat').textContent=`帧 ${i+1}/${F.length} (播放 ${s+1}~${e+1}) · ${f.name||f.n} (${f.x},${f.y})`;
  }
  function play(){
    playing=!playing;
    $('play').textContent=playing?'⏸ 暂停':'▶ 播放';
    if(playing){
      timer=setInterval(()=>{
        const [s,e]=range();
        cur=cur+1;
        if(cur>e)cur=s;
        if(cur<s)cur=s;
        draw(cur);
      },1000/a.fps);
    }
    else clearInterval(timer);
  }
  $('play').onclick=play;
  $('fgif').onclick=()=>{
    const [s,e]=range();
    const f0=F[s],f1=F[e];
    loc2(`/api/gif?path=${enc0(it.p)}&start=${f0.n}&end=${f1.n}`);
  };
  // 帧条高亮当前范围
  function updateHl(){
    const [s,e]=range();
    document.querySelectorAll('#frames canvas').forEach((c,k)=>{
      c.style.borderColor=(k>=s&&k<=e)?'#5aa2ff':'#2e333d';
      c.style.opacity=(k>=s&&k<=e)?'1':'0.3';
    });
  }
  $('fstart').oninput=$('fend').oninput=()=>{updateHl();if(!playing)draw(cur)};
  const fr=$('frames');
  F.forEach((f,i)=>{
    const c=document.createElement('canvas');c.width=f.w;c.height=f.h;
    c.style.cssText='width:48px;height:48px;object-fit:contain;image-rendering:pixelated;cursor:pointer';
    const sy=t.h-f.y-f.h;
    c.getContext('2d').drawImage(off,f.x,sy,f.w,f.h,0,0,f.w,f.h);
    c.onclick=()=>{cur=i;draw(i)};
    fr.appendChild(c);
  });
  draw(0);updateHl();setTimeout(play,300);
}

async function showFlipbook(a){
  const t=await fetchTex(a.texUrl);
  const off=document.createElement('canvas');off.width=t.w;off.height=t.h;
  drawTexture(off.getContext('2d'),t.buf,t.w,t.h,t.fmt);
  $('tinfo').innerHTML=`图集 <b>${t.w}×${t.h}</b> ${t.fmt} · ${a.frames.length} 帧 · ${a.fps} FPS`;
  const cv=$('cv');const F=a.frames;
  const maxw=Math.max(...F.map(f=>f.w)),maxh=Math.max(...F.map(f=>f.h));
  cv.width=maxw;cv.height=maxh;
  const ctx=cv.getContext('2d');
  let cur=0,playing=false,timer=null;
  function draw(i){
    ctx.clearRect(0,0,cv.width,cv.height);
    const f=F[i];
    // 处理 Y 翻转坐标 (uv 为左下原点)
    const sy=t.h-f.y-f.h;
    ctx.drawImage(off,f.x,sy,f.w,f.h,0,0,f.w,f.h);
    $('fstat').textContent=`帧 ${i+1}/${F.length} · (${f.x},${f.y}) ${f.w}×${f.h}`;
    document.querySelectorAll('#frames canvas').forEach((c,k)=>c.style.borderColor=k===i?'#5aa2ff':'#2e333d');
  }
  function play(){
    playing=!playing;
    $('play').textContent=playing?'⏸ 暂停':'▶ 播放';
    if(playing)timer=setInterval(()=>{cur=(cur+1)%F.length;draw(cur)},1000/a.fps);
    else clearInterval(timer);
  }
  $('play').onclick=play;
  $('expcur').onclick=()=>{
    const f=F[cur];
    loc2('/api/png?path='+enc0(a.texPath)+`&x=${f.x}&y=${f.y}&w=${f.w}&h=${f.h}`);
  };
  // 缩略帧条
  const fr=$('frames');
  F.forEach((f,i)=>{
    const c=document.createElement('canvas');c.width=f.w;c.height=f.h;
    const sy=t.h-f.y-f.h;
    c.getContext('2d').drawImage(off,f.x,sy,f.w,f.h,0,0,f.w,f.h);
    c.onclick=()=>{cur=i;draw(i)};
    fr.appendChild(c);
  });
  draw(0);setTimeout(play,300);
}

// ---------- 文本 ----------
function showLocres(a, p){
  const body=$('pbody');
  body.innerHTML=`<div class="btns"><button onclick="loc2('/api/lotxt?path=${encodeURIComponent(p)}')">导出全部文本 (TXT)</button></div>
   <input class="lofilter" id="loq" placeholder="过滤文本… (${a.entries.length} 条)">
   <div id="lotable" style="max-height:60vh;overflow:auto"></div>`;
  const items=a.entries.map(s=>({s,z:/[\u4e00-\u9fff]/.test(s)}));
  $('loq').oninput=()=>{
    const q=$('loq').value.trim();
    const list=(q?items.filter(x=>x.s.includes(q)):items).slice(0,400);
    $('lotable').innerHTML=list.map(x=>`<div style="padding:4px 8px;border-bottom:1px solid #23262d;${x.z?'':'color:#8b93a1'}">${esc(x.s)}</div>`).join('');
  };
  $('loq').oninput();
}
boot();
</script>
</body>
</html>"""


# ================================================================ 服务

def json_safe(o, depth=0):
    """属性值转 JSON 安全类型 (限制深度和总大小)"""
    if isinstance(o, bytes):
        s = o[:16].hex()
        return f"<{len(o)}B {s}…>" if len(o) > 16 else f"<{s}>"
    if isinstance(o, (tuple, list)):
        if depth > 3:
            return f"[{len(o)}项…]"
        return [json_safe(x, depth + 1) for x in o[:50]]
    if isinstance(o, dict):
        if depth > 3:
            return "{…}"
        return {str(k): json_safe(v, depth + 1) for k, v in list(o.items())[:30]}
    if isinstance(o, str) and len(o) > 300:
        return o[:300] + "…"
    if isinstance(o, float) and (o != o or o in (float('inf'), float('-inf'))):
        return 0
    return o


def resolve_flipbook(stem):
    """翻页书 → (fps, frames[{x,y,w,h,name}], 图集stem); 无 UV 帧按网格回退"""
    fb = DB.flipbook_info(stem)
    if not fb or not fb["frames"]:
        return None
    dirp = stem.rsplit("/", 1)[0]
    frames = []
    atlas = None
    at = None
    for fname in fb["frames"]:
        sp = None
        for cand in (f"{dirp}/{fname}", f"{dirp}/Textures/{fname}"):
            if (cand + ".uasset") in DB.entries:
                sp = DB.sprite_info(cand)
                if sp:
                    break
        if sp and sp.get("tex"):
            t = DB.texture_info(sp["tex"])
            if t and t.get("mip") and atlas is None:
                atlas = sp["tex"]
                at = t
            if sp["has_uv"]:
                frames.append(dict(x=int(sp["uv"][0]), y=int(sp["uv"][1]),
                                   w=int(sp["dim"][0]), h=int(sp["dim"][1]), name=fname))
            else:
                frames.append(dict(x=0, y=0, w=int(sp["dim"][0]), h=int(sp["dim"][1]),
                                   name=fname, nousv=True))
    if not atlas:
        return None
    w0 = frames[0]["w"] if frames else 100
    h0 = frames[0]["h"] if frames else 50
    cols = max(1, at["w"] // w0) if w0 else 1
    for i, f in enumerate(frames):
        if f.get("nousv"):
            f["x"] = (i % cols) * w0
            f["y"] = (i // cols) * h0
    return fb["fps"], frames, atlas


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _bin(self, b, ctype, headers=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == "/":
                self._bin(HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            if u.path == "/api/stats":
                total_unc = sum(e.uncompressed_size for e in DB.entries.values())
                self._json(dict(files=len(DB.entries), groups=len(DB.groups),
                                 size=f"{total_unc/1e9:.1f} GB",
                                 cats={c: len(v) for c, v in DB.cat_stems.items()}))
                return
            if u.path == "/api/tree":
                cat = q.get("cat", [""])[0]
                self._json(dict(nodes=build_tree(cat)))
                return
            if u.path == "/api/list":
                cat = q.get("cat", [""])[0]
                query = q.get("q", [""])[0].strip().lower()
                dirf = q.get("dir", [""])[0].strip().rstrip("/")
                offset = int(q.get("offset", ["0"])[0])
                limit = int(q.get("limit", ["300"])[0])
                if query:
                    src = [s for s in DB.groups if query in s.lower()]
                elif cat:
                    src = DB.cat_stems.get(cat, [])
                else:
                    src = list(DB.groups.keys())
                if dirf:
                    pref = dirf + "/"
                    src = [s for s in src if s.startswith(pref)]
                page = src[offset:offset + limit]
                items = []
                for s in page:
                    t = DB.stem_cat.get(s, "")[:8]
                    items.append(dict(p=s, t=t))
                self._json(dict(items=items, total=len(src), more=offset + limit < len(src), cat=cat))
                return
            if u.path == "/api/asset":
                self._api_asset(q.get("path", [""])[0])
                return
            if u.path == "/api/png":
                self._api_png(q)
                return
            if u.path == "/api/frames":
                self._api_frames(q.get("path", [""])[0])
                return
            if u.path == "/api/ogg":
                p = q.get("path", [""])[0]
                snd = DB.sound_info(p)
                if snd and snd.get("ogg"):
                    fn = urllib.parse.quote(p.split("/")[-1]) + ".ogg"
                    self._bin(snd["ogg"], "audio/ogg",
                              {"Content-Disposition": f"attachment; filename*=UTF-8''{fn}"})
                else:
                    self._json({"err": "无音频数据"}, 404)
                return
            if u.path == "/api/lotxt":
                p = q.get("path", [""])[0]
                items = DB.locres_entries(p)
                txt = chr(10).join(items)
                fn = urllib.parse.quote(p.split("/")[-1]) + ".txt"
                self._bin(txt.encode("utf-8"), "text/plain; charset=utf-8",
                          {"Content-Disposition": f"attachment; filename*=UTF-8''{fn}"})
                return
            if u.path == "/api/gif":
                self._api_gif(q)
                return
            if u.path == "/api/audio":
                self._api_audio(q.get("path", [""])[0])
                return
            if u.path == "/api/tex":
                self._api_tex(q.get("path", [""])[0])
                return
            if u.path == "/api/file":
                p = q.get("path", [""])[0]
                b = DB.read_file(p)
                self._bin(b, "application/octet-stream",
                          {"Content-Disposition": f'attachment; filename="{urllib.parse.quote(p.split("/")[-1])}"'})
                return
            if u.path == "/api/export":
                p = q.get("path", [""])[0]
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    for f in DB.groups.get(p, []):
                        z.writestr("/".join(f.split("/")[2:]), DB.read_file(f))
                fn = urllib.parse.quote(p.split("/")[-1]) + ".zip"
                self._bin(buf.getvalue(), "application/zip",
                          {"Content-Disposition": f"attachment; filename*=UTF-8''{fn}"})
                return
            self._json({"err": "not found"}, 404)
        except Exception as ex:
            import traceback
            self._json({"err": f"{ex}", "tb": traceback.format_exc()[-400:]}, 500)

    # ---------- 资产预览
    def _api_asset(self, stem: str):
        if not stem:
            self._json({"err": "no path"}, 400)
            return
        # locres 直通 (stem 可能不带扩展名)
        lp = stem if stem.endswith(".locres") else (stem + ".locres" if (stem + ".locres") in DB.entries else None)
        if lp:
            entries = DB.locres_entries(lp)
            self._json(dict(kind="locres", type="本地化文本", entries=entries[:8000], note=f"共{len(entries)}条"))
            return
        if stem in DB.entries and not stem.endswith(".uasset"):
            self._json(dict(kind="props", type="文件", props={"路径": stem, "大小": len(DB.read_file(stem))}))
            return
        pkg = DB.package_of(stem)
        cls = DB.export_class(pkg)
        base = dict(kind="props", type=cls or "未知", note=stem)
        if cls == "Texture2D":
            t = DB.texture_info(stem)
            if t and t.get("mip"):
                self._json(dict(kind="texture", type="贴图", w=t["w"], h=t["h"], fmt=t["fmt"],
                                src=t["mip"]["source"], texUrl="/api/tex?path=" + urllib.parse.quote(stem)))
                return
            self._json(dict(kind="props", type="贴图", note="无可解码 mip 数据"))
            return
        if cls == "PaperSprite":
            # 序列帧检测: PG_Sprite_14 → 全序列动画
            seq_frames = None
            try:
                seq_frames = DB.sprite_seq_frames(stem)
            except Exception:
                pass
            if seq_frames and len(seq_frames) >= 2:
                atlas = seq_frames[0]["tex"] if seq_frames else None
                t0 = DB.texture_info(atlas) if atlas else None
                if t0 and t0.get("mip"):
                    self._json(dict(kind="animseq", type="精灵序列动画",
                                    fps=10, frames=seq_frames, note=f'{len(seq_frames)} 帧序列',
                                    texPath=atlas,
                                    texUrl="/api/tex?path=" + urllib.parse.quote(atlas)))
                    return
            sp = DB.sprite_info(stem)
            if sp and sp.get("tex"):
                t = DB.texture_info(sp["tex"])
                if t and t.get("mip"):
                    x, y = int(sp["uv"][0]), int(sp["uv"][1])
                    w, h = int(sp["dim"][0]), int(sp["dim"][1])
                    yy = t["h"] - y - h
                    self._json(dict(kind="sprite", type="精灵", w=w, h=h, fmt=t["fmt"],
                                    note=f"源图集 {t['w']}×{t['h']} @({x},{y})",
                                    x=x, y=yy, sw=w, sh=h,
                                    texUrl="/api/tex?path=" + urllib.parse.quote(sp["tex"])))
                    return
            self._json(dict(kind="props", type="精灵", note="无法解析"))
            return
        if cls == "PaperFlipbook":
            rfb = resolve_flipbook(stem)
            if rfb:
                fps, frames, atlas = rfb
                self._json(dict(kind="flipbook", type="翻页动画", fps=fps, frames=frames,
                                note=f"{len(frames)} 帧", texPath=atlas,
                                texUrl="/api/tex?path=" + urllib.parse.quote(atlas)))
                return
            self._json(dict(kind="props", type="翻页动画", note="无帧数据"))
            return
        if cls in ("SoundWave",):
            snd = DB.sound_info(stem)
            if snd:
                info = " / ".join(f"{k}={v}" for k, v in snd["meta"].items()
                                  if isinstance(v, (int, float, bool)) and k != "TotalSamples")
                if snd.get("ogg"):
                    self._json(dict(kind="sound", type="音频", info=info, hasOgg=True))
                else:
                    self._json(dict(kind="props", type="音频", note=info + " · 无内联数据(流式)"))
                return
        if cls == "SoundCue":
            self._json(dict(kind="props", type="SoundCue 混音", note="引用其他 SoundWave"))
            return
        if cls == "DataTable":
            dt = DT_CACHE.get(stem)
            if dt is None:
                try:
                    dt = DB.datatable_info(stem, max_rows=100, time_budget=25.0)
                    DT_CACHE[stem] = dt
                except Exception:
                    dt = None
            if dt:
                cols, rows = dt
                self._json(dict(kind="datatable", type="数据表格",
                                cols=cols,
                                rows=[[rn] + [json_safe(v.get(c), 1) for c in cols] for rn, v in rows[:2000]],
                                note=f"{len(rows)} 行 × {len(cols)} 列"))
                return
            self._json(dict(kind="props", type="数据表格", note="行数据解析失败"))
            return
        if cls in ("DistributionFloatConstant",) or (
                any(e.name.startswith("ParticleSpriteEmitter") for e in pkg.exports)):
            try:
                pi = DB.particle_info(stem)
            except Exception:
                pi = None
            if pi and pi["textures"]:
                self._json(dict(kind="particle", type="粒子特效",
                                emitters=pi["emitters"], modules=pi["modules"],
                                textures=pi["textures"], note=f'{pi["emitters"]} 发射器 · {len(pi["textures"])} 纹理'))
                return
            if pi:
                self._json(dict(kind="props", type="粒子特效",
                                note=f'{pi["emitters"]} 发射器 · 无可用纹理'))
                return
        base_name = stem.rsplit("/", 1)[-1]
        if cls in ("StaticMesh", "SkeletalMesh", "NavCollision") or (
                cls in ("BodySetup",) and base_name.startswith(("SM_", "SK_"))) or (
                cls and base_name.startswith(("SM_", "SK_"))):
            try:
                m = DB.mesh_info(stem)
            except Exception:
                m = None
            if m:
                self._json(dict(kind="mesh", type=cls + " 点云", n=m["n"], lods=m["lods"],
                                bbox=m["bbox"], pts=m["pts"], note=f"{m['n']} 顶点"))
                return
            self._json(dict(kind="props", type=cls, note="无顶点数据"))
            return
        # 通用: 属性
        props = {}
        for e in pkg.exports:
            if e.serial_size == 0 or e.name.startswith("BodySetup"):
                continue
            try:
                r = pkg.export_reader(e)
                pp, ok = pkg.read_properties(r)
                for k, v in pp.items():
                    props[f"{e.name}.{k}"] = json_safe(v[1] if isinstance(v, tuple) else v)
                break
            except Exception:
                pass
        self._json(dict(kind="props", type=cls or "资产", props=props or {"说明": "无属性数据"}))

    def _api_png(self, q):
        stem = q.get("path", [""])[0]
        rect = None
        if "x" in q:
            rect = (int(q["x"][0]), int(q["y"][0]), int(q["w"][0]), int(q["h"][0]))
        try:
            if rect:
                png = DB.texture_png(stem, rect=rect)
            else:
                pkg = DB.package_of(stem)
                cls = DB.export_class(pkg)
                if cls == "PaperSprite":
                    png = DB.sprite_png(stem)
                else:
                    png = DB.texture_png(stem)
        except Exception as ex:
            self._json({"err": str(ex)}, 500)
            return
        fn = urllib.parse.quote(stem.split("/")[-1]) + ".png"
        self._bin(png, "image/png",
                  {"Content-Disposition": f"attachment; filename*=UTF-8''{fn}"})

    def _api_frames(self, stem: str):
        rfb = resolve_flipbook(stem)
        if not rfb:
            self._json({"err": "无帧数据"}, 404)
            return
        fps, frames, atlas = rfb
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for i, f in enumerate(frames):
                png = DB.texture_png(atlas, rect=(f["x"], f["y"], f["w"], f["h"]))
                z.writestr(f"frame_{i:03d}_{f['name']}.png", png)
        fn = urllib.parse.quote(stem.split("/")[-1]) + "_frames.zip"
        self._bin(buf.getvalue(), "application/zip",
                  {"Content-Disposition": f"attachment; filename*=UTF-8''{fn}"})

    def _api_gif(self, q):
        stem = q.get("path", [""])[0]
        fps = int(q.get("fps", ["10"])[0])
        start = q.get("start", [None])[0]
        end = q.get("end", [None])[0]
        frames = None
        # 1) 精灵序列
        try:
            frames = DB.sprite_seq_frames(stem)
        except Exception:
            pass
        # 2) PaperFlipbook
        if not frames:
            try:
                rfb = resolve_flipbook(stem)
                if rfb:
                    _, fbs, atlas = rfb
                    frames = [dict(n=i, x=f["x"], y=f["y"], w=f["w"], h=f["h"],
                                   tex=atlas, name=f["name"]) for i, f in enumerate(fbs)]
            except Exception:
                pass
        if not frames:
            self._json({"err": "无帧数据"}, 404)
            return
        # 帧范围裁剪
        if start is not None or end is not None:
            ns = [f["n"] for f in frames]
            if start is not None:
                s = int(start)
                frames = [f for f in frames if f["n"] >= s]
            if end is not None:
                e = int(end)
                frames = [f for f in frames if f["n"] <= e]
        if not frames:
            self._json({"err": "帧范围为空"}, 400)
            return
        try:
            gif = DB.gif_from_frames(frames, fps=fps)
        except Exception as ex:
            self._json({"err": f"GIF 生成失败: {ex}"}, 500)
            return
        if not gif:
            self._json({"err": "GIF 为空"}, 500)
            return
        fn = urllib.parse.quote(stem.rsplit("/", 1)[-1]) + ".gif"
        self._bin(gif, "image/gif",
                  {"Content-Disposition": f"attachment; filename*=UTF-8''{fn}"})

    def _api_audio(self, stem: str):
        snd = DB.sound_info(stem)
        if snd and snd.get("ogg"):
            self._bin(snd["ogg"], "audio/ogg")
        else:
            self._bin(b"", "audio/ogg")

    def _api_tex(self, stem: str):
        t = DB.texture_info(stem)
        if not t or not t.get("mip"):
            self._bin(b"", "application/octet-stream")
            return
        self._bin(t["mip"]["data"], "application/octet-stream",
                  {"X-w": str(t["w"]), "X-h": str(t["h"]), "X-fmt": t["fmt"]})

    def _crop(self, t, data, x, y, w, h):
        W = t["w"]
        out = bytearray()
        for row in range(y, min(y + h, t["h"])):
            s = (row * W + x) * 4
            out += data[s:s + w * 4]
        return bytes(out)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"资源浏览器: {url}  (Ctrl+C 退出)")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
