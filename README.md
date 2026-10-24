# EXT / VAR 双平台交易机器人 | Trading Bot

## 简介 / Overview

**中文**

基于 Python 与 Playwright/CDP 的 EXT（Extended）、VAR（Variational）双平台交易机器人，包含桌面控制面板、资金与持仓扫描、策略执行、订单确认、浏览器页面恢复和离线回归测试。

本仓库保存当前维护版本的源码。程序正逐步将主文件中的公共逻辑拆分到 `bot_core/`，保留现有策略、金额与方向规则。

**English**

A Python trading bot for Extended (EXT) and Variational (VAR), built with Playwright and a local Edge CDP connection. It includes a desktop dashboard, account and position scanning, strategy execution, order confirmation, browser page recovery, and offline regression tests.

The log viewer updates incrementally and preserves your reading position and text selection while new entries arrive. Background data collection keeps scrolling responsive; use “回到最新” (Latest) to resume following the log.

This repository contains the current maintenance version. Shared logic is being extracted into `bot_core/` while preserving the existing strategy, order sizing, and direction rules. ANCHOR/S1 full closes use a bounded worker, explicit submission evidence, and confirmation that both platforms are flat. Only a proven unsubmitted action can be retried after one page recovery. A legacy pending-order replay tool is available for review; live reconciliation is not enabled.

## 主要功能

- 通过本地 Edge 的 CDP 连接读取交易页面，扫描资金、持仓及指标。
- 通过桌面面板展示运行状态、资金和收益信息。
- 日志增量刷新，上翻时保留阅读和复制位置；后台采集数据，支持“回到最新”恢复跟随。
- 执行 B1、M9、M10、锚点全平等现有流程。
- 根据持仓数量变化确认执行结果，保存未确认订单并防止重复提交。
- 分开管理开仓与平仓待确认记录，避免开仓记录阻挡减仓和全平。
- 复用和恢复 EXT、VAR 页面，并报告持续恢复失败。
- 提供账户流水采集、CSV 导入和离线测试工具。

## 目录说明

| 路径 | 用途 |
| --- | --- |
| `BOT5-2-414 .py` | 主程序，文件名在 `.py` 前有一个空格 |
| `面板4-11.py` | 桌面控制面板 |
| `bot_core/` | 持仓证据、订单确认、页面恢复、保证金检查及账户流水等公共模块 |
| `tests/` | 纯函数、模拟界面和主程序 AST 隔离回放测试 |
| `maintenance/` | 维护、账户审计及历史修复工具 |
| `start_bot_chrome.bat` | 保留历史文件名；当前内容启动 Edge，使用 CDP 9222 和独立配置目录 |
| `一键配置环境修正413.bat` | 历史环境安装脚本，涉及安装和配置变更，运行前需核对内容 |

## 环境与入口

当前本机维护环境为 Windows、Python 3.14。`requirements.txt` 记录本次上传时已安装的相关依赖版本，尚未在全新环境重装验证。

手动准备虚拟环境和依赖：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

面板入口：

```powershell
.\.venv\Scripts\python.exe ".\面板4-11.py"
```

实际运行需要自行准备交易所登录、独立浏览器配置及本机运行配置。打开面板后应先核对页面、资金和持仓，再由操作者决定是否开始交易。

**不要通过导入整个主程序来检查语法或运行测试。** 主文件保留启动与初始化副作用；测试应抽取所需定义或直接测试 `bot_core`。

## 当前维护状态

截至 2026-10-24：

