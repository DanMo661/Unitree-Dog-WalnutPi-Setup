# TF 卡烧录（权威流程）

适用：核桃派 1B / 香橙派 Zero 2W 这类「裸盘 .img」镜像，Windows 侧从读卡器写卡。
一键脚本：`provisioning/flash_sd.ps1`（本文所有结论它的注释里都有对应）。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File provisioning\flash_sd.ps1
# 换镜像：  -Image D:\path\to\xxx.img
```

## 一、核心结论：写卡顺序决定成败

**Windows 只要发现目标盘上有已挂载的卷，就对 `\\.\PhysicalDriveN` 的裸写一律返回「拒绝访问」。**
与只读标志、盘符、automount 无关。

而镜像是**先写 MBR 再写数据**的话：MBR 一落盘，Windows 立刻识别出新分区并挂载 → 后半段写入全部失败。
这就是"能写一点，写到某个位置必挂"的真相（表现常是 143MB/256MB 附近反复失败，因为那是第一个 FAT 分区边界）。

**正确顺序（2026-11-02 实测通过）**：

1. `diskpart clean` 清掉分区表 → 此时盘上无分区，裸写全程可写
2. 写 `1MB .. 文件末尾`（数据区）
3. 回读校验数据区
4. **最后**一次性写前 1MB（MBR + 引导区），写完立刻校验
5. 此后 Windows 再挂载也无所谓——不再需要写入

## 二、写卡脚本硬性要求

| 要求 | 原因 |
|---|---|
| 目标盘守卫：只认 **唯一一块 USB 总线、8–64GB** 的盘，并显式拒绝 Disk 0 | 防写坏系统盘；换读卡器/换卡后盘号会变 |
| 设备句柄用 **ReadWrite** 打开 | 只写句柄 `FileAccess.Write` 无法回读校验（会报"verify read failed"） |
| 1MB 分块 + 失败重试（重开句柄） | 廉价读卡器持续写入会偶发 I/O 错误；本次实测写 2.7GB 零重试，但必须有兜底 |
| 校验用**逐字节比对**，不用 SHA256 流式 | 流式校验一旦中途 I/O 出错只能整段重来；分块可断点重试 |
| 收尾恢复 `automount enable` | 脚本中途失败也不能让系统一直处于禁用自动挂载状态 |

## 三、脚本本身的三个坑（都踩过）

1. **PowerShell 5.1 用 ANSI/GBK 读无 BOM 的 UTF-8 脚本**：中文注释的最后一个字节可能落在 `0x60`（反引号＝续行符），
   把下一行代码吞进注释，表现为"变量莫名不存在"。→ **写卡脚本一律纯 ASCII 注释，不用中文**。
2. **Git Bash 调 `diskpart /s` 会被 MSYS 把 `/s` 转成路径**，diskpart 报"无法处理这些参数"而空转。
   → 用 PowerShell 包装并传**绝对 Windows 路径**，或直接 `& diskpart /s <绝对路径>`。
3. **`diskpart` 的输出是中文**，用英文关键词（`successfully cleaned`）判成败会误判。
   → 判成败看**分区数是否归零**，不看文案。

## 四、烧完的验收（30 秒）

1. `Get-Partition -DiskNumber N`：分区尺寸与镜像一致（核桃派 golden = 143MB FAT + 2440MB ext4）
2. 引导分区能挂上并看到启动文件：`Image`（内核）、`boot.scr`、`sun50i-h616-walnutpi-1b.dtb`、`overlays/`
3. 拔卡前：`Write-VolumeCache` → `fsutil volume dismount X:` → `Set-Disk -IsOffline $true`，看到 `No Media` 再拔

## 五、常见误区（已废弃，别再走）

- **不要用 Rufus 那套流程**：旧 `tools/flash.ps1` + `rufus-4.15p.exe` 已删。Rufus 只适合 ISO/简单场景，
  且我们这套「先数据区后 MBR」的顺序已被实测证明更稳。
- **不要用 `flash_sd[1-5].ps1` 系列**：旧版先写 MBR 再写数据，必然在分区边界失败；
  且用管道调 diskpart，在中文系统下全部空转。已全部删除。
- **AiBurn 不能烧这类镜像**：AiBurn 是匠芯创（ArtInChip）专用工具，只认 `AIC.FW` 头。
  核桃派 1B 是全志 **H616** 方案，golden/官方 img 都是**裸盘镜像**（`upgcmd -i` 会报
  `not supportted image file`）。D12P 那张 `*_nor.img` 是 SPI NOR 固件、走 USB 烧芯片，与 SD 卡无关。
- **卡写速 ~3MB/s 是正常的**（读 ~19MB/s）：2.7GB 镜像写一次约 15 分钟，别以为卡坏了。

## 六、镜像来源

| 文件 | 说明 |
|---|---|
| `D:\ZcodeRely\projects\walnutpi\walnutpi-1b-golden.img` | **默认烧这张**：已配好环境（含 SSH/netplan/依赖）的黄金镜像 |
| `V2.4_07-23_server_WalnutPi-1b_6.1.31_ubuntu22.img` | 官方原版底包（要从零装才用） |
| `walnutpi-1b-netplan.img` | 历史中间版，仅留档 |

烧完首次上电会**自动把根分区扩到整卡**，比平时启动慢，属正常。
