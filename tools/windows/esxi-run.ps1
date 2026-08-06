<#
.SYNOPSIS
    Upload a script to an ESXi host and run it with arguments, from Windows.

.DESCRIPTION
    ESXi's busybox has no base64, so the script is copied over SCP rather than
    inlined. Two details matter and both have bitten:

      * The copy is normalised to LF. A CRLF in the shebang makes /bin/sh report
        "not found" for an interpreter that plainly exists.
      * It is written WITHOUT a BOM. Windows PowerShell 5.1's "-Encoding utf8"
        emits one, and /bin/sh reads it as part of the shebang line.

.EXAMPLE
    .\esxi-run.ps1 -HostIp 192.0.2.10 -Password 'x' -Script ..\esxi-recon.sh
    .\esxi-run.ps1 -HostIp 192.0.2.10 -Password 'x' -Script ..\bringup-recovered-vm.sh `
                   -Arguments 'ubuntu-01 "VM Network" --commit'
#>
param(
    [Parameter(Mandatory = $true)][string]$HostIp,
    [Parameter(Mandatory = $true)][string]$Password,
    [Parameter(Mandatory = $true)][string]$Script,
    [string]$Arguments = '',
    [string]$User      = 'root',
    [int]$TimeOut      = 900
)

Import-Module Posh-SSH -ErrorAction Stop
Get-SSHTrustedHost | Where-Object { $_.HostName -like "*$HostIp*" } |
    Remove-SSHTrustedHost -ErrorAction SilentlyContinue | Out-Null

$sec  = ConvertTo-SecureString $Password -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential($User, $sec)

$tmp  = Join-Path $env:TEMP ([IO.Path]::GetFileName($Script))
$body = (Get-Content -Raw $Script) -replace "`r`n", "`n"
[IO.File]::WriteAllText($tmp, $body, (New-Object Text.UTF8Encoding($false)))

Set-SCPItem -ComputerName $HostIp -Credential $cred -AcceptKey -Force `
            -Path $tmp -Destination '/tmp' -ErrorAction Stop

$name = [IO.Path]::GetFileName($Script)
$ssh  = New-SSHSession -ComputerName $HostIp -Credential $cred -AcceptKey -Force `
                       -ConnectionTimeout 30
try {
    $r = Invoke-SSHCommand -SessionId $ssh.SessionId -TimeOut $TimeOut `
             -Command "sh /tmp/$name $Arguments"
    if ($r.Output) { $r.Output }
    if ($r.Error)  { "--- stderr ---"; $r.Error }
    if ($r.ExitStatus -ne 0) { "--- exit: $($r.ExitStatus) ---" }
}
finally { Remove-SSHSession -SessionId $ssh.SessionId | Out-Null }
