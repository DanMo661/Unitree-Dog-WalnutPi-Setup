# Flash a raw .img to the ONLY USB card reader disk (8-64GB).
#
# Why this order: Windows denies raw writes to \\.\PhysicalDriveN whenever a volume
# on that disk is mounted. Writing the MBR first makes Windows detect partitions
# mid-write and mount them, so the rest of the write fails with Access Denied.
# Fix: write 1MB..end while the disk still has NO partition table, verify, then
# write the MBR (first 1MB) last and verify it.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads BOM-less files as
# ANSI/GBK; a UTF-8 Chinese comment can end in 0x60 (backtick = line continuation)
# and silently swallow the next line.
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File tools\flash_sd.ps1
#         powershell ... -File tools\flash_sd.ps1 -Image D:\path\to\other.img
param(
    [string]$Image = 'D:\ZcodeRely\projects\walnutpi\walnutpi-1b-golden.img',
    [int]$MinGB = 8,
    [int]$MaxGB = 64
)
$ErrorActionPreference = 'Stop'
$bs = [string][char]92

if (-not (Test-Path $Image)) { throw "image not found: $Image" }
$imgLen = (Get-Item $Image).Length
Write-Output ("IMAGE {0} ({1:N0} bytes)" -f (Split-Path $Image -Leaf), $imgLen)

# --- guard: exactly one USB disk in range; disk 0 (system) is never a candidate ---
$cands = @()
for ($t = 0; $t -lt 5 -and $cands.Count -ne 1; $t++) {
    $cands = @(Get-Disk | Where-Object {
        $_.BusType -eq 'USB' -and $_.Size -ge ($MinGB * 1GB) -and $_.Size -le ($MaxGB * 1GB)
    })
    if ($cands.Count -ne 1) { Start-Sleep -Seconds 2 }
}
Get-Disk | Where-Object { $_.BusType -eq 'USB' } | ForEach-Object {
    Write-Output ("  USB Disk{0} {1} {2:N2}GB" -f $_.Number, $_.FriendlyName, ($_.Size/1GB))
}
if ($cands.Count -ne 1) { throw ("expect exactly 1 USB disk in {0}-{1}GB, got {2}" -f $MinGB, $MaxGB, $cands.Count) }
$n = $cands[0].Number
if ($n -eq 0) { throw "refusing disk 0 (system disk)" }
if ($imgLen -gt $cands[0].Size) { throw "image larger than card" }
Write-Output ("TARGET Disk{0} {1} {2:N2}GB" -f $n, $cands[0].FriendlyName, ($cands[0].Size/1GB))
$dev = $bs + $bs + '.' + $bs + 'PhysicalDrive' + $n

$amOn = $false
function Set-Automount([bool]$on) {
    $f = Join-Path $env:TEMP ("dp_am_{0}.txt" -f $(if ($on) {'on'} else {'off'}))
    $(if ($on) { "automount enable`r`nexit`r`n" } else { "automount disable`r`nexit`r`n" }) |
        Set-Content -Path $f -Encoding ASCII
    & diskpart /s $f | Out-Null
}
function Open-Dev { return [IO.File]::Open($dev, 'Open', 'ReadWrite', 'ReadWrite') }

