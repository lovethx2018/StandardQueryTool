# 贡献指南

感谢参与改进。提交前请先确认以下约定。

## 开发环境

- Windows 10/11 x64
- Python 3.13（64 位）
- 安装依赖：

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 提交前检查

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

如果修改了数据源解析或下载逻辑，建议额外运行（会访问真实站点）：

```bat
.venv\Scripts\python.exe scripts\smoke_sources.py
```

## 代码约定

- 保持现有分层：`ui/` 只做界面与线程编排，抓取逻辑放在 `spiders/`，缓存/台账放在 `query_history/`、`ledger/`。
- 标准编号 / 年份 / 日期解析尽量复用 `utils.py` 与现有工具函数，避免再复制一套正则。
- 新增第三方依赖前先确认许可证兼容（本项目为 AGPL-3.0）。
- 不要提交 `_temp/`、`_data/`、`电子规范/`、`release/`、`dist/`、`.venv*/`、用户配置与本机缓存。
- 不要提交任何真实账号、Cookie、Token 或私人路径。

## 解析器改动

站点改版导致解析失败时：

1. 在 `spiders/` 对应模块修复解析；
2. 尽量补一个基于脱敏 HTML 片段或构造字符串的单元测试；
3. 在 PR 描述中附上失效站点的 URL 与修复前后样例。

## 提交信息

使用简短祈使句，说明“为什么”而不是罗列文件，例如：

```text
fix: csres 纯数字查询缺少发布日期
perf: 批量查询命中缓存时跳过网络
docs: 补充 AGPL 与第三方许可说明
```

## 许可证

提交代码即表示你同意以本项目的 AGPL-3.0 许可证发布你的贡献。