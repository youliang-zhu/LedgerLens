# 桌面试用

## 使用

1. 双击桌面 `LedgerLens`。不需要输入账本密码。
2. 上传微信 XLSX、支付宝 CSV，可只选其中一个平台。勾选 AI 时会调用已配置服务，发送字段在页面说明。
3. 首次逐笔查看，选择类型、分类；退款／重复项选择关联原记录。点击“确认，下一笔”，或“保存修改”“暂缓”。每次按钮操作自动保存；未点击保存的编辑不算已保存。
4. 点击“入账已确认记录”，再切换“统计看板”。顶部始终提示待审核和待写入数量。
5. 关闭整个软件窗口即可停止其本地服务，下次打开继续。

## 已知边界

- 正式账本与以前未核验的实验看板隔离，不能把旧看板数值当审核后的统计。
- 本版所有新记录先待审核，不承诺自动准确分类。AI 只提疑点，失败或有覆盖局限会提示。
- 自有转账、不计收支、重复项只保留本地审核决定，不作为收入／支出写入看板。流水账户不是实际银行余额。
- 一笔退款关联一笔原消费，支持多次部分退款；累计不能超过原金额。找不到原消费则暂缓，不强行关联。
- 同来源编号、内容一致的重复上传跳过；同编号内容变化整批拒绝，提示人工核对。相似但不同编号不会自动合并。
- 已入账记录暂不支持在审核入口撤销或修改；写入网络结果不确定时停止重试，防止重复入账。
- 看板保留原生导航，但账目写操作被网关拦截；只放行必要的登录令牌维护。

## 安装与数据

需要 Windows、Python 3.10+、WebView2 Runtime、ezBookkeeping 2.0.1 的完整 Windows 运行文件。

```powershell
python -m pip install -r requirements-desktop.txt
python -m ledgerlens --runtime C:/path/to/ezbookkeeping
# 创建快捷入口（使用实际 pythonw.exe 路径）
./scripts/install-shortcut.ps1 -PythonPath C:/path/to/pythonw.exe -RuntimePath C:/path/to/ezbookkeeping
```

运行目录应包含程序、`conf`、`public`、`templates` 等官方文件，不应混入旧账本。第三方软件保留自己的许可证，不随本仓库重新授权。

- 默认本地数据：`.local/app/review.db`（来源、决定、审计事件）、`.local/app/originals/`（原件）、`.local/app/ez/ledger.db`（已确认账本）。
- 每次发布前备份 ezBookkeeping 数据库至 `.local/app/ez/backups/`。完整备份需退出软件后复制整个 `.local/app/`；单独备份账本不包含审核决定。
- API 密钥仍在仓库根目录 `.env`；账本随机密码和本地会话文件只保存在 `.local/`，不提交到 Git。
- 仅监听 `127.0.0.1`。主程序关闭时释放自己的端口，不结束其他应用或旧实验服务。

## 测试

```powershell
python -m unittest discover -s tests -q
python scripts/smoke-desktop.py --runtime C:/path/to/ezbookkeeping
```

联调使用独立合成账本，不调用云端 AI；验证消费／退款入账、待审核隔离、重复导入、看板令牌维护、重启与退出。真实数据的用途判断仍须用户确认。
