<#
一键把图形程序打包成 exe（Windows）。

用法：在仓库根目录执行
    powershell -ExecutionPolicy Bypass -File build_app.ps1

它会做四件事：
    1. 找本机的 Python
    2. 在仓库里建一个 .venv（只装 numpy + pyinstaller，不动你的系统 Python）
    3. 调 PyInstaller 按 akframe.spec 打包
    4. 告诉你 exe 在哪

产物：dist\AFC\AFC.exe
分发：把 dist\AFC 整个目录压成 zip 发出去，对方解压后双击 exe。

注意：实时计数用的是 Windows 抓屏（BitBlt），不需要 ffmpeg；只有离线读录像
（run_reader.py）才需要它，所以这个包里没有 ffmpeg。
#>
[CmdletBinding()]
param(
    # 想指定某个 Python 就用 -Python "C:\path\to\python.exe"
    [string]$Python = "",
    # 想指定 pip 源就用 -Index https://pypi.tuna.tsinghua.edu.cn/simple
    # （国外网络不用管；默认会先试官方源，装不上自动换清华镜像重试一次）
    [string]$Index = ""
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Test-Python($exe, $extra) {
    # 能 import tkinter 才算合格：打包出来的图形界面要它，缺了的话跑起来会直接崩
    $out = & $exe @extra -c "import sys, tkinter; print(sys.version.split()[0])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) { return $out.Trim() }
    return $null
}

# ---- 1. 找 Python ----------------------------------------------------------
# 优先扫常见安装目录，比 Get-Command 可靠：Windows 自带的 python.exe 是 Microsoft
# Store 的转发器（在 WindowsApps 里），一跑就会弹应用商店，不是真的解释器。
$found = $null
if ($Python -ne "") {
    if (-not (Test-Path -LiteralPath $Python)) { throw "指定的 Python 不存在：$Python" }
    $v = Test-Python $Python @()
    if (-not $v) { throw "指定的 Python 不能用（缺 tkinter？）：$Python" }
    $found = @{ exe = $Python; extra = @(); version = $v }
}
if (-not $found) {
    $cands = @()
    $cands += Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Filter 'Python3*' -Directory -ErrorAction SilentlyContinue
    $cands += Get-ChildItem -Path 'C:\' -Filter 'Python3*' -Directory -ErrorAction SilentlyContinue
    foreach ($d in ($cands | Sort-Object Name -Descending)) {
        $exe = Join-Path $d.FullName 'python.exe'
        if (-not (Test-Path -LiteralPath $exe)) { continue }
        $v = Test-Python $exe @()
        if ($v) { $found = @{ exe = $exe; extra = @(); version = $v }; break }
    }
}
if (-not $found) {
    # 再试 py -3（官方启动器）和 PATH 里的 python
    foreach ($c in @(@('py', '-3'), @('python', ''), @('python3', ''))) {
        $name = $c[0]
        if (-not (Get-Command $name -ErrorAction SilentlyContinue)) { continue }
        if ($name -eq 'python') {
            $p = (Get-Command $name).Source
            if ($p -and $p -like '*WindowsApps*') { continue }
        }
        $extra = @()
        if ($c[1] -ne '') { $extra = @($c[1]) }
        $v = Test-Python $name $extra
        if ($v) { $found = @{ exe = $name; extra = $extra; version = $v }; break }
    }
}
if (-not $found) {
    Write-Host '找不到可用的 Python（需要带 tkinter 的 3.9 以上版本）。' -ForegroundColor Red
    Write-Host '  下载： https://www.python.org/downloads/windows/'
    Write-Host '  安装时勾上 "Add python.exe to PATH"；tkinter 是默认带的，不用额外选。'
    Write-Host '  装完重开一个 PowerShell 再跑本脚本。'
    throw 'Python not found'
}

Write-Host "Python    : $($found.exe)  (版本 $($found.version), tkinter OK)" -ForegroundColor Cyan

# ---- 2. 建虚拟环境并装依赖 ---------------------------------------------------
$venv = Join-Path $PSScriptRoot '.venv'
$vpy = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $vpy)) {
    Write-Host "建虚拟环境: $venv" -ForegroundColor Cyan
    & $found.exe @($found.extra) -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw '建虚拟环境失败（python -m venv）' }
} else {
    Write-Host "虚拟环境  : 已有 $venv（想重建就删掉这个目录）" -ForegroundColor Cyan
}

Write-Host '装依赖    : numpy + pillow + pyinstaller（第一次要下几十 MB，慢是正常的）' -ForegroundColor Cyan

# pip 源：官方源在有些网络上只有几十 kB/s，装不上就自动换清华镜像再试
$sources = @()
if ($Index -ne '') { $sources += $Index }
$sources += @('', 'https://pypi.tuna.tsinghua.edu.cn/simple')
$ok = $false
foreach ($src in $sources) {
    $extra = @()
    if ($src -ne '') { $extra = @('-i', $src); Write-Host "  源: $src" -ForegroundColor DarkGray }
    # pillow 是界面用的（ui.py 里 PIL.Image / ImageTk 预览 ROI）。漏了它 exe 能编出来，
    # 但一双击就 ModuleNotFoundError: No module named 'PIL'。
    & $vpy -m pip install --upgrade pip numpy pillow pyinstaller @extra
    if ($LASTEXITCODE -eq 0) { $ok = $true; break }
    Write-Host '  这一档没装上，换下一个源重试……' -ForegroundColor Yellow
}
if (-not $ok) { throw '安装 numpy / pillow / pyinstaller 失败（换个源试试：-Index <镜像地址>）' }

# ---- 3. 打包 -----------------------------------------------------------------
Write-Host '打包中    : PyInstaller（大约 1~3 分钟）' -ForegroundColor Cyan
& $vpy -m PyInstaller --noconfirm --clean akframe.spec
if ($LASTEXITCODE -ne 0) {
    # 最后一步要往 dist 里写一百多个 dll，被杀软 / 索引器临时占用会报
    # PermissionError（实测在 ucrtbase.dll 上踩过一次）。这种重跑一次基本就好。
    Write-Host '第一次没成功，隔 3 秒重试一次（多半是杀软临时占用了 dll）……' -ForegroundColor Yellow
    Start-Sleep -Seconds 3
    & $vpy -m PyInstaller --noconfirm akframe.spec
}
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 打包失败，看上面的报错' }

# ---- 4. 报结果 ---------------------------------------------------------------
$outdir = Join-Path $PSScriptRoot 'dist\AFC'
$exe = Join-Path $outdir 'AFC.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw "打包结束了但没找到 exe：$exe" }
$bytes = (Get-ChildItem -LiteralPath $outdir -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host ''
Write-Host '打包完成 ✅' -ForegroundColor Green
Write-Host "  exe     : $exe"
Write-Host "  整个目录: $outdir  （共 $([math]::Round($bytes / 1MB, 1)) MB）"
Write-Host '  用法    : 先开游戏（进关卡），再双击 exe'
Write-Host "  分发    : 把 dist\AFC 整个目录压成 zip 发出去"
