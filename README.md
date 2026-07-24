# 移动云盘影视 CAS 瘦身工具（v5.0）

把移动云盘里**已有的**原视频，零流量变成几 KB 的 `.cas` 文件；播放时从 `.cas` 临时恢复出原视频秒传回云盘。
全部在浏览器里用鼠标操作，**不需要命令行、不需要改代码、不需要联网装任何东西**。

---

## ⚠️ 先看这 4 条重要说明（避免翻车）

1. **本程序主流程只用 Python 标准库，零额外依赖。** 只要设备里有 Python3 就能跑；用 Docker 则连 Python 都不用装（镜像已内置全部所需）。本地直接跑 `python3 app.py` 即可，无需 `pip install`。
2. **接口基于公开逆向分析推测、未官方背书。** 移动139 的接口路径/字段来自公开逆向资料，可能与官方实际实现有出入。如运行报错，网页右下角"原始接口返回"会显示真实返回，对照 `yidong.py` 顶部的接口路径排查，或在仓库提 Issue。
3. **CAS 不是备份。** 它依赖网盘底层的"去重池"——热门影视云端有副本，恢复 = 秒传建引用，零流量；
   **冷门/独一份的文件去重池可能没有，删了就真丢了。** 建议先拿一部热门电影试，确认能恢复再批量删。
4. **天翼(189)暂未完成。** 已知天翼不支持秒传上传，CAS 恢复依赖秒传，所以天翼这条路目前不保证可行。本版只支持移动云盘(139)。

---

## 一、准备：确认有 Python3

> 用 Docker 部署（见第六节）就跳过这一步。

打开终端输入 `python3 --version`：
- 显示 `Python 3.x` → 已有。
- 找不到 → `sudo apt install python3`（Debian/Ubuntu）。

---

## 二、把程序放到机器上

下载源码压缩包解压成 `casgen/` 文件夹，放到任意目录。

> 本机直跑只要 `casgen/` 里的源文件即可；用 Docker 部署则用现成镜像（见第六节），无需拷文件。

---

## 三、启动

### 方式 A：本机直接跑（最简单）
```bash
cd casgen
python3 app.py
```
浏览器打开 `http://localhost:5000`。

### 方式 B：另一台设备跑，本机访问
```bash
cd /opt/casgen            # 在目标设备
python3 app.py
```
本机浏览器打开 `http://目标设备IP:5000`。

### 环境变量（可选）
| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `5000` | HTTP 监听端口（HF Spaces 注入 `7860`，本地/飞牛默认 `5000`） |
| `CASGEN_PUBLIC_URL` | — | casgen 网关对外可达地址，**生成 `.strm` 必须配置**，否则报错 |
| `CASGEN_CAS_CLEANUP_DELAY` | `3600` | CAS 网关临时恢复的真实视频保留秒数（到期自动删，保持"省空间"） |

> 比如飞牛 NAS 部署供局域网播放：`CASGEN_PUBLIC_URL=http://192.168.1.50:5000`。

### 启动后怎么知道是否正常？
- 终端会显示服务已启动 + 版本号 + 关键提示（缺 `CASGEN_PUBLIC_URL` 会黄色警告）。
- 浏览器打开 `http://localhost:5000/api/health`，返回 `{"ok":true,"version":"4.7.0",...}` 即正常。
- Docker 镜像内置 `HEALTHCHECK`，`docker ps` 看 `STATUS` 列是 `healthy` 即正常。

---

## 四、网页里怎么用（全鼠标）

1. **登录**：选"移动云盘(139)"，把浏览器的 `Authorization` 头粘进文本框 → 点"登录并加载根目录"。
   - 拿 Authorization：浏览器登录 `yun.139.com` → F12 → Network → 随便点一个请求 → 请求头里的 `Authorization: Bearer xxxx` → 整串复制粘进去。
2. **选文件夹**：目录树层层展开，点"✔ 选定此文件夹"。
3. **先分析**（convert 页面）：点"① 先分析"，看可生成多少 .cas、跳过多少。
4. **转换**：**先不要勾"删除原视频"**，点"② 开始转换"，零流量生成 .cas（几 KB）。
5. **验证恢复**（restore 页面）：从本地缓存点"恢复"，确认能秒传回来。
6. **确认后再批量删**：回到 convert 页面，勾上"删除原视频"再转一次。

---

## 四·五、分享链接一条龙（v2.0+）

别人分享的 139 链接，直接在本工具里解析 → **整文件夹转存**（含子目录+所有文件类型，不只是视频）→ 自动生成 CAS → 删除原视频。

