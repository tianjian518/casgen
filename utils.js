// casgen 公共函数库 — 所有子页面共享
// 用法：<script src="utils.js"></script>

// ========== 基础工具函数 ==========
function esc(s){
  return (s==null?"":String(s)).replace(/[&<>]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
}

function dbg(o){
  const el = document.getElementById("debug");
  if(el) el.textContent = JSON.stringify(o, null, 2);
}

function readLocal(k){
  try{ return localStorage.getItem(k); }catch(e){ return null; }
}

function writeLocal(k, v){
  try{ localStorage.setItem(k, v); }catch(e){}
}

// ========== API 调用（含登录态失效检测 + 自动重登） ==========
const API_TIMEOUT_MS = 90000;  // 前端超时：超过 90s 未响应视为失败，避免界面永远“处理中…”
async function api(payload){
  let resp, r;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), API_TIMEOUT_MS);
  try {
    resp = await fetch("/", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload), signal: ctrl.signal});
    r = await resp.json();
  } catch(e){
    clearTimeout(timer);
    if (e && e.name === "AbortError"){
      return {ok:false, error:"请求超时（>90秒无响应），请稍后重试。若持续超时，多为部署环境到 139 网络不畅。", needLogin:false};
    }
    return {ok:false, error:"请求失败: "+e.message, needLogin:false};
  }
  clearTimeout(timer);
  if(r && r.needLogin){
    // 触发自动重登（utils.js 已统一定义 doAutoReLogin，所有子页面都生效）
    if(typeof doAutoReLogin === 'function'){
      const retry = await doAutoReLogin(payload);
      return retry || {ok:false, needLogin:true, error:"登录已失效"};
    }
    alert("登录已失效，请返回首页重新登录");
    window.location.href = "index.html";
    return {ok:false, needLogin:true, error:r.error};
  }
  return r;
}

// 登录态检测横幅
function showAuthBanner(){
  // 如果页面有 authBanner 元素就显示；否则用 toast 提示（不阻塞操作）
  const b=document.getElementById("authBanner");
  if(b) b.style.display="block";
  else showToast("登录已失效，请返回首页重新登录","warn",6000);
}
function hideAuthBanner(){ const b=document.getElementById("authBanner"); if(b) b.style.display="none"; }

// ========== 子页面通用初始化：版本号 + 登录态检测 ==========
// 在 <script src="utils.js"> 后的页面加载时调用，自动填充版本号并启动定时登录态检测。
async function initPage(){
  // 1) 填充版本号（从 /api/version 获取）
  try {
    const vr = await fetch("/api/version").then(r=>r.json()).catch(()=>null);
    if(vr && vr.ok && vr.version){
      const verEl = document.getElementById("ver");
      if(verEl) verEl.textContent = "v"+vr.version;
    }
  } catch(e){}
  // 2) 登录态检测（立即一次 + 每 60s 轮询）
  async function _checkAuth(){
    try{
      const d = await api({action:"login_status"});
      if(d && d.expired) showAuthBanner(); else hideAuthBanner();
    }catch(e){}
  }
  _checkAuth();
  setInterval(_checkAuth, 60000);
}

