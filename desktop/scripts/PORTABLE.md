# 微日报 Windows 便携版

这是免安装的 Windows x64 版本。解压后双击 `wei-daily-desktop.exe` 即可启动，不需要运行安装程序。

## 包含内容

- `wei-daily-desktop.exe`：Tauri 桌面主程序；
- `wei-daily-backend.exe`：随包携带的本地服务 sidecar，必须与主程序放在同一目录；
- `SHA256SUMS.txt`：便携包关键文件校验清单；
- `版本.txt`：构建版本和目标平台信息。

## 使用前提

- Windows 10/11 x64；
- 系统已安装 Microsoft Edge WebView2 Runtime；
- 微信保持登录，且没有其他程序占用 `127.0.0.1:8765`；
- 首次启动时允许 Windows 防火墙或安全软件放行本机回环服务（如果系统提示）。

便携版不写入安装目录。微日报运行时的数据仍按桌面端约定保存到 Windows 应用数据目录，便于升级和避免 U 盘目录权限问题；因此“免安装”不等同于“数据库跟随 zip 移动”。

## 导出文件

HTML/PDF 导出时，应用会弹出保存位置选择；导出文件不会默认写入便携包目录。

## 校验

可用 PowerShell 执行以下命令检查关键文件：

```powershell
Get-FileHash .\wei-daily-desktop.exe -Algorithm SHA256
Get-FileHash .\wei-daily-backend.exe -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
```

## 重新打包

在已有 Tauri release 产物和 sidecar 的前提下，从项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\desktop\scripts\build-portable.ps1 -LaunchTest
```

脚本只会清理并重建明确的 `output\portable` 目录，不会删除其他构建产物或项目数据。
