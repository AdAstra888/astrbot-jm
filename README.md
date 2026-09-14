# AstrBot JMComic 下载搜索插件

[![AGPL-3.0 License](https://img.shields.io/github/license/suiseikat/astrbot_plugin_jmcomics)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/)

为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 提供 JMComic 搜索、排行榜、详情查询、漫画下载以及 PDF/ZIP 文件发送功能。

## 功能

- `/jm download`：下载整本或指定章节，转换为 PDF 后发送。
- `/jmz`：下载整本或指定章节，打包为 ZIP 后发送。
- `/jms`：按关键词搜索。
- `/jmr`：查看月榜、周榜或日榜。
- `/jm detail`：直接发送本子文字详情和封面，不使用合并转发消息。
- 下载内容按发送者分目录保存，避免不同用户的文件和清理任务互相影响。
- 支持按数量保留或发送后延迟删除两种清理模式。

## 安装

进入 AstrBot 的插件目录并克隆仓库：

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/suiseikat/astrbot_plugin_jmcomics.git
```

随后重启 AstrBot。也可以下载仓库 ZIP，解压到 `data/plugins/astrbot_plugin_jmcomics` 后重启。

插件会检查 `jmcomic`、`Pillow` 和 `img2pdf`。如果自动安装失败，请在 AstrBot 的 Python 环境中手动安装 `requirements.txt` 中的依赖。

## 使用

### 下载 PDF

```text
/jm download <本子号> [范围] [--quality=80] [--max-size=1920]
```

示例：

```text
/jm download 123
/jm download 123 1-5
/jm download 123 3 --quality=80 --max-size=1920
```

`quality` 范围为 1-100；`max-size` 为图片最长边像素，`0` 表示不缩放。

### 下载 ZIP

```text
/jmz <本子号> [范围]
```

### 搜索与排行榜

```text
/jms <关键词> [页码]
/jmr [week|day] [页码]
```

`/jmr` 不加类型时显示月榜。

### 查看详情

```text
/jm detail <本子号>
```

直接发送标题、作者、收藏数、标签、前 10 个章节和封面图片。封面下载失败时仍会发送文字详情。

### 帮助

```text
/jm help
```

## 配置

在 AstrBot 管理面板的插件配置页修改：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `download_dir` | string | `./data/jm_downloads` | 下载根目录；相对路径以 AstrBot 工作目录为基准。 |
| `cleanup_mode` | string | `count` | `count` 按数量保留；`after_send` 在发送完成后延迟删除。 |
| `max_albums` | int | `10` | `count` 模式下每个用户保留的本子、PDF 和 ZIP 数量；`0` 表示不限制。 |
| `after_send_delay` | int | `60` | `after_send` 模式在发送调用完成后等待多少秒再删除，避免适配器仍在读取文件。 |
| `cover_keep_days` | int | `7` | 封面保留天数；`0` 表示永久保留。 |
| `default_pdf_quality` | int | `85` | 默认 PDF 图片质量，范围 1-100。 |
| `enable_jm_log` | bool | `false` | 是否显示 jmcomic 调试日志。 |
| `option_file` | string | 空 | 自定义 jmcomic YAML 配置路径；为空时使用内置默认配置。 |

修改配置后请重启或重新加载插件。

## 清理机制

插件将新下载内容放在：

```text
<download_dir>/<用户ID>/
├── <本子目录>/
├── <本子压缩包>.zip
└── pdfs/
    └── <本子ID>.pdf
```

`count` 模式只检查当前完成下载的用户目录，并分别保留最新的 `max_albums` 个本子目录、PDF 和 ZIP。删除前会验证目标必须位于该用户目录内，不会删除下载根目录、其他用户目录或外部路径。

`after_send` 模式会先等待文件发送调用完成，再等待 `after_send_delay` 秒，最后删除本次原图和生成的 PDF/ZIP。建议 QQ 官方机器人等平台保留默认的 60 秒缓冲；如遇到平台异步上传较慢，可以调大该值。

封面不受以上两种模式影响，由 `cover_keep_days` 独立清理。旧版本遗留在用户目录之外的下载不会被自动删除，避免升级时误删数据。

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

插件会先使用 jmcomic 推断的目录，再在当前用户目录内按本子 ID、标题和修改时间寻找实际图片目录。仍然失败时，请打开 `enable_jm_log` 并检查自定义 `option_file` 的目录规则。

### PDF 无法发送或提示文件不存在

确认 PDF 已生成，并确认 AstrBot 能读取 `<download_dir>/<用户ID>/pdfs/`。Docker 用户还需要确认 `/AstrBot/data` 持久化挂载已实际应用。使用 `after_send` 时可适当增大 `after_send_delay`。

### PDF 没有生成

检查日志中是否有 Pillow 或 img2pdf 错误，并确认容器内有足够磁盘空间。

## 文件结构

```text
astrbot_plugin_jmcomics/
├── assets/
├── _conf_schema.json
├── main.py
├── metadata.yaml
├── requirements.txt
└── README.md
```

## 许可证与致谢

本项目采用 [AGPL-3.0](LICENSE) 许可证。

- [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)
- [AstrBot](https://github.com/AstrBotDevs/AstrBot)

