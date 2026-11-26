# GO2-BRIDGE 工程化改造执行文档

> **文档版本**: v1.0  
> **创建日期**: 2026-10-31  
> **目标**: 将 go2-bridge 从单文件原型改造为大公司级别的工程化项目  
> **适用场景**: 多 agent 并行协作开发

> **执行状态（2026-10-31 深夜刷新，HEAD `b2fd0c5`）**：Phase 1 已完成——旧 Dog 路径删除
> （`f80be4f`，合并 `9a6deae`）+ bridge.py 平铺拆分（stats/telemetry/link/webserver 四模块，
> 2473→454 行）。本文原设想的 `go2_bridge/` 包结构未采用，落地为**平铺模块**（与现役
> 部署/测试/发布链兼容优先）；规范/契约以 CLAUDE.md（契约兼容三规则 + SAFETY-CRITICAL 表）
> + docs/adr/（0001~0007）+ docs/templates/ 承担，依赖边界由 `tools/check_deps.py` +
> test_deps.py 守门。各条目逐项勾销见下文 ✅ 标注；未完成项保留并注明现状。

---

## 文档结构

- **第一部分**: 改造路线图（Roadmap）— 分阶段执行计划
- **第二部分**: 任务清单（Task Breakdown）— 可分配给 agent 的具体任务
- **第三部分**: 规范手册（Engineering Handbook）— 编码、流程、文档、测试规范
- **第四部分**: 验收标准（Acceptance Criteria）— 质量门禁

---

# 第一部分：改造路线图（Roadmap）

## 总览

```
Phase 0: 基础设施准备（1-2 天，串行）
   ↓
Phase 1: 模块化拆分 + 技术债清理（3-5 天，串行）
   ↓
Phase 2: 契约化 + 测试加固（并行 3 条线）
   ├── Track A: 接口契约化（2-3 天）
   ├── Track B: 前端工程化（2-3 天）
   └── Track C: 测试体系完善（3-4 天）
   ↓
Phase 3: 工程化工具链（2-3 天，部分并行）
   ├── CI/CD 流水线
   ├── 代码质量门禁
   └── 文档自动化
   ↓
Phase 4: 持续优化（长期）
   └── 性能监控、技术债追踪、迭代优化
```

---

## Phase 0: 基础设施准备（前置阶段）

**目标**: 建立工程化基础，后续所有改造依赖这个阶段的产出

> **现状（2026-10-31 深夜）**：未按原清单建独立规范文档；规范以**项目既有文档体系**承担——
> 约定/红线/契约三规则/SAFETY-CRITICAL 表在 CLAUDE.md，决策记录在 docs/adr/（0000 模板 +
> 0001~0007），任务模板在 docs/templates/AGENT-TASK-BRIEF.md。PROJECT-STRUCTURE.md /
> GIT-WORKFLOW.md / CODE-REVIEW.md / black·ruff 配置均未建（单项小仓+主控合并模式下暂无必要）。

