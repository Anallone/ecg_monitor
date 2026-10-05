# ESP-IDF 环境引导（自动探测安装位置，个人机 / 工作机通用）
#
# 用法（在项目根目录或 firmware/ 下均可）：
#   powershell -ExecutionPolicy Bypass -File firmware\idf.ps1 build
#   powershell -ExecutionPolicy Bypass -File firmware\idf.ps1 -p COMx flash monitor
#
# 设计目标：**不依赖任何一台机器特有的环境变量或路径配置**。脚本按下面的顺序
# 自动定位 ESP-IDF、工具链目录与 Python 解释器，两台机器上都能直接跑：
#
#   1) 若已设置 IDF_PATH / IDF_TOOLS_PATH，优先采用；
#   2) 否则扫描常见安装位置（含 D:\esp\*\esp-idf、%USERPROFILE%\esp\*、
#      C:\Espressif\frameworks\esp-idf-*）；
#   3) 若 idf.py 已在 PATH 上，从其位置反推 IDF 根目录；
#   4) 工具链目录额外读取官方安装器写入的 esp_idf.json（权威值）。

$ErrorActionPreference = 'Stop'

# Git Bash / MSYS 启动时会带 MSYSTEM，ESP-IDF 的 idf_tools.py 见到它会直接拒绝运行。
# 在这里清掉，使脚本无论从 cmd、PowerShell 还是 Git Bash 调用都能工作。
Remove-Item Env:MSYSTEM -ErrorAction SilentlyContinue
Remove-Item Env:MSYSTEM_PREFIX -ErrorAction SilentlyContinue

function Test-IdfRoot([string]$p) {
    if (-not $p) { return $false }
    return (Test-Path (Join-Path $p 'export.ps1')) -and (Test-Path (Join-Path $p 'tools\idf.py'))
}

function Test-ToolsPath([string]$p) {
    if (-not $p) { return $false }
    return (Test-Path (Join-Path $p 'python_env')) -or (Test-Path (Join-Path $p 'tools'))
}

function Get-Dirs([string]$pattern) {
    Get-ChildItem $pattern -Directory -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }
}

# ---------------------------------------------------------------------------
# 1. 定位 ESP-IDF 根目录
# ---------------------------------------------------------------------------
$IdfRoot = $null
$cands = @()
if ($env:IDF_PATH) { $cands += $env:IDF_PATH }
$cands += @(
    'D:\esp\v5.5.1\esp-idf',                                   # 个人机
    'D:\esp-idf',                                              # 工作机
    'D:\esp\esp-idf',
    (Join-Path $env:USERPROFILE 'esp\esp-idf'),
    'C:\esp\esp-idf'
)
$cands += Get-Dirs 'D:\esp\*\esp-idf'
$cands += Get-Dirs (Join-Path $env:USERPROFILE 'esp\*\esp-idf')
$cands += Get-Dirs 'C:\Espressif\frameworks\esp-idf-*'

foreach ($c in $cands) { if (Test-IdfRoot $c) { $IdfRoot = (Resolve-Path $c).Path; break } }

if (-not $IdfRoot) {
    $cmd = Get-Command idf.py -ErrorAction SilentlyContinue
    if ($cmd) {
        $guess = Split-Path (Split-Path $cmd.Source -Parent) -Parent
        if (Test-IdfRoot $guess) { $IdfRoot = (Resolve-Path $guess).Path }
    }
}
if (-not $IdfRoot) {
    Write-Error "未找到 ESP-IDF（需要含 export.ps1 与 tools\idf.py 的目录）。请安装 ESP-IDF v5.5，或设置 IDF_PATH 后重试。"
    exit 1
}

# ---------------------------------------------------------------------------
# 2. 定位 IDF_TOOLS_PATH（工具链 / Python 环境所在目录）
# ---------------------------------------------------------------------------
$ToolsPath = $null
if ($env:IDF_TOOLS_PATH -and (Test-ToolsPath $env:IDF_TOOLS_PATH)) {
    $ToolsPath = $env:IDF_TOOLS_PATH
}
if (-not $ToolsPath) {
    foreach ($c in @('D:\Espressif', 'D:\esp\.espressif',
                     (Join-Path $env:USERPROFILE '.espressif'), 'C:\Espressif')) {
        if (Test-ToolsPath $c) { $ToolsPath = $c; break }
    }
}
# 官方安装器写入的 esp_idf.json 含权威的 idfToolsPath，优先采用
foreach ($c in @($ToolsPath, 'D:\Espressif', 'D:\esp\.espressif',
                 (Join-Path $env:USERPROFILE '.espressif'))) {
    if (-not $c) { continue }
    $json = Join-Path $c 'esp_idf.json'
    if (Test-Path $json) {
        try {
            $cfg = Get-Content $json -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($cfg.idfToolsPath -and (Test-ToolsPath $cfg.idfToolsPath)) {
                $ToolsPath = $cfg.idfToolsPath
                break
            }
        } catch { }
    }
}
if (-not $ToolsPath) { $ToolsPath = Join-Path $env:USERPROFILE '.espressif' }

$env:IDF_TOOLS_PATH = $ToolsPath
# 中国大陆网络：把 GitHub 下载地址重写为乐鑫 CDN（安装工具链 / 拉取组件时生效）
$env:IDF_GITHUB_ASSETS = 'dl.espressif.com/github_assets'

# ---------------------------------------------------------------------------
# 3. 让 export.ps1 选中 IDF 自带的 Python
#    export.ps1 按 PATH 上第一个 python 推导 python_env 目录名；若系统装的是别的
#    小版本（例如 3.14），会去找并不存在的 idf5.5_py3.14_env 而失败。
#    这里把 IDF 的 python_env 提前，从而不依赖系统 Python 版本。
# ---------------------------------------------------------------------------
$pyEnvRoot = Join-Path $ToolsPath 'python_env'
if (Test-Path $pyEnvRoot) {
    $pyEnv = Get-ChildItem (Join-Path $pyEnvRoot 'idf*_py3*_env\Scripts') -Directory -ErrorAction SilentlyContinue |
             Select-Object -First 1
    if ($pyEnv) { $env:PATH = "$($pyEnv.FullName);$env:PATH" }
}

# ---------------------------------------------------------------------------
# 4. 激活环境并执行 idf.py
# ---------------------------------------------------------------------------
# export.ps1 必须用点号调用，才能把 IDF_PATH 等变量留在当前会话
. (Join-Path $IdfRoot 'export.ps1') > $null

if (-not (Get-Command idf.py -ErrorAction SilentlyContinue)) {
    Write-Error "export.ps1 执行后仍未找到 idf.py，请检查 ESP-IDF 安装是否完整：$IdfRoot"
    exit 1
}

Write-Host "[idf] ESP-IDF : $IdfRoot"
Write-Host "[idf] IDF_TOOLS_PATH : $ToolsPath"
idf.py @args
exit $LASTEXITCODE
