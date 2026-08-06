<#
.SYNOPSIS
    Run a command on an ESXi host or a rescue VM, from Windows, with a password.

.DESCRIPTION
    Windows ships OpenSSH but it cannot take a password non-interactively, and a
    freshly booted SystemRescue VM has a password and no key. Posh-SSH fills that
    gap:

        Install-Module Posh-SSH -Scope CurrentUser

    Prefer key authentication and tools/esxi_run.py where you can. This exists
    for the case that actually happens mid-incident: a rescue VM someone just
    typed a password into at the console.

.EXAMPLE
    .\rsh.ps1 -HostIp 192.0.2.10 -Password 'x' -Cmd 'lsblk'
    .\rsh.ps1 -HostIp 192.0.2.10 -Password 'x' -File .\tools\esxi-recon.sh
    .\rsh.ps1 -HostIp 192.0.2.10 -Password 'x' -File .\tools\esxi-recon.sh -Inline
#>
param(
    [Parameter(Mandatory = $true)][string]$HostIp,
    [Parameter(Mandatory = $true)][string]$Password,
    [string]$Cmd,
    [string]$File,
    [switch]$Inline,          # run the script text directly (hosts without base64)
    [string]$User    = 'root',
    [int]$TimeOut    = 600
)

Import-Module Posh-SSH -ErrorAction Stop

# Rescue ISOs regenerate their host key on every boot, so a cached entry is a
# guaranteed false alarm rather than a signal.
Get-SSHTrustedHost | Where-Object { $_.HostName -like "*$HostIp*" } |
    Remove-SSHTrustedHost -ErrorAction SilentlyContinue | Out-Null

$sec  = ConvertTo-SecureString $Password -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential($User, $sec)
$s = New-SSHSession -ComputerName $HostIp -Credential $cred -AcceptKey -Force `
                    -ConnectionTimeout 30 -ErrorAction Stop
try {
    if ($File) {
        $txt = (Get-Content -Raw $File) -replace "`r`n", "`n"
        if ($Inline) {
            # ESXi's busybox has no base64. Run the script text straight down the
            # exec channel instead. Fine for plain sh; avoid nested heredocs.
            $Cmd = $txt
        } else {
            $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($txt))
            $Cmd = "echo '$b64' | base64 -d > /tmp/_rsh.sh && sh /tmp/_rsh.sh"
        }
    }
    $r = Invoke-SSHCommand -SessionId $s.SessionId -Command $Cmd -TimeOut $TimeOut
    if ($r.Output) { $r.Output }
    if ($r.Error)  { "--- stderr ---"; $r.Error }
    if ($r.ExitStatus -ne 0) { "--- exit: $($r.ExitStatus) ---" }
}
finally { Remove-SSHSession -SessionId $s.SessionId | Out-Null }