### 任务清单
1. **建立项目结构规范** → 输出 `PROJECT-STRUCTURE.md`（未建；结构现状见 ARCHITECTURE.md §3）
2. **定义 Git 分支策略** → 输出 `GIT-WORKFLOW.md`（未建；实际习惯=main + feature/*，主控合并）
3. **建立 Code Review 流程** → 输出 `CODE-REVIEW.md`（未建；nitpick 纪律在 CLAUDE.md/全局约定）
4. **创建文档模板库** → 输出 `docs/templates/` 目录（✅ 已建：`docs/templates/AGENT-TASK-BRIEF.md`）
5. **配置代码格式化工具** → 输出 `.editorconfig` + `pyproject.toml`（未做）

### 验收标准
- [ ] 所有规范文档已创建且经过主控审核（部分：由 CLAUDE.md + docs/adr/ 承担，独立规范文档未建）
- [ ] 项目根目录有 `ENGINEERING.md`（规范手册总入口）（未建；总入口=CLAUDE.md 文档地图节）
- [ ] Git 仓库已创建 `main`/`develop`/`feature/*` 分支保护规则（未做；实际只用 main + feature/*）
- [ ] 代码格式化工具（black/ruff）已配置并能本地运行（未做）

### 并行度
❌ 串行执行（这是地基，必须先做）

---

## Phase 1: 模块化拆分 + 技术债清理

**目标**: 解决单文件热点问题，清理历史遗留代码

### 核心原则
- **零行为变更**: 拆分过程中功能完全一致
- **测试先行**: 每拆一个模块，立刻跑全量测试
- **边删边拆**: 同时删除旧 Dog 路径（-600 行）

### 1.1 删除旧 Dog 路径（Day 1）✅ 已完成（2026-10-31 深夜）

**任务**: 删除 bridge.py 中已退役的 shadow 模式代码

**具体操作**:
```python
# 删除目标
- Dog 类（旧控制路径）
- rc_loop 中的直控分支
- shadow 模式相关代码
- 已注释的残留代码
```

**落地记录**: 提交 `f80be4f`（Step E+F），随 `feature/split-modules` 分支合并 `9a6deae`；
实际删除 Dog/FsmShadow/_RecLink/_RecRelay/_collapse/_shadow_safe/_SHADOW/_fsm_mode/
旧 rc_loop 体。**提前触发**：原计划"active 稳定 1~2 周后"，用户拍板提前（ADR-0004 后续记录）；
回退锚点 tag `pre-split-20261031`；删后首道真机门 = `docs/REGRESSION-CHECKLIST.md` 12 场景。

**验收标准**:
- [x] 删除约 600 行代码（含拆分整体 bridge.py 2473→454 行）
- [x] 全量测试通过（17 套，test_pa_v3 板端除外；8 套仿真测试同步重定向 active，shadow 用例退役）
- [ ] 真机冒烟测试通过（部署后跑 10 分钟）（未跑——按 ADR-0004 后续记录，首道门改为次日回归清单 12 场景全跑）
- [x] 提交完成（提交信息为项目中文风格，非本条原文案：`Step E+F: 删旧控制路径(ADR-0004)...`）

### 1.2 模块化拆分（Day 2-3）✅ 已完成（2026-10-31 深夜，落地为平铺方案）

**拆分目标结构**:
```
go2_bridge/
├── __init__.py
├── main.py              # 入口 + FastAPI app 组装
├── config.py            # 配置管理（原 config 模块）
├── core/
│   ├── __init__.py
│   ├── fsm.py           # 已存在，保持不变
│   ├── state.py         # 全局状态管理（从 bridge.py 提取）
│   └── types.py         # 数据类型定义（NamedTuple/dataclass）
├── hardware/
│   ├── __init__.py
│   ├── sbus.py          # 已存在，可能需要重构
│   ├── netrc.py         # 已存在
│   └── relay.py         # GPIO/PWM 控制（如果有）
├── dog/
│   ├── __init__.py
│   ├── link.py          # DogLink（WebRTC 会话管理）
│   └── telemetry.py     # 遥测数据订阅 + state 镜像
├── web/
│   ├── __init__.py
│   ├── routes.py        # FastAPI 路由定义
│   ├── websocket.py     # WebSocket 处理
│   └── static/          # 前端资源（已存在）
├── audio/
│   ├── __init__.py
│   └── gateway.py       # :15011 动作网关
├── security/
│   ├── __init__.py
│   └── languard.py      # 已存在，移入此处
└── utils/
    ├── __init__.py
    ├── journal.py       # 已存在
    └── stats.py         # SbusStats 提取
```

**拆分顺序**（串行，每步验证）:

> **落地口径（2026-10-31 深夜）**：上面包结构未采用，实际落地为**平铺四模块**
> `stats.py` / `telemetry.py` / `link.py` / `webserver.py` + `core/fsm.py`（既有），
> 入口仍叫 `bridge.py`（454 行）——平铺与现役部署清单/测试导入/发布链兼容，不值得为包结构
> 动全链。各步对应关系见下。

1. **Step 1: 提取 DogLink** → `dog/link.py` ✅ → 实际 `link.py`（647 行，提交 `ca2dcd6`）
   - 从 bridge.py 提取 WebRTC 会话管理逻辑
   - 保留原有接口（submit、订阅回调）
   - 测试: `test_link_sim.py` 通过（含 unitree 库导入与狗地址解析一并随迁）

2. **Step 2: 提取遥测模块** → `dog/telemetry.py` ✅ → 实际 `telemetry.py`（125 行，提交 `227734a`）
   - 提取订阅逻辑和 state 镜像更新（State/state 单例 + log/_LOG_BUF + _ev/_EVENTS 等共享设施）
   - 测试: `test_reconnect_sim.py` + `test_heartbeat_sim.py` 通过

3. **Step 3: 提取 Web 路由** → `web/routes.py` + `web/websocket.py` ✅ → 实际 `webserver.py`
   （300 行，提交 `4e179da`；路由与 WS 同文件未再拆，规模不支撑两文件）
   - 提取所有 FastAPI 路由定义
   - 测试: 手动测试网页端能正常访问

4. **Step 4: 提取全局状态** → `core/state.py` ✅ → 实际随 `telemetry.py`（State 类 + state 单例）
   - ~~建立明确的状态类（用 dataclass + 属性保护）、废弃 GIL 原子性假设改用 `threading.Lock`~~
     **未按此做**：ADR-0001 定案维持"单写者 + GIL 原子单属性赋值，不上锁"；
     唯一写者逐字段登记在 ARCHITECTURE.md §6 状态分布表（ADR-0001 的登记义务）
   - 测试: 所有访问 state 的代码路径（17 套全量）

5. **Step 5: 提取 SbusStats** → `utils/stats.py` ✅ → 实际 `stats.py`（93 行，含 sbus_stats 单例）
   - 独立统计模块
   - 测试: `test_sbus.py` 通过

6. **Step 6: 重构 main.py** ✅ → 入口仍为 `bridge.py`（454 行）
   - rc_loop 保留但只做装配+调度（`_rc_loop_active`；`rc_loop` 为薄委托保签名兼容）
   - 所有业务逻辑在 `core/fsm.py`（DogFsm），执行器 `FsmActive` 也在 bridge.py（含输出落地）

**每步验收标准**:
- [x] 测试全部通过（每拆一块跑全量 17 套）
- [ ] 代码已格式化（black）（black 未配置——项目无格式化门，见 Phase 0）
- [x] 已添加模块级文档（各模块头 docstring 承担 + ARCHITECTURE.md §3 模块清单；独立 README 未建）
- [x] 提交完成（项目中文提交信息，各步独立提交可回溯）

### 1.3 清理变体代码（Day 4）

> **现状（2026-10-31 深夜）**：未做。`_deploy_dog2` 变体去留待用户拍板（进 TODO H）；
> 继电器差异已由双路并出收敛（2026-10-24），剩余差异只有 `config_local.py` 的 `PA_STEREO`。

**任务**: 收敛 `_deploy_dog2` 变体

**操作**:
- 删除重复配置文件
- 变体差异只保留在 `config_local.py`
- 文档化配置差异原因

**验收标准**:
- [ ] 只有一份主代码 + config_local 差异
- [ ] 文档说明 dog2 变体的配置项

---

## Phase 2: 契约化 + 测试加固（并行 3 条线）

**目标**: 建立明确的模块契约，完善测试体系

### Track A: 接口契约化（可分配给 Agent A）

> **现状（2026-10-31 深夜）**：契约保护以**项目化形式**先行落地——契约兼容三规则
> （只增可选字段 / 废弃字段保留占位 / 破坏性变更=App 同步升版）写入 CLAUDE.md，
> RC 应答契约成文 ADR-0007，发布集校验 test_release_files.py。下述 JSON Schema /
> 契约测试 / pre-commit hook 均未做（保留原任务，机器可执行契约仍缺）。

**任务清单**:

#### A1: 定义 API Schema（Day 1）
- 为 `/state` 接口编写 JSON Schema
- 为 WebSocket 消息编写 Schema
- 为 RC 应答格式编写 Schema
- 输出: `schemas/api.json`

#### A2: 编写契约测试（Day 2）
- 使用 `jsonschema` 库验证 Schema
- 测试用例: `test_contract_api.py`
- 验证所有现有接口符合 Schema

#### A3: 建立契约保护机制（Day 3）
- 添加 pre-commit hook: 检查 Schema 未变更
- 如果 Schema 变更，必须同时更新版本号
- 输出: `.pre-commit-config.yaml`

**验收标准**:
- [ ] 所有 Schema 文件已创建
- [ ] 契约测试全部通过
- [ ] pre-commit hook 能拦截非法变更
- [ ] 文档: `docs/API-CONTRACT.md`

---

### Track B: 前端工程化（可分配给 Agent B）

**任务清单**:

#### B1: 前端代码规范化（Day 1）
- HTML/CSS/JS 代码格式化（Prettier）
- 添加 ESLint 检查
- 模块化 JS 代码（如果现在是单文件）

#### B2: 前端文档化（Day 2）
- 为调试台、遥控页编写使用文档
- 为 WebSocket 协议编写前端视角文档
- 输出: `docs/FRONTEND.md`

#### B3: 前端测试（Day 3）
- 截图验证（记录正常状态截图）
- 手动测试检查清单
- 输出: `docs/FRONTEND-TEST-CHECKLIST.md`

**验收标准**:
- [ ] 前端代码已格式化
- [ ] 文档已创建
- [ ] 测试清单可执行

---

### Track C: 测试体系完善（可分配给 Agent C）

**任务清单**:

#### C1: 补充集成测试（Day 1-2）
- 测试 sbus → FSM → submit 完整链路
- 测试 netrc → FSM → submit 完整链路
- 测试 web → FSM → submit 完整链路
- 输出: `test_integration_*.py`

#### C2: 性能基准测试（Day 2-3）
- 测试 rc_loop 延迟（目标 <10ms）
- 测试 WebSocket 推送延迟
- 建立性能基线（Baseline）
- 输出: `test_performance.py` + `benchmarks/baseline.json`

#### C3: 故障注入测试（Day 3-4）
- 模拟网络抖动（netrc 延迟、丢包）
- 模拟设备掉线（DogLink 断开）
- 模拟并发冲突（多输入源竞争）
- 输出: `test_fault_injection.py`

**验收标准**:
- [ ] 新增测试全部通过
- [ ] 性能基线已记录
- [ ] 测试覆盖率 >80%（用 `pytest-cov` 统计）

---

## Phase 3: 工程化工具链

**目标**: 建立自动化质量保障体系

> **现状（2026-10-31 深夜）**：CI/CD、black/ruff/mypy、文档自动化均未做。
> 依赖边界门禁已按项目形态先行落地：`core/` 自包含（唯一豁免 config）+ `languard.py`
> 纯 stdlib，`tools/check_deps.py` 守门 + `test_deps.py` 固化（对应本阶段"代码质量门禁"
> 的依赖检查部分，无第三方工具链）。

### 3.1 CI/CD 流水线（Day 1-2）

**任务**: 配置 GitHub Actions / GitLab CI

**Pipeline 定义**:
```yaml
stages:
  - lint      # 代码检查（ruff/black）
  - test      # 运行测试（pytest）
  - contract  # 契约测试
  - build     # 打包部署脚本
  - deploy    # 部署到板子（手动触发）
```

**验收标准**:
- [ ] CI 配置文件已创建（`.github/workflows/ci.yml`）
- [ ] 每次提交自动触发 CI
- [ ] 测试失败会阻止合并

### 3.2 代码质量门禁（Day 2）

**工具选型**:
- **格式化**: black（行长 100）
- **Lint**: ruff（替代 flake8 + isort）
- **类型检查**: mypy（逐步启用）
- **复杂度检查**: radon（圈复杂度 <10）

**配置文件**: `pyproject.toml`
```toml
[tool.black]
line-length = 100
target-version = ['py39']

[tool.ruff]
line-length = 100
select = ["E", "F", "W", "I", "N"]
ignore = ["E501"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "--cov=go2_bridge --cov-report=html --cov-fail-under=80"
```

**验收标准**:
- [ ] 所有工具已配置
- [ ] 现有代码通过所有检查
- [ ] pre-commit hook 自动运行检查

### 3.3 文档自动化（Day 3）

**任务**:
- 自动生成 API 文档（用 FastAPI 自带的 OpenAPI）
- 自动生成模块依赖图（用 `pydeps`）
- 自动生成测试覆盖率报告

**验收标准**:
- [ ] `/docs` 端点可访问 OpenAPI 文档
- [ ] CI 自动生成并上传覆盖率报告
- [ ] 模块依赖图已生成（`docs/dependencies.svg`）

---

## Phase 4: 持续优化（长期）

### 4.1 性能监控
- 在真机环境记录 rc_loop 延迟、WebRTC 延迟
- 每周检查性能是否退化

### 4.2 技术债追踪
- 建立 `TECH-DEBT.md` 文件
- 记录已知问题、临时方案、待优化项
- 每个 Sprint 分配 20% 时间还技术债

### 4.3 迭代优化
- 定期回顾架构（每月）
- 根据实际使用情况调整模块边界
- 记录架构决策（Architecture Decision Records, ADR）

---

# 第二部分：任务清单（Task Breakdown）

## 任务模板

每个任务使用以下格式：

```markdown
## 任务: [任务名称]

**ID**: TASK-XXX
**负责人**: Agent A / Agent B / 主控
**依赖**: TASK-YYY（必须先完成）
**预计时间**: X 小时
**并行度**: 可并行 / 必须串行

### 目标
[一句话说明做什么]

### 输入
- 文件: xxx.py
- 文档: xxx.md
- 配置: xxx.json

### 操作步骤
1. 第一步
2. 第二步
3. ...

### 产出
- [ ] 文件: `path/to/file.py`
- [ ] 文档: `docs/xxx.md`
- [ ] 测试: `test_xxx.py`（X 个用例全部通过）

### 验收标准
- [ ] 功能验收: xxx
- [ ] 测试验收: xxx
- [ ] 代码质量: black + ruff 通过
- [ ] 文档验收: 已添加模块文档

### 回滚方案
如果出问题怎么回退
```

---

## Phase 0 任务清单

### TASK-001: 建立项目结构规范

**负责人**: 主控  
**依赖**: 无  
**预计时间**: 2 小时

**目标**: 定义标准的项目目录结构

**产出**:
- [ ] `PROJECT-STRUCTURE.md`（说明每个目录的职责）
- [ ] `go2_bridge/` 目录结构（空文件 + `__init__.py`）

**验收标准**:
- [ ] 目录结构清晰，符合 Python 包规范
- [ ] 文档说明了"什么代码放哪个目录"

---

### TASK-002: 定义 Git 分支策略

**负责人**: 主控  
**依赖**: 无  
**预计时间**: 1 小时

**目标**: 建立 Git 工作流

**产出**:
- [ ] `GIT-WORKFLOW.md`

**内容要求**:
```markdown
# Git 工作流

## 分支策略
- `main`: 生产分支，只接受来自 develop 的合并
- `develop`: 开发分支，日常开发的基线
- `feature/*`: 功能分支，从 develop 拉出，完成后合并回 develop
- `hotfix/*`: 紧急修复，从 main 拉出，修复后合并回 main 和 develop

## Commit 规范
格式: `<type>(<scope>): <subject>`

类型:
- feat: 新功能
- fix: Bug 修复
- refactor: 重构
- docs: 文档
- test: 测试
- chore: 构建/工具

示例: `feat(fsm): add emergency stop state`

## PR 规范
- 标题简洁明确
- 描述包含: 背景、变更、测试、影响范围
- 必须通过 CI
- 必须至少 1 人 review
```

---

### TASK-003: 建立 Code Review 流程

**负责人**: 主控  
**依赖**: TASK-002  
**预计时间**: 1 小时

**目标**: 定义代码审查标准

**产出**:
- [ ] `CODE-REVIEW.md`

**内容要求**:
- Review 检查清单（功能、测试、文档、代码质量）
- Nitpick 纪律（挑刺者不修，由作者修）
- 审查时效要求（24 小时内响应）

---

### TASK-004: 创建文档模板库

> **现状（2026-10-31）**：部分完成——已建 `docs/templates/AGENT-TASK-BRIEF.md`
> （agent 任务书模板，实际任务派发在用）；下列三个具名模板未建。

**负责人**: Agent A  
**依赖**: 无  
**预计时间**: 2 小时

**目标**: 建立统一的文档模板

**产出**:
- [ ] `docs/templates/MODULE-README.md`（模块文档模板）
- [ ] `docs/templates/API-DOC.md`（接口文档模板）
- [ ] `docs/templates/CHANGELOG.md`（变更日志模板）

---

### TASK-005: 配置代码格式化工具

**负责人**: Agent B  
**依赖**: 无  
**预计时间**: 1 小时

**目标**: 统一代码风格

**操作步骤**:
1. 安装工具: `pip install black ruff`
2. 创建配置文件: `pyproject.toml`
3. 格式化现有代码: `black .`
4. 验证: `ruff check .`

**产出**:
- [ ] `pyproject.toml`（工具配置）
- [ ] `.editorconfig`（编辑器配置）
- [ ] 所有 Python 文件已格式化

---

## Phase 1 任务清单

### TASK-101: 删除旧 Dog 路径

> ✅ **已完成（2026-10-31 深夜）**：提交 `f80be4f`，随 `feature/split-modules` 合并
> `9a6deae`（分支名与原设想的 `feature/remove-legacy-dog` 不同）。删除对象见 1.1 落地记录；
> 真机 10 分钟未跑，首道门改为次日 `docs/REGRESSION-CHECKLIST.md` 12 场景（ADR-0004 后续记录）。

**负责人**: 主控  
**依赖**: Phase 0 完成  
**预计时间**: 4 小时

**目标**: 删除已退役的 shadow 模式代码（-600 行）

**操作步骤**:
1. 在 `develop` 分支创建 `feature/remove-legacy-dog`
2. 删除 `bridge.py` 中的 Dog 类
3. 删除 rc_loop 中的直控分支
4. 删除所有 `if config.FSM_MODE == "shadow"` 分支
5. 运行全量测试: `python run_test_shim.py`
6. 部署到板子并真机测试 10 分钟

**验收标准**:
- [ ] 删除约 600 行代码
- [ ] 测试全部通过
- [ ] 真机冒烟测试通过
- [ ] Commit: `refactor: remove legacy Dog class and shadow mode`

**回滚方案**:
```bash
git revert <commit-hash>
python deploy_pc.py  # 回滚部署
```

---

### TASK-102: 提取 DogLink 模块

> ✅ **已完成（2026-10-31 深夜）**：实际落地 `link.py`（647 行，平铺非 `go2_bridge/dog/`），
> 提交 `ca2dcd6`。类型注解与独立 README 未加（模块头 docstring 承担文档）。

**负责人**: Agent A  
**依赖**: TASK-101  
**预计时间**: 3 小时

**目标**: 将 WebRTC 会话管理独立成模块

**输入**:
- `bridge.py` 中的 DogLink 类

**操作步骤**:
1. 创建 `go2_bridge/dog/link.py`
2. 移动 DogLink 类定义
3. 保留原有接口: `submit()`, `subscribe()`, `connect()`, `disconnect()`
4. 添加类型注解（用 `typing` 模块）
5. 编写模块文档（见模板）
6. 修改 `main.py` 中的导入
7. 运行测试: `pytest test_link.py test_reconnect.py`

**产出**:
- [ ] `go2_bridge/dog/link.py`（约 200 行）
- [ ] `go2_bridge/dog/__init__.py`
- [ ] `go2_bridge/dog/README.md`（模块文档）

**验收标准**:
- [ ] 相关测试通过
- [ ] 代码已格式化
- [ ] 已添加类型注解
- [ ] 文档说明了接口用法

---

### TASK-103: 提取遥测模块

> ✅ **已完成（2026-10-31 深夜）**：实际落地 `telemetry.py`（125 行，提交 `227734a`），
> State/state 单例、log、事件环、狗姿态有效性等共享设施一并随迁。

**负责人**: Agent A  
**依赖**: TASK-102  
**预计时间**: 2 小时

**目标**: 将遥测订阅逻辑独立

**产出**:
- [ ] `go2_bridge/dog/telemetry.py`
- [ ] 测试通过: `test_heartbeat.py`

---

### TASK-104: 提取 Web 路由

> ✅ **已完成（2026-10-31 深夜）**：实际落地 `webserver.py`（300 行，提交 `4e179da`）；
> HTTP 路由与 WS 处理同文件，未拆 routes/websocket 两文件（规模不支撑）。

**负责人**: Agent B  
**依赖**: TASK-101  
**预计时间**: 3 小时

**目标**: 将 FastAPI 路由独立成模块

**产出**:
- [ ] `go2_bridge/web/routes.py`（HTTP 路由）
- [ ] `go2_bridge/web/websocket.py`（WebSocket 处理）
- [ ] `go2_bridge/web/README.md`

**验收标准**:
- [ ] 网页端能正常访问（手动测试）
- [ ] WebSocket 推送正常

---

### TASK-105: 提取全局状态模块

> ✅ **已完成（2026-10-31 深夜，按 ADR-0001 口径变体落地）**：State 落在 `telemetry.py`
> （非 core/state.py）；**未**上 `threading.Lock`/dataclass——ADR-0001 定案"单写者 +
> GIL 原子单属性赋值，不上锁"，唯一写者已逐字段登记 ARCHITECTURE.md §6
> （web_armed 双写点 / last_cmd 跨线程 dict 两处待登记项已列 TODO H）。

**负责人**: Agent C  
**依赖**: TASK-103, TASK-104  
**预计时间**: 4 小时

**目标**: 建立线程安全的状态管理

**重点**:
- 废弃 GIL 原子性假设
- 使用 `threading.Lock` 保护状态
- 提供 getter/setter 接口

**产出**:
- [ ] `go2_bridge/core/state.py`
- [ ] 状态类使用 `dataclass`
- [ ] 所有访问点已改用接口

**验收标准**:
- [ ] 线程安全测试通过
- [ ] 文档说明了并发安全性

---

### TASK-106: 重构 main.py

> ✅ **已完成（2026-10-31 深夜，入口仍名 bridge.py）**：454 行（非 <200 行目标——
> FsmActive 执行器与 TRICK 口留在其中）；rc_loop 只做装配+调度，业务逻辑全在 core/fsm.py。

**负责人**: 主控  
**依赖**: TASK-102~105 全部完成  
**预计时间**: 2 小时

**目标**: main.py 只保留组装和调度逻辑

**验收标准**:
- [ ] main.py <200 行
- [ ] rc_loop 只做调度，不含业务逻辑
- [ ] 全量测试通过

---

## Phase 2 任务清单

### Track A: 接口契约化

#### TASK-201: 定义 API Schema

**负责人**: Agent A  
**依赖**: Phase 1 完成  
**预计时间**: 3 小时

**目标**: 为所有接口编写 JSON Schema

**产出**:
- [ ] `schemas/state_response.json`（/state 接口）
- [ ] `schemas/websocket_message.json`（WebSocket 消息）
- [ ] `schemas/rc_response.json`（RC 应答格式）

**示例**:
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "State Response",
  "type": "object",
  "required": ["dog", "rc", "audio"],
  "properties": {
    "dog": {
      "type": "object",
      "required": ["armed", "mode", "height"],
      "properties": {
        "armed": {"type": "boolean"},
        "mode": {"type": "integer", "minimum": 0, "maximum": 10},
        "height": {"type": "number"}
      }
    }
  }
}
```

**验收标准**:
- [ ] 所有接口都有 Schema
- [ ] Schema 与实际代码一致
- [ ] 文档: `docs/API-CONTRACT.md`

---

#### TASK-202: 编写契约测试

**负责人**: Agent A  
**依赖**: TASK-201  
**预计时间**: 2 小时

**目标**: 自动验证接口符合 Schema

**产出**:
- [ ] `test_contract_api.py`

**测试内容**:
```python
def test_state_response_schema():
    """验证 /state 返回符合 Schema"""
    response = client.get("/state")
    validate(instance=response.json(), schema=STATE_SCHEMA)

def test_websocket_message_schema():
    """验证 WebSocket 消息符合 Schema"""
    with client.websocket_connect("/ws") as ws:
        data = ws.receive_json()
        validate(instance=data, schema=WS_MESSAGE_SCHEMA)
```

**验收标准**:
- [ ] 所有契约测试通过
- [ ] 测试覆盖所有接口

---

#### TASK-203: 建立契约保护机制

**负责人**: 主控  
**依赖**: TASK-202  
**预计时间**: 2 小时

**目标**: 防止 Schema 被意外破坏

**产出**:
- [ ] `.pre-commit-config.yaml`（pre-commit hook）
- [ ] CI 中的 Schema 检查

**Hook 规则**:
- 如果 `schemas/` 目录有变更，必须同时更新 `CHANGELOG.md`
- Schema 变更必须递增版本号

---

### Track B: 前端工程化

#### TASK-211: 前端代码规范化

**负责人**: Agent B  
**依赖**: Phase 1 完成  
**预计时间**: 2 小时

**目标**: 统一前端代码风格

**操作步骤**:
1. 安装 Prettier: `npm install --save-dev prettier`
2. 创建配置: `.prettierrc.json`
3. 格式化: `npx prettier --write static/**/*.{html,css,js}`
4. 配置 ESLint（如果有 JS 模块）

**产出**:
- [ ] `.prettierrc.json`
- [ ] 所有前端文件已格式化

---

#### TASK-212: 前端文档化

**负责人**: Agent B  
**依赖**: TASK-211  
**预计时间**: 3 小时

**目标**: 编写前端使用文档

**产出**:
- [ ] `docs/FRONTEND.md`

**内容要求**:
- 调试台功能说明（5 页 SPA 各自的作用）
- 遥控页使用说明
- WebSocket 协议说明（前端视角）
- 常见问题排查

---

#### TASK-213: 前端测试清单

**负责人**: Agent B  
**依赖**: TASK-212  
**预计时间**: 2 小时

**目标**: 建立前端验收标准

**产出**:
- [ ] `docs/FRONTEND-TEST-CHECKLIST.md`

**内容**:
```markdown
## 调试台测试清单
- [ ] 仪表盘页面加载正常
- [ ] 实时数据推送（0.5s 刷新）
- [ ] GO2 信息卡显示正确
- [ ] 低电量警告条在 SOC <20% 时显示
- [ ] ...

