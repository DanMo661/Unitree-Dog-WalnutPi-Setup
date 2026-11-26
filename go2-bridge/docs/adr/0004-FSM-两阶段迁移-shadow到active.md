# ADR-0004: FSM 控制路径两阶段迁移：shadow 对拍 → active 唯一执行

- 状态: 采纳（active 已于 2026-10-31 转正，默认值）
- 日期: 2026-10-31
- 关联: config.py `FSM_MODE` / `_DENY_KEYS`、test_fsm_table.py、test_fsm_mode.py、journal 黑盒

## 背景

旧 rc_loop/Dog 控制路径与 DogFsm 状态机并存，需要替换且不能赌一次切换。
shadow 阶段曾因 91111d1 漏改 core/fsm.py 出现假 StandUp 分歧（见 ADR-0003），归因后修复，
真机 34 分钟零分歧后才切 active。

## 决策

两阶段迁移：

1. **shadow**：旧路径执行，DogFsm 并行跑但只对拍，分歧写 journal（kind=fsm_divergence，
   落盘走 flush；注意它**不在 critical fsync 白名单**——只有 kind=event 才 fsync。影子随旧路径
   删除时该记录器一并退役）。
2. **active**（默认）：DogFsm 是唯一指令来源，旧 rc_loop/Dog 停用**但不删**，保留至
   active 真机稳定 1-2 周，届时随轨道二拆分一并删除（-600 行）。

`config.FSM_MODE` 是唯一开关；`_DENY_KEYS` 禁止 config_local 覆盖它（板上不得热切执行路径）。

## 理由

- 控制路径替换是全项目最高风险变更，对拍先行让"分歧"在不动真机的情况下暴露。
- 分歧归因的证据链只有 journal 黑盒可信（异步落盘、重启不丢）；`/logs` 是 600 行内存环，
  重启即失，只作现场参考，不作为分歧证据。
- 切换回退 = 改 config.py 的 FSM_MODE 重新部署（不是板上热切），宁可慢也要走部署门。

## 权衡

双路径并存期 bridge.py 多 ~600 行、阅读负担明显；换取的是"一行改回 shadow 重部署"的回退能力。
删除旧路径后 FSM_MODE 配置项与 test_fsm_mode 的 shadow 分支同步退役。

## 后果

删旧路径（FsmShadow/旧 Dog/rc_loop）前置条件：真机回归清单全绿 + 用户点头；
删完后本 ADR 补"已删除"批注，不删档。

**后续记录（2026-10-31 深夜）**：用户拍板把删除提前到真机回归之前（原门槛=active 真机稳定
1-2 周）。旧路径已随拆分分支删除（提交 f80be4f：Dog/FsmShadow/_RecLink/_RecRelay/_collapse/
_shadow_safe/_SHADOW/_fsm_mode/旧 rc_loop 体，bridge.py 2473→459 行），8 套仿真测试同步
重定向到 active 路径、shadow 用例退役；`FSM_MODE`/`FSM_SHADOW` 配置条目暂留、无读取方。
回退锚点：合并前 main 打 `pre-split-20261031` tag，可整体回退。删除后的首道真机门 =
次日 `docs/REGRESSION-CHECKLIST.md` 12 场景全跑。
