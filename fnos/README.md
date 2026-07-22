# casgen（飞牛应用中心 .fpk 工程）

本目录按飞牛官方开发文档组织，可用官方 `fnpack` CLI 打包成 `casgen.fpk`，
再在飞牛「应用中心 → 手动安装」上传。

```
fnos/
├── manifest                     # INI 元数据
├── app/docker/docker-compose.yaml
├── wizard/install               # 安装向导字段（端口等）
├── ICON.PNG                     # 64×64 图标
└── README.md
```

打包（需在装有 fnOS SDK / fnpack CLI 的开发机执行）：
```
cd fnos
fnpack build
```

> 镜像 `ghcr.io/<你的用户名>/casgen:latest` 需先把主仓库推到 GitHub 由 Actions 自动构建。
> 本工程依公开文档编写，上架前请在真机实测一次。
