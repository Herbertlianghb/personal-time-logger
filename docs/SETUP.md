# Windows 安装与首次配置

## 已有服务器的普通用户

日常不需要安装 Python、Git、Codex 或 Claude。你需要管理员从自己的服务器提供：服务器地址、SSH 端口、可信 Ed25519 主机公钥，以及登记新设备公钥的能力。这些都属于你自己的部署，本项目不提供公共服务器。

不接飞书的独立实例按 [DESKTOP-ONLY.md](DESKTOP-ONLY.md) 登记设备，沿用相同桌面绑定流程。需要生成分类时间线时，另由本人 [Codex 按需整理](MANUAL-ORGANIZATION.md)；日常原文保存不依赖 Codex 在线。

1. 取得 `PersonalTimeLogger` 对应版本的 Windows 分发包并完整解压，保留其中的 `_internal`、`licenses` 和说明文件。不要只复制 EXE。
2. 双击包内 `安装.vbs`，或在 PowerShell 运行包内 `install-desktop.ps1`。安装到当前用户的 Programs 目录并创建桌面、开始菜单入口；无需管理员权限。首次安装默认不启用登录启动，需要时显式使用 `-StartWithWindows`；升级会保留已有受管的登录启动入口。若系统策略拦截脚本，查看脚本后按自己组织策略运行，不修改全机执行策略。
3. 打开“个人时间记录”。干净用户环境首次启动会显示配置窗口。填写自己的服务器地址、端口和可信 `ssh-ed25519 ...` 主机公钥。主机公钥应从服务器控制台或管理员可信渠道取得，不能把未经核对的扫描结果直接当可信公钥。
4. 按界面生成设备身份，把显示的设备 ID 和 **SSH 公钥**交给自己服务器的管理员。私钥只存入当前 Windows 用户的 Credential Manager，不需要复制到服务器或聊天。
5. 管理员按 [服务器设备登记](SERVER.md#登记桌面设备) 完成登记后，保存配置并进入主界面。若现有队列存在但配置缺失，程序会停止自动绑定；按自己的原配置恢复，不能新建绑定覆盖旧队列。
6. 输入一条你明确愿意保存的记录，例如“今天开始使用时间记录系统”，按 Enter。看到“服务器已保存”才代表服务端已确认原文；“本机已保存”只代表待上传。若需要同时核对手机聊天，服务器需另启用聊天镜像。

完成标志：重新打开同一桌面入口能进入主界面，记录收到服务器保存回执，原文和时刻符合你的输入。模型整理与正式 Base 写入是后续独立阶段，不能用它们的等待状态否定原文已保存，也不能把原文回执当作正式归档成功。

尚未取得服务器或飞书账号配置时，请用 [Agent 配置指南](AGENT-SETUP.md) 协助建立自己的部署。软件不会要求你登录维护者账号。

## 本机文件与升级

| 内容 | 位置 |
| --- | --- |
| 应用版本 | `%LOCALAPPDATA%\Programs\PersonalTimeLogger\<版本>` |
| 非秘密绑定配置 | `%USERPROFILE%\.personal-time-logger\device.json` |
| 草稿、稳定提交 ID、上传队列、历史缓存 | `%USERPROFILE%\.personal-time-logger\state\desktop.sqlite3` |
| 界面设置 | `%USERPROFILE%\.personal-time-logger\ui.ini` |
| 设备私钥 | 当前 Windows 用户的 Credential Manager |

升级使用新版完整包的安装入口，保留 `.personal-time-logger` 和 Credential Manager。正在运行时先从托盘真正退出，使草稿保存并释放程序文件；不要强杀进程、删除队列或重建密钥。切换历史日期不会更改新记录时刻。

不能通过复制 `device.json` 完成跨 Windows 账号迁移：对应私钥未随配置文件复制。新设备应单独登记。不要把旧版本和新版同时指向不同数据根运行。

## 常见情况

| 现象 | 下一步 | 完成标志 |
| --- | --- | --- |
| 首次配置缺服务器 | 按 [SERVER.md](SERVER.md) 建立自己的服务器，或向自己的管理员取得接入信息 | 获得可信主机公钥和设备登记入口 |
| 存在旧数据但配置缺失 | 恢复原用户的 `device.json`，保留队列与原 Credential Manager | 同一设备身份恢复，不出现第二份队列 |
| 凭据不可读 | 使用原 Windows 账号；若确已丢失，由管理员停用旧设备后登记新设备 | 新设备可访问，旧私钥未被导出 |
| 本机已存、服务器未存 | 保持程序运行，检查自己的网络/SSH端口，再点“立即重试” | 同一提交收到服务器回执，无重复新记录 |
| 主机公钥不匹配 | 从可信服务器控制台重新核对，查清是否迁机或换钥 | 核对后才更新配置，不关闭主机校验 |
| 服务器已存但无模型结果 | 阅读 [模型阶段](SERVER.md#可选本机模型解释) | worker 返回已处理/空闲，或明确的可处理错误 |
| 正式 Base 未核验 | 检查自己的 Base 配置、授权和字段契约 | 正式行写入并独立回读匹配 |
| 热力图为空/斜纹 | 查看月份、数据源状态与未知起止记录 | 有依据的区间进入日格；没有依据时保持空缺 |

## 从源码构建（仅开发者）

普通用户跳过此节。本地候选包使用 Windows、Python 3.13.7 与 PowerShell 7 构建；开发者可使用相同环境，将源码放在自己的目录。Linux 服务端环境单独见 [SERVER.md](SERVER.md)。以下命令从源码根目录执行，路径均由你选择：

```powershell
python -m venv .venv-desktop
& '.\.venv-desktop\Scripts\python.exe' -m pip install -r requirements.desktop.lock.txt
& '.\deploy\build-desktop.ps1' -PythonPath '.\.venv-desktop\Scripts\python.exe' -BuildRoot '.\work\desktop-build' -PackagePath '.\dist\PersonalTimeLogger-1.4.15'
```

构建脚本只收窄自身子进程 PATH，避免混入其他程序的 Qt/ICU 动态库，不修改全机 PATH。构建输出已存在时停止，使用新的输出目录；不要覆盖已装版本或用户数据。

开发运行入口为 `python -m time_logger.desktop_app`；首次配置也会保存真实本机身份，因此首次流程验证应使用隔离的 Windows 用户环境。测试、构建和试运行是否执行，以当前用户授权范围为准。新增首次配置与热力图的实际验证范围以同次交付说明为准，不能用历史作者环境结果替代。
