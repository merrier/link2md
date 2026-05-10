import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import link2md  # noqa: E402


REAL_WORLD_SHARE_TEXTS = {
    "xhs_obsidian": (
        "绝美 Obsidian 主题｜兼具简洁与美貌 @数码薯 @科技薯 "
        "http://xhslink.com/o/cxhNl268RX 先复制这段，去【小红书】看看有多精彩~"
    ),
    "xhs_space": (
        "商业航天唯一性十家公司 不管是整星制造、核心芯片，... "
        "http://xhslink.com/o/wbDb6gt0Dq 把这段复制好，然后去【小红书】就能看笔记。"
    ),
    "douyin_ai": (
        "4.64 复制打开抖音，看看【珍妮丁丁说AI的作品】用codex跑通自媒体运营全流程！运营太烦了！ "
        "这... https://v.douyin.com/M72tTO-c6Ro/ :6pm KWM:/ y@G.vs 07/18"
    ),
    "bilibili_lego": "【全网独家，乐高MOC入门完全指南-哔哩哔哩】 https://b23.tv/K0av0B0",
}


class Link2MdTests(unittest.TestCase):
    def test_extract_url_from_share_text(self):
        text = "这个视频不错 https://v.douyin.com/abc123/ 复制打开"

        self.assertEqual(link2md.extract_url(text), "https://v.douyin.com/abc123/")

    def test_real_world_share_text_url_extraction(self):
        cases = {
            "xhs_obsidian": ("http://xhslink.com/o/cxhNl268RX", "xiaohongshu"),
            "xhs_space": ("http://xhslink.com/o/wbDb6gt0Dq", "xiaohongshu"),
            "douyin_ai": ("https://v.douyin.com/M72tTO-c6Ro/", "douyin"),
            "bilibili_lego": ("https://b23.tv/K0av0B0", "bilibili"),
        }

        for name, (url, platform) in cases.items():
            with self.subTest(name=name):
                extracted = link2md.extract_url(REAL_WORLD_SHARE_TEXTS[name])
                self.assertEqual(extracted, url)
                self.assertEqual(link2md.detect_platform(extracted), platform)

    def test_detect_supported_platforms(self):
        cases = {
            "https://www.bilibili.com/video/BV123": "bilibili",
            "https://b23.tv/abc": "bilibili",
            "https://www.douyin.com/video/123": "douyin",
            "https://v.douyin.com/abc": "douyin",
            "https://www.xiaohongshu.com/explore/123": "xiaohongshu",
            "https://xhslink.com/a/abc": "xiaohongshu",
        }

        for url, platform in cases.items():
            with self.subTest(url=url):
                self.assertEqual(link2md.detect_platform(url), platform)

    def test_bilibili_initial_state_to_markdown(self):
        html = """
        <html>
          <head><title>fallback</title></head>
          <script>
            window.__INITIAL_STATE__ = {
              "videoData": {
                "title": "B站标题",
                "desc": "视频简介",
                "pubdate": 1710000000,
                "pic": "https://example.com/cover.jpg",
                "bvid": "BV1xx",
                "cid": 123,
                "owner": {"name": "作者A"}
              },
              "tags": [{"tag_name": "Python"}, {"tag_name": "工具"}]
            };
          </script>
        </html>
        """
        response = link2md.HttpResponse(
            url="https://www.bilibili.com/video/BV1xx",
            status=200,
            content_type="text/html",
            text=html,
        )

        with mock.patch.object(link2md, "fetch_bilibili_subtitles", return_value=[]):
            item = link2md.parse_bilibili("https://b23.tv/short", response, "")

        markdown = link2md.render_markdown(item)
        self.assertIn("# B站标题", markdown)
        self.assertIn("- 平台：bilibili", markdown)
        self.assertIn("- 作者：作者A", markdown)
        self.assertIn("- 标签：Python, 工具", markdown)
        self.assertIn("视频简介", markdown)

    def test_douyin_render_data(self):
        encoded = link2md.urllib.parse.quote(
            '{"aweme":{"desc":"抖音文案","createTime":1710000000,'
            '"author":{"nickname":"作者B"},"textExtra":[{"hashtagName":"旅行"}]}}'
        )
        html = f'<script id="RENDER_DATA" type="application/json">{encoded}</script>'
        response = link2md.HttpResponse(
            url="https://www.douyin.com/video/1",
            status=200,
            content_type="text/html",
            text=html,
        )

        item = link2md.parse_douyin("https://v.douyin.com/abc", response)

        self.assertEqual(item.title, "抖音文案")
        self.assertEqual(item.author, "作者B")
        self.assertEqual(item.tags, ["旅行"])

    def test_douyin_render_data_video_url(self):
        encoded = link2md.urllib.parse.quote(
            '{"aweme":{"desc":"抖音文案","video":{"playAddr":{"urlList":'
            '["https://example.com/video/play.mp4"]}}}}'
        )
        html = f'<script id="RENDER_DATA" type="application/json">{encoded}</script>'
        response = link2md.HttpResponse(
            url="https://www.douyin.com/video/1",
            status=200,
            content_type="text/html",
            text=html,
        )

        item = link2md.parse_douyin("https://v.douyin.com/abc", response)

        self.assertEqual(item.video_url, "https://example.com/video/play.mp4")

    def test_xiaohongshu_initial_state(self):
        html = """
        <script>
          window.__INITIAL_STATE__ = {
            "note": {
              "displayTitle": "小红书标题",
              "desc": "小红书正文",
              "time": 1710000000000,
              "user": {"nickname": "作者C"},
              "tagList": [{"tagName": "探店"}],
              "imageList": [{"url": "https://example.com/xhs.jpg"}]
            }
          };
        </script>
        """
        response = link2md.HttpResponse(
            url="https://www.xiaohongshu.com/explore/1",
            status=200,
            content_type="text/html",
            text=html,
        )

        item = link2md.parse_xiaohongshu("https://xhslink.com/a/abc", response)
        markdown = link2md.render_markdown(item)

        self.assertEqual(item.title, "小红书标题")
        self.assertEqual(item.author, "作者C")
        self.assertEqual(item.description, "小红书正文")
        self.assertEqual(item.tags, ["探店"])
        self.assertEqual(item.images, ["https://example.com/xhs.jpg"])
        self.assertIn("![image](https://example.com/xhs.jpg)", markdown)

    def test_video_transcript_warning_without_command(self):
        item = link2md.ContentItem(
            source_url="https://example.com/source",
            final_url="https://example.com/source",
            platform="xiaohongshu",
            video_url="https://example.com/video.mp4",
            is_video=True,
        )

        with mock.patch.dict(link2md.os.environ, {}, clear=True):
            link2md.add_video_transcript(item)

        self.assertFalse(item.transcript)
        self.assertIn("未配置转写 provider", item.warnings[0])

    def test_local_transcriber_with_command(self):
        transcriber = link2md.LocalCommandTranscriber("fake-transcribe {audio}")

        with mock.patch.object(link2md, "run_transcribe_command", return_value="第一句\n第二句"):
            rows = transcriber.transcribe(Path("/tmp/audio.wav"))

        self.assertEqual(rows, [("", "", "第一句"), ("", "", "第二句")])

    def test_build_transcriber_auto_prefers_local_command(self):
        transcriber = link2md.build_transcriber(provider="auto", transcribe_cmd="fake {audio}")

        self.assertIsInstance(transcriber, link2md.LocalCommandTranscriber)

    def test_build_transcriber_volcengine_requires_env(self):
        with mock.patch.dict(link2md.os.environ, {}, clear=True):
            with self.assertRaises(link2md.Link2MdError) as ctx:
                link2md.build_transcriber(provider="volcengine")

        self.assertIn("火山 AUC 转写缺少环境变量", str(ctx.exception))

    def test_video_transcript_warning_without_video_url(self):
        item = link2md.ContentItem(
            source_url="https://v.douyin.com/abc",
            final_url="https://www.douyin.com/video/1",
            platform="douyin",
            is_video=True,
        )

        link2md.add_video_transcript(item, transcriber="local", transcribe_cmd="fake {audio}")

        self.assertFalse(item.transcript)
        self.assertIn("未找到可下载视频地址", item.warnings[0])

    def test_video_transcript_with_command(self):
        item = link2md.ContentItem(
            source_url="https://example.com/source",
            final_url="https://example.com/source",
            platform="douyin",
            video_url="https://example.com/video.mp4",
            is_video=True,
        )

        with mock.patch.object(link2md, "download_video", return_value=Path("/tmp/video.mp4")):
            with mock.patch.object(link2md, "extract_audio"):
                with mock.patch.object(link2md, "run_transcribe_command", return_value="第一句\n第二句"):
                    link2md.add_video_transcript(item, transcribe_cmd="fake-transcribe {audio}", transcriber="local")

        self.assertEqual(item.transcript, [("", "", "第一句"), ("", "", "第二句")])

    def test_video_transcript_with_browser_capture(self):
        item = link2md.ContentItem(
            source_url="https://v.douyin.com/abc",
            final_url="https://www.douyin.com/video/1",
            platform="douyin",
            is_video=True,
        )

        with mock.patch.object(link2md.BrowserAudioCapture, "capture", return_value=Path("/tmp/capture.webm")) as capture:
            with mock.patch.object(link2md, "convert_audio_to_wav") as convert:
                with mock.patch.object(link2md, "run_transcribe_command", return_value="浏览器录音文本"):
                    link2md.add_video_transcript(
                        item,
                        transcriber="local",
                        transcribe_cmd="fake {audio}",
                        video_capture="browser",
                        browser_capture_seconds=30,
                    )

        capture.assert_called_once()
        convert.assert_called_once()
        self.assertEqual(item.transcript, [("", "", "浏览器录音文本")])

    def test_parse_content_passes_video_capture_options(self):
        response = link2md.HttpResponse(
            url="https://www.douyin.com/video/1",
            status=200,
            content_type="text/html",
            text="<html></html>",
        )

        with mock.patch.object(link2md, "http_get", return_value=response):
            with mock.patch.object(link2md, "add_video_transcript") as add_transcript:
                link2md.parse_content(
                    "https://v.douyin.com/abc",
                    transcribe_cmd="fake {audio}",
                    transcriber="local",
                    video_capture="browser",
                    browser_capture_seconds=12,
                )

        _, kwargs = add_transcript.call_args
        self.assertEqual(kwargs["video_capture"], "browser")
        self.assertEqual(kwargs["browser_capture_seconds"], 12)

    def test_volcengine_utterances_to_rows(self):
        rows = link2md.volcengine_utterances_to_rows(
            [
                {"start_time": 1000, "end_time": 2500, "text": "第一句"},
                {"start_time": 2500, "end_time": 4000, "text": "第二句"},
            ]
        )

        self.assertEqual(rows, [("00:01", "00:02", "第一句"), ("00:02", "00:04", "第二句")])

    def test_volcengine_transcriber_submit_and_poll(self):
        transcriber = link2md.VolcengineAUCTranscriber(
            app_id="app",
            access_token="token",
            cluster_id="cluster",
            audio_url_command="upload {audio}",
            poll_interval=0,
        )

        responses = [
            {"resp": {"message": "success", "id": "task-1"}},
            {"resp": {"code": 2000}},
            {"resp": {"code": 1000, "utterances": [{"start_time": 0, "end_time": 1200, "text": "完成"}]}},
        ]
        with mock.patch.object(link2md, "run_shell_command", return_value="https://example.com/audio.wav"):
            with mock.patch.object(link2md, "http_post_json", side_effect=responses):
                rows = transcriber.transcribe(Path("/tmp/audio.wav"))

        self.assertEqual(rows, [("00:00", "00:01", "完成")])

    def test_fail_soft_fallback_markdown(self):
        item = link2md.build_fallback("see https://xhslink.com/a/abc", RuntimeError("blocked"))
        markdown = link2md.render_markdown(item)

        self.assertIn("# 链接内容抓取失败", markdown)
        self.assertIn("- 平台：xiaohongshu", markdown)
        self.assertIn("blocked", markdown)

    def test_main_fail_soft_writes_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "failed.md"
            with mock.patch.object(link2md, "parse_content", side_effect=RuntimeError("blocked")):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = link2md.main(["https://v.douyin.com/abc", "--fail-soft", "-o", str(output)])

            self.assertEqual(code, 0)
            markdown = output.read_text(encoding="utf-8")
            self.assertIn("# 链接内容抓取失败", markdown)
            self.assertIn("- 平台：douyin", markdown)
            self.assertIn("blocked", markdown)

    def test_main_writes_output_file(self):
        item = link2md.ContentItem(
            source_url="https://www.bilibili.com/video/BV123",
            final_url="https://www.bilibili.com/video/BV123",
            platform="bilibili",
            title="输出标题",
            description="正文",
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "note.md"
            with mock.patch.object(link2md, "parse_content", return_value=item):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = link2md.main(["https://www.bilibili.com/video/BV123", "-o", str(output)])

            self.assertEqual(code, 0)
            self.assertIn("# 输出标题", output.read_text(encoding="utf-8"))

    def test_main_writes_output_directory(self):
        item = link2md.ContentItem(
            source_url="https://www.xiaohongshu.com/explore/123",
            final_url="https://www.xiaohongshu.com/explore/123",
            platform="xiaohongshu",
            title="输出 标题/带符号",
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "notes"
            with mock.patch.object(link2md, "parse_content", return_value=item):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = link2md.main(["https://www.xiaohongshu.com/explore/123", "-o", str(output_dir) + "/"])

            output = output_dir / "输出-标题-带符号.md"
            self.assertEqual(code, 0)
            self.assertTrue(output.exists())
            self.assertIn("# 输出 标题/带符号", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
