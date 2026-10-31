# 不接飞书的桌面原文模式

本模式从 `v1.4.15-rc.1` 提供公共源码和桌面包；旧 `v1.4.7-rc.1` 源码包没有这个服务端入口。部署和升级独立实例仍需相应授权。

## 能做什么

桌面仍用设备 SSH 密钥连接 VPS，服务器按原提交 ID 保存原文和设备报告时刻，返回“服务器已保存”。按日期回看原文、按 UTC+8 日期计数，不需要 App ID、App Secret 或 open_id。服务端明确返回 `mode=desktop_only`。

原文模式不启动飞书机器人、自动模型、Base 写入或 Notion。原文只读；补充、更正说明另写一条。默认只显示报告时刻和条数，不把报告时刻当成活动开始，不把消息间隔算作工时。管理员可显式启用 `manual_organization`，让本人 [Codex 按需整理](MANUAL-ORGANIZATION.md)：整理结果保留原文引用和修订，通过服务端校验后才显示分类与已有区间，未知继续留空；不创建定时任务。

1.4.15 客户端显示“服务器已保存原文”，已上传原文不留在不存在的自动整理队列中。按需整理后标为“按需整理”，不称飞书正式记录。旧 1.4.7 上传协议仍兼容，但旧界面可能继续显示“待整理”“正式历史”；按需交换命令需要新版客户端和同版本服务端。

## 保留已安装客户端的身份和记录

保留原 `device.json`、Credential Manager 中的私钥、`state/desktop.sqlite3` 及草稿。服务器地址、端口、主机公钥不变时，无需重建设备、迁移队列或复制凭据。

只把该设备已有的公钥和设备 ID 登记到新实例。原有离线队列沿用提交 ID 和原报告时刻重传；响应丢失后重试仍是同一条记录。不要在管理员电脑伪造她的设备身份，也不要为了测试再次生成同一事件。

## 独立部署，不改现用飞书服务

使用本次源码的独立目录和新数据库，推荐以下布局（`owner-01` 是部署者选定的稳定标识，不是设备 ID）：

```text
/home/time-logger/desktop-only/owner-01/app/          本次公共源码
/home/time-logger/desktop-only/owner-01/data/config.json
/home/time-logger/desktop-only/owner-01/data/evidence.sqlite3
```

不得覆盖 `/opt/personal-time-logger`，不得指向现有 `/var/lib/personal-time-logger` 数据库，不重启现用接收服务。只上传明确的公共源码；不能把私人工作目录整体复制到 VPS。代码和配置目录归服务账号，数据目录 0700，配置与数据库 0600。若部署于其他家目录，按实际账号调整所有路径。

使用 `config/desktop-only.example.json` 创建配置，填写 `mode=desktop_only`、独立 `owner_id`、新数据库绝对路径，以及布尔值 `desktop_enabled/ingest`。不复制任何飞书凭据或业务配置。示例中的数据库路径需要换成所选独立数据目录。

新库首次创建时绑定 `owner_id`。已有库缺少原文模式标记、属于其他 owner 或含不同身份记录时，程序会拒绝打开，且不会把它自动迁移成原文库。已有空文件或初始化中断的文件也不会被自动收养；由管理员查清原因后处理，不能删用户已有记录重来。

复用现有 Python 环境只作解释器使用，不升级其依赖；先按已授权范围确认依赖兼容。以下为部署后登记命令示例，需要替换实际设备 ID 和路径：

```sh
cd /home/time-logger/desktop-only/owner-01/app
/opt/personal-time-logger/.venv/bin/python -B -m time_logger.standalone_bridge --config /home/time-logger/desktop-only/owner-01/data/config.json --device-id EXISTING_DEVICE_ID --register-device
```

在实际 SSH 账号的 `authorized_keys` 中为该设备追加受限条目，保留原有行。不要改全机 sshd 配置或给予普通 shell/SFTP 权限：

```text
restrict,command="cd /home/time-logger/desktop-only/owner-01/app && /opt/personal-time-logger/.venv/bin/python -B -m time_logger.standalone_bridge --config /home/time-logger/desktop-only/owner-01/data/config.json --device-id EXISTING_DEVICE_ID" ssh-ed25519 EXISTING_DEVICE_PUBLIC_KEY
```

同一设备公钥若已有条目，必须查清其用途与冲突，不能盲目追加第二行。客户端登录名仍为 `time-logger`。这里是按设备公钥路由到独立应用数据；同一 Linux 账号不构成对该账号或服务器管理员的强隔离。

每次 SSH 请求按需运行入口并事务提交，无需新建 systemd 服务、常驻接收进程或启用 timer。不要用旧 `serve` 命令启动此配置；旧飞书入口继续要求真实飞书身份。

## 授权与完成标志

代码修改不代表已部署。新增 VPS 目录、登记设备、修改 `authorized_keys`、远程检查和真实记录写入，按用户当次范围执行。必要的检查包括：公钥仅能进入这一实例；错误设备被拒；原现用服务保持原配置；她的原队列同一提交收到回执并可回看。

只有她客户端收到“服务器已保存”回执并完成约定核验，才称接入完成。没有这份结果时，保留队列，不能声称已同步。以后接飞书属于另一次显式接入/迁移，不能直接修改现有库的 owner 或悄悄启用外部写入。
