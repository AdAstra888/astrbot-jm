# AstrBot JMComic & PicACG 下载搜索插件

[![AGPL-3.0 License](https://img.shields.io/github/license/suiseikat/astrbot_plugin_jmcomics)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)

为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 提供 JMComic 搜索、排行榜、详情查询、漫画下载以及 PDF/ZIP 文件发送功能，默认按「个人 / 小群自用」调优。
ps:这些都是ai写的，用dsh和codex轮流拷打完成的，有什么事不关我的事，我直接就是一个甩锅，hh
## 功能

- `/jm <本子号>`：下载整本或指定章节，转换为 PDF 后发送。
- `/jm rand [week|month|day] [关键词]`：随机推荐一本，自动跳过已经发过的本子。
- `/jm z <本子号>`：下载整本或指定章节，打包为 ZIP 后发送。
- `/jm s <关键词>`：按关键词搜索，关键词可以带空格。
- `/jm r [week|day|month]`：查看月榜、周榜或日榜。
- `/jm d <本子号>`：直接发送本子文字详情和封面。
- `/jm stop`：停止当前用户正在进行的下载。
- `/jm clean`：立即清理过期文件。
- 下载过程按章节/页数推送进度，进度频率可配置，也可关闭。
- 下载内容按发送者分目录保存，清理任务只作用于该用户目录。
- 相同本子和参数的 PDF 会被缓存，跨用户复用，重复请求不再重新下载。
- 下载有全局并发上限、单用户任务上限、同本子互斥锁和冷却时间，连点不会重复下载。
- 发送前检查文件大小，超限自动降级清晰度并给出可操作的提示。
- 支持按天数定时清理（`auto_cleanup_days`），也可以沿用按数量清理或发后删除。

旧命令 `/jmz`、`/jms`、`/jmr` 仍然可用，效果与对应子命令一致。

哔咔漫画（PicACG）走另一组命令，和 JM 完全独立：

- `/pica <本子ID>`：下载整本或指定章节（如 `1-5`、`1,3,5-10`），转 PDF 发送。
- `/pica z <本子ID>`：打包 ZIP。
- `/pica s <关键词> [页码]`：搜索。
- `/pica d <本子ID>`：详情，附封面图。
- `/pica eps <本子ID> [页码]`：章节列表。
- `/pica r [day|week|month]`：排行榜。
- `/pica rand [关键词] [day|week|month]`：随机推荐（跳过已发过的；带关键词时从搜索结果里挑）。
- `/pica fav [页码]`：我的收藏。
- `/pica stop`：停止当前用户的哔咔下载。
- `/pica clean`：立即清理过期文件。
- `/pica login`：检查登录状态。

哔咔与 JM 共用并发闸门、PDF 缓存、发送大小限制和清理策略，所以两个平台不会互相拖慢。

## 安装

进入 AstrBot 的插件目录并克隆仓库：

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/AdAstra888/astrbot-jm.git
```

随后安装依赖并重启 AstrBot：

```bash
pip install -r astrbot_plugin_jmcomics/requirements.txt
```

也可以下载仓库 ZIP，解压到 `data/plugins/astrbot_plugin_jmcomics` 后重启。插件不再在导入时自动 pip 安装依赖，缺少 `jmcomic` 时会在日志里提示安装命令。

## 使用

所有操作都从 `/jm` 一个命令进入，也可以用旧命令。

子命令支持中文：`下载`、`搜索`、`排行`、`详情`、`随机`、`停止`、`清理`、`帮助`，和英文写法等价。第一个词既不是子命令、也不像本子号时（例如 `/jm 全彩`），会直接当成搜索关键词。哔咔同理，`/pica 搜索`、`/pica 停止` 都能用。

### 下载 PDF

```text
/jm <本子号> [范围] [--quality=80] [--max-size=1920]
```

示例：

```text
/jm 123
/jm 123 1-5
/jm 123 3 --quality=80 --max-size=1920
```

`quality` 范围为 1-100，默认取 `default_pdf_quality`；`max-size` 为图片最长边像素，`0` 表示不缩放。`quality` 大于等于 95 且 `max-size=0` 时直接复用原图，不做二次压缩。本子号和范围只能给一次，重复指定会被拒绝。

### 下载 ZIP

```text
/jm z <本子号> [范围]
```

### 随机推荐

```text
/jm rand
/jm rand week
/jm rand month
/jm rand 巨乳
```

不写参数时从今日榜随机取一本；也可以指定周榜、月榜，或按关键词从搜索结果里随机取。发过的本子会记录在下载目录的 `sent_albums.json` 里，随机时会自动跳过，最多保留最近 1000 条。

### 搜索与排行榜

```text
/jm s <关键词> [页码]
/jm r [week|day|month] [页码]
```

搜索会把剩余内容整体当作关键词，末尾的纯数字会被识别为页码，所以关键词里带空格也没问题。排行榜的类型和页码顺序可变，不写类型时显示月榜。

### 查看详情

```text
/jm d <本子号>
```

直接发送标题、作者、收藏数、标签、前 10 个章节和封面图片。封面下载失败时仍会发送文字详情。

### 停止下载

```text
/jm stop
```

只停止自己当前的任务。已经下载到磁盘的图片会保留，重新发起同一本子时会跳过已存在的文件继续下载。排队中的任务也可以取消。

### 清理与帮助

```text
/jm clean
/jm help
```

`/jm clean` 立即清理超过 `auto_cleanup_days` 天未使用的文件（未设置时按 7 天处理）。

### 哔咔漫画

先在插件配置里填 `pica_account` 和 `pica_password`，重启后发送 `/pica login` 确认能登录（token 会缓存到 `cache/pica_token.json`，之后不再重复登录）。

```text
/pica s 关键词              搜索，拿到 24 位本子 ID
/pica <ID>                  下载整本
/pica <ID> 1-5              只下第 1~5 章
/pica eps <ID>              看章节列表，确认序号
/pica stop                  停止
```

如果登录提示 `invalid email or password`，先确认这组账号能在官方网页/App 登录；哔咔的账号分服务器，第三方镜像站的账号无法在官方接口登录。

很早的旧章节可能已下架或需要解锁，接口会直接报错。这类章节会被跳过，下载结束后单独提示哪些章节失败，不会影响其他章节。

两种并发不是一回事：`max_concurrent` 限制**同时进行的下载任务数**（默认 1，两个平台共用这个闸门），`pica_concurrency` 限制**单章内同时下载的图片数**（默认 4）。任务排队时会提示“已加入队列”。

关于凭据：`pica_password` 存在 AstrBot 的配置里，是明文的，别把 `data/config` 目录泄露出去；登录拿到的 token 缓存在 `cache/pica_token.json`，Linux/macOS 下会自动设成 `600` 权限。插件不会在日志或报错里打印密码。

## 配置

在 AstrBot 管理面板的插件配置页修改：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `download_dir` | string | `./data/jm_downloads` | 下载根目录；相对路径以 AstrBot 工作目录为基准。缓存也放在这里。 |
| `cleanup_mode` | string | `count` | `count` 按数量保留；`after_send` 在发送完成后延迟删除。 |
| `max_albums` | int | `10` | `count` 模式下每个用户保留的本子、PDF 和 ZIP 数量；`0` 表示不限制。 |
| `after_send_delay` | int | `10` | `after_send` 模式在发送调用完成后等待多少秒再删除，避免适配器仍在读取文件。 |
| `cover_keep_days` | int | `7` | 封面保留天数；`0` 表示永久保留。 |
| `auto_cleanup_days` | int | `0` | 定时清理：后台每小时删除超过 N 天未使用的本子目录、ZIP 和 PDF；`0` 表示关闭。与 `cleanup_mode` 互不影响。 |
| `default_pdf_quality` | int | `85` | 默认 PDF 图片质量，范围 1-100。 |
| `progress_updates` | bool | `true` | 是否在下载过程中推送进度消息。 |
| `progress_interval` | int | `8` | 两条进度消息的最小间隔（秒），建议 8-15。 |
| `max_concurrent` | int | `1` | 全局同时下载数上限；个人/小群建议保持 1。 |
| `cooldown_seconds` | int | `30` | 同一用户两次下载命令的最短间隔；`0` 表示不限制。 |
| `max_file_size_mb` | int | `90` | 发送文件大小上限；超限自动降清晰度重试，`0` 表示不检查。 |
| `pdf_cache_max` | int | `30` | 全局 PDF 缓存数量，按最近使用淘汰；`0` 表示不缓存。 |
| `min_free_disk_mb` | int | `200` | 下载前要求的最小剩余磁盘空间；`0` 表示不检查。 |
| `allowed_groups` | string | 空 | 允许使用的群号，逗号或空格分隔；留空不限制，私聊始终可用。 |
| `enable_jm_log` | bool | `false` | 是否显示 jmcomic 调试日志。 |
| `option_file` | string | 空 | 自定义 jmcomic YAML 配置路径；不存在时回退到内置默认配置。 |
| `pica_account` | string | 空 | 哔咔账号（邮箱或用户名），留空则 `/pica` 不可用。 |
| `pica_password` | string | 空 | 哔咔密码，登录成功后会缓存 token。 |
| `pica_token` | string | 空 | 可选，手动指定 token 可跳过登录。 |
| `pica_api_base` | string | 官方地址 | 哔咔接口地址；用分流 App 时改成它实际请求的域名。 |
| `pica_allow_http_fallback` | bool | `false` | 图片下载是否允许从 HTTPS 降级到 HTTP；只有镜像站强制 HTTP 时才开。 |
| `pica_proxy` | string | 空 | 哔咔请求代理，例如 `http://127.0.0.1:7890`。 |
| `pica_verify_ssl` | bool | `true` | 图床证书异常时可关闭。 |
| `pica_concurrency` | int | `4` | 单章图片并发数，1~8。 |

修改配置后请重启或重新加载插件。

## 文件与缓存

```text
<download_dir>/
├── covers/                 # 详情页封面，按 cover_keep_days 清理
├── cache/pdfs/             # PDF 缓存，按 pdf_cache_max 轮转
└── <用户ID>/
    ├── <本子目录>/
    ├── <本子>.zip
    └── pdfs/               # 仅当 pdf_cache_max=0 时使用
```

PDF 缓存文件名包含本子号、章节范围、质量和缩放参数，参数不同不会互相覆盖。缓存命中时直接发送，不再下载和转换。

`count` 模式只检查当前完成下载的用户目录，分别保留最新的 `max_albums` 个本子目录、PDF 和 ZIP。删除前会验证目标必须位于该用户目录内，不会删除下载根目录、其他用户目录或外部路径。

`after_send` 模式会先等待文件发送调用完成，再等待 `after_send_delay` 秒，最后删除本次原图和生成的 PDF/ZIP；开启 PDF 缓存时，缓存文件不在删除范围内。

`auto_cleanup_days` 大于 0 时，后台每小时扫描一次所有用户目录，删除超过 N 天未修改的本子目录、ZIP 和用户 PDF；`cache/` 目录（全局 PDF 缓存）不参与按天清理，仍由 `pdf_cache_max` 轮转。`/jm clean` 可立即触发一次。

## Docker 部署

AstrBot 容器必须持久化挂载 `/AstrBot/data`，否则重建或升级容器会丢失插件配置和下载文件：

```bash
docker run -d \
  --name astrbot \
  -v /宿主机/astrbot_data:/AstrBot/data \
  soulter/astrbot:latest
```

QQ 官方机器人适配器由 AstrBot 自己读取并上传文件，只要 AstrBot 容器能访问生成路径即可。

如果使用 NapCat，并且适配器只把本地文件路径交给 NapCat，则 NapCat 也必须把同一个宿主机目录挂载到相同的 `/AstrBot/data` 路径：

```bash
docker run -d \
  --name napcat \
  -v /宿主机/astrbot_data:/AstrBot/data \
  ...
```

仅在 Compose 或启动命令中写了挂载还不够；修改后必须重新创建容器，并用 `docker inspect` 确认挂载已经生效。

## 常见问题

### 下载完成后提示没有找到图片

插件会按章节对象上的实际保存目录聚合图片，不再按修改时间猜目录。仍然失败时，请打开 `enable_jm_log` 并检查自定义 `option_file` 的 `dir_rule`，确认下载目录落在 `download_dir` 之内。

### 提示文件超过大小限制

按提示降低参数重试即可，例如 `/jm 123 --quality=60 --max-size=1600`。也可以在配置里调大 `max_file_size_mb`，但要注意平台本身的上传限制。

### PDF 无法发送或提示文件不存在

确认 AstrBot 能读取 `<download_dir>/cache/pdfs/`（或 `pdf_cache_max=0` 时的用户 `pdfs/` 目录）。Docker 用户还需要确认 `/AstrBot/data` 持久化挂载已实际应用。

### PDF 没有生成

检查日志中是否有 Pillow 或 img2pdf 错误，并确认容器内有足够磁盘空间。临时文件生成在 PDF 同目录的 `.jm_pdf_*` 文件夹内，生成结束会自动删除。

### 没有进度消息或无法停止

进度消息由 `progress_updates` 控制，最小间隔由 `progress_interval` 控制；单章本子按页数推送，多章本子按章节推送。`/jm stop` 依赖 jmcomic 2.7+ 的协作取消，老版本只能等当前请求返回后停止；已经下载的图片不会被删除。

### 群聊里不响应

检查 `allowed_groups` 是否填写了当前群号；留空表示不限制。私聊不受该配置影响。

## 文件结构

```text
astrbot_plugin_jmcomics/
├── assets/
├── tests/
├── _conf_schema.json
├── main.py
├── pica/
├── metadata.yaml
├── requirements.txt
└── README.md
```

## 开发与测试

```bash
python tests/test_core.py
```

测试使用标准库断言和桩模块，不需要安装 AstrBot 或 jmcomic，覆盖参数解析、子命令分发、页序、目录聚合、PDF 路径/缓存、ZIP 打包、按天清理和删除边界。

## 许可证与致谢

本项目采用 [AGPL-3.0](LICENSE) 许可证。

- [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)
- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
