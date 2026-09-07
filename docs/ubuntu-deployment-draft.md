# Ubuntu 部署步骤草案（暂不执行）

> 本文仅用于验证服务器兼容性。只有本机真实频道和真实 AI 接口验收全部通过，并再次取得用户明确批准后，才允许上传或部署。

## 目标环境

- 无图形界面的 Ubuntu；
- Python 3.11～3.14；
- 本地磁盘保存 SQLite 数据库；
- 独立低权限服务账号；
- 服务器能够访问 Telegram 数据中心和 OpenAI 兼容接口。

## 建议目录

```text
/opt/tg-collector/                 程序和虚拟环境
/etc/tg-collector/config.yaml      非敏感配置，root:tg-collector 0640
/etc/tg-collector/runtime.env      凭据环境文件，root:tg-collector 0640
/var/lib/tg-collector/             SQLite、Telegram session、总结
/var/backups/tg-collector/         SQLite 一致性备份
```

Telegram session 所在目录应为 `0700`，session 文件应为 `0600`；不得提交到 Git、写入镜像或公开备份。

## 计划中的安装流程

1. 创建 `tg-collector` 系统用户和上述目录；
2. 上传已经通过本机验收的源码包；
3. 使用 `uv sync --locked` 创建运行环境；
4. 写入不含密钥的 `config.yaml`；
5. 通过权限受限的环境文件注入 Telegram 和 AI 凭据；
6. 以服务账号在 SSH 终端运行一次 `tg-collector login`；
7. 执行 `init-db`、`sources validate`、`backfill`、`status`；
8. 运行短时采集和总结测试；
9. 验证 SQLite 备份与恢复；
10. 获得最终批准后启用长期服务。

## 服务拆分草案

### 采集服务

```ini
[Unit]
Description=Telegram allowlist collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tg-collector
Group=tg-collector
WorkingDirectory=/opt/tg-collector
EnvironmentFile=/etc/tg-collector/runtime.env
ExecStart=/opt/tg-collector/.venv/bin/tg-collector --config /etc/tg-collector/config.yaml run
Restart=on-failure
RestartSec=10
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/tg-collector

[Install]
WantedBy=multi-user.target
```

### 总结与清理调度服务

```ini
[Unit]
Description=Telegram summary and retention scheduler
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tg-collector
Group=tg-collector
WorkingDirectory=/opt/tg-collector
EnvironmentFile=/etc/tg-collector/runtime.env
ExecStart=/opt/tg-collector/.venv/bin/tg-collector --config /etc/tg-collector/config.yaml scheduler
Restart=on-failure
RestartSec=10
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/tg-collector

[Install]
WantedBy=multi-user.target
```

采集与总结必须保持为两个独立进程；AI 故障不能阻塞 Telegram 采集。两个进程共享 SQLite WAL，但只允许一个采集主进程使用同一个 Telegram session。

## 上线前验证清单

- [ ] 本机真实频道登录、来源解析、回填和断线补拉均通过；
- [ ] 本机真实 OpenAI 兼容接口总结通过；
- [ ] 三段 `Asia/Shanghai` 窗口无重复和遗漏；
- [ ] 到期消息、编辑历史、总结记录和 Markdown 文件同步删除；
- [ ] session 和环境文件权限正确；
- [ ] SQLite `PRAGMA integrity_check` 为 `ok`；
- [ ] 备份可恢复并继续同步；
- [ ] 用户再次明确批准上传和部署。
