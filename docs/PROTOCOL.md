# 实验数据接口 v1

以下均为合成示例或字段说明，不含真实账单。金额使用非负整数分，文件存入被忽略的 `.local/`。

## 来源与人工答案

`prepare` 生成带指纹的 `dataset.json`、原件副本、`truth.blank.json`、`scenarios.json` 和离线审核页。每条来源有稳定 `source_id` 及原表行号，全部人工答案初始为未确认。

同一经济事件可有多条来源，人工确认后共用 `group_id`。退款另建事件，`refund_allocations` 使用 `group_id` 指向原消费；退款与消费不得归入同一重复组。

## 候选结果

```json
{
  "schema_version": 1,
  "dataset_id": "完整数据集指纹",
  "candidate_id": "C01",
  "phase": "initial",
  "provenance": {
    "product": "实际软件名称",
    "version": "实际版本",
    "adapter": "适配器版本",
    "mode": "native",
    "evidence": ["真实导出文件路径、校验值和操作记录"]
  },
  "observations": [{
    "source_id": "SYNTHETIC-SOURCE",
    "datetime": "2026-09-01 12:00:00",
    "amount_cents": 10000,
    "direction": "支出",
    "status": "支付成功",
    "transaction_id": "SYNTHETIC-ORDER"
  }],
  "entries": [{
    "entry_id": "E1",
    "source_ids": ["SYNTHETIC-SOURCE"],
    "kind": "expense",
    "amount_cents": 10000,
    "refund_allocations": [],
    "reason": ""
  }],
  "reported_totals": {"expense": 10000, "income": 0, "refund": 0, "net_expense": 10000}
}
```

- `phase`：`initial` / `corrected`，各自单独保存。
- `mode`：`native` / `configured` / `combined`，区分原生、配置和组合能力。
- `kind`：`expense` / `income` / `refund` / `transfer` / `excluded` / `pending`。`transfer` 仅指自身账户转移；排除和待审核需说明原因。
- 原消费使用退款前金额；退款分配示例：`[{"entry_id":"E1","amount_cents":2500}]`。
- `observations` 必须来自产品的可核查解析结果；不可得的字段用 `null`，不能从输入复制正确值充当产品表现。
- `reported_totals` 必须来自产品，不能用检查器自行重算的值填充。不可核查则省略并标未测试。
- 每条来源恰好映射一次。是否误合并、用途是否正确仍需与人工答案对照。

## 重复导入证据

`replay` 接收真实操作前后的候选导出及证据 JSON：`case`（`repeat` / `overlap`）、`executed:true`、`before_hash`、`after_hash`、`input_file_hashes`、`execution_log` 和 `imported_source_ids`。

候选 JSON 指纹由 `evalkit.common.digest` 计算。不得仅填写 `executed:true` 就宣称实际测试过；操作日志和导出证据仍须核查。当前重叠区间在源码中固定，见 README 的原型限制。

## API 与比较

AI 返回必须满足 `prompts/reviewer-v1.txt` 的 JSON 结构。`completed_not_a_pass` 仅表示完成检查，`completed_with_gaps` 表示覆盖有缺口，`incomplete` 表示未完成。

`compare` 只汇集相同数据集及人工真值版本的规则报告，不自动合并全部证据或选出赢家。所有输出拒绝覆盖已有文件。
