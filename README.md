# tronclass-picker

把畅课（TronClass）上的课程文件批量下载到本地，并把课程通知汇总成日志。

纯 Python 标准库实现，零依赖，不需要 `pip install` 任何东西。

> 只读取你自己账号本来就有权限查看的内容，不绕过任何访问控制，不调用任何写接口。
> 归档结果仅供个人复习使用，请遵守学校的信息管理规定，不要二次传播。

## 特性

- **网页界面** — 本地起一个小服务，浏览器里勾选文件，点一下就下。不用记命令。
- **增量同步** — 记录已下载的文件，重跑只补新增，不重复拉也不覆盖你改过的。
- **通知汇总** — 课程公告每轮扫一遍，新增的标题和正文摘要写进当天日志。
- **快速找文件** — 搜索框支持课程名和文件名混搜，`ch3`、`图形学`、`实验` 都能命中。
- **不碰密码** — 复用浏览器里已经登录的会话，脚本里不存账号密码。

## 快速开始

```bash
git clone https://github.com/Ava051213/tronclass-picker.git
cd tronclass-picker

python -m tronclass_picker init      # 生成 config.json，改成你学校的畅课地址
python -m tronclass_picker login     # 浏览器里登录一次，自动抓 Cookie
python -m tronclass_picker web       # 打开网页界面
```

Windows 下直接双击 `run.bat`，它会检查 Python、首次生成配置、带你登录、然后开界面。

`login` 需要 Playwright 来驱动浏览器（`pip install playwright`），会优先用系统已装的
Chrome 或 Edge，不用额外下载内核。不想装的话用 `login --paste` 手动粘 Cookie。

## 界面

左边是文件表，点整行勾选，搜索框边输边过滤；右边是实时日志。
顶部可以在「本学期 / 全部课程」之间切换，也可以重新登录。

扫描和下载都在后台线程，不会卡界面。

## 常见配置

改 `config.json`：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | 深大地址 | 换学校改这里 |
| `documents.extensions` | 34 种 | 下载哪些扩展名 |
| `documents.organize_by` | `course` | 按课程 / 学期 / 平铺存放 |
| `notices.days` | `30` | 通知回溯天数 |
| `notices.output` | `both` | 日志 / markdown / 都要 |
| `network.request_interval` | `0.5` | 请求间隔秒数，别调太低 |

完整配置见 `config.example.json`。

## 命令行

不用界面时也能跑：

```bash
python -m tronclass_picker                     # 按配置下载 + 汇总通知
python -m tronclass_picker --only notices      # 只汇总通知
python -m tronclass_picker --course 图形学      # 只处理某门课
python -m tronclass_picker --dry-run           # 只列出要下什么
```

挂定时任务时把「起始于」设成项目目录，参数填 `-m tronclass_picker --only notices -q`。

## 自测

```bash
python tests/test_local.py
```

起一个本地假接口跑完整链路（下载、去重、类型过滤、通知汇总、登录失效），
68 项检查，不联网也不需要真实账号。

## 许可

MIT
