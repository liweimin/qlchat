import base64
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


DEFAULT_URL = "https://h5.qlchat.com/topic/details-listening?topicId=2000025257849054"
COURSE_LIST_API = "/api/wechat/transfer/h5/interact/getCourseList"
MEDIA_URL_API = "/api/wechat/topic/media-url"
DEFAULT_PAGE_SIZE = 20
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS"))
    return base_dir()


BUNDLED_BROWSERS = resource_dir() / "ms-playwright"
if BUNDLED_BROWSERS.exists():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BUNDLED_BROWSERS)

from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from Crypto.Cipher import AES  # noqa: E402
from Crypto.Util.Padding import unpad  # noqa: E402


APP_DIR = base_dir()
PROFILE_DIR = APP_DIR / "qlchat-profile"
QLCHAT_AES_KEY = b"711AAB17E204816B783374025FD08DE8"
QLCHAT_AES_IV = b"0102030405060708"
MEDIA_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".mp4", ".m4v", ".mov", ".flv", ".webm", ".m3u8"}


@dataclass
class Course:
    index: int
    topic_id: str
    source_topic_id: str
    title: str
    style: str


@dataclass
class MediaInfo:
    ok: bool
    play_url: str = ""
    duration: int = 0
    message: str = ""
    kind: str = "media"
    extension: str = ".bin"


