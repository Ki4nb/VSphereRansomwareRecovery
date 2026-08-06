# Windows drivers

Two PowerShell helpers for the case that keeps happening mid-incident: you are
on a Windows workstation, and the machine you need to reach is a SystemRescue VM
that someone just typed a root password into at the console. There is no key on
it yet, and Windows' built-in OpenSSH cannot take a password non-interactively.

```powershell
Install-Module Posh-SSH -Scope CurrentUser
```

| Script | Use |
|---|---|
| `rsh.ps1` | run a command, or push and run a script |
| `esxi-run.ps1` | upload a script over SCP and run it with arguments |

Everything else — the batch repair driver and the bring-up driver — is Python in
the parent directory, because it is the same job on every platform and there was
no reason to maintain two copies:

```powershell
python3 ..\esxi_run.py    192.0.2.10 --key $HOME\.ssh\ir_key --script ..\esxi-recon.sh
python3 ..\batch_driver.py --esxi 192.0.2.10 --rescue 192.0.2.50 --key $HOME\.ssh\ir_key
```

Prefer keys. Generate a dedicated pair for the engagement and revoke it when you
are done; treat the host's existing `authorized_keys` as attacker-controlled
until you have read it yourself.

```powershell
ssh-keygen -t ed25519 -f $HOME\.ssh\ir_key -C incident-response
type $HOME\.ssh\ir_key.pub | .\rsh.ps1 -HostIp <ip> -Password '<pw>' `
    -Cmd 'mkdir -p /root/.ssh && cat >> /root/.ssh/authorized_keys'
```

## Two things that cost hours

**PowerShell 5.1 writes a BOM.** `Set-Content -Encoding utf8` and `Out-File`
both emit one, `/bin/sh` reads it as part of the shebang, and you get
`line 1: #!/bin/sh: not found` for a file that is obviously fine. Both scripts
here write with `UTF8Encoding($false)`. If you roll your own, do the same.

**Line endings.** Same failure, same confusing message. Normalise to LF before
uploading anything; `.gitattributes` keeps the repo copies correct, but a file
you generate locally is your problem.