- 已完成待确认拦截第一阶段：按操作类型隔离，开仓和平仓记录分开保存，同币同方向重复平仓继续受保护。
- 已优化面板日志：移除定时全量重画和三分钟自动跳回，新增分批插入、滚动事件合并、隐藏插页按需刷新。
- 锚点全平结果分支要求 EXT、VAR 均成功且确认空仓，才提交锚点更新；失败记录平台和残留或无法确认的币种。
- VAR 全平按实际持仓行执行，有限等待确认按钮就绪；只在明确未提交时恢复页面并重试一次。已点击或结果未知时保留待确认，不重复下单。
- 逐币平仓后用两次一致的完整持仓表确认目标币消失，即使其他币仍有仓位也能结算该笔平仓；先前已提交的单币平仓同样只核验、不重发。平台 ALL 待确认仍禁止重复提交。
- ANCHOR/S1 全平共享 120 秒工作时限和 5 秒外层清理余量；会话报错保留真实原因，读表异常不再伪装成零持仓。
- 全平预检查使用新鲜结构化持仓证据，避免 `dq_reason=positions_verified` 缺少文本计数时被误挡。执行器仍需重新读取两平台并确认全空。
- 第二阶段已提供纯函数对账和只读回放命令；旧记录按同方向、当前仓位不少于意图金额 50% 判断。真实 M9/M10 数量增量确认规则保持不变。启动自动对账和实盘手动结算尚未启用，待逐条回放审阅后接入。
- 维护重启可通过进程环境 `BOT_CONTROLLED_RESTART_RESUME=1` 保留有效的持久化锚点，避免启动重置模块记录；普通启动行为保持原样。该选项不添加交易锁，不代表自动部署授权。
- 保证金界面字段读取仍可能出现无法识别的情况。当前约定是读取失败时沿用原流程，按钮禁用时停止提交。

源码更新与正在运行的进程是两个状态，补丁需由操作者选择合适时机重启加载。提交仓库不会自动启动交易，也没有配置自动部署。

## 测试记录

本次维护在原本机环境执行的结果：

- 主程序及相关模块 `ast.parse` 语法检查通过。
- Python 全量 507 项：506 项通过，1 项既有历史快照用例失败；测试禁止网络、子进程、真实浏览器和实盘状态写入。
- 新增 VAR 全平与预检查回放 37 项、旧记录对账回放 19 项全部通过；另保留真实锚点分支的“失败不抬锚点、后续轮次可再触发”回归测试。
- 新增的待确认隔离测试 25 项全部通过。
- 新增的面板日志与后台刷新测试 26 项全部通过，使用独立隐藏 Tk 控件和模拟采集器。
- 合成 DOM 界面解析测试 7 项全部通过。

既有失败为 `tests/test_recorded_incidents.py` 中的 `test_oct22_zero_snapshot_is_rejected_as_unknown`：它读取本机运行中的 `out/positions.json`、`out/equity.json`，当前数据已不同于原来的历史异常快照。

部分历史测试和维护脚本依赖本机审计目录、维修备份或运行数据，这些文件不上传。因此上述结果是本机维护记录，不能理解为全新克隆后所有测试无需准备即可通过。原 `maintenance/run_offline_tests.py` 也依赖历史基线文件与停止状态。

不依赖交易账户的基础测试示例：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_symbols.py"
node tests/test_margin_dom.cjs
```

Node.js 仅用于上述合成 DOM 测试。维护脚本中的停止、重启准备与补丁应用工具需要人工明确选择执行，不作为安装或测试的自动步骤。

旧待确认记录回放示例（只读输入，在临时目录输出逐条报告与建议副本，不改机器人状态）：

```powershell
.\.venv\Scripts\python.exe maintenance\replay_unconfirmed.py --journal "<复制的 execution_unconfirmed.json>" --positions "<同一时段的 positions.json>"
```

默认以输入持仓快照的时间回放历史判断，不代表当前实时持仓。缺失币种不视为空仓，旧记录没有基准数量时也不会生成虚构成交、利润或网格层。

## 本机数据边界

`.gitignore` 使用默认忽略、显式允许源码的规则。以下内容保留在本机：

- 交易日志、持仓与资金快照、未确认订单和账户流水。
- `control.json` 等运行控制文件，以及面板和环境的本机配置。
- 浏览器用户配置、登录态、Cookie 和钱包会话。
- 虚拟环境、缓存、截图、审计输出和维修备份。

API Key 等凭据通过本机环境变量或未入库的配置提供，不写入源码。