1. 粘贴分享链接（提取码自动识别 `提取码:xxxx`/`?pwd=xxxx` 等）。
2. 点"① 解析链接" → 列出分享内容。
3. 选目标目录（`📁 浏览选择` 或直接填路径）→ 勾选：
   - ☑ 转存后自动生成 CAS
   - ☑ 生成 CAS 后删除原视频（⚠️ 先确认 .cas 能恢复再勾）
   - ☑ 转存后加入监控（v3.0+，新剧集自动追更）
4. 点"② 转存"。

---

## 四·六、分享链接定时监控（v3.0+）

针对**还在更新中的剧集**：勾选"加入监控"后，工具按设定间隔（最短 60 分钟）自动检查链接，自动转存新增文件 → 自动转 CAS → 自动删原视频。链接失效自动标记停止；登录失效全屏提示重登，重登后自动恢复。

---

## 五、生成 .strm（v4.0+，播放器直挂）

convert 后想把 `.cas` 直接挂到网易爆米花 / 飞牛影视 / OpenList 播放，需要先生成 `.strm`：

1. **部署时设置 `CASGEN_PUBLIC_URL`**（播放器要能访问到的地址，比如 `http://192.168.1.50:5000`）。
2. 进 strm 页面，勾选是否递归子文件夹，点"🚀 开始生成"。
3. 生成的 `.strm` 内容是 `http://你的地址/cas/<139相对路径>`，挂载到播放器后点 .strm → casgen 网关 → 302 直跳 139 直链播放，**不耗 casgen 带宽**。
4. 临时恢复的真实视频保留 `CASGEN_CAS_CLEANUP_DELAY` 秒（默认 1 小时）后自动删。

---

## 六、Docker 部署（飞牛 / 群晖 / 任意 Docker 设备 — 推荐）

镜像由 GitHub Actions 自动构建并推到 Docker Hub：`tianjian518/casgen`（多架构：amd64、arm64、arm/v7）。

### 路线 A：飞牛 Docker「镜像仓库」搜 `tianjian518/casgen` 拉取运行
端口映射 `5000:5000`，浏览器开 `http://飞牛IP:5000`。

### 路线 B：docker compose
```bash
docker compose up -d   # 拉镜像运行（或加 --build 本地构建）
```

### HF Spaces（抱脸）
SDK 选 Docker，把仓库文件 push 上去，HF 自动构建并用注入的 `PORT` 启动。
⚠️ Space 默认**公开**，建议设为 Private 或加访问密码，避免别人拿到你的 139 token。

---

## 七、出错/排错

| 症状 | 原因 + 处理 |
|---|---|
| 登录按钮一直转圈 | ① 前端 90s 会自动超时；② 容器启动时调度器抢占 139 资源（v4.6 已修：登录成功前调度器不扫描） |
| `登录失败：200000400 msisdn 格式不正确` | 139 转存/分享接口必填手机号，登录后自动从 token 解码注入（v3.0 修过） |
| `生成 .cas 失败：资源配额不足(00010012)` | 139 免费云盘配额被撑满。**勾选"生成后删除原视频"** 释放空间（v4.6 改为"先删后传"，自动绕开配额） |
| `CASGEN_PUBLIC_URL 未配置，无法生成 .strm` | 部署时设环境变量 `CASGEN_PUBLIC_URL=http://你的IP:端口` |
| `生成 .strm 失败：资源配额不足 / HTTP 04000002` | v4.6 修了 `.strm` 上传残留的 `fileRenameMode` 字段；若仍失败刷新镜像到 v4.6+ |
| 操作页面卡死 | 浏览器开 F12 → Network 看具体请求；服务端日志（容器 `docker logs`）会打 `METHOD path -> code (ms)` |
| `/api/health` 返回 false | 服务异常；`docker logs <容器>` 看日志；`docker restart` |
| 容器启动后被 OOM 杀掉 | 飞牛 ARM 内存小的（<1G）偶发；进入容器计划把 `monitor` 间隔调长或暂停监控 |

> 任何报错先看网页右下角"🛠 原始接口返回"或 `docker logs`，里面是真实返回，可据此排查。

---

## 八、健康检查 / 版本端点（运维用）

| 端点 | 用途 |
|---|---|
| `GET /api/health` | 服务存活 + 登录态 + 调度器状态 + 运行时长 |
| `GET /api/version` | 版本号 + 关键配置（`CASGEN_PUBLIC_URL` 是否配置） + 端点列表 |
| `GET /cas/<rel>` | CAS 播放网关（v4.0+）|

