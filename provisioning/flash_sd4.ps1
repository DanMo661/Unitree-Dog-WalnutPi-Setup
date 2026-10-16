$ErrorActionPreference = 'Stop'
# v2: 多合一读卡器会枚举多个 0GB 空 USB 盘, 目标判定改为 "USB + 14~16GB 恰好一张"
$img = 'D:\ZcodeRely\walnutpi\walnutpi-1b-netplan.img'

Write-Output "USB disks present:"
@(Get-Disk | Where-Object { $_.BusType -eq 'USB' }) | ForEach-Object {
    Write-Output ("  Disk{0} {1} {2:N2}GB {3}" -f $_.Number, $_.FriendlyName, ($_.Size/1GB), $_.PartitionStyle)
}
$cands = @(Get-Disk | Where-Object { $_.BusType -eq 'USB' -and $_.Size -ge 8GB -and $_.Size -le 64GB })
if ($cands.Count -ne 1) { throw ("Expect exactly one 14-16GB USB disk, got " + $cands.Count) }
$disk = $cands[0]
if ($disk.FriendlyName -notlike '*Mass*Storage*') { throw "Disk identity mismatch: $($disk.FriendlyName)" }
if ($disk.IsOffline) { Set-Disk -Number $disk.Number -IsOffline $false; Start-Sleep 2 }
Write-Output ("TARGET: Disk{0} = {1}, {2:N2}GB" -f $disk.Number, $disk.FriendlyName, ($disk.Size/1GB))

$imgLen = (Get-Item $img).Length
if ($imgLen -gt $disk.Size) { throw "Image larger than card" }
Write-Output ("IMAGE: {0} ({1:N0} bytes)" -f (Split-Path $img -Leaf), $imgLen)

# 卡上还有分区/卷挂载时, mountvol 够不着无盘符卷 -> diskpart clean 清 MBR 释放句柄
Set-Disk -Number $disk.Number -IsReadOnly $false
if ($disk.NumberOfPartitions -gt 0) {
    Write-Output ("Clearing disk via diskpart (was {0} partition(s), blank-new card expected)..." -f $disk.NumberOfPartitions)
    $dp = "select disk $($disk.Number)`r`nclean`r`nexit"
    $out = $dp | diskpart | Out-String
    Write-Output ($out -split "`r`n" | Where-Object { $_ -match 'clean|错误|denied|成功' } | ForEach-Object { "  diskpart: $_" })
    Start-Sleep 2
    $after = @(Get-Partition -DiskNumber $disk.Number -ErrorAction SilentlyContinue).Count
    if ($after -gt 0) { throw "diskpart clean left $after partitions, abort" }
}
Write-Output "Writing..."

$src = [IO.File]::OpenRead($img)
$dst = [IO.File]::Open('\\.\PhysicalDrive' + $disk.Number, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
$buf = New-Object byte[] 1048576
$sw = [Diagnostics.Stopwatch]::StartNew()
$pos = 0L
while ($pos -lt $imgLen) {
    $want = [int][Math]::Min([long]1048576, $imgLen - $pos)
    $got = $src.Read($buf, 0, $want)
    if ($got -le 0) { break }
    $wr = $got
    if ($wr % 512 -ne 0) {
        $wr = [int]([Math]::Ceiling($got / 512) * 512)
        [Array]::Clear($buf, $got, $wr - $got)
    }
    $dst.Write($buf, 0, $wr)
    $pos += $got
    if (($pos % 268435456) -lt 1048576) {
        Write-Output ("WROTE {0:N0}/{1:N0} MB  {2:N1} MB/s" -f ($pos/1MB), ($imgLen/1MB), ($pos/1MB/$sw.Elapsed.TotalSeconds))
    }
}
$dst.Flush($true)
$dst.Close()
$src.Close()
Write-Output ("WRITE DONE: {0:N0} bytes in {1:N0} s" -f $pos, $sw.Elapsed.TotalSeconds)

Write-Output "Verifying (read back + SHA256)..."
$sw2 = [Diagnostics.Stopwatch]::StartNew()
$hImg = [Security.Cryptography.SHA256]::Create()
$hDsk = [Security.Cryptography.SHA256]::Create()
$src2 = [IO.File]::OpenRead($img)
$dst2 = [IO.File]::Open('\\.\PhysicalDrive' + $disk.Number, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
$b1 = New-Object byte[] 1048576
$b2 = New-Object byte[] 1048576
$vpos = 0L
while (($n = $src2.Read($b1, 0, 1048576)) -gt 0) {
    $r = 0
    while ($r -lt $n) {
        $r2 = $dst2.Read($b2, $r, $n - $r)
        if ($r2 -le 0) { throw "Disk EOF during verify" }
        $r += $r2
    }
    [void]$hImg.TransformBlock($b1, 0, $n, $null, 0)
    [void]$hDsk.TransformBlock($b2, 0, $n, $null, 0)
    $vpos += $n
    if (($vpos % 1073741824) -lt 1048576) { Write-Output ("VERIFIED {0:N1} GB" -f ($vpos/1GB)) }
}
$src2.Close()
$dst2.Close()
[void]$hImg.TransformFinalBlock($b1, 0, 0)
[void]$hDsk.TransformFinalBlock($b2, 0, 0)
$sumImg = [BitConverter]::ToString($hImg.Hash) -replace '-', ''
$sumDsk = [BitConverter]::ToString($hDsk.Hash) -replace '-', ''
Write-Output ("SHA256 image: {0}" -f $sumImg)
Write-Output ("SHA256 disk : {0}" -f $sumDsk)
Write-Output ("verify took {0:N0} s" -f $sw2.Elapsed.TotalSeconds)

try { Set-Disk -Number $disk.Number -IsOffline $true } catch { }
Write-Output "Done (drive letter stays removed; re-plug card to mount)"
if ($sumImg -eq $sumDsk) { Write-Output "RESULT: PASS" } else { Write-Output "RESULT: FAIL - hash mismatch" }
