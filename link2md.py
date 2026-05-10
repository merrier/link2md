#!/usr/bin/env python3
"""Convert public social/video links into a Markdown note.

The scraper deliberately uses only the Python standard library. Platform pages
change often, so extraction is layered: platform-specific JSON first, then
generic OpenGraph/meta tags, then a useful fallback document.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as dt
import gzip
import hashlib
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

SHORT_DOMAINS = {
    "b23.tv",
    "bili2233.cn",
    "v.douyin.com",
    "www.iesdouyin.com",
    "xhslink.com",
    "xhs.cn",
}

TRANSCRIBE_COMMAND_ENV = "LINK2MD_TRANSCRIBE_CMD"
TRANSCRIBER_ENV = "LINK2MD_TRANSCRIBER"
VIDEO_CAPTURE_ENV = "LINK2MD_VIDEO_CAPTURE"
BROWSER_CAPTURE_SECONDS_ENV = "LINK2MD_BROWSER_CAPTURE_SECONDS"
VOLCENGINE_APP_ID_ENV = "LINK2MD_VOLCENGINE_APP_ID"
VOLCENGINE_ACCESS_TOKEN_ENV = "LINK2MD_VOLCENGINE_ACCESS_TOKEN"
VOLCENGINE_CLUSTER_ID_ENV = "LINK2MD_VOLCENGINE_CLUSTER_ID"
VOLCENGINE_AUDIO_URL_CMD_ENV = "LINK2MD_VOLCENGINE_AUDIO_URL_CMD"
VOLCENGINE_POLL_ATTEMPTS_ENV = "LINK2MD_VOLCENGINE_POLL_ATTEMPTS"
VOLCENGINE_POLL_INTERVAL_ENV = "LINK2MD_VOLCENGINE_POLL_INTERVAL"
VOLCENGINE_SUCCESS_CODE = 1000
VOLCENGINE_RUNNING_CODES = {2000, 2001}
VIDEO_KEYS = {
    "video",
    "video_url",
    "videoUrl",
    "playAddr",
    "play_addr",
    "playApi",
    "playUrl",
    "url",
}


class Link2MdError(RuntimeError):
    pass


@dataclasses.dataclass
class HttpResponse:
    url: str
    status: int
    content_type: str
    text: str


@dataclasses.dataclass
class ContentItem:
    source_url: str
    final_url: str
    platform: str
    title: str = ""
    author: str = ""
    published_at: str = ""
    description: str = ""
    tags: List[str] = dataclasses.field(default_factory=list)
    images: List[str] = dataclasses.field(default_factory=list)
    video_url: str = ""
    is_video: bool = False
    transcript: List[Tuple[str, str, str]] = dataclasses.field(default_factory=list)
    warnings: List[str] = dataclasses.field(default_factory=list)


class MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: List[str] = []
        self.meta: Dict[str, str] = {}
        self.links: Dict[str, str] = {}
        self.scripts: List[Tuple[Dict[str, str], str]] = []
        self._in_title = False
        self._script_attrs: Optional[Dict[str, str]] = None
        self._script_text: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attr = {k.lower(): v or "" for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = attr.get("property") or attr.get("name") or attr.get("itemprop")
            content = attr.get("content")
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag == "link":
            rel = attr.get("rel")
            href = attr.get("href")
            if rel and href:
                self.links[rel] = href
        elif tag == "script":
            self._script_attrs = attr
            self._script_text = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._script_attrs is not None:
            self._script_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._script_attrs is not None:
            self.scripts.append((self._script_attrs, "".join(self._script_text)))
            self._script_attrs = None
            self._script_text = []

    @property
    def title(self) -> str:
        return normalize_space(" ".join(self.title_parts))


def normalize_space(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", html.unescape(str(value))).strip()


def first_text(*values: Any) -> str:
    for value in values:
        text = normalize_space(value)
        if text:
            return text
    return ""


def unique(values: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        normalized = normalize_space(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def extract_url(text: str) -> str:
    match = re.search(r"https?://[^\s<>\]\"']+", text)
    if not match:
        raise Link2MdError("没有找到 http/https 链接")
    return match.group(0).rstrip(".,;，。；)")


def detect_platform(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    if "bilibili.com" in host or host in {"b23.tv", "bili2233.cn"}:
        return "bilibili"
    if "douyin.com" in host or "iesdouyin.com" in host:
        return "douyin"
    if "xiaohongshu.com" in host or host in {"xhslink.com", "xhs.cn"}:
        return "xiaohongshu"
    return "unknown"


def http_get(url: str, timeout: float = 15, cookie: str = "") -> HttpResponse:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        "Accept-Encoding": "gzip",
    }
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            charset = response.headers.get_content_charset() or "utf-8"
            text = raw.decode(charset, errors="replace")
            return HttpResponse(
                url=response.geturl(),
                status=response.status,
                content_type=response.headers.get("Content-Type", ""),
                text=text,
            )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise Link2MdError(f"HTTP {exc.code}: {body[:200]}") from exc
    except urllib.error.URLError as exc:
        raise Link2MdError(f"请求失败: {exc}") from exc


def http_get_bytes(url: str, timeout: float = 60, cookie: str = "") -> bytes:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    }
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise Link2MdError(f"视频下载失败 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise Link2MdError(f"视频下载失败: {exc}") from exc


def http_post_json(url: str, data: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: float = 60) -> Any:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            text = raw.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise Link2MdError(f"接口请求失败 HTTP {exc.code}: {body_text[:200]}") from exc
    except urllib.error.URLError as exc:
        raise Link2MdError(f"接口请求失败: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise Link2MdError("接口返回不是 JSON") from exc


def parse_html(text: str) -> MetadataParser:
    parser = MetadataParser()
    parser.feed(text)
    return parser


def load_json_like(raw: str) -> Optional[Any]:
    raw = raw.strip().rstrip(";")
    raw = raw.replace(":undefined", ":null")
    raw = raw.replace(",undefined", ",null")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def find_balanced_object(text: str, marker: str) -> Optional[str]:
    start = text.find(marker)
    if start < 0:
        return None
    brace_start = text.find("{", start)
    if brace_start < 0:
        return None

    depth = 0
    in_string: Optional[str] = None
    escaped = False
    for index in range(brace_start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
            continue
        if char in {'"', "'"}:
            in_string = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start : index + 1]
    return None


def iter_objects(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from iter_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_objects(child)


def find_first_key(value: Any, keys: Iterable[str]) -> str:
    wanted = set(keys)
    for item in iter_objects(value):
        if isinstance(item, dict):
            for key in wanted:
                found = item.get(key)
                if isinstance(found, (str, int, float)):
                    text = normalize_space(found)
                    if text:
                        return text
    return ""


def find_all_key_values(value: Any, keys: Iterable[str], limit: int = 20) -> List[str]:
    wanted = set(keys)
    found: List[str] = []
    for item in iter_objects(value):
        if isinstance(item, dict):
            for key in wanted:
                current = item.get(key)
                if isinstance(current, str):
                    found.append(current)
                elif isinstance(current, list):
                    found.extend(str(v) for v in current if isinstance(v, (str, int, float)))
                if len(found) >= limit:
                    return unique(found)[:limit]
    return unique(found)[:limit]


def normalize_media_url(value: str) -> str:
    value = normalize_space(value)
    if value.startswith("//"):
        return "https:" + value
    return value


def is_probable_video_url(value: str) -> bool:
    value = normalize_media_url(value)
    if not value.startswith(("http://", "https://")):
        return False
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in (
            ".mp4",
            ".m3u8",
            "video",
            "play",
            "douyin",
            "ixigua",
        )
    )


def find_first_media_url(value: Any, keys: Iterable[str]) -> str:
    wanted = set(keys)
    for item in iter_objects(value):
        if not isinstance(item, dict):
            continue
        for key, current in item.items():
            if key not in wanted:
                continue
            candidates: List[str] = []
            if isinstance(current, str):
                candidates.append(current)
            elif isinstance(current, list):
                candidates.extend(str(v) for v in current if isinstance(v, (str, int, float)))
                for child in current:
                    if isinstance(child, dict):
                        candidates.extend(find_all_key_values(child, ["url", "urlList", "url_list", "src", "href"], limit=5))
            elif isinstance(current, dict):
                candidates.extend(find_all_key_values(current, ["url", "urlList", "url_list", "src", "href", "uri"], limit=10))
            for candidate in candidates:
                url = normalize_media_url(candidate)
                if is_probable_video_url(url):
                    return url
    return ""


def parse_timestamp(value: Any) -> str:
    if value is None:
        return ""
    try:
        timestamp = int(value)
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        return dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return normalize_space(value)


def extract_json_ld(parser: MetadataParser) -> Dict[str, Any]:
    for attrs, script in parser.scripts:
        if attrs.get("type", "").lower() == "application/ld+json":
            data = load_json_like(script)
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict):
                return data
    return {}


def generic_item(source_url: str, response: HttpResponse, platform: str) -> ContentItem:
    parser = parse_html(response.text)
    meta = parser.meta
    json_ld = extract_json_ld(parser)
    image = first_text(
        meta.get("og:image"),
        meta.get("twitter:image"),
        json_ld.get("image") if isinstance(json_ld.get("image"), str) else "",
    )
    author = json_ld.get("author")
    if isinstance(author, dict):
        author = author.get("name")
    video_url = first_text(meta.get("og:video"), meta.get("og:video:url"))
    item = ContentItem(
        source_url=source_url,
        final_url=response.url,
        platform=platform,
        title=first_text(meta.get("og:title"), meta.get("twitter:title"), json_ld.get("name"), parser.title),
        author=first_text(meta.get("author"), author),
        published_at=first_text(meta.get("article:published_time"), json_ld.get("datePublished")),
        description=first_text(
            meta.get("og:description"),
            meta.get("description"),
            meta.get("twitter:description"),
            json_ld.get("description"),
        ),
        images=[image] if image else [],
        video_url=video_url,
        is_video=bool(video_url),
    )
    return item


def parse_bilibili(source_url: str, response: HttpResponse, cookie: str) -> ContentItem:
    item = generic_item(source_url, response, "bilibili")
    item.is_video = True
    state_raw = find_balanced_object(response.text, "window.__INITIAL_STATE__")
    state = load_json_like(state_raw or "") if state_raw else None
    if isinstance(state, dict):
        video = state.get("videoData") or {}
        owner = video.get("owner") if isinstance(video, dict) else {}
        item.title = first_text(video.get("title"), item.title)
        item.author = first_text(owner.get("name") if isinstance(owner, dict) else "", item.author)
        item.description = first_text(video.get("desc"), item.description)
        item.published_at = first_text(parse_timestamp(video.get("pubdate")), item.published_at)
        item.images = unique([video.get("pic", ""), *item.images])
        item.tags = unique([tag.get("tag_name", "") for tag in state.get("tags", []) if isinstance(tag, dict)])

        bvid = first_text(video.get("bvid"), state.get("bvid"))
        cid = first_text(video.get("cid"))
        if bvid and cid:
            try:
                item.transcript = fetch_bilibili_subtitles(bvid, cid, cookie)
            except Link2MdError as exc:
                item.warnings.append(f"B 站字幕读取失败：{exc}")
    return item


def fetch_json(url: str, cookie: str = "") -> Any:
    response = http_get(url, cookie=cookie)
    try:
        return json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise Link2MdError("接口返回不是 JSON") from exc


def fetch_bilibili_subtitles(bvid: str, cid: str, cookie: str = "") -> List[Tuple[str, str, str]]:
    api = f"https://api.bilibili.com/x/player/v2?bvid={urllib.parse.quote(bvid)}&cid={urllib.parse.quote(cid)}"
    data = fetch_json(api, cookie)
    subtitles = (((data or {}).get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    if not subtitles:
        return []
    subtitle_url = subtitles[0].get("subtitle_url") or ""
    if subtitle_url.startswith("//"):
        subtitle_url = "https:" + subtitle_url
    if not subtitle_url:
        return []
    subtitle_data = fetch_json(subtitle_url, cookie)
    rows = []
    for row in subtitle_data.get("body", []):
        content = normalize_space(row.get("content"))
        if content:
            rows.append((format_seconds(row.get("from")), format_seconds(row.get("to")), content))
    return rows


def format_seconds(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return ""
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def parse_douyin(source_url: str, response: HttpResponse) -> ContentItem:
    item = generic_item(source_url, response, "douyin")
    item.is_video = True
    render_data = None
    parser = parse_html(response.text)
    for attrs, script in parser.scripts:
        if attrs.get("id") == "RENDER_DATA":
            decoded = urllib.parse.unquote(script)
            render_data = load_json_like(decoded)
            break
    if render_data is None:
        state_raw = find_balanced_object(response.text, "RENDER_DATA")
        render_data = load_json_like(urllib.parse.unquote(state_raw or "")) if state_raw else None
    if render_data is not None:
        item.title = first_text(find_first_key(render_data, ["desc", "caption", "title"]), item.title)
        item.author = first_text(find_first_key(render_data, ["nickname", "authorName", "name"]), item.author)
        item.published_at = first_text(parse_timestamp(find_first_key(render_data, ["createTime", "create_time"])), item.published_at)
        item.tags = find_all_key_values(render_data, ["hashtagName", "tag_name", "challengeName"])
        item.images = unique([*find_all_key_values(render_data, ["cover", "originCover", "dynamicCover"], limit=8), *item.images])
        item.video_url = first_text(item.video_url, find_first_media_url(render_data, VIDEO_KEYS))
        item.is_video = item.is_video or bool(item.video_url)
    return item


def parse_xiaohongshu(source_url: str, response: HttpResponse) -> ContentItem:
    item = generic_item(source_url, response, "xiaohongshu")
    state = None
    for marker in ["window.__INITIAL_STATE__", "__INITIAL_STATE__"]:
        state_raw = find_balanced_object(response.text, marker)
        if state_raw:
            state = load_json_like(state_raw)
            if state is not None:
                break
    if state is not None:
        item.title = first_text(find_first_key(state, ["title", "displayTitle"]), item.title)
        item.author = first_text(find_first_key(state, ["nickname", "userName", "name"]), item.author)
        item.description = first_text(find_first_key(state, ["desc", "description", "content"]), item.description)
        item.published_at = first_text(parse_timestamp(find_first_key(state, ["time", "createTime", "lastUpdateTime"])), item.published_at)
        item.tags = find_all_key_values(state, ["name", "tagName"], limit=12)
        item.images = unique([*find_all_key_values(state, ["url", "traceId"], limit=8), *item.images])
        item.video_url = first_text(item.video_url, find_first_media_url(state, VIDEO_KEYS))
        item.is_video = item.is_video or bool(item.video_url)
    item.is_video = item.is_video or "type=video" in response.url
    return item


def parse_content(
    source_text: str,
    cookie: str = "",
    transcribe_cmd: str = "",
    transcribe: bool = True,
    transcriber: str = "auto",
    video_capture: str = "none",
    browser_capture_seconds: int = 120,
) -> ContentItem:
    source_url = extract_url(source_text)
    platform = detect_platform(source_url)
    response = http_get(source_url, cookie=cookie)
    platform = detect_platform(response.url) if platform == "unknown" else platform

    if platform == "bilibili":
        item = parse_bilibili(source_url, response, cookie)
    elif platform == "douyin":
        item = parse_douyin(source_url, response)
    elif platform == "xiaohongshu":
        item = parse_xiaohongshu(source_url, response)
    else:
        item = generic_item(source_url, response, platform)

    if transcribe:
        add_video_transcript(
            item,
            cookie=cookie,
            transcribe_cmd=transcribe_cmd,
            transcriber=transcriber,
            video_capture=video_capture,
            browser_capture_seconds=browser_capture_seconds,
        )
    return item


def transcript_rows_from_text(text: str) -> List[Tuple[str, str, str]]:
    rows = []
    for line in text.splitlines():
        content = normalize_space(line)
        if content and not re.match(r"^\d+$", content) and "-->" not in content and content.upper() != "WEBVTT":
            rows.append(("", "", content))
    return rows


def download_video(video_url: str, directory: Path, cookie: str = "") -> Path:
    video_url = normalize_media_url(video_url)
    if not video_url.startswith(("http://", "https://")):
        raise Link2MdError(f"不支持的视频地址: {video_url}")
    suffix = Path(urllib.parse.urlparse(video_url).path).suffix or ".mp4"
    output = directory / f"video{suffix}"
    output.write_bytes(http_get_bytes(video_url, cookie=cookie))
    return output


def extract_audio(video_path: Path, audio_path: Path) -> None:
    if not shutil.which("ffmpeg"):
        raise Link2MdError("未找到 ffmpeg，无法从视频中提取音频")
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        str(audio_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        detail = normalize_space(result.stderr)[-300:]
        raise Link2MdError(f"ffmpeg 提取音频失败: {detail}")


def convert_audio_to_wav(input_path: Path, output_path: Path) -> None:
    if not shutil.which("ffmpeg"):
        raise Link2MdError("未找到 ffmpeg，无法转换音频")
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        detail = normalize_space(result.stderr)[-300:]
        raise Link2MdError(f"ffmpeg 转换音频失败: {detail}")


def run_shell_command(command: str, path: Path, timeout: int = 1800) -> str:
    if "{audio}" in command:
        shell_command = command.replace("{audio}", shlex.quote(str(path)))
    else:
        shell_command = f"{command} {shlex.quote(str(path))}"
    result = subprocess.run(
        shell_command,
        shell=True,
        cwd=str(path.parent),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = normalize_space(result.stderr or result.stdout)[-300:]
        raise Link2MdError(f"命令执行失败: {detail}")
    return result.stdout.strip()


def run_transcribe_command(command: str, audio_path: Path, timeout: int = 1800) -> str:
    output = run_shell_command(command, audio_path, timeout=timeout)
    if output:
        return output
    for transcript in sorted(audio_path.parent.glob("*")):
        if transcript.suffix.lower() in {".txt", ".srt", ".vtt"}:
            text = transcript.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text
    return ""


class Transcriber:
    name = "base"

    def transcribe(self, audio_path: Path) -> List[Tuple[str, str, str]]:
        raise NotImplementedError


@dataclasses.dataclass
class LocalCommandTranscriber(Transcriber):
    command: str
    timeout: int = 1800
    name: str = "local"

    def transcribe(self, audio_path: Path) -> List[Tuple[str, str, str]]:
        text = run_transcribe_command(self.command, audio_path, timeout=self.timeout)
        return transcript_rows_from_text(text)


@dataclasses.dataclass
class VolcengineAUCTranscriber(Transcriber):
    app_id: str
    access_token: str
    cluster_id: str
    audio_url_command: str
    poll_attempts: int = 60
    poll_interval: float = 3.0
    submit_url: str = "https://openspeech.bytedance.com/api/v1/auc/submit"
    query_url: str = "https://openspeech.bytedance.com/api/v1/auc/query"
    name: str = "volcengine"

    @classmethod
    def from_env(cls) -> "VolcengineAUCTranscriber":
        missing = [
            env_name
            for env_name in (
                VOLCENGINE_APP_ID_ENV,
                VOLCENGINE_ACCESS_TOKEN_ENV,
                VOLCENGINE_CLUSTER_ID_ENV,
                VOLCENGINE_AUDIO_URL_CMD_ENV,
            )
            if not os.environ.get(env_name, "").strip()
        ]
        if missing:
            raise Link2MdError(f"火山 AUC 转写缺少环境变量: {', '.join(missing)}")
        return cls(
            app_id=os.environ[VOLCENGINE_APP_ID_ENV].strip(),
            access_token=os.environ[VOLCENGINE_ACCESS_TOKEN_ENV].strip(),
            cluster_id=os.environ[VOLCENGINE_CLUSTER_ID_ENV].strip(),
            audio_url_command=os.environ[VOLCENGINE_AUDIO_URL_CMD_ENV].strip(),
            poll_attempts=int(os.environ.get(VOLCENGINE_POLL_ATTEMPTS_ENV, "60")),
            poll_interval=float(os.environ.get(VOLCENGINE_POLL_INTERVAL_ENV, "3")),
        )

    @classmethod
    def is_configured(cls) -> bool:
        return all(
            os.environ.get(env_name, "").strip()
            for env_name in (
                VOLCENGINE_APP_ID_ENV,
                VOLCENGINE_ACCESS_TOKEN_ENV,
                VOLCENGINE_CLUSTER_ID_ENV,
                VOLCENGINE_AUDIO_URL_CMD_ENV,
            )
        )

    def transcribe(self, audio_path: Path) -> List[Tuple[str, str, str]]:
        audio_url = self.upload_audio(audio_path)
        task_id = self.submit_task(audio_url)
        return self.poll_task(task_id)

    def upload_audio(self, audio_path: Path) -> str:
        output = run_shell_command(self.audio_url_command, audio_path, timeout=1800)
        audio_url = first_text(*(line for line in output.splitlines() if line.strip()))
        if not audio_url.startswith(("http://", "https://")):
            raise Link2MdError(f"音频上传命令未返回公网 URL: {audio_url}")
        return audio_url

    def submit_task(self, audio_url: str) -> str:
        data = {
            "app": {
                "appid": self.app_id,
                "token": self.access_token,
                "cluster": self.cluster_id,
            },
            "user": {"uid": generate_local_uid()},
            "audio": {"format": "wav", "url": audio_url},
            "request": {"model_name": "bigmodel", "enable_itn": True},
        }
        response = http_post_json(self.submit_url, data, headers=self.headers())
        resp = response.get("resp", {}) if isinstance(response, dict) else {}
        if resp.get("message") != "success" or not resp.get("id"):
            raise Link2MdError(f"火山 AUC 任务提交失败: {resp}")
        return str(resp["id"])

    def poll_task(self, task_id: str) -> List[Tuple[str, str, str]]:
        data = {
            "appid": self.app_id,
            "token": self.access_token,
            "cluster": self.cluster_id,
            "id": task_id,
        }
        for _ in range(self.poll_attempts):
            response = http_post_json(self.query_url, data, headers=self.headers())
            resp = response.get("resp", {}) if isinstance(response, dict) else {}
            code = resp.get("code")
            if code == VOLCENGINE_SUCCESS_CODE:
                return volcengine_utterances_to_rows(resp.get("utterances", []))
            if code in VOLCENGINE_RUNNING_CODES:
                time.sleep(self.poll_interval)
                continue
            raise Link2MdError(f"火山 AUC 转写失败: {resp}")
        raise Link2MdError(f"火山 AUC 转写超时，已轮询 {self.poll_attempts} 次")

    def headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer; {self.access_token}"}


def generate_local_uid() -> str:
    mac = uuid.getnode()
    mac_address = ":".join(("%012X" % mac)[i : i + 2] for i in range(0, 12, 2))
    return hashlib.md5(mac_address.encode("utf-8")).hexdigest()


def format_milliseconds(value: Any) -> str:
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return ""
    return format_seconds(milliseconds / 1000)


def volcengine_utterances_to_rows(utterances: Any) -> List[Tuple[str, str, str]]:
    rows = []
    if not isinstance(utterances, list):
        return rows
    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        content = normalize_space(utterance.get("text"))
        if content:
            rows.append(
                (
                    format_milliseconds(utterance.get("start_time")),
                    format_milliseconds(utterance.get("end_time")),
                    content,
                )
            )
    return rows


def build_transcriber(provider: str = "auto", transcribe_cmd: str = "") -> Optional[Transcriber]:
    provider = (provider or "auto").lower()
    command = transcribe_cmd or os.environ.get(TRANSCRIBE_COMMAND_ENV, "").strip()
    if provider == "auto":
        if command:
            return LocalCommandTranscriber(command)
        if VolcengineAUCTranscriber.is_configured():
            return VolcengineAUCTranscriber.from_env()
        return None
    if provider == "local":
        if not command:
            raise Link2MdError(f"本地转写缺少命令。请设置 {TRANSCRIBE_COMMAND_ENV} 或传入 --transcribe-cmd")
        return LocalCommandTranscriber(command)
    if provider == "volcengine":
        return VolcengineAUCTranscriber.from_env()
    raise Link2MdError(f"未知转写 provider: {provider}")


class BrowserAudioCapture:
    def __init__(self, headless: bool = True, max_seconds: int = 120) -> None:
        self.headless = headless
        self.max_seconds = max_seconds

    def capture(self, page_url: str, output_path: Path, cookie: str = "") -> Path:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise Link2MdError("未安装 Playwright。请运行 pip install 'link2md[browser]' 并安装 Chromium。") from exc

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=self.headless,
                    args=["--autoplay-policy=no-user-gesture-required"],
                )
                context_kwargs: Dict[str, Any] = {
                    "user_agent": USER_AGENT,
                    "viewport": {"width": 1280, "height": 720},
                }
                if cookie:
                    context_kwargs["extra_http_headers"] = {"Cookie": cookie}
                context = browser.new_context(**context_kwargs)
                page = context.new_page()
                page.goto(page_url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_selector("video", timeout=30_000)
                payload = page.evaluate(BROWSER_AUDIO_CAPTURE_SCRIPT, self.max_seconds * 1000)
                context.close()
                browser.close()
        except Exception as exc:
            if isinstance(exc, Link2MdError):
                raise
            raise Link2MdError(f"浏览器音频捕获失败: {exc}") from exc

        if not isinstance(payload, dict) or not payload.get("base64"):
            raise Link2MdError(f"浏览器音频捕获未返回音频: {payload}")
        output_path.write_bytes(base64.b64decode(payload["base64"]))
        return output_path


BROWSER_AUDIO_CAPTURE_SCRIPT = r"""
async (maxMs) => {
  const video = document.querySelector('video');
  if (!video) {
    throw new Error('页面中没有找到 video 元素');
  }
  video.muted = false;
  video.volume = 1;
  if (video.ended) {
    video.currentTime = 0;
  }
  if (video.readyState < 1) {
    await new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => reject(new Error('等待视频元数据超时')), 30000);
      video.addEventListener('loadedmetadata', () => {
        window.clearTimeout(timer);
        resolve();
      }, { once: true });
      video.addEventListener('error', () => {
        window.clearTimeout(timer);
        reject(new Error('视频加载失败'));
      }, { once: true });
    });
  }
  await video.play();
  const stream = video.captureStream ? video.captureStream() : (video.mozCaptureStream ? video.mozCaptureStream() : null);
  if (!stream) {
    throw new Error('当前浏览器不支持 video.captureStream');
  }
  const audioTracks = stream.getAudioTracks();
  if (!audioTracks.length) {
    throw new Error('video 元素没有可捕获的音轨');
  }
  const audioStream = new MediaStream(audioTracks);
  const mimeTypes = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'video/webm;codecs=opus',
    'video/webm'
  ];
  const mimeType = mimeTypes.find((type) => MediaRecorder.isTypeSupported(type)) || '';
  const chunks = [];
  const recorder = new MediaRecorder(audioStream, mimeType ? { mimeType } : undefined);
  recorder.ondataavailable = (event) => {
    if (event.data && event.data.size > 0) chunks.push(event.data);
  };
  const stopped = new Promise((resolve, reject) => {
    recorder.onerror = () => reject(recorder.error || new Error('MediaRecorder failed'));
    recorder.onstop = resolve;
  });
  recorder.start(1000);
  const timeout = window.setTimeout(() => {
    if (recorder.state !== 'inactive') recorder.stop();
  }, maxMs);
  video.addEventListener('ended', () => {
    if (recorder.state !== 'inactive') recorder.stop();
  }, { once: true });
  await stopped;
  window.clearTimeout(timeout);
  const blob = new Blob(chunks, { type: recorder.mimeType || mimeType || 'audio/webm' });
  if (!blob.size) {
    throw new Error('录音结果为空');
  }
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('读取录音结果失败'));
    reader.onload = () => resolve(reader.result);
    reader.readAsDataURL(blob);
  });
  return {
    mimeType: blob.type,
    base64: String(dataUrl).split(',')[1],
    bytes: blob.size
  };
}
"""


def prepare_audio_for_transcription(
    item: ContentItem,
    temp_dir: Path,
    cookie: str = "",
    video_capture: str = "none",
    browser_capture_seconds: int = 120,
) -> Path:
    audio_path = temp_dir / "audio.wav"
    if item.video_url:
        video_path = download_video(item.video_url, temp_dir, cookie=cookie)
        extract_audio(video_path, audio_path)
        return audio_path
    if video_capture == "browser":
        capture_path = temp_dir / "browser-capture.webm"
        BrowserAudioCapture(max_seconds=browser_capture_seconds).capture(item.final_url or item.source_url, capture_path, cookie=cookie)
        convert_audio_to_wav(capture_path, audio_path)
        return audio_path
    raise Link2MdError("检测到视频，但未找到可下载视频地址或平台字幕，无法自动转写。")


def add_video_transcript(
    item: ContentItem,
    cookie: str = "",
    transcribe_cmd: str = "",
    transcriber: str = "auto",
    video_capture: str = "none",
    browser_capture_seconds: int = 120,
) -> None:
    if item.transcript or not (item.is_video or item.video_url):
        return
    try:
        transcriber_instance = build_transcriber(provider=transcriber, transcribe_cmd=transcribe_cmd)
    except Link2MdError as exc:
        item.warnings.append(f"视频转写配置错误：{exc}")
        return
    if transcriber_instance is None:
        item.warnings.append(
            f"检测到视频，但未配置转写 provider。设置 {TRANSCRIBE_COMMAND_ENV}，或配置火山 AUC 环境变量后可生成视频文字稿。"
        )
        return
    try:
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            audio_path = prepare_audio_for_transcription(
                item,
                temp_dir,
                cookie=cookie,
                video_capture=video_capture,
                browser_capture_seconds=browser_capture_seconds,
            )
            rows = transcriber_instance.transcribe(audio_path)
    except Link2MdError as exc:
        item.warnings.append(f"视频转写失败：{exc}")
        return
    except subprocess.TimeoutExpired:
        item.warnings.append("视频转写失败：转写命令超时")
        return
    if rows:
        item.transcript = rows
    else:
        item.warnings.append("视频转写未返回可用文字")


def markdown_escape(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def render_markdown(item: ContentItem) -> str:
    title = item.title or "未命名内容"
    lines = [f"# {markdown_escape(title)}", ""]
    lines.extend(
        [
            f"- 平台：{item.platform}",
            f"- 原始链接：{item.source_url}",
            f"- 最终链接：{item.final_url}",
        ]
    )
    if item.author:
        lines.append(f"- 作者：{item.author}")
    if item.published_at:
        lines.append(f"- 发布时间：{item.published_at}")
    if item.tags:
        lines.append(f"- 标签：{', '.join(item.tags)}")
    lines.append("")

    if item.description:
        lines.extend(["## 内容摘要", "", markdown_escape(item.description), ""])

    if item.video_url:
        lines.extend(["## 视频", "", f"- {item.video_url}", ""])

    if item.images:
        lines.extend(["## 图片", ""])
        for image in item.images:
            if image.startswith("http"):
                lines.append(f"![image]({image})")
            else:
                lines.append(f"- {image}")
        lines.append("")

    if item.transcript:
        lines.extend(["## 字幕/转写", ""])
        for start, end, content in item.transcript:
            stamp = f"{start}-{end}".strip("-")
            prefix = f"`{stamp}` " if stamp else ""
            lines.append(f"- {prefix}{content}")
        lines.append("")

    if item.warnings:
        lines.extend(["## 注意", ""])
        for warning in item.warnings:
            lines.append(f"- {warning}")
        lines.append("")

    lines.extend(
        [
            "---",
            f"Generated by link2md at {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.",
            "",
        ]
    )
    return "\n".join(lines)


def slugify(value: str) -> str:
    value = normalize_space(value) or "link2md"
    value = re.sub(r"[\\/:*?\"<>|]+", "-", value)
    value = re.sub(r"\s+", "-", value)
    return value[:80].strip("-") or "link2md"


def read_cookie(cookie_arg: str) -> str:
    if not cookie_arg:
        return ""
    path = Path(cookie_arg).expanduser()
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return cookie_arg.strip()


def build_fallback(source_text: str, error: Exception) -> ContentItem:
    source_url = extract_url(source_text)
    return ContentItem(
        source_url=source_url,
        final_url=source_url,
        platform=detect_platform(source_url),
        title="链接内容抓取失败",
        warnings=[str(error), "如果页面需要登录或验证码，请通过 --cookie 传入 Cookie 后重试。"],
    )


def write_markdown(markdown: str, output_arg: str, title: str) -> Path:
    output = Path(output_arg)
    output_is_dir = output.is_dir() or output_arg.endswith(("/", "\\", os.sep))
    if output_is_dir:
        output.mkdir(parents=True, exist_ok=True)
        output = output / f"{slugify(title)}.md"
    output.write_text(markdown, encoding="utf-8")
    return output


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="将小红书、B 站、抖音链接的公开内容转换成 Markdown。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            示例：
              python3 link2md.py "https://www.bilibili.com/video/BV..."
              python3 link2md.py "https://v.douyin.com/..." -o note.md
              python3 link2md.py "复制来的分享文案 https://xhslink.com/..." --cookie cookies.txt
              LINK2MD_TRANSCRIBE_CMD='whisper {audio} --language Chinese --model small --output_format txt' python3 link2md.py "https://v.douyin.com/..."
              python3 link2md.py "https://v.douyin.com/..." --transcriber volcengine
              python3 link2md.py "https://v.douyin.com/..." --video-capture browser
            """
        ),
    )
    parser.add_argument("link", help="链接或包含链接的分享文本")
    parser.add_argument("-o", "--output", help="输出 Markdown 文件；不传则打印到 stdout")
    parser.add_argument("--cookie", default="", help="Cookie 字符串或 Cookie 文件路径")
    parser.add_argument("--fail-soft", action="store_true", help="抓取失败时仍输出带错误说明的 Markdown")
    parser.add_argument(
        "--transcribe-cmd",
        default="",
        help=f"视频转写命令，使用 {{audio}} 代表抽取后的音频文件；也可设置 {TRANSCRIBE_COMMAND_ENV}",
    )
    parser.add_argument(
        "--transcriber",
        default=os.environ.get(TRANSCRIBER_ENV, "auto"),
        choices=["auto", "local", "volcengine"],
        help=f"视频转写 provider；默认读取 {TRANSCRIBER_ENV} 或 auto",
    )
    parser.add_argument(
        "--video-capture",
        default=os.environ.get(VIDEO_CAPTURE_ENV, "none"),
        choices=["none", "browser"],
        help=f"没有视频直链时的音频捕获方式；默认读取 {VIDEO_CAPTURE_ENV} 或 none",
    )
    parser.add_argument(
        "--browser-capture-seconds",
        type=int,
        default=int(os.environ.get(BROWSER_CAPTURE_SECONDS_ENV, "120")),
        help=f"浏览器录音最长秒数；默认读取 {BROWSER_CAPTURE_SECONDS_ENV} 或 120",
    )
    parser.add_argument("--no-transcribe", action="store_true", help="跳过视频转文字")
    args = parser.parse_args(argv)

    try:
        item = parse_content(
            args.link,
            cookie=read_cookie(args.cookie),
            transcribe_cmd=args.transcribe_cmd,
            transcribe=not args.no_transcribe,
            transcriber=args.transcriber,
            video_capture=args.video_capture,
            browser_capture_seconds=args.browser_capture_seconds,
        )
    except Exception as exc:
        if not args.fail_soft:
            print(f"link2md: {exc}", file=sys.stderr)
            return 1
        item = build_fallback(args.link, exc)

    markdown = render_markdown(item)
    if args.output:
        print(str(write_markdown(markdown, args.output, item.title)))
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