## 遥控页测试清单
- [ ] 双摇杆响应正常
- [ ] 解锁/急停按钮生效
- [ ] WebSocket 连接正常
- [ ] ...
```

---

### Track C: 测试体系完善

#### TASK-221: 补充集成测试

**负责人**: Agent C  
**依赖**: Phase 1 完成  
**预计时间**: 4 小时

**目标**: 测试完整的数据链路

**产出**:
- [ ] `test_integration_sbus_to_dog.py`
- [ ] `test_integration_netrc_to_dog.py`
- [ ] `test_integration_web_to_dog.py`

**测试场景**:
```python
def test_sbus_to_dog_full_chain():
    """测试: SBUS 输入 → FSM → submit → DogLink"""
    # 1. 模拟 SBUS 数据
    # 2. 触发 rc_loop
    # 3. 验证 FSM 状态转移
    # 4. 验证 submit 被调用
```

---

#### TASK-222: 性能基准测试

**负责人**: Agent C  
**依赖**: TASK-221  
**预计时间**: 3 小时

**目标**: 建立性能基线

**产出**:
- [ ] `test_performance.py`
- [ ] `benchmarks/baseline.json`

**测试指标**:
- rc_loop 延迟（目标 <10ms）
- WebSocket 推送延迟
- 状态查询响应时间

**Baseline 记录**:
```json
{
  "date": "2026-10-31",
  "metrics": {
    "rc_loop_latency_ms": 5.2,
    "ws_push_latency_ms": 12.8,
    "state_query_latency_ms": 2.1
  }
}
```

---

#### TASK-223: 故障注入测试

**负责人**: Agent C  
**依赖**: TASK-222  
**预计时间**: 4 小时

**目标**: 验证系统在异常情况下的鲁棒性

**产出**:
- [ ] `test_fault_injection.py`

**测试场景**:
- 网络延迟 500ms（模拟 netrc 慢速）
- 丢包 20%（模拟 SBUS 不稳定）
- DogLink 断开重连
- 并发冲突（两个输入源同时激活）

---

## Phase 3 任务清单

### TASK-301: 配置 CI/CD 流水线

**负责人**: 主控  
**依赖**: Phase 2 完成  
**预计时间**: 4 小时

**目标**: 自动化测试和部署

**产出**:
- [ ] `.github/workflows/ci.yml`

**Pipeline 定义**:
```yaml
name: CI