$src = $null
$dst = $null
try {
    # --- clean: wipe any partition table so Windows has nothing to mount ---
    Set-Automount $false
    $amOn = $true
    $dpf = Join-Path $env:TEMP 'dp_clean.txt'
    ("select disk $n`r`nclean`r`nexit`r`n") | Set-Content -Path $dpf -Encoding ASCII
    & diskpart /s $dpf | Out-Null
    Start-Sleep -Seconds 2
    $np = @(Get-Partition -DiskNumber $n -ErrorAction SilentlyContinue)
    Write-Output ("CLEAN: partitions={0}" -f $np.Count)
    if ($np.Count -ne 0) { throw "partitions still present after clean" }

    $src = [IO.File]::OpenRead($Image)
    $head = New-Object byte[] 1048576
    $null = $src.Read($head, 0, 1048576)
    $dst = Open-Dev

    # --- body write: 1MB chunks, retry with reopen (cheap readers hiccup) ---
    $null = $src.Seek(1048576, 'Begin')
    $buf = New-Object byte[] 1048576
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $pos = 1048576L
    $retries = 0
    while ($pos -lt $imgLen) {
        $want = [int][Math]::Min([long]1048576, $imgLen - $pos)
        $got = $src.Read($buf, 0, $want)
        if ($got -le 0) { throw "short image read at $pos" }
        $ok = $false
        for ($i = 0; $i -lt 6 -and -not $ok; $i++) {
            try {
                $null = $dst.Seek($pos, 'Begin')
                $dst.Write($buf, 0, $got)
                $ok = $true
            } catch {
                $retries++
                Write-Output ("RETRY#{0} at {1} MB: {2}" -f $i, [math]::Round($pos/1MB), $_.Exception.Message.Trim())
                try { $dst.Close() } catch {}
                Start-Sleep -Milliseconds 500
                $dst = Open-Dev
            }
        }
        if (-not $ok) { throw ("write failed at {0} MB" -f [math]::Round($pos/1MB)) }
        $pos += $got
        if (($pos / 1MB) % 64 -eq 0) {
            Write-Output ("WROTE {0}/{1} MB at {2:N1} MB/s" -f [math]::Round($pos/1MB), [math]::Round($imgLen/1MB), ($pos/1MB/$sw.Elapsed.TotalSeconds))
        }
    }
    $dst.Flush()
    Write-Output ("BODY DONE {0:N0} bytes in {1:N0}s retries={2}" -f ($pos - 1048576), $sw.Elapsed.TotalSeconds, $retries)

    # --- body verify (same handle must be ReadWrite; Write-only cannot read back) ---
    $null = $src.Seek(1048576, 'Begin')
    $b1 = New-Object byte[] 1048576
    $b2 = New-Object byte[] 1048576
    $pos = 1048576L
    $bad = 0
    $sw2 = [Diagnostics.Stopwatch]::StartNew()
    while ($pos -lt $imgLen) {
        $want = [int][Math]::Min([long]1048576, $imgLen - $pos)
        $g2 = $src.Read($b2, 0, $want)
        if ($g2 -le 0) { throw "short image read (verify) at $pos" }
        $ok = $false; $g1 = 0
        for ($i = 0; $i -lt 6 -and -not $ok; $i++) {
            try { $null = $dst.Seek($pos, 'Begin'); $g1 = $dst.Read($b1, 0, $want); $ok = $true }
            catch {
                try { $dst.Close() } catch {}
                Start-Sleep -Milliseconds 500
                $dst = Open-Dev
            }
        }
        if (-not $ok) { throw ("verify read failed at {0} MB" -f [math]::Round($pos/1MB)) }
        if ($g1 -ne $want -or -not [Linq.Enumerable]::SequenceEqual([byte[]]$b2, [byte[]]$b1)) { $bad++ }
        $pos += $want
        if (($pos / 1MB) % 256 -eq 0) {
            Write-Output ("VERIFIED {0}/{1} MB at {2:N1} MB/s" -f [math]::Round($pos/1MB), [math]::Round($imgLen/1MB), ($pos/1MB/$sw2.Elapsed.TotalSeconds))
        }
    }
    if ($bad -gt 0) { throw ("BODY VERIFY FAILED: {0} chunks differ" -f $bad) }
    Write-Output ("BODY VERIFY OK in {0:N0}s" -f $sw2.Elapsed.TotalSeconds)

    # --- MBR last: single write; Windows may mount right after, but no more writes needed ---
    $ok = $false
    for ($i = 0; $i -lt 6 -and -not $ok; $i++) {
        try { $null = $dst.Seek(0, 'Begin'); $dst.Write($head, 0, 1048576); $dst.Flush(); $ok = $true }
        catch { try { $dst.Close() } catch {}; Start-Sleep -Milliseconds 500; $dst = Open-Dev }
    }
    if (-not $ok) { throw "MBR write failed" }
    $null = $dst.Seek(0, 'Begin')
    $hb = New-Object byte[] 1048576
    $g = $dst.Read($hb, 0, 1048576)
    if ($g -ne 1048576 -or -not [Linq.Enumerable]::SequenceEqual([byte[]]$head, [byte[]]$hb)) { throw "HEAD VERIFY FAILED" }
    Write-Output "HEAD(MBR) WRITE+VERIFY OK"
} finally {
    if ($src) { try { $src.Close() } catch {} }
    if ($dst) { try { $dst.Close() } catch {} }
    if ($amOn) { Set-Automount $true }
}

Start-Sleep -Seconds 2
Get-Partition -DiskNumber $n -ErrorAction SilentlyContinue |
    Select-Object PartitionNumber, @{n='MB';e={[math]::Round($_.Size/1MB)}} |
    Format-Table -AutoSize | Out-String | Write-Output
Write-Output "FLASH_COMPLETE - safe to unplug"
