# link2md

`link2md` 是一个轻量命令行工具，用来把小红书笔记链接、B 站视频链接、抖音视频链接转换成 Markdown 笔记。

它优先提取平台页面里的结构化数据和字幕；如果遇到视频内容，也可以接入本地转写命令或火山引擎 AUC，把视频音频转成文字后写入 Markdown。

## 功能

- 支持小红书、B 站、抖音长链接和常见短链。
- 支持直接传入“复制分享文案”，工具会自动提取其中的链接。
- 输出标题、平台、原始链接、最终链接、作者、发布时间、正文/简介、标签、图片/封面、视频地址和警告信息。
- B 站优先读取公开字幕接口。
- 视频转写支持本地命令 provider 和火山引擎 AUC provider。
- 实验支持浏览器录音 fallback：当页面没有暴露可下载视频直链时，尝试用 Playwright 打开页面并录制 `<video>` 音轨。
- 不依赖第三方库即可完成基础链接解析；浏览器录音能力按需安装。

## 安装

从 PyPI 安装：

```bash
python3 -m pip install link2md
```

如果需要浏览器录音 fallback，一并安装浏览器扩展依赖：

```bash
python3 -m pip install 'link2md[browser]'
python3 -m playwright install chromium
```

安装后确认命令可用：

```bash
link2md --help
```

也可以直接从 GitHub 安装最新版：

```bash
python3 -m pip install 'git+https://github.com/merrier/link2md.git'
```

## 本地开发安装

克隆仓库后建议使用项目内虚拟环境：

```bash
git clone https://github.com/merrier/link2md.git
cd link2md
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -e '.[browser]'
```

如果使用 fish shell，激活虚拟环境要用 fish 版本脚本：

```fish
source .venv/bin/activate.fish
```

也可以不激活环境，直接调用：

```bash
.venv/bin/link2md --help
```

## 基础使用

输出到终端：

```bash
link2md "https://www.bilibili.com/video/BV..."
```

输出到指定 Markdown 文件：

```bash
link2md "https://v.douyin.com/..." -o note.md
```

输出到目录，文件名会根据标题自动生成：

```bash
link2md "复制来的分享文案 https://xhslink.com/..." -o notes/
```

如果平台页面需要登录或验证码，可以传 Cookie 字符串或 Cookie 文件路径：

```bash
link2md "https://www.xiaohongshu.com/explore/..." --cookie cookies.txt
```

抓取失败时仍生成一份带错误说明的 Markdown：

```bash
link2md "https://v.douyin.com/..." --fail-soft -o failed.md
```

跳过视频转文字：

```bash
link2md "https://v.douyin.com/..." --no-transcribe
```

## 视频转文字

`link2md` 会优先使用平台字幕。没有字幕时，如果页面暴露了可下载的视频地址，工具会下载视频、用 `ffmpeg` 抽取 16 kHz WAV 音频，然后交给转写 provider。

### 本地命令 provider

本地 provider 适合接 Whisper、whisper.cpp、mlx-whisper 等命令行工具。命令里用 `{audio}` 表示工具抽取出的 WAV 文件路径。

```bash
LINK2MD_TRANSCRIBE_CMD='whisper {audio} --language Chinese --model small --output_format txt' \
  link2md "https://v.douyin.com/..." -o video.md
```

也可以直接传参数：

```bash
link2md "https://v.douyin.com/..." \
  --transcriber local \
  --transcribe-cmd 'whisper {audio} --language Chinese --model small --output_format txt'
```

### 火山引擎 AUC provider

火山 AUC 是任务式接口：先上传音频得到公网 URL，再提交转写任务，最后轮询结果。`link2md` 不内置对象存储上传逻辑，你需要提供一个上传命令；这个命令接收 `{audio}` 并在 stdout 打印公网音频 URL。

```bash
export LINK2MD_VOLCENGINE_APP_ID='your-app-id'
export LINK2MD_VOLCENGINE_ACCESS_TOKEN='your-access-token'
export LINK2MD_VOLCENGINE_CLUSTER_ID='your-cluster-id'
export LINK2MD_VOLCENGINE_AUDIO_URL_CMD='your-upload-command {audio}'

link2md "https://v.douyin.com/..." --transcriber volcengine -o video.md
```

可选轮询配置：

```bash
export LINK2MD_VOLCENGINE_POLL_ATTEMPTS=60
export LINK2MD_VOLCENGINE_POLL_INTERVAL=3
```

### 浏览器录音 fallback

如果视频页面没有暴露可下载视频直链，可以启用实验性的浏览器录音 fallback。它会用 Playwright 打开页面，尝试通过 `video.captureStream()` 录制 `<video>` 音轨，再转成 WAV 给转写 provider。

安装浏览器能力：

```bash
python3 -m pip install -e '.[browser]'
python3 -m playwright install chromium
```

使用示例：

```bash
link2md "https://v.douyin.com/..." \
  --video-capture browser \
  --browser-capture-seconds 120 \
  --transcriber local \
  --transcribe-cmd 'whisper {audio} --language Chinese --model small --output_format txt'
```

浏览器录音可能因为平台登录、验证码、自动化检测、跨源视频资源或页面不支持 `captureStream()` 而失败。失败时工具会把原因写入 Markdown 的 `## 注意`。

## 输出结构

生成的 Markdown 通常包含：

- 标题
- 平台
- 原始链接和最终链接
- 作者和发布时间
- 标签
- 内容摘要或正文
- 视频地址
- 图片或封面
- 字幕/转写
- 注意事项或失败原因

## 开发

运行测试：

```bash
python3 -m unittest discover -s tests
```

使用项目虚拟环境运行测试：

```bash
.venv/bin/python -m unittest discover -s tests
```

当前测试不会真实访问平台，真实分享链接样本只用于 URL 提取和平台识别回归。

## 发布到 PyPI

仓库内置 GitHub Actions 发布流程：[`.github/workflows/publish-to-pypi.yml`](.github/workflows/publish-to-pypi.yml)。

发布流程使用 PyPI Trusted Publishing，不需要在 GitHub Secrets 里保存 PyPI token。你需要先在 PyPI 和 TestPyPI 后台创建 Trusted Publisher：

- PyPI project name: `link2md`
- Owner: `merrier`
- Repository: `link2md`
- Workflow filename: `publish-to-pypi.yml`
- PyPI environment name: `pypi`
- TestPyPI environment name: `testpypi`

Workflow 行为：

- 推送到 `main`：构建包，检查元数据，并发布到 TestPyPI。
- 手动触发 `workflow_dispatch`：构建包，检查元数据，并发布到 TestPyPI。
- 推送 `v*` tag：构建包，检查元数据，并发布到 PyPI。

发布前先确认版本号，修改 [pyproject.toml](pyproject.toml) 和 [setup.py](setup.py) 里的 `version`。PyPI 版本号不可重复。

本地预检查：

```bash
python3 -m pip install --upgrade build twine
rm -rf dist/ build/ *.egg-info/
python3 -m build
python3 -m twine check dist/*
```

发布正式版本：

```bash
git tag v0.1.0
git push origin v0.1.0
```

发布成功后，其他用户即可通过下面的命令安装：

```bash
python3 -m pip install link2md
```

## 说明

这个工具不是反风控抓取器。小红书、抖音、B 站页面结构和访问策略会变化，部分内容可能需要登录、Cookie 或人工验证。工具会尽量输出可用 Markdown，并在失败时保留清晰的错误说明。
