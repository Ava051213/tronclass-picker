# tronclass-picker

把畅课（TronClass）上的课程文档批量下载到本地，并把平台/课程通知汇总成一份 Markdown 周报。
纯 Python 标准库实现，不需要 `pip install` 任何东西。

> 使用前请确认：本工具只读取你自己账号本来就有权限查看的内容，不绕过任何访问控制，
> 不下载平台标注为禁止下载的资源。请遵守畅课平台服务条款和你所在学校的信息管理规定，
> 归档结果仅限个人复习使用，不要二次传播。

---

## 一、它能做什么

| 功能 | 说明 |
| --- | --- |
| 课程文档下载 | 遍历你所有课程，抓取课件、讲义、实验指导等附件，按课程分目录存放 |
| 增量更新 | 记录已下载文件，重复运行不会重复拉取，也不会覆盖你改过的文件 |
| 通知汇总 | 抓取平台公告 + 每门课的课程通知，按时间倒序整理成 Markdown |
| 断点友好 | 单个文件失败不影响其他文件，下次运行自动补下 |
| 登录失效提示 | 会话过期时给出明确提示和重新登录命令，不会静默失败 |

## 二、快速开始

```bash
cd tronClass-picker

# 1. 生成配置模板
python -m tronclass_picker init

# 2. 登录（浏览器里登录一次，自动把 Cookie 存进 config.json）
python -m tronclass_picker login

# 3. 验证能不能读到课程
python -m tronclass_picker check

# 4. 交互选择要下哪些课（推荐首次使用）
python -m tronclass_picker pick

# 或者按 config.json 的配置直接跑
python -m tronclass_picker
```

Windows 下直接双击 `run.bat`，它会自己把流程走完：

1. 找 Python（`python` 找不到就试 `py -3`，都没有则提示安装，要求 3.10+）
2. 首次运行自动生成 `config.json`
3. 没有登录凭据时先弹浏览器登录，登录完接着往下走
4. 进入交互向导，选完即跑
5. 结束后停住，方便你看结果

带参数运行时（例如任务计划里）它不介入，直接把参数透传，行为跟命令行一致。

`run.bat` 里一个中文都没有，是刻意的：cmd.exe 用 GBK 代码页解析批处理文件，
里面写中文会碎成乱码命令。所有中文提示都由 Python 打印。

如果双击闪退，把窗口里的内容发出来，或者在 cmd 里手动跑：

```
cd /d D:\Mia\Project\tronClass-picker
python -m tronclass_picker
```


## 三、登录与身份验证

畅课本身不校验密码，登录由学校的统一身份认证（CAS / OAuth）完成，多数学校还带
多因子验证和验证码。脚本不去模拟那套流程，而是复用你浏览器里已经登录成功的会话 Cookie——
这既是最稳的做法，也不会让你的账号密码落在任何文件里。

三种方式，任选其一：

**方式 A：自动捕获（推荐）**

```bash
pip install playwright
python -m tronclass_picker login
```

会弹出一个真实浏览器窗口，你手动完成统一身份认证，回到终端按回车，Cookie 自动写入
`config.json` 的 `auth.cookie`。

不需要执行 `playwright install chromium`——脚本会优先驱动你系统里已经装好的 Chrome 或 Edge，
只有两者都没有的情况下才需要下载 Playwright 自带的内核。浏览器的配置目录持久化在
`output/.browser`，所以第二次登录通常已经是登录态，回车就过。

**方式 B：手动粘贴**

```bash
python -m tronclass_picker login --paste
```

先在浏览器登录 `https://lms.szu.edu.cn`，按 F12 → Network → 随便点一个请求 →
复制请求头里 `Cookie:` 后面那一整行，粘贴进终端。

**方式 C：直接改配置**

把 Cookie 串填进 `config.json`：

```json
{ "auth": { "cookie": "sessionid=xxxx; csrftoken=yyyy" } }
```

> Cookie 一般能撑几天到一两周。失效后所有命令都会提示 `会话已失效`，重跑一次
> `login` 即可。`config.json` 里是凭据，不要提交到 git（`.gitignore` 已排除）。

## 四、归档规则

### 4.1 收哪些文档

按扩展名白名单过滤，默认：

```
.pdf .doc .docx .ppt .pptx .xls .xlsx .csv .txt .md .rtf
.odt .odp .ods .zip .rar .7z .tar .gz .epub .mobi .caj
.py .java .c .cpp .h .chisel .scala .ipynb
.mp3 .mp4 .m4a .wav
```

图片（`.png/.jpg`）和可执行文件默认不收——前者多是封面缩略图，后者有安全风险。
要收就改 `config.json` 的 `documents.extensions`。

