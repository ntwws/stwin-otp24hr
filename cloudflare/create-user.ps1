param(
  [Parameter(Mandatory=$true)][string]$AdminUsername,
  [Parameter(Mandatory=$true)][string]$AdminPassword,
  [Parameter(Mandatory=$true)][string]$Username,
  [Parameter(Mandatory=$true)][string]$Password
)
$ErrorActionPreference = 'Stop'
$Api = 'https://hero-line-coordinator.ntwfiber02.workers.dev'
$loginBody = @{ username=$AdminUsername; password=$AdminPassword } | ConvertTo-Json
$login = Invoke-RestMethod -Method Post -Uri "$Api/auth/login" -ContentType 'application/json' -Body $loginBody
$headers = @{ Authorization = "Bearer $($login.token)" }
$userBody = @{ username=$Username; password=$Password; role='user' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$Api/admin/users" -Headers $headers -ContentType 'application/json' -Body $userBody
