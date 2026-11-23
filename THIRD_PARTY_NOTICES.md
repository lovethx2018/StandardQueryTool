# 第三方组件与许可证

本项目自身以 **AGPL-3.0** 发布。以下为主要运行时依赖的许可证摘要；具体版本以 `requirements.txt` 为准，完整条款以各项目上游为准。

| 组件 | 用途 | 许可证 |
| --- | --- | --- |
| PyQt5 / PyQt5-Qt5 / PyQt5-sip | 桌面 GUI | GPL-3.0 或 Riverbank 商业许可 |
| PyMuPDF | PDF 页渲染（`--pdf-page-renderer` 子进程） | AGPL-3.0 或 Artifex 商业许可 |
| requests | HTTP | Apache-2.0 |
| urllib3 | HTTP | MIT |
| certifi | CA 证书 | MPL-2.0 |
| idna | 域名编码 | BSD-3-Clause |
| charset-normalizer | 编码检测 | MIT |
| chardet | 编码检测 | LGPL-2.1 |
| beautifulsoup4 | HTML 解析 | MIT |
| lxml | HTML/XML 解析 | BSD-3-Clause |
| openpyxl | xlsx 读写 | MIT |
| xlrd | xls 读取 | BSD-3-Clause |
| Pillow | 图片处理 | HPND |
| PyInstaller | 打包 | GPL-2.0-or-later（含 bootloader 例外） |

## 关于 AGPL

`PyMuPDF` 采用 AGPL-3.0 / 商业双许可。由于本项目的发布包包含 PyMuPDF，仓库整体按 **AGPL-3.0** 发布，以保持一致。

如果你希望以非 AGPL 方式分发：

1. 购买 PyMuPDF 商业许可，或替换为许可证兼容的 PDF 渲染方案；
2. 同时处理 PyQt5 的 GPL/商业许可问题（购买商业许可或迁移 GUI 方案）。

标准文本本身的版权归各发布机构所有，不在本仓库许可证覆盖范围内。