// ========== 目录选择器（可复用的 mountFolderPicker） ==========
// prefix: UI 元素 ID 前缀（如 "share"），targetInputId: 选中后回填的输入框 ID
function mountFolderPicker(prefix, targetInputId){
  let pick = {currentParent:"root", path:[["root","根目录"]]};
  const picker = document.getElementById(prefix+"Picker");
  const crumb = document.getElementById(prefix+"Crumb");
  const folders = document.getElementById(prefix+"Folders");
  const cur = document.getElementById(prefix+"Cur");
  const input = document.getElementById(targetInputId);
  
  if(!picker || !crumb || !folders){
    console.warn("[mountFolderPicker] 缺少必需元素:", prefix, {picker:!!picker, crumb:!!crumb, folders:!!folders});
    return;
  }
  
  function renderCrumb(){
    crumb.innerHTML = pick.path.map((p,i)=>'<a data-i="'+i+'">'+esc(p[1])+'</a>').join(" / ");
    crumb.querySelectorAll("a").forEach(a=>{
      a.onclick = async () => {
        const i = +a.dataset.i;
        pick.path = pick.path.slice(0, i+1);
        pick.currentParent = pick.path[i][0];
        const r = await api({action:"list", parent:pick.currentParent});
        dbg(r);
        if (r && r.ok && Array.isArray(r.items)){
          renderFolders(r.items);
        } else {
          alert("加载路径数据失败：" + (r?.error || "未知错误"));
        }
        renderCrumb();
      };
    });
  }
  
  async function renderFolders(items){
    folders.innerHTML = "";
    if (!items || !Array.isArray(items)){
      folders.innerHTML = '<span class="err">加载失败，请重试</span>';
      return;
    }
    items.forEach(it=>{
      if (!it || typeof it !== "object") return;
      if (it.type !== "folder") return;
      const d = document.createElement("span");
      d.className = "folder";
      d.textContent = "📁 " + it.name;
      d.onclick = async () => {
        const fid = it.fileId || it._rawId || "";
        if (!fid){ alert("该目录缺少 fileId，无法展开。"); return; }
        pick.currentParent = fid;
        pick.path.push([fid, it.name]);
        const r = await api({action:"list", parent:fid});
        dbg(r);
        if (r && r.ok && Array.isArray(r.items)){
          renderFolders(r.items);
        } else {
          alert("加载子目录失败：" + (r?.error || "未知错误"));
          pick.path.pop();
        }
        renderCrumb();
      };
      folders.appendChild(d);
    });
    if (!folders.children.length) folders.innerHTML = '<span class="muted">（此文件夹内没有子文件夹）</span>';
  }
  
  document.getElementById(prefix+"Btn").onclick = async () => {
    picker.style.display = "block";
    pick = {currentParent:"root", path:[["root","根目录"]]};
    const r = await api({action:"list",parent:"root"});
    dbg(r);
    if (r && r.ok && Array.isArray(r.items)){
      renderFolders(r.items);
    } else {
      folders.innerHTML = '<span class="err">加载根目录失败：' + esc(r?.error || '未知错误') + '</span>';
    }
    renderCrumb();
    if (cur) cur.textContent = "";
  };
  
  document.getElementById(prefix+"Use").onclick = () => {
    const parts = pick.path.slice(1).map(p=>p[1]);
    input.value = parts.join("/");
    if (cur) cur.textContent = "已选：" + (parts.join("/") || "根目录");
    picker.style.display = "none";
  };
  
  document.getElementById(prefix+"NewBtn").onclick = async () => {
    const name = document.getElementById(prefix+"NewName").value.trim();
    if (!name){ if(cur) cur.textContent="请输入目录名"; return; }
    const r = await api({action:"create_folder",parent:pick.currentParent,name});
    dbg(r);
    if (!r.ok){ if(cur) cur.textContent="创建失败："+esc(r.error); return; }
    pick.currentParent = r.fileId;
    pick.path.push([r.fileId, name]);
    document.getElementById(prefix+"NewName").value = "";
    const r2 = await api({action:"list",parent:r.fileId});
    dbg(r2);
    if (r2 && r2.ok && Array.isArray(r2.items)){
      renderFolders(r2.items);
    }
    renderCrumb();
    if (cur) cur.textContent = "已新建并进入：" + name;
  };
  
  document.getElementById(prefix+"Cancel").onclick = () => {
    picker.style.display = "none";
  };
}

// ========== 监控状态文本 ==========
function monStatusText(m){
  const map = {ok:"正常 ✔",paused:"已暂停(需重登)",invalid:"链接失效 ✖",error:"出错(将重试)",no_client:"未登录"};
  return map[m.status] || (m.status||"未知");
}

