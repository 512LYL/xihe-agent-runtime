<#
.SYNOPSIS
    push-all.ps1 —— 单一上游 + 一推两发（模板化，可复制给日后新仓）

.DESCRIPTION
    纪律（来自仓库拓扑定稿，逐条照做）：
      1. 「H（GitHub 主源）→ G2（GitLink 基金补助版）」**永远单向推**；
         ❌ 绝不反向 pull；❌ 不做双向互镜像。
      2. 若 H 尚未建成 → 删掉 origin 一段，gitlink 升为唯一上游，其余完全不变。
      3. 每次推送后 30 秒体检（远端 / 最近提交 / 本地体积）。
      4. 推送前先跑敏感信息扫描，命中即中止。

.NOTES
    本脚本**只推不改**：不做 rebase / 不 force push / 不删远端分支。
    真实远端地址请写在本地（不要写进本文件），或用 -GitHubUrl / -GitLinkUrl 传入。

.EXAMPLE
    # 只做体检，不推送
    pwsh scripts/push-all.ps1 -DryRun

.EXAMPLE
    # 扫描通过后推两处
    pwsh scripts/push-all.ps1 -Message "feat: 多通道路由 v0.1.1" `
        -GitHubUrl "https://github.com/<you>/xihe-agent-runtime.git" `
        -GitLinkUrl "https://www.gitlink.org.cn/<you>/xihe-agent-runtime.git"
#>
[CmdletBinding()]
param(
    [string]$Message = "",
    [string]$GitHubUrl = "",
    [string]$GitLinkUrl = "",
    [switch]$DryRun,
    [switch]$SkipScan
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Write-Host "== repo: $repo" -ForegroundColor Cyan

# ---------------------------------------------------------------- 0. 闸门：敏感信息扫描
if (-not $SkipScan) {
    Write-Host "`n== [0/4] 敏感信息扫描（公开前最后一道闸）" -ForegroundColor Cyan
    python (Join-Path $PSScriptRoot 'scan_secrets.py') --path $repo
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ 扫描未通过 —— 已中止推送。修复后重跑。" -ForegroundColor Red
        exit 1
    }
}

# ---------------------------------------------------------------- 1. 提交
if ($Message) {
    Write-Host "`n== [1/4] 暂存并提交" -ForegroundColor Cyan
    git -C $repo add -A
    git -C $repo commit -m $Message
} else {
    Write-Host "`n== [1/4] 未给 -Message，跳过提交（假定已手动提交）" -ForegroundColor Yellow
}

# ---------------------------------------------------------------- 2. 配远端（可选）
if ($GitHubUrl) {
    git -C $repo remote remove origin 2>$null
    git -C $repo remote add origin $GitHubUrl
    Write-Host "   origin  = $GitHubUrl  （H：GitHub 主源，唯一上游）"
}
if ($GitLinkUrl) {
    git -C $repo remote remove gitlink 2>$null
    git -C $repo remote add gitlink $GitLinkUrl
    Write-Host "   gitlink = $GitLinkUrl  （G2：GitLink 基金补助版，单向镜像）"
}

$remotes = git -C $repo remote
if (-not $remotes) {
    Write-Host "`n⚠️  尚未配置任何远端 —— 只做本地体检，不推送。" -ForegroundColor Yellow
    Write-Host "    （H/G2 未建成前这是正常状态，符合「只做本地初稿」纪律）"
    $DryRun = $true
}

$branch = (git -C $repo rev-parse --abbrev-ref HEAD).Trim()

# ---------------------------------------------------------------- 3. 推送
Write-Host "`n== [3/4] 推送（单向：主源 → 镜像）" -ForegroundColor Cyan
if ($DryRun) {
    Write-Host "   [DryRun] 跳过推送" -ForegroundColor Yellow
} else {
    if ($remotes -contains 'origin') {
        Write-Host "   git push origin $branch"
        git -C $repo push origin $branch
    } else {
        Write-Host "   （无 origin：H 未建成，改由 gitlink 作唯一上游）" -ForegroundColor Yellow
    }
    if ($remotes -contains 'gitlink') {
        Write-Host "   git push gitlink $branch  （单向镜像）"
        git -C $repo push gitlink $branch
    }
    Write-Host "   ❌ 绝不执行 git pull gitlink —— 反向拉取会破坏单一上游" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------- 4. 体检
Write-Host "`n== [4/4] 推送后 30 秒体检" -ForegroundColor Cyan
Write-Host "`n-- git remote -v";        git -C $repo remote -v
Write-Host "`n-- git log --oneline -3"; git -C $repo log --oneline -3
Write-Host "`n-- git count-objects -vH（本地体积，应远小于 100 MB）"; git -C $repo count-objects -vH
Write-Host "`n-- git status --short";   git -C $repo status --short

Write-Host "`n✅ 完成。若两侧 README / Release 有任何不一致，以**主源**为准。" -ForegroundColor Green
