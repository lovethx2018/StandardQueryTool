# 安全策略

## 支持版本

只对当前主分支与最新 Release 提供安全修复。

## 报告问题

请不要在公开 Issue 中粘贴账号、Cookie、Token、完整内网地址或可复现的敏感数据。请使用 GitHub 的 **Private vulnerability reporting / Security advisory** 私密报告，或通过仓库维护者公开的联系方式联系。

## 设计说明

- 程序不内置任何标准平台账号，也不保存用户 Cookie。
- `config.py` 中的协筑口令是该站点公开共享口令，不属于用户凭据。
- 自动更新在清单提供 `sha256` 时会校验下载包；未提供时仅告警（兼容旧清单）。
- 安装版把用户数据写入 `%APPDATA%`，临时文件写入 `%TEMP%`；便携版跟随程序目录。