来源是课程活动列表：每个 material 类活动自带 `uploads` 数组，里面就是可下载的附件
（pdf、pptx、zip 等），一次请求全拿到，不必逐个活动再查一次。活动没带 `uploads` 时才
退回 `/api/activities/{id}/upload_references` 补查。

`/api/course/{id}/coursewares` 返回的其实就是同一批 material 活动，属于重复数据，
所以 `documents.include_coursewares` 默认关闭。

作业、测验、讨论这类活动不采集附件，只要课件/讲义。

每条附件都带 `allow_download` 标记，为 `false` 的直接跳过，日志里会汇总有多少个被平台禁止——
这是合规底线，脚本不做任何绕过。

### 4.2 平台关了下载的文件

有些课件老师关掉了下载开关，网页上没有下载按钮，接口里 `allow_download` 是 `false`。
这类文件默认不纳入，扫描结束会提示有几个。

加上 `--include-restricted` 后，脚本会照常走标准签名接口去取。实测深大这边能拿到：
平台对这类文件返回的是服务端转码后的 PDF（URL 里带 `/download/processed/`），
文件名可能还写着 `.pptx`，下载完会自动按实际类型改成 `.pdf`，否则双击打不开。

前提是接口本身放行。要是返回 403/404，脚本就记一条失败，不会伪造 token、
不会去解预览流、不会绕过任何访问控制。用不用这个开关你自己判断——老师关下载
是有原因的，下回来的东西自己看就行，别外传。

### 4.3 通知范围

- 课程通知：`/api/courses/{id}/bulletins`，每门课老师发的公告，带发布人、正文、附件数
- 平台公告：`/api/announcement`，全站公告栏；学校没启用时返回空列表，属正常
- 范围：默认扫全部课程，不受 `courses.semester` / `courses.include` 影响（`notices.all_courses`）
- 时间窗口：默认最近 30 天，改 `notices.days`；填 0 表示全量
- 增量：已经汇总过的公告不会重复出现，每期只写新增的

**每条通知都会写进日志**，跑完直接翻 `output/logs/run-YYYYMMDD.log` 就能看：

```
本次新增 3 条通知：
  · [计算机网络] 实验报告提交要求（04-24 12:36）
      各位同学，由于 word 文档上传至畅课平台后，排版会与本地浏览不一样……
  · [互联网编程] 随堂测试1（04-02 16:16）
      各位同学，下周4月9号课堂上我们进行一次随堂测试，形式为纸质试卷……
```

`notices.output` 控制落到哪儿：`log` 只进日志、`markdown` 只写文件、`both`（默认）两边都有。
只要日志的话改成 `log` 就行，不占磁盘。

全量扫 42 门课的公告大约 60 秒（并发 4 个 + 每次请求间隔 0.5 秒）。

### 4.4 落盘结构

```
output/
├── documents/
│   └── 数字集成电路与系统设计/
│       ├── 第3章_时序逻辑.pdf
│       └── 实验指导.docx
├── notices/
│   ├── 2026-10-29_0150_通知汇总.md
│   └── latest.md              # 永远指向最新一期
├── logs/
│   └── run-20261029.log
└── .state.json                # 增量状态，删掉会导致全部重下
```

`documents.organize_by` 可选 `course`（默认，按课程）、`semester`（按学期/课程）、
`flat`（全部平铺）。

## 五、运行方式

**网页界面**（双击 `run.bat` 默认就是这个）：

启动后会开一个只监听 `127.0.0.1` 的本地服务，并自动用浏览器打开。界面长这样：

- 顶部：当前账号、课程数、扫描状态、「本学期 / 全部课程」切换、重新扫描、登录
- 左侧：搜索框 + 文件表（勾选 / 课程 / 文件名 / 大小），点整行切换勾选
- 右侧：实时日志，每门课扫到几个文件、下载进度都在这里滚
- 底部：已选计数、「汇总通知」「下载选中」两个按钮

几个细节：

- 搜索框边输边过滤，支持课程名和文件名混搜（`ch3`、`图形学`、`实验`）
- 受限文件默认不显示，勾上「含受限」才出现，带 ★ 标记
- 扫描和下载都在后台线程，界面不会卡
- 按 `/` 快速聚焦搜索框，`esc` 退出输入
- 关掉命令行窗口服务就停了

页面是单个 HTML 文件，没有任何前端依赖，也不用构建。

**命令行菜单**（不想开窗口时用）：

**交互选择**（双击 `run.bat` 就是这个，不用记任何参数）：

启动后先给一个主菜单：

```
==============================================
  畅课归档 · 交互模式
==============================================
  当前账号：吴炫越
  全程只读，不会修改平台上的任何内容

要做什么？
  [1] 下载课程文件
  [2] 汇总最新通知
  [3] 两样都做
  [4] 查看我的课程
  [5] 退出
```

