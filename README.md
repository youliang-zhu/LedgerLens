# LedgerLens

An accuracy-first personal finance toolkit for WeChat Pay and Alipay, combining rule-based validation, AI-assisted review, and human verification.

面向微信、支付宝日常账单的私人财务管理项目。优先保证账目可信，再减少人工操作。

**当前阶段：本地桌面试用版，尚未完成人工验收。** 已完成首轮候选软件及真实 API 实验，新增审核入口与 ezBookkeeping 看板；实验成功不代表账目已准确。

## 电脑入口

已配置机器双击桌面 **LedgerLens**：上传微信／支付宝账单 → 查看规则及 AI 提醒 → 确认／修改／暂缓 → 点击“入账已确认记录” → 查看“统计看板”。免手动登录，关闭窗口即停止本次启动的服务，无开机自启。

首次试用仍逐笔确认；疑点和暂缓项不进入正式统计。进度自动保存，真实数据保存在 `.local/app/`。原来的 `review.html` 是独立实验标注工具，不是桌面入口。

新机器安装见 [桌面使用说明](docs/DESKTOP.md)。

## 核心原则

- 每月集中导入导出的账单，不要求逐笔手记。
- 防止重复计入，区分消费、收入与自有账户转移。
- 退款关联原消费，避免重复抵扣或误记为普通收入。
- 不确定的用途与关联交给人工，AI 不作为最终裁判。
- 保留首次结果和修正后结果，不用纠错后的数据掩盖首次错误。

详情：[核心需求](docs/SPEC.md) · [验收标准](docs/ACCEPTANCE.md) · [数据接口](docs/PROTOCOL.md) · [隐私说明](SECURITY.md)

## 当前能力与限制

| 已实现 | 尚未验证或尚未实现 |
| --- | --- |
| 微信 XLSX、支付宝 CSV 的指定导出格式解析与来源追溯 | 各平台所有历史格式、银行卡导入 |
| 完整性、整数分金额、退款分配、字段保真及人工真值比较 | 自动判断所有重复交易、转账用途与退款关系 |
| 重复／重叠导入实验；桌面按来源编号防止重复上传 | 跨平台不同编号的自动合并准确率 |
| 固定提示词、真实 API 调用、返回结果校验 | AI 疑点的准确率与完整人工验收 |
| 桌面逐条审核、自动保存、确认后写入看板 | 已入账撤销／纠错、完整资产账户管理 |

桌面入口支持其他月份；原实验命令仍保留固定日期范围与重叠测试区间，避免改变既有实验口径。不要把解析失败当成无交易。

## 本地运行

要求 Python 3.10+。从仓库根目录执行：

```bash
python -m venv .venv
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
# macOS / Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m evalkit --help
```

使用自己的原始文件（不修改原件）：

```bash
python -m evalkit prepare --alipay /path/to/alipay.csv --wechat /path/to/wechat.xlsx --output .local/datasets
```

命令返回数据集目录，其中的 `review.html` 可用本地浏览器打开。审核页面不联网，关闭前点击“下载审核进度”；网页本身包含私人数据，不应公开托管。

后续命令：`check`、`truth-check`、`review-page`、`replay`、`api-prepare`、`api-send`、`compare`。每个命令可加 `--help`；检查报告拒绝覆盖已有文件。

## AI 复核

示例配置见 `config/api.example.json`，固定提示词见 `prompts/reviewer-v1.txt`。当前选择 GPT-6 Astra；实际服务须支持配置的 Responses 接口。

1. 将 `config/api.example.json` 复制到仓库根目录，命名为 `api.local.json`。默认已填 OpenAI 官方地址和 `gpt-6-astra`；第三方密钥需要改成对应服务的 Responses 地址，不会自动猜测服务商。
2. 将 `.env.example` 复制为根目录下的 `.env`，只在 `BOOKKEEPING_REVIEW_API_KEY=` 后粘贴密钥并保存。程序自动读取，无需设置系统环境变量；如已有同名进程环境变量，则优先使用它。
3. 运行 `python -m evalkit api-check` 做离线检查。`ready_for_connection_test` 只说明配置可读取，不证明密钥有效、模型可用或额度足够；命令不联网、不显示密钥。
4. 使用 `api-prepare` 生成本地请求预览，检查发送内容。API 命令默认读取根目录的 `api.local.json`，也可通过 `--config` 指定。
5. 只有明确执行 `api-send --send` 才发送数据；可能产生服务商费用。密钥文件不会包含在请求日志中。

`.env` 与 `api.local.json` 均已被 Git 忽略。`key_env` 是变量名称，不应填写真实密钥。`.env` 是本地明文文件，并非加密保险箱。接口格式参考 [OpenAI Docs](https://developers.openai.com/api/docs/quickstart)。

默认隐藏部分个人标识与自由文本，但时间、金额等仍属个人交易数据，不是完全匿名数据。调用失败、覆盖不足或编造记录引用均不算通过。AI 只提出疑点，不修改账目。

## 测试与发布范围

公开测试仅使用合成数据。真实账单、私人实验文档、审核记录、截图和密钥不在仓库中。测试通过仅说明覆盖的工具行为符合预期，不证明任何记账方案达到准确性要求。

## 许可证

许可证尚待仓库所有者决定，当前未授予开源许可证。公开可见不等于已授权任意复制、修改或分发。
