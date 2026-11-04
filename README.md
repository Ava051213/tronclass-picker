# tronclass-picker

把畅课（TronClass）上的课程文件批量下到本地，同时把待办和截止时间摊在一屏里。

主体用 Python 标准库写成；只有自动登录这一步需要 Playwright，装在项目内的 `.vendor/`，不往系统目录写东西。

> 只读取你自己账号本来就有权限查看的内容，不调用任何写接口。归档结果仅供个人复习，别二次传播。

## 能干什么

- **待办** — 拉出所有未完成作业，按截止时间排，标出还剩几小时 / 几天，过期和今天到期的单独高亮。
- **文件** — 课程活动和课件里的附件，按课程分组，勾选就下。
- **增量** — 记下已下载的文件，重跑只补新的，不覆盖你改过的。
- **登录** — 点一下弹出浏览器走学校统一认证，登录完成后自动抓 Cookie 写回配置，不用手动复制请求头。

## 跑起来

```bash
python -m tronclass_picker
```

Windows 下双击 `run.bat` 也行。首次运行会自动生成 `config.json`，浏览器会打开本地页面。

进去先点右上角「登录」，在弹出的 Chrome 里完成统一认证。窗口会自己关掉，页面接着扫描。

## 待办接口

给外部程序留了一个 JSON 口子，端口固定 8765：

```
GET http://127.0.0.1:8765/api/todos
```

每次调用现拉一次平台接口，不用等页面扫描。字段说明和错误码见 `README.local.md`。

只要接口不要页面：`python -m tronclass_picker --no-browser --no-scan`

## 文件落在哪

```
output/documents/<课程名>/<原文件名>
```

同名文件自动加 `_2`、`_3`，不覆盖。

## 配置

改 `config.json`，全部字段见 `config.example.json`。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | 深大地址 | 换学校改这里 |
| `paths.output` | `./output` | 归档根目录 |
| `documents.concurrency` | `3` | 并发下载数，平台有风控，别调太高 |
| `documents.max_size_mb` | `800` | 单文件大小上限 |
| `network.request_interval` | `0.5` | 请求最小间隔（秒） |

## 一点说明

平台把附件标记为「禁止下载」时，网页上会隐藏下载按钮，但接口层仍然返回签名地址。本工具照常下载这类文件——它们本来就是发给你看的，只是换了种取法。要不要用，你自己判断。

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `client.py` | Cookie、限速、重试、流式下载 |
| `api.py` | 全部接口：课程、附件、待办、公告、签名地址 |
| `collect.py` | 课程 → 文件清单，顺带记录新增与已下载 |
| `download.py` | 一个循环，并发落盘 |
| `server.py` | 本地服务 + JSON 接口：/api/state、/api/todos、/api/logs、/api/health、/api/scan、/api/reload、/api/notices、/api/login、/api/download、/api/logs/clear |
| `login.py` | 驱动系统 Chrome 走统一认证，抓 Cookie |
| `config.py` | 配置读取 |
| `web/index.html` + `static/app.js` + `static/style.css` | 三件套前端 |

## 许可

MIT
