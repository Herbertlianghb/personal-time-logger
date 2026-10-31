# 公开分发边界

项目自有代码采用MIT，全文见根目录LICENSE。依赖许可不随MIT改写；Qt/PySide6与其他第三方声明在分发包`licenses`中保留。课程只做理论来源归属，不随代码提供课程原文或声称获得其再分发授权。

原工作目录可能同时保存私人验收记录、截图、真实Base快照和内部交接，不能将整个目录压缩或直接`git add .`。`.gitignore`按白名单保留公共文件；它不能替代实际公开内容的阅读。

`deploy/export-source.py`导出一个新的源码ZIP，仅读取下列公共范围：

- 根README、LICENSE、.gitignore、桌面入口与两份依赖锁。
- `time_logger/*.py`、`tests/*.py`及标为受控合成的`tests/controlled-corpus.json`。
- 明确列出的公共配置示例、模型prompt/schema和公共docs。
- 明确列出的构建、安装、服务管理脚本、unit与第三方许可证。

不导出旧交付/验收md、截图、design参考包、`base-schema.snapshot.json`、work、private、.codex、运行数据库、日志、设备配置或模型认证。导出不启动服务、不读取用户业务库、不创建Git仓库，也不发布。脚本拒绝覆盖已有目标和符号链接，源码包是可分发代码快照，不是运行数据备份。

从源码根目录执行，输出路径由用户指定且必须尚不存在：

```powershell
python '.\deploy\export-source.py' --output 'C:\Downloads\personal-time-logger-1.4.15-rc.1-source.zip'
```

Windows可执行包由`build-desktop.ps1`和`package-desktop.py`生成，按[SETUP.md](SETUP.md)构建。普通用户使用完整单目录包和安装入口，不需要安装开发环境。构建产物不带设备配置、私钥、飞书Secret或数据库。`安装.vbs`是调用PowerShell的脚本入口，不是MSI或签名安装器。每次版本变更后仍须按当次授权验证自己的首次流程；源码导出成功和构建成功不等于接入成功。

v1.4.15-rc.1试用文件为`personal-time-logger-1.4.15-rc.1-source.zip`与`PersonalTimeLogger-1.4.15-rc.1-Windows.zip`，均带[试用版说明](RELEASE-1.4.15.md)。Windows ZIP须完整解压；源码ZIP不带构建环境或运行数据。历史试用包及其原发布标签保留，不覆盖。

GitHub内容只从公共源码白名单准备，不包含私人开发目录与内部协作说明。若沿用开发仓库历史，还须检查历史提交；`.gitignore`不会移除已经跟踪的内容或历史里的秘密。Windows ZIP作为Release附件提供，不将运行库、安装包、私人截图或内部交接放进源码提交。

默认测试不访问真实Base。真实Base测试必须显式设置`PTL_RUN_LIVE_BASE_TESTS=1`、用户自己的`PTL_LIVE_LARK_PROFILE`与`PTL_LIVE_BASE_RESOURCE`，资源文件需`allow_live_test_writes=true`且其中`data.base.name`严格为`PERSONAL_TIME_LOGGER_TEST_ONLY`；只能指向新建的可丢弃测试Base，不能用生产表。正常安装不需要执行真实写入测试。

HTTP live 测试另需 `PTL_LIVE_FEISHU_CONFIG` 指向用户自己的受限应用配置文件；时间条件阳性对照须显式提供 `PTL_LIVE_POSITIVE_RECORD_ID` 和带时区的 `PTL_LIVE_POSITIVE_START`。缺少阳性对照时跳过该项，不使用任何维护者记录 ID。

每次发布、部署和新增验证均以用户当次授权为准。v1.4.15-rc.1发布公共源码与试用包；新用户完整接入验证仍暂停，具体边界见试用版说明。公开发布不自动升级任何人的设备或独立实例。
