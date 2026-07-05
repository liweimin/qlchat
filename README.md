# 千聊媒体下载器

`qlchat-downloader.exe` 是独立 Windows 程序，内置 Python 运行时和 Playwright Chromium，不需要用户安装 Node、Python 或浏览器。

## 使用

1. 双击 `qlchat-downloader.exe`。
2. 在“千聊课程地址”里粘贴要下载的千聊课程页面 URL。
3. 第一次使用先点“登录/续期”，在弹出的浏览器里登录千聊，完成后回到程序点“我已登录，继续”。
4. 点“下载”。

默认勾选“下载全部”。如果只想增量下载，取消“下载全部”，填写“从 / 到”的课程序号，例如从 `30` 到 `32`。

下载文件保存在程序同级目录下的 `downloads` 文件夹。文件名格式为：

```text
001 欢迎你.mp3
002 一日学习营实录 精华片段.mp4
```

程序会按课程原本的媒体类型保存，音频课保存为 `.mp3`，视频课保存为 `.mp4`。标题会自动去掉列表前缀里的“音频课 |”“视频课 |”，但保留标题本身的日期、编号等内容。

## 登录失效

如果下载时提示“无权限访问”或要求登录，点“登录/续期”重新登录，再点“下载”即可。登录状态保存在程序同级目录的 `qlchat-profile` 文件夹。

## 通用性

只要是当前登录账号有权限访问的千聊课程，粘贴对应课程 URL 后都可以尝试下载。当前版本支持千聊音频课和视频课的直链下载。

## 开发说明

核心代码在 `tools/qlchat_downloader_gui.py`。程序是一个 Tkinter GUI，下载时通过 Playwright 启动内置 Chromium，并复用 `qlchat-profile` 里的登录状态。

核心流程：

1. `launch_context` 使用持久化浏览器 profile，保证登录状态可复用。
2. `api_in_page` 在千聊页面同源上下文里请求接口，避免手工拼 Cookie。
3. `getCourseList` 拉取课程列表，`normalize_courses` 只保留真正的课程条目，并生成连续序号。
4. `media-url` 获取媒体信息；音频读 `data.audio`，视频读 `data.video`，`decrypt_qlchat_url` 解开千聊返回的加密播放地址。
5. `download_url` 写入临时文件后再替换目标文件，成功和失败都会写入 `downloads/qlchat-manifest.json`。

如果后续千聊接口变化，优先检查这几个函数：`fetch_topic_info`、`fetch_all_courses`、`fetch_media`、`decrypt_qlchat_url`、`resolve_media_with_player`。

运行数据不要提交到仓库：`qlchat-profile/` 是登录态，`downloads/` 是下载结果，`build/` 是打包缓存。

## 本地构建

Windows 下用 Python 3.12：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\scripts\build-windows.ps1
```

构建产物是仓库根目录的 `qlchat-downloader.exe`。exe 是构建结果，不提交到 Git；本地测试或 GitHub Actions 构建后再分发。

## GitHub 构建

仓库包含 `.github/workflows/build-windows.yml`。推送到 `main` 或手动运行 workflow 后，GitHub Actions 会在 Windows 环境构建 `qlchat-downloader.exe` 并上传 artifact。

## 发布 Release

发布新版本时打 tag，例如：

```powershell
git tag v0.1.0
git push origin v0.1.0
```

仓库包含 `.github/workflows/release-windows.yml`。推送 `v*` tag 后，GitHub Actions 会重新构建 Windows exe，执行 smoke test，并把 `qlchat-downloader.exe` 上传到对应 GitHub Release。