on: [push, pull_request]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - run: pip install black ruff
      - run: black --check .
      - run: ruff check .

  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - run: pip install -r requirements.txt
      - run: pytest --cov --cov-fail-under=80

  contract:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - run: pytest test_contract_*.py
```

---

### TASK-302: 配置代码质量门禁

**负责人**: 主控  
**依赖**: TASK-301  
**预计时间**: 2 小时

**目标**: 自动化代码质量检查

**产出**:
- [ ] `.pre-commit-config.yaml`

**Hook 列表**:
- black（格式化）
- ruff（Lint）
- mypy（类型检查，逐步启用）
- 契约检查（自定义 hook）

---

### TASK-303: 文档自动化

**负责人**: Agent A  
**依赖**: TASK-301  
**预计时间**: 3 小时

**目标**: 自动生成文档

**产出**:
- [ ] OpenAPI 文档自动生成（FastAPI 内置）
- [ ] 模块依赖图: `docs/dependencies.svg`（用 pydeps）
- [ ] 测试覆盖率报告: `htmlcov/index.html`

**验收标准**:
- [ ] 访问 `http://板子IP:8000/docs` 可查看 API 文档
- [ ] CI 自动上传覆盖率报告

---

# 第三部分：规范手册（Engineering Handbook）

## 3.1 代码规范

