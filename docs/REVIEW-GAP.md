# 人工审核接入现状

核查日期：2026-09-28。需求依据：[SPEC](SPEC.md)。

## 已核实

- 当前试用版本为 ezBookkeeping 2.0.1，对应源码提交 `323cf0c683d7889ad0c28377039ced1a44167c1e`。
- 原生入口：交易详情 → 导入 → 选择账单 → 下一步。可以预览、修改分类／账户／标签、勾选要导入的记录。
- 导入模型中的 `selected` 和 `valid` 是导入页面的选择和有效性状态；有效性检查包括账户、分类等字段是否可用，不等于已核实交易用途、重复关系或退款关联。
- 本次核查的导入流程和持久化交易模型未发现独立的人工审核状态及决策记录。不能把导入预览视为已实现疑点审核流程。
- LedgerLens 的 `review.html` 是独立的实验标注工具，需下载／恢复审核进度；其人工结论尚未接入 ezBookkeeping，不能直接更新看板。
- 当前看板是未核验的试用结果。现有启动脚本仍启动独立服务，尚未实现桌面入口及退出时停止服务。

## 后续接入（已获用户确认）

上传微信／支付宝账单 → 规则与独立 API 检查 → 人工处理疑点 → 写入 ezBookkeeping → 查看统计。

用户已确认：疑点记录留在待审核区，确认前不写入正式账本，并明确提示统计尚不完整。

新增的 `ledgerlens` 桌面入口已实现原始记录、规则／AI 提醒、确认／修改／暂缓、进度持久化和确认后写入看板。首次试用所有记录仍逐笔确认；原 `review.html` 不变。实现边界与测试方法见 [桌面使用说明](DESKTOP.md)。上面的“已核实”部分记录的是接入前状态。

## 证据

- [官方导入说明](https://ezbookkeeping.mayswind.net/zh_Hans/export_and_import/)
- [2.0.1 导入模型与校验](https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/src/models/imported_transaction.ts)
- [2.0.1 导入界面流程](https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/src/views/desktop/transactions/import/ImportDialog.vue)
- [2.0.1 持久化交易模型](https://github.com/mayswind/ezbookkeeping/blob/v2.0.1/pkg/models/transaction.go)

核查方式为版本对应源码、官方文档和本地审核页代码检查；本次没有再次导入账单，也没有修改试用账本。