选 1 或 3 会接着问课程范围（本学期 / 全部 / 手动挑），然后扫描并列出文件：

```
共 24 个文件，合计约 73.1 MB：

  [  1]    18 KB  软件工程 / 深圳大学实验报告统一模板.doc
  [  2]   252 KB  软件工程 / 实验一：软件界面设计.docx
  [  3]   1.7 MB  数字集成电路与系统设计 / ch4-时序逻辑描述.pdf
  ...
  [ 24]   490 KB  计算机图形学 / ex_1.2.rar
```

输入编号挑（支持 `1,3,5-8`），也可以直接输关键字过滤：

```
> ch3
匹配「ch3」的文件 2 个：
  [  1]    2.0 MB  数字集成电路与系统设计 / ch3-组合逻辑描述.pdf
  [  2]    3.0 MB  计算机图形学 / CG_ch03_1.pptx
继续输入编号选择，或 r 恢复全部
```

选 2 不选课程，直接扫全部课程的通知。

扫描时如果发现平台关了下载的文件，会单独问一句要不要一起列出来，带 ★ 标注。
登录过期会在原地带你重登一次，不用退出重来。

选择只作用于这一轮，不写回 `config.json`。

**单次执行**（手动跑，或交给外部调度）：

```bash
python -m tronclass_picker
```

**脚本内定时**（进程常驻，每 6 小时一轮）：

```bash
python -m tronclass_picker --interval 6h
```

支持 `30s` / `30m` / `6h` / `1d` 单位。Ctrl+C 退出。

**Windows 任务计划**（推荐，不占进程）：

打开「任务计划程序」→ 创建基本任务 → 触发器选每天 → 操作选「启动程序」，填：

```
程序或脚本：  python
添加参数：    -m tronclass_picker -q
起始于：      D:\Mia\Project\tronClass-picker
```

「起始于」必须填项目目录，否则找不到 `config.json`。用 `-q` 时只输出警告和错误，
成功的那一轮不会产生任何弹窗。

**每天只要通知**的话，把「添加参数」换成 `-m tronclass_picker --only notices -q`。
一轮约 60 秒（要扫 42 门课）。内容按天写进 `output/logs/run-YYYYMMDD.log`，
早上打开昨天的日志就能看。想省磁盘就把 `notices.output` 设成 `log`，不产出 markdown。

定时跑起来之后的行为是：文档只补当前学期里新上传的文件（`.state.json` 去重），
通知每轮扫全部课程、只写当轮新增的公告。两者都靠状态文件做增量，重复跑不会有副作用。

命令行版本（在 cmd 里执行）：

```bat
schtasks /create /tn "畅课通知" /tr "cmd /c cd /d D:\Mia\Project\tronClass-picker && python -m tronclass_picker --only notices -q" /sc daily /st 08:30
```


**常用参数**

```
--only docs|notices|all   只跑其中一项
--course 关键字            只下载匹配的课程，可重复；不影响通知
--types pdf,pptx          临时限定文件类型
--include-restricted      一并尝试平台关了下载的文件
--dry-run                 只列出将要下载的文件，不落盘
--interval 6h             定时模式
--output D:\课程归档       临时覆盖输出目录
-v / -q                   调试日志 / 只报错
```

比如只想补下某一门课的 pptx：

```bash
python -m tronclass_picker --only docs --course 数字集成电路 --types pptx
```

**下载和通知的筛选是分开的**：`courses.semester`、`courses.include`、`--course`
只收窄文档下载；通知默认扫全部课程（`notices.all_courses`），因为公告量小，跨学期也可能要看。
只有 `courses.exclude` 对两者都生效。

## 六、异常处理与排查

所有异常都会写进 `output/logs/run-YYYYMMDD.log`，同时打到控制台。退出码：

| 退出码 | 含义 | 处理 |
| --- | --- | --- |
| 0 | 全部成功 | — |
| 1 | 部分文件失败 | 看日志里「以下文件下载失败」，重跑一次通常能补上 |
| 2 | 登录失效 | 重跑 `login` |
| 3 | 配置错误 | 按提示改 `config.json` |
| 4 | 网络/接口异常 | 检查网络或校园网是否需要 SecureLink |

内置的容错：

- 每次请求失败按指数退避重试（默认 3 次），429 会读 `Retry-After` 等待
- 下载先写 `.part` 再改名，中断不会留下半截文件
- 校验 `Content-Length`，长度不符算失败并重试
- 单个文件异常记录后继续，不影响整轮
- 请求间隔默认 1 秒，避免触发平台风控
- 检测到返回登录页 / 401 / 403 时立即判定会话失效并中止，不会把登录页当成文件存下来

