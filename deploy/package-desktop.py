"""Assemble a credential-free one-folder package with upstream license notices."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from time_logger.desktop_app import VERSION


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--built", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parent.parent
    built, output = args.built.resolve(), args.output.resolve()
    if output.exists() or not (built / "PersonalTimeLogger.exe").is_file():
        raise SystemExit("Target exists or complete executable is missing")
    shutil.copytree(built, output)
    licenses = output / "licenses"
    licenses.mkdir()
    shutil.copytree(source / "deploy" / "licenses", licenses / "Qt-LGPL")
    distributions = ("PySide6-Essentials", "shiboken6", "paramiko", "cryptography", "bcrypt", "PyNaCl", "cffi", "pycparser", "PyInstaller", "pyinstaller-hooks-contrib")
    for name in distributions:
        dist = importlib.metadata.distribution(name)
        target = licenses / (name + "-" + dist.version)
        target.mkdir()
        for member in dist.files or []:
            path = Path(str(member))
            if ".dist-info" not in str(path) or not any(part.lower() in {"licenses", "license", "copying", "notice"} or part.lower().startswith(("license", "copying", "notice")) for part in path.parts):
                continue
            original = Path(dist.locate_file(member))
            if original.is_file():
                suffix = Path(*path.parts[path.parts.index(next(part for part in path.parts if part.endswith(".dist-info"))) + 1:])
                dest = target / suffix
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, dest)
        metadata = dist.read_text("METADATA")
        if metadata:
            # Package metadata/credits contain no project configuration or credentials.
            (target / "METADATA.txt").write_text(metadata, encoding="utf-8")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        shutil.copyfile(python_license, licenses / "Python-LICENSE.txt")
    shutil.copyfile(source / "requirements.desktop.lock.txt", output / "requirements.desktop.lock.txt")
    shutil.copyfile(source / "LICENSE", output / "LICENSE")
    shutil.copyfile(source / "README.md", output / "README.md")
    shutil.copyfile(source / "deploy" / "install-desktop.ps1", output / "install-desktop.ps1")
    shutil.copyfile(source / "deploy" / "install.vbs", output / "安装.vbs")
    (output / "docs").mkdir()
    for name in ("SETUP.md", "SERVER.md", "CONFIGURATION.md", "AGENT-SETUP.md", "AGENT-PROMPTS.md", "DISTRIBUTION.md", "RELEASE-1.4.7.md", "DESKTOP-ONLY.md", "MANUAL-ORGANIZATION.md", "RELEASE-1.4.15.md"):
        document = source / "docs" / name
        shutil.copyfile(document, output / "docs" / document.name)
    (output / "使用说明.txt").write_text(
        "个人时间记录 " + VERSION + "\n\n"
        "完整解压后双击‘安装.vbs’，安装到当前用户，无需安装 Python。也可直接打开 PersonalTimeLogger.exe。\n"
        "首次打开按配置向导连接你自己的服务端；完整说明见 docs/SETUP.md。\n"
        "需要自己的 Linux 服务端或经授权的独立实例；飞书模式另需自己的飞书应用，本软件不提供共享服务器或作者账号。\n"
        "不接飞书的实例可由本人 Codex 按需整理，见 docs/MANUAL-ORGANIZATION.md；不创建定时任务。\n"
        "全局快捷键优先 Ctrl+Alt+T；冲突时使用 Ctrl+Shift+F9，实际以程序托盘提示为准。\n"
        "Enter 或‘记录’直接发送，Ctrl+Enter 换行；中文选字期间不会发送。\n"
        "鼠标移入深绿色悬浮入口，小窗从入口展开；移开后保存草稿并用动画收回入口，拖动入口可调整位置。\n"
        "在窗口‘···’或托盘菜单选择‘输入小窗’，打开可独立钉选置顶的输入窗；草稿与原窗口共享。\n"
        "输入框有焦点或文字也会收起；输入法组字、菜单/详情操作时暂缓，菜单可选‘保持展开’或关闭‘悬浮入口’。\n"
        "关闭窗口收起小窗，悬浮入口和托盘继续保留。托盘菜单可真正退出；未上传原文保留，下次打开自动续传。\n"
        "断网时先保留本机，联网后自动补传；‘立即重试’可立即重试未上传内容并刷新结果。\n"
        "查看旧日期只改变回看，新记录使用实际提交时刻。\n"
        "小窗顶部的工作、学习进度分别累计今日记录时长；目标读取飞书，在‘··· → 每日目标（飞书）’打开修改入口，达标亮绿灯。\n"
        "上方时间线独立滚动；‘概览 / 放大’统一缩放时间轴，通过‘查看节点’回看来源与详情。\n"
        "未知时段和估算保留，AI后台时间单列，不重复加入人工投入。\n"
        "本机已存、服务器已存、模型整理、正式飞书核验是不同阶段。\n"
        "服务器启用聊天镜像后，原文在服务器保存后同步到飞书聊天；不代表模型整理或正式归档已完成。\n"
        "设备绑定配置和本机队列独立保存在当前用户目录的 .personal-time-logger，不包含在本分发包。\n"
        "更换版本时保留 %USERPROFILE%\\.personal-time-logger\\state，不覆盖或删除其中数据。\n"
        "源码与 Agent 配置方法见项目仓库及 docs/AGENT-SETUP.md。\n",
        encoding="utf-8",
    )
    (output / "第三方软件声明.txt").write_text(
        "本程序使用未修改、动态加载的 Qt/PySide6 Essentials 与 Shiboken 6.10.2。\n"
        "上游包元数据提供 LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only 许可；本交付按 LGPL v3 使用。\n"
        "完整 LGPL v3 与其引用的 GPL v3 文本位于 licenses/Qt-LGPL；原始包声明和其他依赖许可也保留在 licenses。\n"
        "Qt 动态库保留在 _internal/PySide6，采用单目录包，可替换对应兼容版本。\n"
        "Qt/PySide6 官方源码入口：https://code.qt.io/cgit/qt/qtbase.git/ 与 https://code.qt.io/cgit/pyside/pyside-setup.git/\n"
        "版本标签为 v6.10.2；官方许可说明：https://doc.qt.io/qtforpython-6/licenses.html\n"
        "Python、Paramiko、Cryptography 等各自保留原许可；具体版本见 requirements.desktop.lock.txt。\n"
        "此包不包含飞书 App Secret、模型认证、设备私钥、用户数据库或 Windows 字体文件。\n",
        encoding="utf-8",
    )
    members = []
    for path in sorted(output.rglob("*")):
        if path.is_file():
            members.append({"path": path.relative_to(output).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (output / "package-manifest.json").write_text(json.dumps({"name": "PersonalTimeLogger", "version": VERSION, "files": members}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"package": str(output), "files": len(members), "bytes": sum(p.stat().st_size for p in output.rglob("*") if p.is_file())}))


if __name__ == "__main__":
    main()