### Python 代码风格

**基础规范**: PEP 8

**工具强制**:
- **格式化**: black (行长 100)
- **Lint**: ruff
- **类型检查**: mypy (逐步启用)

**命名规范**:
```python
# 模块名: 小写下划线
dog_link.py

# 类名: 大驼峰
class DogFsm:
    pass

# 函数/变量: 小写下划线
def get_current_state():
    arm_status = True

# 常量: 大写下划线
MAX_RETRY_COUNT = 3

# 私有成员: 单下划线前缀
def _internal_helper():
    pass
```

**函数长度**:
- 目标: <50 行
- 超过 100 行必须拆分

**注释规范**:
```python
def submit_command(cmd: dict) -> bool:
    """
    提交控制指令到狗本体
    
    Args:
        cmd: 控制指令字典，必须包含 'type' 和 'data'
    
    Returns:
        True 如果提交成功，False 如果队列满或链接断开
    
    Raises:
        ValueError: 如果 cmd 格式不正确
    """
    pass
```

**模块文档**:

每个模块必须有 `README.md`（用模板）:
```markdown
# 模块名

## 职责
简述这个模块做什么

## 对外接口
- `function_a()`: 功能描述
- `ClassB`: 类描述

## 依赖
- 依赖模块 A（原因）
- 依赖模块 B（原因）

## 使用示例
\`\`\`python
from go2_bridge.dog import link
dog_link = link.DogLink()
\`\`\`

## 测试
相关测试文件: `test_link.py`
```

