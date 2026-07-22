# 网盘影视 转 CAS 瘦身工具（零依赖版）

把网盘里**已有的**原视频，零流量变成几 KB 的 `.cas` 文件，然后删掉原视频腾出空间；
播放时从 `.cas` 临时恢复出原视频。全部在浏览器里用鼠标操作，**不需要命令行、不需要改代码、不需要联网装任何东西**。

---

## ⚠️ 先看这 3 条重要说明（避免翻车）

1. **本程序是纯标准库写的，不依赖 Docker、不依赖 pip。** 只要电脑/盒子里有 Python3 就能跑；用 Docker 则连 Python 都不用装。
2. **接口是逆向推测、未真机实测。** 移动139 的接口路径/字段基于光鸭版 OpenList 139 驱动推测。
   如果运行报错，网页右下角"原始接口返回"会显示真实返回，把那段发我，我改几行就能跑。
3. **CAS 不是备份。** 它依赖网盘底层的"去重池"——热门影视云端有副本，删索引留数据，恢复=秒传建引用；
   **冷门/独一份的文件去重池可能没有，删了就真丢了。** 建议先拿一部热门电影试，确认能恢复再批量删。
4. **天翼(189)暂未完成。** 因为你确认过"天翼不支持秒传上传"，CAS 恢复依赖秒传，所以天翼这条路目前不保证可行
   （详见下面"关于天翼"一节）。本版只支持移动云盘(139)。

---

## 一、准备：确认有 Python3

> 如果你用 **Docker 部署（见第七节）**，这步和下面的 Python 启动都不需要，跳过即可。

打开终端（统信叫"终端"，N1 用 SSH 或飞牛的终端），输入：

```
python3 --version
```

- 显示 `Python 3.x` → 已有，跳过下一步。
- 提示找不到命令 → 安装：
  - 统信 UOS：`sudo apt install python3`
  - 飞牛 N1（Debian）：`sudo apt update && sudo apt install python3`

> 注意：这里用的是系统自带的 `apt` 源（国内能连），**不是 pip、不是 Docker**，所以不受你之前的网络报错影响。

---

## 二、把程序放到机器上

把 `casgen` 这个文件夹整体拷过去：
- **在统信上直接跑（最简单）**：下载下面的压缩包，解压到任意目录（比如 `桌面/casgen`）。
- **在 N1 上跑**：把 `casgen` 文件夹用飞牛的"文件管理"传到盒子里的某个目录（比如 `/vol1/1000/casgen`）。

文件夹里只需这 4 个文件：`app.py`、`yidong.py`、`index.html`、`README.md`。

---

## 三、启动（两种方式，任选其一）

### 方式 A：统信本机直接跑（推荐，最省事）
1. 终端进入文件夹：`cd 桌面/casgen`（路径按你实际解压位置改）
2. 启动：`python3 app.py`
3. 浏览器打开：`http://localhost:5000`

### 方式 B：在 N1 盒子上跑，用统信的浏览器访问
1. 在 N1 终端进入文件夹：`cd /vol1/1000/casgen`
2. 启动：`python3 app.py`
3. 在统信的浏览器打开：`http://N1的IP:5000`
   （N1 的 IP 在飞牛后台能看到，例如 `http://192.168.1.50:5000`）

> 启动后终端会显示"CAS 转换服务已启动"。要停止就按 `Ctrl+C`。

---

## 四、网页里怎么用（全鼠标）

1. **登录**：选"移动云盘(139)"，把浏览器的 `Authorization` 头粘进文本框，点"登录并加载根目录"。
   - 怎么拿 Authorization：用电脑浏览器登录 `yun.139.com` → 按 F12 打开"开发者工具" → 切到 Network(网络) 标签 →
     随便点一个网盘请求 → 在请求头(Request Headers)里找 `Authorization: Bearer xxxx` → 把整串 `Bearer xxxx` 复制下来粘进去。
2. **选文件夹**：在目录树里一层层点进你想处理的影视文件夹，点"✔ 选定此文件夹用于转换"。
3. **先分析**：点"① 先分析"，会告诉你这个文件夹里有多少个能转、多少个跳过（云端没 SHA256 的跳过）。
4. **转换**：**先不要勾"删除原视频"**，点"② 开始转换"。它会零流量生成 `.cas` 文件（几 KB）。
5. **验证恢复**：在"第 4 步"里点"恢复"，确认网盘里能重新生成原视频。
6. **确认无误后再批量删**：回到第 3 步，**勾上"删除原视频"**再点一次转换，才会删源腾空间。

