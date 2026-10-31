# 服务端与飞书接入

仅需桌面记录、不接飞书时，使用 [桌面原文模式](DESKTOP-ONLY.md)，部署独立目录和数据库；需要时可让 [Codex 按需整理](MANUAL-ORGANIZATION.md)。以下内容是飞书模式，不能混用两种配置或入口。独立模式从 `v1.4.15-rc.1` 提供。

本文面向部署者。普通 Windows 用户由自己信任的管理员完成本节，然后使用 [SETUP.md](SETUP.md)。没有默认托管服务，不复制维护者的账户、服务器或配置。

## 组件与最小路径

| 组件 | 职责 | 不负责 |
| --- | --- | --- |
| Windows 桌面 | 草稿、离线队列、显示与设备 SSH 连接 | 保存飞书 App Secret、替用户创建服务器 |
| Linux 服务端 | 本人飞书消息接收、事务保存、修订与队列、桌面 RPC | 依赖桌面在线才接收手机消息 |
| 飞书自建应用 | 本人单聊入口、回执、可选聊天镜像与 Base API 权限 | 自动解释所有时间事实 |
| 本机 Codex worker（可选） | 取有界批次，提出结构化候选并回传 | 直接把模型输出当事实或复制认证到服务器 |
| 飞书 Base（可选） | 正式结构化时间记录 | 用消息间隔自动补全人工时长 |

最小路径为：自己的飞书自建应用与本人身份 → 自己的 Linux 服务端 → 桌面设备登记 → 一条原文保存回执。模型、Base 写入、聊天镜像、定时任务和离机备份分别启用，不是首次保存的隐含前提。

## 账号和飞书授权

1. 在飞书开发者平台使用自己的账号创建企业自建应用，启用机器人，并使应用对本人可用。保存自己的 App ID 和 App Secret；Secret 只写入受限服务端配置，不发给桌面用户或放入源码。
2. 在应用的事件配置中选择长连接模式。本实现注册消息接收与撤回事件（代码在 `Service.run`），不需要公网 HTTP webhook。只运行一个此应用的 SDK 接收实例。
3. 按实际功能申请权限：接收本人发给机器人的单聊消息；发送消息；读取此聊天的历史和单条消息；启用 Base 时再申请指定 Base 的字段读取及记录读取/新增/更新。权限名称和审批流程以当前飞书开发者平台为准，Agent 应逐项核对对应 API，不能为了省事申请组织全部数据权限。
4. 取得 **同一自建应用范围内**的本人 `open_id`。可在自己应用的授权/API 调试工具读取已登录本人身份；不能用其他应用范围的 `open_id`，也不能把机器人ID或聊天ID填进去。无法确认时暂停接收配置，不能放宽为“接收所有人”。
5. 发布应用的当前版本到自己的组织/可见范围，使权限与机器人生效。给机器人发送一条本人明确授权保存的记录后，服务会绑定已配置本人身份的单聊；其他人和群消息不会作为本人事实采集。`chat_id` 不知道可保留 null，`binding_verified` 保留 false，不能凭猜测设置 true。

这些授权可能需要用户在浏览器登录、管理员审批或确认本人身份，Agent 无法代办时应只给一个明确的当前步骤。完成标志是同一应用的本人身份已确认，目标功能的权限生效；拿到 App ID 不代表完成授权。

## Linux 安装

使用自己的 Linux 主机，准备 Python 3.11、可用的 SQLite 扩展、venv、SSH 和 systemd（仅在采用所附 unit 时需要）。依赖的确切版本见 `requirements.lock.txt`。应用 API 目标是 `open.feishu.cn`，本版本不提供 Lark 国际域名切换。

推荐使用附带 unit 的标准目录：源码 `/opt/personal-time-logger`、受限配置和数据库 `/var/lib/personal-time-logger`、独立用户 `time-logger`。这些是通用安装约定，不指向维护者机器。桌面 SSH 用户名当前固定为 `time-logger`。自定义目录需要同步调整 unit、设备 forced command 与 worker 配置；不要只改一处。

以下步骤在自己的 Linux 管理控制台执行，由部署者根据发行版创建缺失的独立用户与目录，不覆盖已有不同内容的目标：