**典型问题**

*提示「返回的是登录页，会话已失效」*
Cookie 过期了。重跑 `python -m tronclass_picker login`。

*所有课程都是 0 个文件*
先跑 `check` 看课程列表是否正常。课程列到了但没有文件，多半是这门课本来就没上传附件，
或者该租户的接口路径不同，用 `-v` 看具体请求日志。

*下载到一半全是 403*
签名直链有效期很短，且部分资源平台禁止下载。这类文件属于「平台不允许」，脚本会记录失败但不会绕过。

*想只跑当前学期*
`/api/my-courses` 会返回你入学以来的全部课程。用学期关键字过滤：

```json
{ "courses": { "semester": "2026-2027" } }
```

学期名由 `academic_year.name` 和 `semester.code` 拼出，形如 `2026-2027 第1学期`。
先跑 `check` 看列表里的学期标注，照着填。

*想只盯某一门课*
```json
{ "courses": { "include": ["数字集成电路"], "exclude": ["体育"] } }
```

## 七、配置速查

`config.json` 里常用的几项：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | `https://lms.szu.edu.cn` | 学校畅课地址，换学校改这里 |
| `auth.cookie` | 空 | 登录后的 Cookie 串 |
| `paths.output` | `./output` | 归档根目录 |
| `documents.extensions` | 见上文 | 文档扩展名白名单 |
| `documents.concurrency` | `3` | 并发下载数，别超过 8 |
| `documents.max_size_mb` | `800` | 单文件上限，0 为不限 |
| `documents.include_restricted` | `false` | 是否尝试下载被平台关闭下载的文件 |
| `notices.days` | `30` | 通知回溯天数 |
| `notices.all_courses` | `true` | 通知扫全部课程，忽略学期/关键词筛选 |
| `notices.output` | `both` | `log` 只进日志 / `markdown` 只写文件 / `both` |
| `notices.include_content` | `true` | 是否抓公告正文（多一次请求） |
| `network.request_interval` | `0.5` | 请求间隔秒数 |
| `courses.semester` | 空 | 学期关键字，如 `"2026-2027"` |
| `courses.include` / `exclude` | 空 | 按课程名/编号/ID 关键字筛选 |

环境变量也能覆盖：`TRONCLASS_BASE_URL`、`TRONCLASS_COOKIE`、`TRONCLASS_OUTPUT`。

## 八、项目结构

```
tronclass_picker/
├── models.py      领域模型：Course/DocTask/Selection/Job/JobResult
├── planning.py    决策层：课程筛选、文件名清洗、落点分配、组装 Job
├── app.py         应用层：connect → scan → plan → run
├── cli.py         入口：argv → Selection → 调 App
├── interactive.py 交互向导：问询并产出 Selection
├── web.py         本地网页服务
├── web/index.html 前端页面（单文件，无依赖）
├── login.py       浏览器登录抓 Cookie
├── docs.py        扫描器 + 下载器
├── notices.py     通知采集与 Markdown 渲染
├── reporting.py   结果摘要
├── api.py         畅课接口封装（只读）
├── client.py      HTTP 会话、限速、重试、流式下载
├── config.py      配置加载与校验
├── state.py       增量状态
├── logx.py        日志
└── exits.py       退出码约定
```

**分层**：命令行和向导是两个入口，都只负责产出一个 `Selection`（要处理哪些课、
哪些文件、做哪几件事）。后面统一走规划器变成 `Job`，再由 App 执行成 `JobResult`，
最后由报告层输出。执行链路不知道选择是从哪来的，所以命令行和向导不会各写一套流程。

```
命令行参数 ─┐
            ├─→ Selection ─→ Planner ─→ Job ─→ App.run ─→ JobResult ─→ 摘要
交互向导   ─┘
```

`planning.py` 不碰网络也不写磁盘，输入输出都是数据类，所以筛选、命名、去重
这些规则都能直接单测。

根目录还有 `run.bat`（双击启动网页界面）、`config.example.json`（配置模板，
复制成 `config.json` 再填 Cookie）、`tests/test_local.py`（本地 mock 端到端自测）。

自测起一个本地假接口跑完整链路，不联网。

跑自测：

```bash
python tests/test_local.py
```

会起一个本地假接口，验证下载、去重、类型过滤、通知汇总、失效检测全流程，用完自动清理。

## 九、许可

MIT。随便用、改、转发，保留版权声明即可。出了问题作者不负责，见下方免责。

## 十、免责

本项目仅用于个人学习资料的离线备份。作者不对使用者的账号安全、平台处罚或数据合规问题负责。
遇到学校明令禁止自动抓取的场景，请立即停止使用。
