#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小自测：不依赖 AstrBot / jmcomic 安装，用桩模块验证纯逻辑。

运行：python tests/test_core.py
"""

import io
import sys
import types
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ----------------------------------------------------------------------
# 桩模块
# ----------------------------------------------------------------------
class _Star:
    def __init__(self, context=None):
        self.context = context


class _Plain:
    def __init__(self, text=""):
        self.text = text


class _File:
    def __init__(self, file="", name=""):
        self.file = file
        self.name = name


class _Image:
    @staticmethod
    def fromFileSystem(path):
        return path


class _Filter:
    @staticmethod
    def command(*args, **kwargs):
        def decorator(func):
            return func

        return decorator


def _install_stubs():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    import logging

    api.logger = logging.getLogger("test")

    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = _Filter()

    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = _Plain
    components.File = _File
    components.Image = _Image

    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = _Star
    star.register = lambda *a, **k: (lambda cls: cls)

    jm = types.ModuleType("jmcomic")
    jm.JmAlbumDetail = object
    jm.JmModuleConfig = types.SimpleNamespace(
        disable_jm_log=lambda: None, get_html_domain=lambda: "example.com"
    )
    jm.JmOption = types.SimpleNamespace(default=staticmethod(lambda: None))
    jm.JmcomicException = type("JmcomicException", (Exception,), {})
    jm.MissingAlbumPhotoException = type("MissingAlbumPhotoException", (Exception,), {})
    jm.RequestRetryAllFailException = type("RequestRetryAllFailException", (Exception,), {})
    jm.DirRule = object
    jm.JmMagicConstants = types.SimpleNamespace(
        ORDER_BY_LATEST="latest", TIME_ALL="all", CATEGORY_ALL="all"
    )
    jm.create_option_by_file = lambda path: None
    jm.download_album = lambda *a, **k: None

    jm_downloader = types.ModuleType("jmcomic.jm_downloader")
    jm_downloader.JmDownloader = object
    jm.jm_downloader = jm_downloader

    fake = types.ModuleType("img2pdf")
    fake.LAST_PATHS = []

    def _convert(paths, outputstream=None, **kwargs):
        fake.LAST_PATHS = list(paths)
        payload = b"%PDF-1.3 fake\n" + b"\n".join(Path(p).name.encode() for p in paths)
        outputstream.write(payload)
        return payload

    fake.convert = _convert

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.message_components": components,
        "astrbot.api.star": star,
        "jmcomic": jm,
        "jmcomic.jm_downloader": jm_downloader,
        "img2pdf": fake,
    }
    sys.modules.update(modules)
    return fake


fake_img2pdf = _install_stubs()

from PIL import Image  # noqa: E402

import main as plugin_module  # noqa: E402


def build_plugin(tmp_path: Path) -> "plugin_module.JmComicPlugin":
    return plugin_module.JmComicPlugin(
        context=None,
        config={
            "download_dir": str(tmp_path / "downloads"),
            "max_concurrent": 1,
            "cooldown_seconds": 0,
            "max_file_size_mb": 90,
            "pdf_cache_max": 3,
        },
    )


def test_natural_sort(tmp_path: Path):
    plugin = build_plugin(tmp_path)
    names = ["10.jpg", "2.jpg", "1.jpg", "20.jpg"]
    files = sorted((tmp_path / name for name in names), key=plugin._natural_key)
    assert [f.name for f in files] == ["1.jpg", "2.jpg", "10.jpg", "20.jpg"], files


def test_parse_album_command(tmp_path: Path):
    plugin = build_plugin(tmp_path)
    parse = plugin._parse_album_command

    album_id, chapter_range, extra = parse(["/jm", "download", "123"], 2)
    assert (album_id, chapter_range, extra) == ("123", None, {})

    album_id, chapter_range, extra = parse(
        ["/jm", "download", "123", "1-5", "--quality=70", "--max-size", "1600"], 2
    )
    assert album_id == "123"
    assert chapter_range == (1, 5)
    assert extra == {"quality": "70", "max-size": "1600"}

    assert parse(["/jmz", "42", "3"], 1)[1] == (3, 3)

    for bad in (
        ["/jm", "download"],
        ["/jm", "download", "abc"],
        ["/jm", "download", "1", "0-3"],
        ["/jm", "download", "1", "5-2"],
        ["/jm", "download", "1", "1-x"],
        ["/jm", "download", "1", "--quality"],
        ["/jm", "download", "1", "--unknown=1"],
        ["/jm", "download", "1", "1", "2"],
    ):
        try:
            parse(bad, 2)
        except ValueError:
            continue
        raise AssertionError(f"应当拒绝: {bad}")


def test_parse_int(_tmp_path: Path):
    parse_int = plugin_module.JmComicPlugin._parse_int
    assert parse_int("80", "quality", 1, 100) == 80
    for bad in ("abc", "0", "101", ""):
        try:
            parse_int(bad, "quality", 1, 100)
        except ValueError:
            continue
        raise AssertionError(f"应当拒绝: {bad}")


def test_parse_keyword_and_ranking(_tmp_path: Path):
    keyword, page = plugin_module.JmComicPlugin._parse_keyword_args("/jms 巨乳 人妻 2")
    assert (keyword, page) == ("巨乳 人妻", 2)

    keyword, page = plugin_module.JmComicPlugin._parse_keyword_args("/jms 单行关键词")
    assert (keyword, page) == ("单行关键词", 1)

    assert plugin_module.JmComicPlugin._parse_ranking_args("/jmr 2") == ("month", 2)
    assert plugin_module.JmComicPlugin._parse_ranking_args("/jmr week 2") == ("week", 2)
    assert plugin_module.JmComicPlugin._parse_ranking_args("/jmr 2 day") == ("day", 2)

    for bad in ("/jmr week 0", "/jmr week 101", "/jmr foo"):
        try:
            plugin_module.JmComicPlugin._parse_ranking_args(bad)
        except ValueError:
            continue
        raise AssertionError(f"应当拒绝: {bad}")


def test_pdf_paths(tmp_path: Path):
    plugin = build_plugin(tmp_path)
    cached, cacheable = plugin._pdf_output_path("u1", "123", (1, 5), 80, 1600)
    assert cacheable
    assert cached.parent == plugin.pdf_cache_dir
    assert cached.name == "123_1-5_q80_s1600.pdf"

    plugin.pdf_cache_max = 0
    uncached, cacheable = plugin._pdf_output_path("u1", "123", None, 80, 0)
    assert not cacheable
    assert uncached.parent == plugin._user_base_dir("u1") / "pdfs"


def test_album_locks_are_reused_without_unbounded_growth(tmp_path: Path):
    plugin = build_plugin(tmp_path)
    first = plugin._get_album_lock("same-cache-key")
    assert plugin._get_album_lock("same-cache-key") is first
    # 释放引用后，弱引用锁表允许长期运行时回收历史 key。
    del first
    import gc

    gc.collect()
    assert "same-cache-key" not in plugin._album_locks


def test_common_album_root(tmp_path: Path):
    plugin = build_plugin(tmp_path)
    user_dir = plugin._user_base_dir("u1", create=True)
    assert plugin._common_album_root([user_dir], user_dir) is None
    album = user_dir / "123"
    chapter_one = album / "001"
    chapter_two = album / "002"
    chapter_one.mkdir(parents=True)
    chapter_two.mkdir(parents=True)
    assert plugin._common_album_root([chapter_one, chapter_two], user_dir) == album
    assert plugin._common_album_root([chapter_one], user_dir) == chapter_one
    outside = tmp_path / "outside"
    outside.mkdir()
    assert plugin._common_album_root([outside], user_dir) is None


class _FakeOption:
    def __init__(self, mapping):
        self.mapping = mapping

    def decide_image_save_dir(self, photo):
        return str(self.mapping[photo])


def test_collect_images_follows_chapter_order(tmp_path: Path):
    plugin = build_plugin(tmp_path)
    user_dir = plugin._user_base_dir("u1", create=True)
    chapter_one = user_dir / "123" / "001"
    chapter_two = user_dir / "123" / "002"
    chapter_one.mkdir(parents=True)
    chapter_two.mkdir(parents=True)
    for name in ("1.jpg", "10.jpg", "2.jpg"):
        (chapter_two / name).write_bytes(b"x")
    (chapter_one / "1.jpg").write_bytes(b"x")
    (chapter_one / "3.jpg").write_bytes(b"x")
    (chapter_one / "note.txt").write_text("x")

    photos = ["p1", "p2"]
    option = _FakeOption({"p1": chapter_one, "p2": chapter_two})
    files, dirs = plugin._collect_album_images(option, photos)
    assert dirs == [chapter_one, chapter_two]
    assert [f.name for f in files] == ["1.jpg", "3.jpg", "1.jpg", "2.jpg", "10.jpg"]
    assert files[0].parent == chapter_one


def test_fallback_is_conservative(tmp_path: Path):
    plugin = build_plugin(tmp_path)
    user_dir = plugin._user_base_dir("u1", create=True)

    single = user_dir / "album_777"
    single.mkdir()
    (single / "1.jpg").write_bytes(b"x")
    assert plugin._fallback_album_dir("u1", "777") == single.resolve()

    second = user_dir / "777_copy"
    second.mkdir()
    (second / "1.jpg").write_bytes(b"x")
    assert plugin._fallback_album_dir("u1", "777") is None


def test_flatten_alpha(tmp_path: Path):
    plugin = build_plugin(tmp_path)
    rgba = Image.new("RGBA", (2, 2), (0, 0, 0, 0))
    flattened = plugin._flatten_for_pdf(rgba)
    assert flattened.mode == "RGB"
    assert flattened.getpixel((0, 0)) == (255, 255, 255)


def test_render_pdf_order_and_resize(tmp_path: Path):
    plugin = build_plugin(tmp_path)
    source = tmp_path / "images"
    source.mkdir()
    for name, size in (("1.jpg", (40, 30)), ("2.png", (400, 200)), ("10.jpg", (30, 40))):
        Image.new("RGB", size, (120, 30, 200)).save(source / name)

    files = sorted(source.iterdir(), key=plugin._natural_key)
    output = tmp_path / "out.pdf"
    ok, error = plugin._render_pdf(files, output, quality=80, max_size=100)
    assert ok, error
    assert output.read_bytes().startswith(b"%PDF")
    assert [Path(p).name for p in fake_img2pdf.LAST_PATHS] == ["00000.jpg", "00001.jpg", "00002.jpg"]
    assert len(fake_img2pdf.LAST_PATHS) == 3


def test_write_zip(tmp_path: Path):
    plugin = build_plugin(tmp_path)
    album = tmp_path / "downloads" / "u1" / "123"
    chapter = album / "001"
    chapter.mkdir(parents=True)
    for name in ("1.jpg", "10.jpg", "2.jpg"):
        (chapter / name).write_bytes(b"x")
    (chapter / "note.txt").write_text("x")

    zip_path = album.parent / "123.zip"
    count = plugin._write_zip(album.parent, [album], zip_path)
    assert count == 3
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    assert names == ["123/001/1.jpg", "123/001/2.jpg", "123/001/10.jpg"], names


def test_delete_refuses_outside(tmp_path: Path):
    plugin = build_plugin(tmp_path)
    user_dir = plugin._user_base_dir("u1", create=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    inside = user_dir / "123"
    inside.mkdir()
    plugin._delete_paths_safely(user_dir, [outside, inside])
    assert outside.exists()
    assert not inside.exists()


def test_rand_parsing_and_history(_tmp_path: Path):
    split = plugin_module.JmComicPlugin._split_subcommand
    assert split("/jm rand") == ("rand", [])
    assert split("/jm random week") == ("rand", ["week"])
    assert split("/jm 随机") == ("rand", [])
    assert split("/jm 推荐 巨乳") == ("rand", ["巨乳"])

    parse = plugin_module.JmComicPlugin._parse_rand_tokens
    assert parse([]) == ("day", "")
    assert parse(["week"]) == ("week", "")
    assert parse(["本月"]) == ("month", "")
    assert parse(["week", "巨乳"]) == ("week", "巨乳")
    assert parse(["巨乳", "人妻"]) == ("day", "巨乳 人妻")
    try:
        parse(["x" * 41])
    except ValueError:
        pass
    else:
        raise AssertionError("超长关键词应报错")


def test_sent_albums_bookkeeping(tmp_path: Path):
    plugin = build_plugin(tmp_path)
    plugin._mark_album_sent("123")
    plugin._mark_album_sent("not-a-number")
    assert "123" in plugin._sent_albums
    assert "not-a-number" not in plugin._sent_albums

    # 记录应当落盘，重建插件后仍能读到
    reloaded = build_plugin(tmp_path)
    assert "123" in reloaded._sent_albums

    # 超过 1000 条时淘汰最旧的记录
    plugin._sent_albums["123"] = 0.0  # 造一个最旧的记录
    for index in range(1005):
        plugin._sent_albums[str(10000 + index)] = float(index + 1)
    plugin._mark_album_sent("999999")
    assert len(plugin._sent_albums) == 1000
    assert "123" not in plugin._sent_albums


def test_do_rand_skips_sent(tmp_path: Path):
    import asyncio as _asyncio
    import types as _types

    plugin = build_plugin(tmp_path)
    plugin._mark_album_sent("111")

    class _Client:
        @staticmethod
        def day_ranking(page=1):
            return _types.SimpleNamespace(
                content=[("111", "已发过的"), ("222", "新的")], total=2, page_count=1
            )

    async def _get_option(user_id=None):
        client = _Client()
        return _types.SimpleNamespace(build_jm_client=lambda: client)

    sent_texts = []
    details = []

    async def _safe_send(event, text):
        sent_texts.append(text)

    async def _do_detail(event, album_id):
        details.append(album_id)

    plugin._get_option = _get_option
    plugin._safe_send = _safe_send
    plugin._do_detail = _do_detail

    _asyncio.run(plugin._do_rand(_FakeEvent(), "day", ""))

    assert details == ["222"], details
    assert "222" in plugin._sent_albums
    assert "111" not in sent_texts


def test_split_subcommand(_tmp_path: Path):
    split = plugin_module.JmComicPlugin._split_subcommand
    assert split("/jm 123") == ("download", ["123"])
    assert split("/jm download 123 1-5") == ("download", ["123", "1-5"])
    assert split("/jm z 123") == ("zip", ["123"])
    assert split("/jm zip 123") == ("zip", ["123"])
    assert split("/jm stop") == ("stop", [])
    assert split("/jm clean") == ("clean", [])
    assert split("/jm d 123") == ("detail", ["123"])
    assert split("/jm s 巨乳 人妻 2") == ("search", ["巨乳", "人妻", "2"])
    assert split("/jm r week 2") == ("rank", ["week", "2"])
    assert split("/jm") == ("help", [])
    assert split("/jm blah") == ("help", ["blah"])
    # 某些平台会把命令前缀剥掉再交给 handler
    assert split("123") == ("download", ["123"])
    assert split("stop") == ("stop", [])


def test_cleanup_expired(tmp_path: Path):
    import os as _os
    import time as _time

    plugin = build_plugin(tmp_path)
    plugin.cover_keep_days = 0
    user_dir = plugin._user_base_dir("u1", create=True)
    old_album = user_dir / "123"
    old_album.mkdir()
    (old_album / "1.jpg").write_bytes(b"x")
    old_zip = user_dir / "123.zip"
    old_zip.write_bytes(b"x")
    pdf_dir = user_dir / "pdfs"
    pdf_dir.mkdir()
    old_pdf = pdf_dir / "123.pdf"
    old_pdf.write_bytes(b"x")
    fresh = user_dir / "456"
    fresh.mkdir()
    (fresh / "1.jpg").write_bytes(b"x")
    cache_dir = user_dir / "cache"
    cache_dir.mkdir()
    keep = cache_dir / "keep.bin"
    keep.write_bytes(b"x")
    cover_dir = plugin.cover_dir
    cover = cover_dir / "cover_123.jpg"
    cover.write_bytes(b"x")

    old_time = _time.time() - 10 * 86400
    for path in (old_album / "1.jpg", old_zip, old_pdf, keep, cover):
        _os.utime(path, (old_time, old_time))
        _os.utime(path.parent, (old_time, old_time))

    removed = plugin._cleanup_expired(7)
    assert removed >= 3, removed
    assert not old_album.exists()
    assert not old_zip.exists()
    assert not old_pdf.exists()
    assert fresh.exists()
    assert keep.exists()
    assert cover.exists(), "auto cleanup must leave cover retention to cover_keep_days"
    assert plugin._cleanup_expired(0) == 0


class _FakeEvent:
    def __init__(self, user="u1", message=""):
        self.user = user
        self.message_str = message
        self.sent = []

    def get_sender_id(self):
        return self.user

    def plain_result(self, text):
        self.sent.append(text)
        return _Plain(text)

    async def send(self, result):
        self.sent.append(getattr(result, "text", result))


def test_progress_reporter_start(tmp_path: Path):
    import asyncio

    plugin = build_plugin(tmp_path)
    event = _FakeEvent()

    class _Photo:
        def __init__(self):
            self.count = 2

        def __len__(self):
            return self.count

    class _Album:
        title = "测试本子"

        def __init__(self):
            self.photos = [_Photo(), _Photo()]

        def __len__(self):
            return len(self.photos)

        def __getitem__(self, index):
            return self.photos[index]

    async def scenario():
        reporter = plugin_module._ProgressReporter(plugin, event, "123", None, 1)
        reporter.start_album(_Album())
        await asyncio.sleep(0.05)
        return reporter

    reporter = asyncio.run(scenario())
    assert reporter.total_photos == 2
    assert reporter.total_images == 4
    assert event.sent and "测试本子" in event.sent[0]
    assert "2 章 / 4 页" in event.sent[0]


def test_stop_without_job(tmp_path: Path):
    import asyncio

    plugin = build_plugin(tmp_path)
    event = _FakeEvent(user="u1")

    results = asyncio.run(plugin._accept_stop(event, []))
    assert results and "当前没有进行中的下载任务" in results[0].text

    plugin._user_jobs["u1"] = 1
    results = asyncio.run(plugin._accept_stop(event, []))
    assert "已取消排队中的下载任务" in results[0].text
    assert "u1" in plugin._cancel_requests

    plugin._user_jobs.clear()
    plugin._cancel_requests.clear()
    job = {"album_id": "123", "cancel": __import__("threading").Event(), "control": None}
    plugin._active_jobs["u1"] = job
    results = asyncio.run(plugin._accept_stop(event, []))
    assert "已请求停止" in results[0].text
    assert job["cancel"].is_set()


def main() -> int:
    import shutil
    import os
    import time

    base = Path(__file__).resolve().parent / f".tmp_{os.getpid()}_{time.time_ns()}"
    base.mkdir(parents=True, exist_ok=True)

    tests = [
        test_natural_sort,
        test_parse_album_command,
        test_parse_int,
        test_parse_keyword_and_ranking,
        test_pdf_paths,
        test_album_locks_are_reused_without_unbounded_growth,
        test_common_album_root,
        test_collect_images_follows_chapter_order,
        test_fallback_is_conservative,
        test_flatten_alpha,
        test_render_pdf_order_and_resize,
        test_write_zip,
        test_delete_refuses_outside,
        test_split_subcommand,
        test_rand_parsing_and_history,
        test_do_rand_skips_sent,
        test_sent_albums_bookkeeping,
        test_cleanup_expired,
        test_progress_reporter_start,
        test_stop_without_job,
    ]
    for index, test in enumerate(tests):
        case_dir = base / f"{index:02d}_{test.__name__}"
        case_dir.mkdir()
        test(case_dir)
        print(f"ok  {test.__name__}")
    shutil.rmtree(base, ignore_errors=True)
    print(f"\n{len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
