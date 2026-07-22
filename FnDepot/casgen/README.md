# casgen（飞牛 FnDepot 社区商店条目）

本目录是用来发到 **飞牛 FnDepot 社区商店** 的应用描述。

## 用法
1. 在 GitHub 新建一个**必须叫 `FnDepot`、且 Public** 的仓库。
2. 把本目录下的 `fnpack.json` 和 `casgen/` 文件夹原样放进去（`casgen/` 里要有 `ICON.PNG` 和 `casgen.fpk`）。
3. 飞牛上装好 FnDepot 客户端 → 添加源 → 填你的 `https://github.com/<你的用户名>/FnDepot` → 就能搜到并一键安装。

> 注意：`casgen.fpk` 里的镜像地址 `ghcr.io/<你的用户名>/casgen:latest` 需要先把主仓库 `casgen` 推到 GitHub（Actions 会自动构建镜像）。
> 安装前请先在真机点一次安装测试；若 FnDepot 客户端对 `.fpk` 内部结构有微调要求，以客户端报错为准就地改 `casgen.fpk` 内的 `docker-compose.yaml`。
