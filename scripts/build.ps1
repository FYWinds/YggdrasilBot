<#
.SYNOPSIS
  Build and push the yggdrasil-bot image with the in-cluster rootless BuildKit.

.DESCRIPTION
  1. Asks Gitea to sync its pull-mirror of the GitHub repo so <Ref> is present.
  2. Port-forwards svc/buildkitd (namespace buildkit) to 127.0.0.1:1234.
  3. Runs buildctl with the Gitea git URL as build context (BuildKit clones it
     in-cluster, nothing is uploaded from this machine) and pushes the image to
     the Gitea container registry over in-cluster HTTP.

  Required local tools:
    - kubectl, with a kubeconfig for the k3s cluster
    - buildctl, from a moby/buildkit release archive
      (https://github.com/moby/buildkit/releases, bin/buildctl.exe)
    - git (to resolve -Ref)

  Required environment:
    - GITEA_TOKEN: Gitea access token (scope write:repository) for mirror-sync.
    - DOCKER_CONFIG: directory holding a config.json with registry credentials
      buildctl forwards to buildkitd for the push, e.g.
        {"auths":{"gitea-http.gitea.svc:3000":{"auth":"<base64 user:token>"}}}
      where the token has scope write:package. Defaults to ~/.docker.

.EXAMPLE
  ./scripts/build.ps1
  ./scripts/build.ps1 -Ref v0.2.0 -CertDir ~/.config/buildkit/client
#>
[CmdletBinding()]
param(
    # Git sha or tag to build; defaults to the current HEAD.
    [string]$Ref = "",
    # Image tags; defaults to v<pyproject version> and sha-<short sha>.
    [string[]]$Tag = @(),
    # Directory with the BuildKit client ca.pem, cert.pem, key.pem (see infra/buildkit/README.md).
    [string]$CertDir = (Join-Path $HOME ".config/buildkit/client"),
    [string]$GiteaUrl = "https://git.fyw.fyi",
    [string]$Repo = "FYWinds/YggdrasilBot",
    [string]$Image = "gitea-http.gitea.svc:3000/fywinds/yggdrasil-bot",
    [int]$LocalPort = 1234,
    # Skip the mirror sync (e.g. the ref is already in Gitea).
    [switch]$NoSync
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$repoRoot = Split-Path -Parent $PSScriptRoot

foreach ($tool in "kubectl", "buildctl", "git") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool not found on PATH"
    }
}
foreach ($pem in "ca.pem", "cert.pem", "key.pem") {
    if (-not (Test-Path (Join-Path $CertDir $pem))) { throw "missing $pem in $CertDir" }
}

if (-not $Ref) { $Ref = "HEAD" }
$sha = (git -C $repoRoot rev-parse "$Ref^{commit}").Trim()
if ($LASTEXITCODE -ne 0) { throw "cannot resolve ref $Ref" }
$short = $sha.Substring(0, 7)

if ($Tag.Count -eq 0) {
    $pyproject = git -C $repoRoot show "${sha}:pyproject.toml"
    $match = $pyproject | Select-String -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
    if (-not $match) { throw "no version in pyproject.toml at $short" }
    $Tag = @("v$($match.Matches[0].Groups[1].Value)", "sha-$short")
}
$names = ($Tag | ForEach-Object { "${Image}:$_" }) -join ","

if (-not $NoSync) {
    if (-not $env:GITEA_TOKEN) { throw "GITEA_TOKEN is not set" }
    Write-Host "Syncing Gitea mirror $Repo"
    Invoke-RestMethod -Method Post -Uri "$GiteaUrl/api/v1/repos/$Repo/mirror-sync" `
        -Headers @{ Authorization = "token $env:GITEA_TOKEN" } | Out-Null
    # mirror-sync only queues the fetch; wait until the commit is visible.
    $deadline = (Get-Date).AddMinutes(3)
    while ($true) {
        try {
            Invoke-RestMethod -Uri "$GiteaUrl/api/v1/repos/$Repo/git/commits/$sha" `
                -Headers @{ Authorization = "token $env:GITEA_TOKEN" } | Out-Null
            break
        } catch {
            if ((Get-Date) -gt $deadline) { throw "commit $short not in Gitea mirror after 3 minutes" }
            Start-Sleep -Seconds 5
        }
    }
}

Write-Host "Port-forwarding buildkit/svc/buildkitd to 127.0.0.1:$LocalPort"
$pf = Start-Process kubectl -ArgumentList "-n", "buildkit", "port-forward", "svc/buildkitd", "${LocalPort}:1234" `
    -NoNewWindow -PassThru
try {
    $deadline = (Get-Date).AddSeconds(20)
    while (-not (Test-NetConnection 127.0.0.1 -Port $LocalPort -InformationLevel Quiet -WarningAction SilentlyContinue)) {
        if ($pf.HasExited) { throw "kubectl port-forward exited with $($pf.ExitCode)" }
        if ((Get-Date) -gt $deadline) { throw "port-forward not ready after 20s" }
        Start-Sleep -Milliseconds 500
    }

    Write-Host "Building $sha -> $names"
    buildctl --addr "tcp://127.0.0.1:$LocalPort" `
        --tlscacert (Join-Path $CertDir "ca.pem") `
        --tlscert (Join-Path $CertDir "cert.pem") `
        --tlskey (Join-Path $CertDir "key.pem") `
        build `
        --frontend dockerfile.v0 `
        --opt "context=http://gitea-http.gitea.svc:3000/$Repo.git#$sha" `
        --opt "label:org.opencontainers.image.revision=$sha" `
        --output "type=image,`"name=$names`",push=true,registry.insecure=true"
    if ($LASTEXITCODE -ne 0) { throw "buildctl failed with $LASTEXITCODE" }
    Write-Host "Pushed $names"
} finally {
    if (-not $pf.HasExited) { Stop-Process -Id $pf.Id -Force }
}