```sh
sudo useradd --system --create-home --home-dir /var/lib/personal-time-logger --shell /bin/bash time-logger
sudo install -d -m 0755 /opt/personal-time-logger
sudo install -d -m 0700 -o time-logger -g time-logger /var/lib/personal-time-logger
```

将干净源码包中的应用文件安装到 `/opt/personal-time-logger`，不要上传本机配置、模型认证或运行数据库。使用所选 Python 3.11 创建独立环境并安装锁定依赖：

```sh
sudo python3.11 -m venv /opt/personal-time-logger/.venv
sudo /opt/personal-time-logger/.venv/bin/python -m pip install -r /opt/personal-time-logger/requirements.lock.txt
```

把 `config/config.example.json` 复制为 `/var/lib/personal-time-logger/config.json`，填入自己的应用和本人身份，保持 `parse/base_sync/notion_sync` 为 false，`desktop_enabled` 为 true。用本机编辑器直接填写秘密，限制配置为 owner `time-logger`、mode 0600；不要把 App Secret 放进命令行参数或聊天输出。

可先在自己的控制台以前台方式启动，观察启动退出状态；长连接会实际接收本人消息，属于运行服务而非纯配置：

```sh
cd /opt/personal-time-logger
sudo -u time-logger .venv/bin/python -m time_logger --config /var/lib/personal-time-logger/config.json serve
```

选择 systemd 常驻时，在源码目录执行以下命令，将 `YOUR_SERVER_HOSTNAME` 替换为预期部署服务器的实际 hostname。安装脚本会检查 unit 并在成功后执行 daemon-reload，不启动服务，发现不同内容目标会停止。第二行会启用本项目服务，应在用户批准运行自己的服务后执行：

```sh
sudo .venv/bin/python deploy/install_units.py --expect-host YOUR_SERVER_HOSTNAME
sudo systemctl enable --now personal-time-logger.service
```

附带 backup timer 是可选的，不因安装 unit 就启用；安装器会创建本项目备份目录，实际备份、定时启用和保留策略需要用户决定。不要修改全机 SSH、安全策略或其他服务来绕过配置问题。

`health` 命令会打开应用 Store，可能初始化缺失的应用表；仅在自己的配置和数据路径下调用：

```sh
sudo -u time-logger /opt/personal-time-logger/.venv/bin/python -m time_logger --config /var/lib/personal-time-logger/config.json health
```

完成标志：自己的服务保持运行，健康文件更新，`ws_state` 对应长连接已连接；本人单聊原文收到保存回执。健康文件存在或进程启动不证明手机消息已经保存。

## 登记桌面设备

Windows 首次配置界面生成唯一设备 ID 和公钥。管理员只接收这两项，不索取私钥。登记命令只针对自己的原始库：

```sh
cd /opt/personal-time-logger
sudo -u time-logger .venv/bin/python -m time_logger.desktop_bridge --config /var/lib/personal-time-logger/config.json --device-id REPLACE_DEVICE_ID --register-device
```

在自己的服务器创建公钥目录和文件（`touch` 保留已有内容）：

```sh
sudo install -d -m 0700 -o time-logger -g time-logger /var/lib/personal-time-logger/.ssh
sudo touch /var/lib/personal-time-logger/.ssh/authorized_keys
sudo chown time-logger:time-logger /var/lib/personal-time-logger/.ssh/authorized_keys
sudo chmod 0600 /var/lib/personal-time-logger/.ssh/authorized_keys
```

将这一设备公钥追加到 `time-logger` 的 `authorized_keys`，每行采用设备专属 forced command。以下是一行模板；替换设备 ID 和公钥，保留限制与引号，不覆盖其他公钥：

```text
restrict,command="cd /opt/personal-time-logger && /opt/personal-time-logger/.venv/bin/python -m time_logger.desktop_bridge --config /var/lib/personal-time-logger/config.json --device-id REPLACE_DEVICE_ID" ssh-ed25519 REPLACE_DEVICE_PUBLIC_KEY
```

