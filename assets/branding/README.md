# LedgerLens 正式图标

用户于 2026-09-28 选定 B：深蓝色双 L。沿用原始内置图像生成结果，不重新生成或修改设计。

- 高清原图：`concepts/b-double-l.png`。
- 应用 PNG：`../../ledgerlens/static/icon.png`，与高清原图字节一致。
- Windows 多尺寸图标：`../../ledgerlens/static/icon.ico`，包含 16、24、32、48、64、128、256 像素。
- 使用位置：桌面快捷方式、应用窗口、网页图标、审核页面标识。
- 生成记录和完整提示词：[候选记录](concepts/README.md)。

运行 `scripts/build-icon.ps1` 从原图转换格式；运行 `scripts/install-shortcut.ps1 -UpdateIcon` 只更新本安装已有快捷方式的图标，原快捷方式备份在 `.local/shortcut-backups`。新建快捷方式也会自动使用本图标。

应用窗口图标在下次启动生效。高清原图保留供后续移动端适配，当前未生成 iOS / Android 发布资源。