---

## 五、关于天翼(189)

移动(139)的 CAS 恢复依赖"按哈希秒传"，你已确认**天翼(189)不支持秒传上传**，
所以天翼的"零流量生成 CAS + 恢复"目前找不到可行路径。因此本版只做了 139。
如果你坚持要天翼，需要换思路（比如浏览器复制 cookie 登录 + 依赖天翼自己的去重），
但成功率未知。是否继续攻克天翼，请告诉我。

---

## 六、出错怎么处理

网页右下角"🛠 原始接口返回"里会显示程序收到的真实返回。把那段内容发给我，
我据此微调 `yidong.py` 顶部的接口路径（`list_path` 等）即可，你不用改任何代码。

---

## 七、用 Docker 部署（飞牛 FnOS / 群晖 / 任意 Linux 盒子通用 —— 推荐）

镜像由本仓库的 **GitHub Actions 自动构建并推到 Docker Hub**，所以你在飞牛 Docker 的「镜像仓库」里就能直接搜到，不用装 Python、不用管依赖。

### 路线 A：飞牛 FnOS Docker「镜像仓库」直接搜到安装（你要的这条）
1. 飞牛应用中心装好「Docker」应用（系统自带）。
2. 打开 Docker → **镜像仓库** → 搜索 `tianjian518/casgen`
   （把前缀 `tianjian518` 换成**你自己的 Docker Hub 用户名**；注册/获取见下方"Docker Hub 账号"）。
3. 搜到后点「拉取」，等进度条走完。
4. 切到「容器」→「创建容器」→ 镜像选刚拉取的 `tianjian518/casgen` →
   **端口映射**填：容器端口 `5000` → 主机端口 `5000` → 确定。
5. 浏览器打开 `http://飞牛IP:5000` 即可用。

> 第一次把代码推到 GitHub 后，约 1~2 分钟镜像才在 Docker Hub 就绪；之后每次推代码会自动重新构建。

### 路线 B：用 docker compose（电脑/盒子命令行均可）
把本仓库的 `docker-compose.yml` 放到任意目录，执行：
```bash
docker compose up -d        # 直接拉 Docker Hub 镜像运行
# 或本地从源码构建： docker compose up -d --build
```
浏览器开 `http://IP:5000`。

### Docker Hub 账号（镜像就推在这里）
- 注册：打开 hub.docker.com → Sign up，Docker ID 建议全小写英文（这就是镜像名前缀）。
  也可直接「Continue with GitHub」用 GitHub 登录（Docker ID 会等于你的 GitHub 用户名）。
- 生成推送令牌：头像 → Account settings → Security → New Access Token（权限 Read & Write）。
- 本仓库的 GitHub Actions 用 `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` 两个仓库密钥把镜像推上去，
  密钥由维护者（你）在 GitHub 仓库 Settings → Secrets 里配置，令牌用完可随时删除。

---

## 八、GitHub 仓库说明

本仓库即镜像源码，Docker 镜像由 GitHub Actions 自动构建并推到 Docker Hub：

```
casgen/  （仓库根目录）
├── app.py / yidong.py / index.html   # 程序本体（纯标准库，零依赖）
├── README.md                         # 使用说明
├── LICENSE                           # MIT
├── Dockerfile                        # 镜像构建
├── docker-compose.yml                # 飞牛/任意 Docker 运行
└── .github/workflows/docker.yml      # 推 main 自动构建并推到 Docker Hub
```

> 另：若以后想走飞牛「应用市场 / FnDepot 社区商店」路线，可基于此仓库另行打包 `.fpk` 并建 `FnDepot` 公开仓库，
> 本仓库默认只提供 Docker Hub 镜像这一种安装方式。

安全提醒：
- **CAS 不是备份**，冷门/独一份的文件删了可能真丢，先拿热门影视试。
- 不要公开分享你的移动云盘 `Authorization`，它会让你登录态泄露。
- GitHub PAT / Docker Hub Token 都是"钥匙"，部署完成后请到对应网站删掉（revoke），别长期留着。
