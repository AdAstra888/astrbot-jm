# -*- coding: utf-8 -*-
"""PicACG（哔咔）支持子包。

这里刻意不做任何导入：`client.py` 依赖 aiohttp，而插件主体（JM 部分）不依赖它。
需要客户端时由 `pica.plugin` 在函数内按需导入，缺依赖时只让哔咔命令报错，
不影响 JM 命令加载。
"""