---

## 3.2 架构约定

### 模块分层

```
展示层 (web/)
    ↓ 只依赖
业务层 (core/, dog/, audio/)
    ↓ 只依赖
硬件层 (hardware/)
    ↓ 只依赖
工具层 (utils/)
```

**依赖规则**:
- 上层可以依赖下层，下层不能依赖上层
- 同层模块尽量解耦，通过接口通信
- 禁止循环依赖

**检查工具**: `pydeps go2_bridge --show-deps`

### 状态管理

**原则**: 单一真相源（Single Source of Truth）

**实现**:
- 全局状态集中在 `core/state.py`
- 其他模块只读状态（getter）
- 只有 FSM 和遥测模块可写状态（setter）
- 所有写操作使用 `threading.Lock` 保护

**禁止**:
- 模块间通过全局变量通信
- 依赖 GIL 原子性

### 错误处理

**原则**: 明确的错误边界

**实现**:
```python
# ✅ 好：明确的异常类型
class DogLinkError(Exception):
    """WebRTC 链接相关错误"""
    pass

def submit_command(cmd):
    if not self.connected:
        raise DogLinkError("链接未建立")

# ❌ 坏：吞异常
try:
    submit_command(cmd)
except:
    pass  # 静默失败，难以调试

# ✅ 好：记录并传播
try:
    submit_command(cmd)
except DogLinkError as e:
    logger.error(f"提交失败: {e}")
    raise
```

---

## 3.3 流程规范

### Git 工作流（详细版）

#### 分支策略

```
main (生产)
  ↑
develop (开发基线)
  ↑
feature/task-xxx (功能分支)
```

#### 开发流程

1. **拉取任务**
   ```bash
   git checkout develop
   git pull origin develop
   git checkout -b feature/task-102-extract-doglink
   ```

2. **本地开发**
   ```bash
   # 开发 + 提交
   git add go2_bridge/dog/link.py
   git commit -m "feat(dog): extract DogLink to separate module"
   
   # 运行测试
   pytest test_link.py
   
   # 代码检查
   black .
   ruff check .
   ```

3. **提交 PR**
   ```bash
   git push origin feature/task-102-extract-doglink
   # 在 GitHub/GitLab 创建 PR: feature/task-102 → develop
   ```

4. **Code Review**
   - 等待 CI 通过
   - 等待至少 1 人 review
   - 根据反馈修改

5. **合并**
   - Squash merge（保持 develop 历史干净）
   - 删除 feature 分支

#### Commit 规范

**格式**: `<type>(<scope>): <subject>`

**类型**:
- `feat`: 新功能
- `fix`: Bug 修复
- `refactor`: 重构（不改变外部行为）
- `docs`: 文档
- `test`: 测试
- `chore`: 构建/工具/依赖

**Scope**: 模块名（dog/web/core/fsm）

**示例**:
```
feat(dog): extract DogLink to separate module
fix(fsm): prevent race condition in state transition
refactor(web): split routes into separate files
docs(api): add contract documentation
test(integration): add sbus-to-dog full chain test
chore(deps): upgrade fastapi to 0.104.0
```