def safe_json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def parse_topic_id(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.parse_qs(parsed.query).get("topicId", [""])[0]


def is_http_url(url: str) -> bool:
    return urllib.parse.urlparse(str(url or "")).scheme in {"http", "https"}


def decrypt_qlchat_url(value: str) -> str:
    value = str(value or "").strip()
    if not value or is_http_url(value):
        return value
    try:
        encrypted = base64.b64decode(value)
        decrypted = AES.new(QLCHAT_AES_KEY, AES.MODE_CBC, QLCHAT_AES_IV).decrypt(encrypted)
        try:
            decrypted = unpad(decrypted, AES.block_size)
        except ValueError:
            decrypted = decrypted.rstrip(b"\0")
        return decrypted.decode("utf-8").strip() or value
    except Exception:
        return value


def guess_extension(url: str, kind: str) -> str:
    path = urllib.parse.unquote(urllib.parse.urlparse(str(url or "")).path)
    suffix = Path(path).suffix.lower()
    if suffix in MEDIA_EXTENSIONS:
        return suffix
    if kind == "video":
        return ".mp4"
    if kind == "audio":
        return ".mp3"
    return ".bin"


def duration_seconds(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def build_topic_page_url(original_url: str, course: Course, live_id: str = "") -> str:
    parsed = urllib.parse.urlparse(original_url)
    query = urllib.parse.parse_qs(parsed.query)
    query["topicId"] = [course.topic_id]
    if live_id:
        query["liveId"] = [live_id]

    path = parsed.path or "/topic/details-listening"
    if "video" in course.style.lower():
        path = "/topic/details-video"
    elif not path.startswith("/topic/"):
        path = "/topic/details-listening"

    return urllib.parse.urlunparse(
        (
            parsed.scheme or "https",
            parsed.netloc or "h5.qlchat.com",
            path,
            "",
            urllib.parse.urlencode(query, doseq=True),
            "",
        )
    )


def state_ok(payload) -> bool:
    return payload and payload.get("state", {}).get("code") == 0


def state_message(payload) -> str:
    state = (payload or {}).get("state") or {}
    return state.get("msg") or state.get("message") or json.dumps(state, ensure_ascii=False)


def looks_like_login_error(message: str) -> bool:
    return bool(re.search(r"登录|登陆|未登录|请先登录|无权限|没有权限|login|sign in|auth|unauthorized|forbidden|permission", str(message), re.I))


def clean_title(value: str) -> str:
    value = str(value or "")
    value = re.sub(r"^\s*\d{1,3}\s*(?=(?:音频课|视频课|语音直播|视频直播|文章))", "", value)
    value = re.sub(r"^\s*(?:音频课|视频课|语音直播|视频直播|文章)\s*[|｜:：-]?\s*", "", value)
    value = re.sub(r"\s*[|｜]\s*", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def sanitize_file_part(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(value or ""))
    value = re.sub(r"\s+", " ", value).strip()
    return value[:120] or "untitled"


def pick_title(item, fallback: str) -> str:
    topic = item.get("topicPo") or {}
    title = (
        item.get("businessName")
        or item.get("name")
        or item.get("title")
        or topic.get("businessName")
        or topic.get("topic")
        or topic.get("title")
        or topic.get("liveName")
        or topic.get("name")
        or fallback
    )
    return clean_title(title) or fallback


def normalize_courses(rows) -> list[Course]:
    courses: list[Course] = []
    sequence = 0
    for row in rows:
        if not row or row.get("businessType") != "topic":
            continue
        topic = row.get("topicPo") or {}
        topic_id = str(row.get("businessId") or row.get("id") or topic.get("id") or "")
        if not topic_id:
            continue
        sequence += 1
        courses.append(
            Course(
                index=sequence,
                topic_id=topic_id,
                source_topic_id=str(row.get("sourceTopicId") or topic.get("sourceTopicId") or ""),
                title=pick_title(row, f"topic-{topic_id}"),
                style=str(topic.get("style") or row.get("style") or ""),
            )
        )
    return courses


def launch_context(playwright, headless: bool):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 430, "height": 900},
        user_agent=DEFAULT_UA,
        accept_downloads=False,
    )


def api_in_page(page, method: str, url: str, body=None):
    result = None
    for attempt in range(3):
        try:
            result = page.evaluate(
                """
                async ({ method, url, body }) => {
                  const options = {
                    method,
                    credentials: 'include',
                    headers: {
                      accept: 'application/json, text/plain, */*',
                      'content-type': 'application/json;charset=UTF-8',
                      'x-requested-with': 'XMLHttpRequest'
                    }
                  };
                  if (body !== null && body !== undefined) options.body = JSON.stringify(body);
                  const res = await fetch(url, options);
                  const text = await res.text();
                  return { status: res.status, text };
                }
                """,
                {"method": method, "url": url, "body": body},
            )
            break
        except PlaywrightError as exc:
            if "Execution context was destroyed" not in str(exc) or attempt == 2:
                raise
            page.wait_for_timeout(800)

    payload = safe_json(result["text"])
    if payload is None:
        raise RuntimeError(f"接口没有返回 JSON: {url}, HTTP {result['status']}")
    return payload


def wait_for_course_list(page, capture, timeout_ms=15000) -> bool:
    if any(state_ok(item["json"]) for item in capture["course_responses"]):
        return True
    try:
        response = page.wait_for_response(lambda res: "/interact/getCourseList" in res.url and res.status == 200, timeout=timeout_ms)
        payload = response.json()
        capture["course_responses"].append({"url": response.url, "json": payload})
    except Exception:
        pass
    return any(state_ok(item["json"]) for item in capture["course_responses"])


def capture_ids(requests) -> tuple[str, str]:
    channel_id = ""
    live_id = ""
    for req in requests:
        body = req.get("body") or {}
        if not channel_id and body.get("channelId"):
            channel_id = str(body["channelId"])
        if not live_id and body.get("liveId"):
            live_id = str(body["liveId"])
        parsed = urllib.parse.urlparse(req.get("url") or "")
        qs = urllib.parse.parse_qs(parsed.query)
        if not channel_id and qs.get("channelId"):
            channel_id = qs["channelId"][0]
        if not live_id and qs.get("liveId"):
            live_id = qs["liveId"][0]
    return channel_id, live_id


def fetch_all_courses(page, base_body, page_size: int, log) -> list:
    rows = []
    for page_no in range(1, 101):
        body = dict(base_body)
        body["page"] = {"page": page_no, "size": page_size}
        payload = api_in_page(page, "POST", COURSE_LIST_API, body)
        if not state_ok(payload):
            raise RuntimeError(f"课程列表接口失败: {state_message(payload)}")
        page_rows = payload.get("data", {}).get("dataList") or []
        rows.extend(page_rows)
        log(f"已加载课程列表第 {page_no} 页：{len(page_rows)} 行\n")
        if len(page_rows) < page_size:
            break
        time.sleep(0.25)
    return rows


def fetch_topic_info(page, topic_id: str) -> dict:
    payload = api_in_page(page, "GET", f"/api/wechat/topic/getInfo?{urllib.parse.urlencode({'topicId': topic_id})}")
    if not state_ok(payload):
        return {}
    return (payload.get("data") or {}).get("topicPo") or {}


def pick_video_item(video):
    if isinstance(video, dict):
        return video
    if not isinstance(video, list):
        return {}
    candidates = [item for item in video if isinstance(item, dict) and (item.get("playUrl") or item.get("url") or item.get("mediaUrl"))]
    if not candidates:
        return {}

    definition_rank = {"ld": 1, "sd": 2, "hd": 3, "fhd": 4, "2k": 5, "4k": 6}

    def score(item):
        try:
            pixels = int(item.get("width") or 0) * int(item.get("height") or 0)
        except (TypeError, ValueError):
            pixels = 0
        definition = str(item.get("definition") or item.get("resolution") or "").lower()
        return pixels, definition_rank.get(definition, 0)

    return max(candidates, key=score)


def media_from_item(item, kind: str) -> MediaInfo:
    if not isinstance(item, dict):
        return MediaInfo(False, kind=kind, message=f"{'视频' if kind == 'video' else '音频'}地址为空")
    play_url = item.get("playUrl") or item.get("url") or item.get("mediaUrl") or ""
    play_url = decrypt_qlchat_url(play_url)
    duration = duration_seconds(item.get("duration") or item.get("second"))
    if not play_url:
        return MediaInfo(False, duration=duration, kind=kind, message=f"{'视频' if kind == 'video' else '音频'}地址为空")
    return MediaInfo(True, play_url=play_url, duration=duration, kind=kind, extension=guess_extension(play_url, kind))


def fetch_media(page, course: Course) -> MediaInfo:
    params = {}
    if course.source_topic_id and course.source_topic_id != course.topic_id:
        params["topicId"] = course.source_topic_id
        params["relayTopicId"] = course.topic_id
    else:
        params["topicId"] = course.topic_id
    url = f"{MEDIA_URL_API}?{urllib.parse.urlencode(params)}"
    payload = api_in_page(page, "GET", url)
    if not state_ok(payload):
        return MediaInfo(False, message=state_message(payload))

    data = payload.get("data") or {}
    audio = data.get("audio") or {}
    video = data.get("video") or data.get("videoList") or data.get("videos")
    media_order = ("video", "audio") if "video" in course.style.lower() else ("audio", "video")

    empty_messages = []
    for kind in media_order:
        if kind == "audio":
            media = media_from_item(audio, "audio")
        else:
            media = media_from_item(pick_video_item(video), "video")
        if media.ok:
            return media
        empty_messages.append(media.message)

    return MediaInfo(False, message="；".join(dict.fromkeys(empty_messages)) or "媒体地址为空")


def resolve_media_with_player(page, course: Course, original_url: str, live_id: str, media_kind: str, log) -> str:
    topic_url = build_topic_page_url(original_url, course, live_id)
    response_candidates: list[str] = []

    def on_response(response):
        url = response.url
        low = url.lower()
        if is_http_url(url) and (
            "vod-qcloud.com" in low
            or any(suffix in low for suffix in (".mp3", ".m4a", ".aac", ".mp4", ".m4v", ".flv", ".webm", ".m3u8"))
        ):
            response_candidates.append(url)

    page.on("response", on_response)
    try:
        label = "视频" if media_kind == "video" else "音频" if media_kind == "audio" else "媒体"
        log(f"第 {course.index:03d} 节接口返回非直链，正在用播放器解析真实{label}地址...\n")
        page.goto("about:blank", wait_until="domcontentloaded", timeout=30000)
        page.goto(topic_url, wait_until="domcontentloaded", timeout=60000)
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                urls = page.evaluate(
                    """
                    () => Array.from(document.querySelectorAll('audio, video, source'))
                      .map(el => el.currentSrc || el.src || el.getAttribute('src') || '')
                      .filter(Boolean)
                    """
                )
            except PlaywrightError as exc:
                if "Execution context was destroyed" not in str(exc):
                    raise
                page.wait_for_timeout(500)
                continue
            for url in urls:
                if is_http_url(url):
                    return url
            if response_candidates:
                return response_candidates[0]
            page.wait_for_timeout(500)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    raise RuntimeError("播放器没有生成真实媒体地址，请确认已登录且当前账号有权限播放这一节。")


def download_url(url: str, target: Path, referer: str, force: bool) -> tuple[str, int]:
    if target.exists() and target.stat().st_size > 0 and not force:
        return "exists", target.stat().st_size
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA, "Referer": referer})
    with urllib.request.urlopen(request, timeout=60) as response, tmp.open("wb") as output:
        shutil.copyfileobj(response, output)
    tmp.replace(target)
    return "downloaded", target.stat().st_size


