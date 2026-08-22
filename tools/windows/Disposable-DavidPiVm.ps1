[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Plan', 'Create', 'Start', 'Status', 'Remove')]
    [string]$Action,

    [string]$VmName = 'david-pi-disposable-test',
    [string]$VmRoot = "$env:LOCALAPPDATA\DavidPiDisposableVm",
    [string]$IsoPath
)

$ErrorActionPreference = 'Stop'
$VBoxManage = Join-Path $env:ProgramFiles 'Oracle\VirtualBox\VBoxManage.exe'
$SafetyFloorBytes = 25GB

function Assert-VirtualBox {
    if (-not (Test-Path -LiteralPath $VBoxManage)) {
        throw 'VirtualBox is not installed in the standard location.'
    }
}

function Get-FreeBytes {
    $drive = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
    return [int64]$drive.FreeSpace
}

function Invoke-VBox {
    param([Parameter(ValueFromRemainingArguments)][string[]]$Arguments)
    & $VBoxManage @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "VBoxManage failed: $($Arguments -join ' ')"
    }
}

function Assert-SafeVmRoot {
    $resolvedParent = [IO.Path]::GetFullPath((Split-Path -Parent $VmRoot))
    $resolvedRoot = [IO.Path]::GetFullPath($VmRoot)
    if (-not $resolvedRoot.StartsWith($resolvedParent + [IO.Path]::DirectorySeparatorChar)) {
        throw 'VM root did not resolve beneath its intended parent.'
    }
    if ($resolvedRoot -in @('C:\', $env:USERPROFILE, $env:LOCALAPPDATA)) {
        throw 'Refusing a broad VM root.'
    }
}

if ($Action -eq 'Plan') {
    [pscustomobject]@{
        Name = $VmName
        Root = [IO.Path]::GetFullPath($VmRoot)
        MemoryMiB = 6144
        CpuCount = 4
        OsDiskMiB = 20480
        PrimaryDiskMiB = 4096
        BackupDiskMiB = 4096
        Network = 'NAT'
        SshForward = '127.0.0.1:2222 -> guest:22'
        WindowsFreeGiB = [math]::Round((Get-FreeBytes) / 1GB, 1)
        RequiredSafetyFloorGiB = 25
    } | Format-List
    return
}

Assert-VirtualBox
Assert-SafeVmRoot

switch ($Action) {
    'Create' {
        if (-not $IsoPath -or -not (Test-Path -LiteralPath $IsoPath -PathType Leaf)) {
            throw 'Create requires -IsoPath pointing to the checksum-verified Debian netinst ISO.'
        }
        if ((Get-FreeBytes) -lt ($SafetyFloorBytes + 6GB)) {
            throw 'Insufficient Windows space to create the VM while preserving the 25 GiB safety floor.'
        }
        $existing = & $VBoxManage list vms
        if ($existing -match [regex]::Escape('"' + $VmName + '"')) {
            throw "A VirtualBox VM named $VmName already exists."
        }
        New-Item -ItemType Directory -Path $VmRoot -Force | Out-Null
        $osDisk = Join-Path $VmRoot 'david-pi-os.vdi'
        $primaryDisk = Join-Path $VmRoot 'david-pi-primary.vdi'
        $backupDisk = Join-Path $VmRoot 'david-pi-backup.vdi'

        Invoke-VBox createvm --name $VmName --ostype Debian_64 --basefolder $VmRoot --register
        try {
            Invoke-VBox modifyvm $VmName --memory 6144 --cpus 4 --ioapic on --pae on --nested-hw-virt off
            Invoke-VBox modifyvm $VmName --nic1 nat --nictype1 virtio --natpf1 'ssh,tcp,127.0.0.1,2222,,22'
            Invoke-VBox modifyvm $VmName --boot1 dvd --boot2 disk --boot3 none --boot4 none --audio-enabled off --usb-xhci on
            Invoke-VBox createmedium disk --filename $osDisk --size 20480 --format VDI --variant Standard
            Invoke-VBox createmedium disk --filename $primaryDisk --size 4096 --format VDI --variant Standard
            Invoke-VBox createmedium disk --filename $backupDisk --size 4096 --format VDI --variant Standard
            Invoke-VBox storagectl $VmName --name SATA --add sata --controller IntelAhci --portcount 4 --bootable on
            Invoke-VBox storageattach $VmName --storagectl SATA --port 0 --device 0 --type hdd --medium $osDisk
            Invoke-VBox storageattach $VmName --storagectl SATA --port 1 --device 0 --type hdd --medium $primaryDisk
            Invoke-VBox storageattach $VmName --storagectl SATA --port 2 --device 0 --type hdd --medium $backupDisk
            Invoke-VBox storageattach $VmName --storagectl SATA --port 3 --device 0 --type dvddrive --medium ([IO.Path]::GetFullPath($IsoPath))
        }
        catch {
            & $VBoxManage unregistervm $VmName --delete | Out-Null
            throw
        }
        Write-Host 'VM created. During Debian partitioning, select only the 20 GiB OS disk.'
        Write-Host 'The two 4 GiB disks must remain blank until david-pi prepare-storage is run.'
        Write-Host 'Start with: powershell -File tools\windows\Disposable-DavidPiVm.ps1 -Action Start'
    }
    'Start' {
        Invoke-VBox startvm $VmName --type gui
    }
    'Status' {
        Invoke-VBox showvminfo $VmName --machinereadable
        Write-Host ('Windows C: free: {0:N1} GiB' -f ((Get-FreeBytes) / 1GB))
    }
    'Remove' {
        $answer = Read-Host "Type DELETE $VmName to remove only this disposable VM and its attached disks"
        if ($answer -cne "DELETE $VmName") {
            throw 'Confirmation did not match; nothing was removed.'
        }
        $state = & $VBoxManage showvminfo $VmName --machinereadable 2>$null
        if ($state -match 'VMState="running"') {
            throw 'Power off the VM cleanly before removal.'
        }
        Invoke-VBox unregistervm $VmName --delete
        if (Test-Path -LiteralPath $VmRoot) {
            $resolved = [IO.Path]::GetFullPath($VmRoot)
            if ($resolved.StartsWith([IO.Path]::GetFullPath($env:LOCALAPPDATA) + [IO.Path]::DirectorySeparatorChar)) {
                Remove-Item -LiteralPath $resolved -Recurse -Force
            }
        }
        Write-Host ('VM removed. Windows C: free: {0:N1} GiB' -f ((Get-FreeBytes) / 1GB))
    }
}