**坏示例**:
```
❌ update code
❌ fix bug
❌ WIP
❌ 修改了一些东西
```

---

### Code Review 检查清单

**功能性**:
- [ ] 功能符合任务目标
- [ ] 没有引入新 bug
- [ ] 边界条件已处理

**测试**:
- [ ] 新代码有对应测试
- [ ] 测试覆盖关键路径
- [ ] 测试全部通过

**代码质量**:
- [ ] 命名清晰易懂
- [ ] 函数职责单一
- [ ] 没有重复代码
- [ ] 复杂逻辑有注释

**文档**:
- [ ] 公开接口有文档字符串
- [ ] 模块有 README
- [ ] 变更已记录到 CHANGELOG

**架构**:
- [ ] 符合模块分层规则
- [ ] 没有循环依赖
- [ ] 接口设计合理

**Nitpick 纪律**:
- Reviewer 只指出问题，不自己修
- 作者根据反馈修改
- 如果有争议，主控裁决

---

## 3.4 文档规范

### 文档层级

```
ENGINEERING.md           # 规范手册总入口（本文档）
├── PROJECT-STRUCTURE.md # 项目结构
├── GIT-WORKFLOW.md      # Git 工作流
├── CODE-REVIEW.md       # Code Review 流程
└── docs/
    ├── API-CONTRACT.md  # 接口契约
    ├── FRONTEND.md      # 前端文档
    ├── ARCHITECTURE.md  # 架构设计（已有）
    └── modules/
        ├── dog.md       # dog 模块文档
        ├── web.md       # web 模块文档
        └── ...
```

### 模块文档模板

见 `docs/templates/MODULE-README.md`:

```markdown
# [模块名]

## 职责
一句话说明这个模块做什么

## 对外接口
### 类
- `ClassName`: 类的职责

### 函数
- `function_name(arg1, arg2) -> ReturnType`: 功能描述

## 依赖
- `module_a`: 为什么依赖
- `module_b`: 为什么依赖

## 配置
如果有配置项，列出来

## 使用示例
\`\`\`python
from go2_bridge.xxx import yyy
\`\`\`

## 测试
相关测试文件: `test_xxx.py`

## 已知问题
列出技术债务或临时方案

## 变更历史
主要变更记录（详细的在 CHANGELOG.md）
```

### API 文档

使用 FastAPI 自带的 OpenAPI 文档:
- 自动生成: 访问 `/docs`
- 手动补充: 在路由函数中添加 docstring

```python
@app.get("/state")
async def get_state():
    """
    获取系统完整状态
    
    返回字段说明见 schemas/state_response.json
    
    响应示例:
    {
      "dog": {"armed": true, "mode": 2},
      "rc": {"source": "sbus", "fresh": true},
      "audio": {"ready": true, "volume": 80}
    }
    """
    return state.to_dict()
```

---

## 3.5 测试规范

### 测试分层

```
test_unit_*.py          # 单元测试（单个函数/类）
test_integration_*.py   # 集成测试（多模块协作）
test_contract_*.py      # 契约测试（接口 Schema）
test_performance.py     # 性能测试
test_fault_injection.py # 故障注入测试
```

### 测试原则

**F.I.R.S.T 原则**:
- **Fast**: 快速运行（单元测试 <1s）
- **Independent**: 测试间独立（可任意顺序运行）
- **Repeatable**: 可重复（结果稳定）
- **Self-validating**: 自验证（不需要人工判断）
- **Timely**: 及时编写（与代码同步）

### 测试命名

```python
# 格式: test_<测试对象>_<场景>_<预期结果>

def test_fsm_unlock_when_armed_should_transition_to_unlock():
    """测试: FSM 在 armed 状态下收到 unlock 指令应转移到 unlock 状态"""
    pass

def test_doglink_submit_when_disconnected_should_raise_error():
    """测试: DogLink 断开时 submit 应抛出异常"""
    pass
```

### 覆盖率要求

**目标**: 80% 以上

**关键路径**: 必须 100% 覆盖
- FSM 状态转移
- 安全链（失控保护、互锁）
- 契约接口

**豁免**: 可不测试的代码
- 简单的 getter/setter
- 日志代码
- 不可达的防御性代码（加注释说明原因）

### Mock 策略

**原则**: 只 mock 外部依赖，不 mock 被测系统

```python
# ✅ 好：mock 外部依赖（WebRTC、硬件）
@patch('go2_bridge.dog.link.WebRTCClient')
def test_doglink_submit(mock_webrtc):
    link = DogLink()
    link.submit({'cmd': 'test'})
    mock_webrtc.send.assert_called_once()

# ❌ 坏：mock 被测对象
@patch('go2_bridge.core.fsm.DogFsm.frame')
def test_fsm(mock_frame):
    # 这样测不到真正的逻辑
    pass
```

---

# 第四部分:验收标准（Acceptance Criteria）

## 4.1 Phase 验收标准

### Phase 0 完成标准
- [ ] 所有规范文档已创建（PROJECT-STRUCTURE, GIT-WORKFLOW, CODE-REVIEW）
- [ ] 文档模板库已建立（`docs/templates/`）
- [ ] 代码格式化工具已配置（black, ruff）
- [ ] Git 分支保护规则已设置
- [ ] 主控审核通过

### Phase 1 完成标准
- [x] 旧 Dog 路径已删除（-600 行）（2026-10-31 深夜，`f80be4f`/`9a6deae`）
- [x] bridge.py 已拆分成模块（实际为平铺 stats/telemetry/link/webserver + core/fsm.py，非包结构）
- [x] 入口文件只留装配与调度（bridge.py 454 行；未改名为 main.py，<200 行目标未达成）
- [x] 全量测试通过（17 套，test_pa_v3 板端除外；原"15 套"口径已过期）
- [ ] 真机冒烟测试通过（10 分钟）（未跑——首道真机门改为次日回归清单 12 场景，ADR-0004）
- [ ] 每个模块有 README 文档（未建独立 README，模块头 docstring + ARCHITECTURE.md §3 承担）
- [ ] 变体代码已收敛（只保留 config_local 差异）（未做，见 1.3 / TODO H）

### Phase 2 完成标准
- [ ] 所有接口有 JSON Schema（`schemas/` 目录）
- [ ] 契约测试全部通过
- [ ] pre-commit hook 能拦截 Schema 变更
- [ ] 前端代码已格式化
- [ ] 前端文档已创建（FRONTEND.md）
- [ ] 测试覆盖率 >80%
- [ ] 集成测试、性能测试、故障注入测试全部通过