`.ssh` 为 0700、`authorized_keys` 为 0600，归 `time-logger`。设备公钥只能到桌面 RPC，不能给它普通 SSH/SFTP shell 或管理权限。用于模型 worker 的管理/操作公钥是另一把密钥，不能将桌面私钥导出后复用。

若公钥正确但仍被拒绝，请查看自己服务器的 SSH 日志，按发行版核对系统账号锁定状态、PAM 与公钥登录策略。不要设置空密码或关闭全机主机校验来排障；本轮未验证任何具体 Linux 发行版的账号策略。

主机固定公钥从可信控制台读取 `/etc/ssh/ssh_host_ed25519_key.pub`，将其中 `ssh-ed25519` 和 base64 两段提供给 Windows 用户。非22端口也要一致填写。确认 `desktop_enabled=true` 后，桌面可保存配置并提交原文。撤销设备使用同一 CLI 的 `--disable-device`，并移除该设备的授权公钥；不删除原文或队列。

## 可选本机模型解释

当前 `CodexInterpreter` 仅实现 Codex CLI，代码严格匹配 `codex-cli 0.163.0`，模型名为 `gpt-6.1-sol`。应以实际代码为准；其他版本、Claude CLI、其他模型或转发网关没有自动兼容承诺。Claude 可作为配置 Agent，不等于支持用 Claude 做批处理。CLI/模型在用户账户不可用时，保留原文并如实说明阻塞，不从维护者机器复制登录态。

用户在本机自己的可信 Codex 环境登录；不把模型认证传到服务器。复制 `config/client.example.json` 到自己选择的私有目录，配置独立 SSH config、固定 known_hosts、自己的操作密钥和本机 outbox。SSH Host 别名固定为 `personal-time-logger`，其 `HostName` 必须指向自己的服务器，模板见 `config/ssh-config.example`。

服务端 `parse=true` 才导出解释批次。本机 worker 从源码根目录运行：

```powershell
& '.\.venv\Scripts\python.exe' -m time_logger.remote_worker --config 'C:\PersonalTimeLogger\private\client-config.json'
```

本机需要相应 Python 依赖与 Codex CLI；这是可选的后台解释环境，普通用户使用桌面安装包无需开发环境。worker 不使用桌面设备公钥的 forced command，操作账号应只有本项目所需权限。首次手动运行明确成功后，用户再决定是否用 `install-client-task.ps1` 注册登录/15分钟任务。该任务运行 `client_daemon`，同时尝试离机备份，因此须配置 `offsite_directory` 并得到用户对计划任务和备份的授权；不要重复注册已有任务。

完成标志：worker 返回已导入/已提出或空闲的结构化结果，桌面能显示相应整理状态。网络失败时 outbox 保留同批次提案，不删除重做。模型成功不代表正式 Base 写入成功。

## 可选正式 Base 与聊天镜像

Base 使用自己的 `base_token`、`time_table_id` 和字段，应用身份须拥有目标表的相应访问权限。当前字段映射为固定中文字段名；按 [字段契约](CONFIGURATION.md#正式-base-字段契约) 建立自己的兼容表或映射，不读取公开包中的私人快照。启用前读取实时字段，检查类型和选项；不自动创建新分类、不写公式字段。

在用户明确授权正式写入后，将 `base_sync` 设为 true，协调自己的服务一次重启。首次正式写入应使用用户明确提供的记录，检查写后回读与人工修改冲突；不能用反复创建新记录替代不确定响应恢复。

聊天镜像需要已经确认的本人单聊，独立设置 `desktop_chat_mirror=true`。原文先事务保存，镜像后发；镜像失败不撤销服务器保存回执。`notion_sync` 不自动创建 Notion 页面或任务；需要独立接入，默认保持 false。

## 故障与更新

凭据/权限失败时核对自己的应用、本人身份和权限；不扩大接收范围。模型失败暂停派生处理而继续保存原文。Schema/远端人工冲突时停止对应正式写入，保留候选。更新只替换兼容代码，保留原始数据库、修订和本机 outbox；不能整库回滚覆盖新消息。计划重启使用本项目的 `systemctl restart`，不依赖 SIGHUP。详细错误仅输出分类，避免输出凭据和无关原文。
