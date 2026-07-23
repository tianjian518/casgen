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
async function api(payload){
  let resp, r;
  try {
    resp = await fetch("/", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload)});
    r = await resp.json();
  } catch(e){
    return {ok:false, error:"请求失败: "+e.message, needLogin:false};
  }
  if(r && r.needLogin){
    // 触发自动重登逻辑（如果 index.html 定义了 doAutoReLogin）
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
function showAuthBanner(){ const b=document.getElementById("authBanner"); if(b) b.style.display="block"; }
function hideAuthBanner(){ const b=document.getElementById("authBanner"); if(b) b.style.display="none"; }

// ========== 目录选择器（可复用的 mountFolderPicker） ==========
// prefix: UI 元素 ID 前缀（如 "share"），targetInputId: 选中后回填的输入框 ID
function mountFolderPicker(prefix, targetInputId){
  let pick = {currentParent:"root", path:[["root","根目录"]]];
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