Docker 镜像内置 `HEALTHCHECK`，每 30s 探活一次，`docker ps` 的 STATUS 列会显示 `healthy / unhealthy`。

---

## 九、安全提示

- **不要公开分享你的移动云盘 `Authorization`**，它等同于你的登录态，泄露即账号失窃。
- `casgen_auth.json` 是**明文 token**，存放在程序目录。本工具定位**单机/NAS 自用**，别把它暴露到公网；
  如必须公网访问（HF Spaces），务必把 Space 设为 Private 或加访问密码。
- **CAS 不是备份**，冷门/独一份文件删了可能真丢，先用热门影视验证可恢复再批量删。
- 任何自动化（监控/批量删除）务必设最小权限，先小范围试。

---

## 十、文件结构

```
casgen/
├── app.py              # HTTP 服务 + 路由（含 /api/health / /api/version）
├── yidong.py           # 139 移动云盘接口（list/upload/restore/generate_cas 等，含重试退避）
├── share139.py         # 分享链接解析/转存（V6 明文协议 + 可选加密外链）
├── monitor.py          # 分享链接定时监控（守护线程，登录成功前不扫）
├── monitor_store.py    # 监控配置持久化
├── rename.py           # L1 本地正则重命名
├── utils.js            # 前端公共库（API/Toast/loading/自动续登/共享 CSS）
├── index.html / convert.html / share.html / strm.html / restore.html / rename.html
├── healthcheck.py      # Docker HEALTHCHECK 探针
├── Dockerfile          # 多架构镜像（非 root + HEALTHCHECK）
├── docker-compose.yml  # 一键运行
├── .github/workflows/docker.yml   # 推 main 自动构建并冒烟测试
└── README.md
```

---

## 十一、版本说明

- **v5.0.0**：修复 .strm 链接缺失上层目录导致无法播放的关键 bug + 三项增强 ——
  - **修复（关键）**：`.strm` 内链接此前只含选中目录及其子目录（缺从云盘根到所选目录的上层路径），播放网关从云盘根解析失败。现前端把完整路径（如 `电影/科幻`）一并传给后端，`walk` 用 `path_prefix` 拼出完整相对路径，链接形如 `http://IP:5000/cas/电影/科幻/星际穿越/xxx.cas`，可正确解析播放。
  - **旧 .strm 自动清理**：Strm 页新增「删除已存在的旧 .strm 后重新生成」选项，勾选后自动删除同名旧文件再重写，免去手动清理（升级后旧版错误链接需重生成）。
  - **路径完整性校验**：未取到完整目录路径时，前端提示去首页重选、后端 `generate_strm` 显式报错，避免生成无效 `.strm`。
  - **集成包同步**：FnDepot / fnOS 部署清单补充 WebDAV 与 `CASGEN_PUBLIC_URL` 等环境变量，支持新功能开箱即用。
- **v4.9.0**：新增 WebDAV 服务 ——
  - 同端口 `/dav/` 前缀提供 WebDAV 协议，播放器可直接挂载 139 云盘里的 `.strm` 文件。
  - 支持 WebDAV 方法：OPTIONS / PROPFIND / GET / HEAD / PUT / DELETE / MKCOL / MOVE / LOCK / UNLOCK。
  - Basic Auth 认证，独立用户名/密码通过 `CASGEN_WEBDAV_USER` / `CASGEN_WEBDAV_PASS` 配置；未配置时 WebDAV 禁用并返回 503。
  - 可选 `CASGEN_WEBDAV_ROOT` 限定暴露的子树（默认 `root` 即整个云盘）。
  - `.strm` 文件 GET 时自动解析内容：若指向自身 `/cas/` 网关，则通过 `cas_get_play_link` 秒传恢复并 302 重定向到 139 直链；外部 URL 直接 302。
  - 新增 `webdav.py` 纯标准库模块；`yidong.py` 新增 `resolve_path()`（支持目录/文件两种末尾节点）和 `upload_file()`（通用二进制上传）。
  - 首页增加 WebDAV 挂载说明卡片，自动显示挂载地址/用户名/根目录。
