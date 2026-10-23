param(
    [Parameter(Mandatory = $false)]
    [string]$Domain,

    [Parameter(Mandatory = $false)]
    [string]$ApiKey,

    [Parameter(Mandatory = $false)]
    [int]$PollSeconds = 5,

    [Parameter(Mandatory = $false)]
    [int]$MaxPolls = 12,

    [Parameter(Mandatory = $false)]
    [string]$VercelBypassSecret
)

$ErrorActionPreference = "Stop"

$bypassSecretFile = Join-Path $PSScriptRoot ".vercel-bypass-secret"
if (-not $VercelBypassSecret -and (Test-Path $bypassSecretFile)) {
    $VercelBypassSecret = (Get-Content $bypassSecretFile -Raw).Trim()
}

if (-not $Domain) {
    $Domain = Read-Host "Vercel domain (for example https://obedience-api.vercel.app)"
}

$Domain = $Domain.TrimEnd("/")
if ($Domain -notmatch "^https?://") {
    throw "Domain must start with http:// or https://"
}

if (-not $ApiKey) {
    $ApiKey = Read-Host "API key"
}

if (-not $ApiKey) {
    throw "An API key is required"
}

$headers = @{
    Authorization = "Bearer $ApiKey"
    "Content-Type" = "application/json"
}
if ($VercelBypassSecret) {
    $headers["x-vercel-protection-bypass"] = $VercelBypassSecret
}

$sampleRow = @("0", "123")
$payload = @{
    operation = "solve"
    circuit = @{
        columns = @("A", "OUT")
        rows = @(, $sampleRow)
    }
    standard_parts = @{}
    imvolution_mode = "free"
    iteration_limit = 1000
} | ConvertTo-Json -Depth 10

Write-Host "Submitting job..."
$submission = Invoke-RestMethod `
    -Uri "$Domain/api/v1/jobs" `
    -Method Post `
    -Headers $headers `
    -Body $payload

$jobId = $submission.job_id
if (-not $jobId) {
    throw "The API response did not contain a job_id"
}

Write-Host "Job submitted: $jobId"
Write-Host "Status: $($submission.status)"

$pollCount = 0
do {
    Start-Sleep -Seconds $PollSeconds
    $pollCount++
    $status = Invoke-RestMethod `
        -Uri "$Domain/api/v1/jobs/$jobId" `
        -Method Get `
        -Headers $(if ($VercelBypassSecret) {
            @{
                Authorization = "Bearer $ApiKey"
                "x-vercel-protection-bypass" = $VercelBypassSecret
            }
        } else {
            @{ Authorization = "Bearer $ApiKey" }
        })

    Write-Host "Status: $($status.status)"
} while ($status.status -in @("queued", "running") -and $pollCount -lt $MaxPolls)

Write-Host ""
Write-Host "Final response:"
$status | ConvertTo-Json -Depth 10

if ($status.status -in @("queued", "running")) {
    Write-Host "The job is still active after $MaxPolls polls."
}