def file_name(course: Course, width: int, extension: str) -> str:
    extension = extension if extension.startswith(".") else f".{extension}"
    return f"{str(course.index).zfill(width)} {sanitize_file_part(course.title)}{extension}"


class DownloaderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("千聊媒体下载")
        self.root.geometry("860x640")
        self.root.minsize(780, 560)

        self.url_var = tk.StringVar(value=DEFAULT_URL)
        self.out_var = tk.StringVar(value=str(APP_DIR / "downloads"))
        self.all_var = tk.BooleanVar(value=True)
        self.from_var = tk.StringVar()
        self.to_var = tk.StringVar()
        self.force_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="就绪")

        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.running = False
        self.login_done = threading.Event()

        self._build_ui()
        self._poll_log_queue()

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(4, weight=1)
        pad = {"padx": 12, "pady": 6}

        url_frame = ttk.LabelFrame(self.root, text="课程")
        url_frame.grid(row=0, column=0, sticky="ew", **pad)
        url_frame.columnconfigure(1, weight=1)
        ttk.Label(url_frame, text="千聊课程地址").grid(row=0, column=0, padx=8, pady=8)
        ttk.Entry(url_frame, textvariable=self.url_var).grid(row=0, column=1, sticky="ew", padx=8, pady=8)

        out_frame = ttk.LabelFrame(self.root, text="下载位置")
        out_frame.grid(row=1, column=0, sticky="ew", **pad)
        out_frame.columnconfigure(1, weight=1)
        ttk.Label(out_frame, text="目录").grid(row=0, column=0, padx=8, pady=8)
        ttk.Entry(out_frame, textvariable=self.out_var).grid(row=0, column=1, sticky="ew", padx=8, pady=8)
        ttk.Button(out_frame, text="选择", command=self.choose_output_dir).grid(row=0, column=2, padx=8, pady=8)
        ttk.Button(out_frame, text="打开目录", command=self.open_output_dir).grid(row=0, column=3, padx=8, pady=8)

        range_frame = ttk.LabelFrame(self.root, text="下载范围")
        range_frame.grid(row=2, column=0, sticky="ew", **pad)
        self.all_check = ttk.Checkbutton(range_frame, text="下载全部", variable=self.all_var, command=self.toggle_range)
        self.all_check.grid(row=0, column=0, padx=8, pady=8)
        ttk.Label(range_frame, text="从").grid(row=0, column=1, padx=(18, 4), pady=8)
        self.from_entry = ttk.Entry(range_frame, textvariable=self.from_var, width=8)
        self.from_entry.grid(row=0, column=2, padx=4, pady=8)
        ttk.Label(range_frame, text="到").grid(row=0, column=3, padx=(18, 4), pady=8)
        self.to_entry = ttk.Entry(range_frame, textvariable=self.to_var, width=8)
        self.to_entry.grid(row=0, column=4, padx=4, pady=8)
        ttk.Checkbutton(range_frame, text="覆盖已存在文件", variable=self.force_var).grid(row=0, column=5, padx=(24, 8), pady=8)
        self.toggle_range()

        action_frame = ttk.Frame(self.root)
        action_frame.grid(row=3, column=0, sticky="ew", **pad)
        action_frame.columnconfigure(5, weight=1)
        self.download_button = ttk.Button(action_frame, text="下载", command=self.start_download)
        self.download_button.grid(row=0, column=0, padx=4)
        self.login_button = ttk.Button(action_frame, text="登录/续期", command=self.start_login)
        self.login_button.grid(row=0, column=1, padx=4)
        self.continue_button = ttk.Button(action_frame, text="我已登录，继续", command=self.continue_login, state="disabled")
        self.continue_button.grid(row=0, column=2, padx=4)
        ttk.Label(action_frame, textvariable=self.status_var).grid(row=0, column=5, sticky="e", padx=8)

        log_frame = ttk.LabelFrame(self.root, text="运行输出")
        log_frame.grid(row=4, column=0, sticky="nsew", **pad)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", height=18)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def toggle_range(self):
        state = "disabled" if self.all_var.get() else "normal"
        self.from_entry.configure(state=state)
        self.to_entry.configure(state=state)

    def choose_output_dir(self):
        directory = filedialog.askdirectory(initialdir=self.out_var.get() or str(APP_DIR))
        if directory:
            self.out_var.set(directory)

    def open_output_dir(self):
        out_dir = Path(self.out_var.get()).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(out_dir)
        else:
            subprocess.Popen(["open", str(out_dir)])

    def validate_range(self):
        if self.all_var.get():
            return 1, 0
        start = self.from_var.get().strip()
        end = self.to_var.get().strip()
        if not start or not end:
            messagebox.showerror("范围错误", "请填写起始和结束序号，或勾选“下载全部”。")
            return None
        if not start.isdigit() or not end.isdigit() or int(start) <= 0 or int(end) <= 0:
            messagebox.showerror("范围错误", "序号必须是正整数。")
            return None
        if int(end) < int(start):
            messagebox.showerror("范围错误", "结束序号不能小于起始序号。")
            return None
        return int(start), int(end)

    def set_running(self, running: bool):
        self.running = running
        state = "disabled" if running else "normal"
        self.download_button.configure(state=state)
        self.login_button.configure(state=state)
        if not running:
            self.continue_button.configure(state="disabled")

    def start_login(self):
        if self.running:
            return
        self.login_done.clear()
        self.run_worker(self.login_worker)

    def start_download(self):
        if self.running:
            return
        range_value = self.validate_range()
        if range_value is None:
            return
        self.run_worker(lambda: self.download_worker(*range_value))

    def continue_login(self):
        self.login_done.set()
        self.continue_button.configure(state="disabled")
        self.log("已确认登录完成，正在保存登录状态...\n")

    def run_worker(self, target):
        self.log_text.delete("1.0", "end")
        self.set_running(True)
        self.status_var.set("运行中")
        threading.Thread(target=self._worker_wrapper, args=(target,), daemon=True).start()

    def _worker_wrapper(self, target):
        try:
            target()
            self.log_queue.put(("status", "完成"))
        except Exception as exc:
            self.log_queue.put(("log", f"\nERROR: {exc}\n"))
            self.log_queue.put(("status", "失败"))
        finally:
            self.log_queue.put(("done", ""))

    def login_worker(self):
        url = self.url_var.get().strip() or DEFAULT_URL
        self.log_queue.put(("log", "正在打开登录浏览器...\n"))
        with sync_playwright() as playwright:
            context = launch_context(playwright, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            self.log_queue.put(("log", "请在打开的浏览器里完成千聊登录；完成后回到本窗口点击“我已登录，继续”。\n"))
            self.log_queue.put(("continue", "normal"))
            self.login_done.wait()
            context.close()
        self.log_queue.put(("continue", "disabled"))
        self.log_queue.put(("log", "登录状态已保存。\n"))

    def download_worker(self, start_index: int, end_index: int):
        url = self.url_var.get().strip() or DEFAULT_URL
        out_dir = Path(self.out_var.get().strip() or str(APP_DIR / "downloads")).expanduser()
        topic_id = parse_topic_id(url)
        if not topic_id:
            raise RuntimeError("课程地址里没有 topicId，请粘贴千聊课程页面地址。")

        with sync_playwright() as playwright:
            context = launch_context(playwright, headless=True)
            api_page = context.pages[0] if context.pages else context.new_page()
            player_page = context.new_page()
            capture = {"requests": [], "course_responses": []}

            def on_request(request):
                if "qlchat.com" not in request.url:
                    return
                capture["requests"].append(
                    {"url": request.url, "method": request.method, "body": safe_json(request.post_data or "")}
                )

            def on_response(response):
                if "/interact/getCourseList" not in response.url:
                    return
                try:
                    capture["course_responses"].append({"url": response.url, "json": response.json()})
                except Exception:
                    pass

            api_page.on("request", on_request)
            api_page.on("response", on_response)

            self.log(f"打开课程页：{url}\n")
            api_page.goto(url, wait_until="domcontentloaded", timeout=60000)
            wait_for_course_list(api_page, capture, 15000)
            topic_info = fetch_topic_info(api_page, topic_id)

            first_body = {}
            for req in capture["requests"]:
                if "/interact/getCourseList" in req["url"] and isinstance(req.get("body"), dict):
                    first_body = req["body"]
                    break

            channel_id, live_id = capture_ids(capture["requests"])
            channel_id = str(first_body.get("channelId") or channel_id or topic_info.get("channelId") or "")
            live_id = str(first_body.get("liveId") or live_id or topic_info.get("liveId") or "")
            if not channel_id or not live_id:
                raise RuntimeError("没有识别到课程列表。请先点击“登录/续期”，确认已购买后再下载。")

            self.log(f"识别到 channelId={channel_id}, liveId={live_id}\n")
            base_body = {"channelId": channel_id, "liveId": live_id, "sort": first_body.get("sort") or "asc"}
            rows = fetch_all_courses(api_page, base_body, DEFAULT_PAGE_SIZE, self.log)
            courses = normalize_courses(rows)
            courses = [c for c in courses if c.index >= start_index and (end_index == 0 or c.index <= end_index)]
            if not courses:
                raise RuntimeError("指定范围内没有课程。")

            out_dir.mkdir(parents=True, exist_ok=True)
            width = max(3, len(str(courses[-1].index)))
            manifest = {
                "courseUrl": url,
                "seedTopicId": topic_id,
                "channelId": channel_id,
                "liveId": live_id,
                "downloadedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "items": [],
            }

            self.log(f"准备处理 {len(courses)} 节课程。\n")
            for course in courses:
                media = fetch_media(api_page, course)
                if not media.ok:
                    if looks_like_login_error(media.message):
                        raise RuntimeError(f"第 {course.index} 节获取媒体失败：{media.message}。请先点击“登录/续期”。")
                    self.log(f"跳过 {course.index:03d} {course.title}: {media.message}\n")
                    manifest["items"].append(
                        {
                            "index": course.index,
                            "topicId": course.topic_id,
                            "title": course.title,
                            "mediaType": media.kind,
                            "status": "skipped",
                            "reason": media.message,
                        }
                    )
                    continue

                if not is_http_url(media.play_url):
                    media.play_url = resolve_media_with_player(player_page, course, url, live_id, media.kind, self.log)
                    media.extension = guess_extension(media.play_url, media.kind)

                target = out_dir / file_name(course, width, media.extension)
                try:
                    status, size = download_url(media.play_url, target, url, self.force_var.get())
                    self.log(f"{status.upper():10} {target.name} ({size} bytes)\n")
                    manifest["items"].append(
                        {
                            "index": course.index,
                            "topicId": course.topic_id,
                            "title": course.title,
                            "mediaType": media.kind,
                            "duration": media.duration,
                            "file": target.name,
                            "bytes": size,
                            "status": status,
                        }
                    )
                except Exception as exc:
                    self.log(f"失败 {target.name}: {exc}\n")
                    manifest["items"].append(
                        {
                            "index": course.index,
                            "topicId": course.topic_id,
                            "title": course.title,
                            "mediaType": media.kind,
                            "duration": media.duration,
                            "file": target.name,
                            "status": "failed",
                            "reason": str(exc),
                        }
                    )
                time.sleep(0.3)

            (out_dir / "qlchat-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            context.close()
            self.log(f"完成。清单：{out_dir / 'qlchat-manifest.json'}\n")

    def log(self, text: str):
        self.log_queue.put(("log", text))

    def append_log(self, text: str):
        self.log_text.insert("end", text)
        self.log_text.see("end")

    def _poll_log_queue(self):
        while True:
            try:
                kind, value = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.append_log(value)
            elif kind == "status":
                self.status_var.set(value)
            elif kind == "continue":
                self.continue_button.configure(state=value)
            elif kind == "done":
                self.set_running(False)
        self.root.after(100, self._poll_log_queue)


def main():
    if "--smoke-test" in sys.argv:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
        return

    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    DownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