- **v4.8.0**：全面调试修复 ——
  - **关键 Bug**：`strm.html` 前端发送 `action:"strm_create"` 但后端只认 `"generate_strm"`（Strm 页面按钮永远报"未知操作"）→ 统一双向兼容；前端 status 字段 `"created"`/`"skipped_no_cas"` 与后端 `"uploaded"`/`"skipped_existing"` 不匹配 → 统一。
  - **文件夹判断**：`yidong._is_folder()` 对数字类型 `fileType` 调 `.lower()` 抛 `AttributeError` → 健壮化（str+int+isFolder 三路兼容）；`app._fmt()` 把数字 1 和 2 都当文件夹（2=文件被误判）→ 修正为仅 1=文件夹。
  - **自动重登递归**：`api()` 检测 `needLogin` → 调 `doAutoReLogin()` → 内部调 `api(login)` → 若 login 也 `needLogin` 则无限递归 → `doAutoReLogin` 改用裸 `fetch` 发 login 请求。
  - **登录态恢复**：`_restore_auth()` token 过期时仍把 CLIENT 挂上（后续操作全失败）→ token 过期直接 return 不挂 CLIENT。
  - **前端统一**：5 个子页面版本号 `<span id="ver">` 从未赋值 → `utils.js` 新增 `initPage()` 自动从 `/api/version` 填充 + 启动登录态定时检测；`authBanner` 元素所有页面都不存在导致 `showAuthBanner()` 静默失效 → 改为无元素时 `showToast` 提示。
  - **Strm 页 UX**：未配置 `CASGEN_PUBLIC_URL` 时页面加载即显示醒目警告并禁用按钮（之前要点"生成"才报错）；去掉无用的"递归遍历" checkbox（后端 `walk()` 本身递归）。
  - **数据一致性**：`monitor_add` interval 硬编码 `60` → 统一用 `monitor_store.MIN_INTERVAL`；`share_parse`/`share_save` 未传 `phone`/`MSISDN` 给 139 分享接口 → 补传。
  - **安全加固**：静态文件服务路径拼接加 `abspath` 前缀检查（防御路径穿越）。
  - **表格友好显示**：`restore.html`/`share.html` 表格 size 列从原始字节数 → `fmtSize()` 友好格式。
- **v4.7.0**：全面打磨 ——
  - **UX**：全局 toast 通知、按钮 loading 态、危险操作（rename 执行 / restore / 删除监控）确认弹窗、所有子页面"登录失效"自动续登（utils.js 统一 doAutoReLogin）、移动端响应式（toast + nav + table 横向滚动）、首页"使用三步" onboarding、友好空状态。
  - **工程稳健**：优雅停机（SIGTERM/SIGINT → 停调度器 → 关服务，Docker stop 干净退出）、请求日志带耗时（`METHOD path -> code (B, ms)`）、`/api/health` + `/api/version` 健康检查端点、启动期配置校验（端口合法性 + `CASGEN_PUBLIC_URL` 提示）、`yidong._post_json` 指数退避重试（连接错误/超时/5xx，4xx 不重试）、`handle()` 捕获 `BrokenPipeError` 防客户端断连报错、响应头 `Cache-Control: no-store` 防止前端缓存。
  - **DevOps**：Dockerfile 改为**非 root 用户**运行 + HEALTHCHECK（`healthcheck.py` 探针）；GitHub Actions 加 **smoke test** 步骤（启动镜像 → 访问 `/api/health` + `/api/version` → 通过才算构建成功）。
- **v4.6.0**：整文件夹转存（顶层目录走 `ca_path_lst`，139 连子目录树与所有文件类型一起转）；`generate(delete_source=True)` 改为"先删原视频释放空间、再上传 .cas"两步法绕开 139 免费云盘配额（实测 199/238 成功）；删除分批每批 100；`.strm` 上传移除 139 拒收的 `fileRenameMode` 字段；`generate_strm` 已存在 `.strm` 跳过；首页彩色渐进标题（蓝→紫→粉） + `v4.6` 胶囊；6 个页面统一常驻顶部导航栏。
- **v4.0.0**：CAS→Strm 自实现（casgen 自带 `/cas` 播放网关，302 直链 + 延迟清理临时文件），网易爆米花/飞牛影视直挂 `.cas` 播放；L1 本地正则重命名（零联网去水印）。
- **v3.0.0**：分享链接定时监控（自动追更新增剧集）+ 登录态失效检测与重登。
- **v2.0.0**：分享链接一条龙（解析 → 转存 → 自动生成 CAS → 删除原视频）；零额外 pip 依赖；视频白名单过滤。
- **v1.0.0**：139 影视零流量转 CAS 首版。

> 每个版本都有独立镜像标签：`tianjian518/casgen:latest`（main 最新）、`:x.y.z`、`:x.y`、`:x`。
> 例如固定 v4.6：`docker pull tianjian518/casgen:4.6`。