// ========== 共享 CSS（注入一次，所有页面生效） ==========
(function injectSharedStyles(){
  if (document.getElementById('casgen-shared-style')) return;
  const s = document.createElement('style');
  s.id = 'casgen-shared-style';
  s.textContent = `
    /* Toast 通知 */
    .toast { position: fixed; top: 68px; left: 50%; transform: translateX(-50%);
      background: #1f2937; color: #fff; padding: 10px 18px; border-radius: 8px;
      font-size: 14px; z-index: 9999; box-shadow: 0 6px 20px rgba(0,0,0,0.2);
      animation: toast-in .25s ease; max-width: 92vw; pointer-events: none; }
    .toast-success { background: #16a34a; }
    .toast-error   { background: #dc2626; }
    .toast-warn    { background: #d97706; }
    .toast-info    { background: #2563eb; }
    .toast-out { animation: toast-out .3s ease forwards; }
    @keyframes toast-in  { from {opacity:0; transform:translate(-50%,-14px);} to {opacity:1;} }
    @keyframes toast-out { to   {opacity:0; transform:translate(-50%,-14px);} }
    /* 按钮 loading */
    .btn-loading { opacity: .65; pointer-events: none; }
    /* 空状态 CTA */
    .empty-cta { background: #fffbeb; border:1px dashed #fcd34d; border-radius:10px;
      padding: 22px; text-align:center; margin: 14px 0; }
    .empty-cta .big { font-size: 17px; font-weight: 700; color: #92400e; margin-bottom: 8px; }
    .empty-cta .sub { color:#78716c; font-size:13px; margin-bottom: 12px; }
    .empty-cta a.btn { display:inline-block; background:#f59e0b; color:#fff;
      padding: 9px 22px; border-radius: 6px; text-decoration:none; font-weight:600; }
    /* 移动端响应式（≤640px） */
    @media (max-width: 640px) {
      .topnav { padding: 8px 10px !important; }
      .topnav .brand { font-size: 13px; margin-right: 4px; }
      .topnav a { font-size: 12px; padding: 5px 8px; }
      .topnav .navspacer { display: none; }
      .brand-title { font-size: 19px !important; }
      .ver-chip { font-size: 11px !important; padding: 2px 8px !important; }
      .card { margin: 8px 0; padding: 12px; }
      h1 { font-size: 18px; }
      table { display: block; overflow-x: auto; white-space: nowrap; }
      textarea, input[type=text] { font-size: 14px; }
    }
  `;
  document.head.appendChild(s);
})();

// ========== Toast 通知 ==========
function showToast(msg, type="info", duration=3500){
  try{
    const t = document.createElement("div");
    t.className = "toast toast-" + type;
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(()=>{ t.classList.add("toast-out"); setTimeout(()=>t.remove(), 320); }, duration);
  }catch(e){ /* fallback */ try{ console.log("[toast]", type, msg); }catch(_){ } }
}

// ========== 按钮 loading 助手 ==========
async function withLoading(btn, fn, loadingText="处理中…"){
  if(!btn || !(btn instanceof HTMLElement)) return fn();
  const orig = btn.textContent;
  const wasDisabled = btn.disabled;
  btn.textContent = loadingText;
  btn.classList.add("btn-loading");
  btn.disabled = true;
  try { return await fn(); }
  finally {
    btn.textContent = orig;
    btn.classList.remove("btn-loading");
    btn.disabled = wasDisabled;
  }
}

// ========== 自动续登（搬到 utils.js，所有子页面都生效） ==========
// 注意：login 请求使用裸 fetch 而非 api()，避免 needLogin 响应触发递归。
async function doAutoReLogin(originalPayload){
  const saved = readLocal("casgen_auth");
  if(!saved) return null;
  const prov = readLocal("casgen_prov") || "139";
  try {
    const loginResp = await fetch("/", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"login", provider:prov, authorization:saved})});
    const loginR = await loginResp.json();
    if(!loginR || !loginR.ok) return null;
  } catch(e){
    return {ok:false, error:"自动重登失败: "+e.message};
  }
  try {
    const resp = await fetch("/", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(originalPayload)});
    return await resp.json();
  } catch(e){
    return {ok:false, error:"请求失败: "+e.message};
  }
}

// ========== 文件大小友好显示 ==========
function fmtSize(n){
  n = Number(n)||0;
  if(n < 1024) return n + " B";
  if(n < 1024*1024) return (n/1024).toFixed(1) + " KB";
  if(n < 1024*1024*1024) return (n/1024/1024).toFixed(1) + " MB";
  return (n/1024/1024/1024).toFixed(2) + " GB";
}

// ========== 安全复制到剪贴板 ==========
async function copyText(text){
  try{ await navigator.clipboard.writeText(text); showToast("已复制到剪贴板","success",2000); return true; }
  catch(e){ showToast("复制失败，请手动复制","error"); return false; }
}