### Phase 3 完成标准
- [ ] CI/CD 流水线已配置（GitHub Actions）
- [ ] 每次提交自动运行 CI
- [ ] pre-commit hook 自动运行检查
- [ ] OpenAPI 文档可访问（`/docs`）
- [ ] 模块依赖图已生成（`docs/dependencies.svg`）
- [ ] 测试覆盖率报告自动生成

---

## 4.2 代码质量门禁

**每次提交必须通过**:
1. **格式检查**: `black --check .`
2. **Lint 检查**: `ruff check .`
3. **单元测试**: `pytest test_unit_*.py`
4. **契约测试**: `pytest test_contract_*.py`

**合并到 develop 必须通过**:
5. **集成测试**: `pytest test_integration_*.py`
6. **覆盖率要求**: `pytest --cov --cov-fail-under=80`
7. **Code Review**: 至少 1 人 approve

**部署到板子必须通过**:
8. **全量测试**: 所有测试套件（现 17 套，test_pa_v3 板端除外；原文"15 套"已过期）
9. **真机冒烟测试**: 10 分钟运行无异常

---

## 4.3 文档完整性检查

**每个模块必须有**:
- [ ] `README.md`（职责、接口、依赖、示例）
- [ ] Docstring（公开函数/类）
- [ ] 变更记录（CHANGELOG.md）

**每个接口必须有**:
- [ ] JSON Schema（`schemas/` 目录）
- [ ] 契约测试（`test_contract_*.py`）
- [ ] API 文档（FastAPI OpenAPI）

**每个功能必须有**:
- [ ] 单元测试（覆盖关键路径）
- [ ] 集成测试（端到端验证）

---

## 4.4 架构健康度指标

**模块耦合度**:
- [ ] 无循环依赖（用 `pydeps` 检查）
- [ ] 模块间依赖符合分层规则

**代码复杂度**:
- [ ] 单个函数圈复杂度 <10（用 `radon` 检查）
- [ ] 单个文件 <500 行

**测试健康度**:
- [ ] 覆盖率 >80%
- [ ] 测试运行时间 <30s（单元测试）
- [ ] 测试稳定性（无 flaky test）

**性能基线**:
- [ ] rc_loop 延迟 <10ms
- [ ] WebSocket 推送延迟 <20ms
- [ ] 状态查询响应 <5ms

---

# 附录：快速启动指南

## 给主控的启动清单

> **现状（2026-10-31 深夜）**：Phase 1 已完成（删旧路径 + 平铺拆分），下述 Day 1~4 流程
> 已执行完毕留档；Phase 2/3 尚未启动，流程说明照旧适用。

1. **Phase 0 准备**（1 天）:
   ```bash
   # 创建规范文档
   - 编写 PROJECT-STRUCTURE.md
   - 编写 GIT-WORKFLOW.md
   - 编写 CODE-REVIEW.md
   - 创建文档模板（docs/templates/）
   - 配置工具（black, ruff）
   ```

2. **Phase 1 执行**（3-5 天）:
   ```bash
   # Day 1: 删旧路径
   git checkout -b feature/task-101-remove-legacy
   # ... 删除代码 ...
   pytest  # 全量测试
   python deploy_pc.py  # 真机验证
   
   # Day 2-3: 拆模块（依次执行 TASK-102~106）
   # 每拆一个模块，立刻测试
   
   # Day 4: 清理变体
   ```

3. **Phase 2 并行**（2-3 天）:
   ```bash
   # 分配任务给 3 个 agent
   - Agent A → Track A（契约化）
   - Agent B → Track B（前端）
   - Agent C → Track C（测试）
   
   # 主控负责合并和验证
   ```

4. **Phase 3 工具链**（2-3 天）:
   ```bash
   # 配置 CI/CD
   # 配置质量门禁
   # 自动化文档生成
   ```

## 给 Agent 的工作流程

1. **领取任务**:
   - 从任务清单中领取一个任务
   - 确认依赖已完成
   - 阅读任务描述

2. **执行任务**:
   ```bash
   git checkout develop
   git pull
   git checkout -b feature/task-xxx
   
   # 编写代码
   # 编写测试
   # 编写文档
   
   # 本地验证
   black .
   ruff check .
   pytest
   ```

3. **提交交付**:
   ```bash
   git add .
   git commit -m "feat(scope): description"
   git push origin feature/task-xxx
   
   # 创建 PR
   # 附上: 改动文件清单 + 测试输出 + 「我改过什么」申报
   ```

4. **响应 Review**:
   - 根据反馈修改
   - 更新 PR
   - 等待合并

---

## 常见问题

### Q: 如果测试失败怎么办？
A: 不要强行绕过。回到代码，找根本原因。如果是测试本身的问题，修测试；如果是代码问题，修代码。

### Q: 如果任务依赖阻塞怎么办？
A: 先做其他不依赖的任务，或者和主控沟通调整顺序。

### Q: 如果拆分模块后测试失败怎么办？
A: 回滚（`git reset --hard`），重新拆。拆分应该是零行为变更，测试失败说明拆错了。

### Q: 如果 Code Review 有争议怎么办？
A: 主控裁决。技术问题用数据说话（性能测试、可维护性分析）。

### Q: 如果真机验证失败怎么办？
A: 立刻回滚（deploy_pc.py 有 .bak 自动回滚机制）。分析日志，找根因，修复后重新验证。

---

## 工具参考

### 开发工具
```bash
# 格式化
black .

# Lint
ruff check .

# 类型检查
mypy go2_bridge/

# 测试
pytest
pytest --cov                    # 带覆盖率
pytest -k test_fsm              # 只跑 FSM 测试

# 依赖分析
pydeps go2_bridge --show-deps

# 复杂度检查
radon cc go2_bridge/ -a         # 圈复杂度
radon mi go2_bridge/            # 可维护性指数
```

### Git 工具
```bash
# 分支管理
git checkout -b feature/task-xxx
git branch -d feature/task-xxx  # 删除已合并分支

# Commit 规范检查
git log --oneline               # 查看提交历史

# 回滚
git reset --hard HEAD~1         # 撤销最后一次提交
git revert <commit-hash>        # 安全回滚（保留历史）
```

---

**文档结束**

如有疑问，联系主控或查阅具体的规范文档（`PROJECT-STRUCTURE.md`, `GIT-WORKFLOW.md` 等）。
