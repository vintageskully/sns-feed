"""X / Threads / Facebook 새 글을 텔레그램 채널로 보내는 스크립트.

GitHub Actions에서 30분마다 실행된다.
- 밤 11시 30분~새벽 5시(한국 시간)에는 보내지 않고, 그 사이 글은 새벽 5시 실행 때 보낸다.
- 영어 글은 무료 Google 번역으로 한국어 번역을 붙인다(실패하면 원문만 보낸다).
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

KST = timezone(timedelta(hours=9))
# 집 PC 모드: X만 따로 받음 (pc_setup.bat 이 PC_MODE=1 로 실행)
PC_MODE = os.environ.get("PC_MODE") == "1"
STATE_FILE = "state_pc.json" if PC_MODE else "state.json"
ACCOUNTS_FILE = "accounts_pc.txt" if PC_MODE else "accounts.txt"
X_PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "x_profile")
PC_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pc_config.json")
if PC_MODE and os.path.exists(PC_CONFIG):
    try:
        _cfg = json.load(open(PC_CONFIG, encoding="utf-8"))
        os.environ.setdefault("TELEGRAM_BOT_TOKEN", _cfg.get("token", ""))
        os.environ.setdefault("TELEGRAM_CHAT_ID", str(_cfg.get("chat_id", "")))
        os.environ.setdefault("X_COOKIES", _cfg.get("x_cookies", ""))
    except Exception:
        pass
MAX_AGE = timedelta(days=3)       # 이보다 오래된 글(고정 글 등)은 보내지 않음
WARN_AFTER_FAILS = 6              # 연속 6번(약 3시간) 실패하면 텔레그램으로 알림
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

TG_TOKEN = re.sub(r"^bot", "", os.environ.get("TELEGRAM_BOT_TOKEN", "").strip().strip("'\"").strip())
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
X_COOKIES = os.environ.get("X_COOKIES", "").strip()
FB_COOKIES = os.environ.get("FB_COOKIES", "").strip()
SEND_LATEST = os.environ.get("SEND_LATEST", "").lower() == "true"
IGNORE_QUIET = os.environ.get("IGNORE_QUIET", "").lower() == "true"
BACKFILL_FILE = "backfill.txt"   # 예: "2026-11-02 07:00 ~ 2026-11-02 11:00" (한국 시간)


def read_backfill(state):
    """지난 시간대 글 다시 받기. 같은 내용은 한 번만 실행."""
    if PC_MODE:
        return None
    try:
        raw = open(BACKFILL_FILE, encoding="utf-8").read().strip()
    except FileNotFoundError:
        return None
    if not raw or state.get("backfill_done") == raw:
        return None
    m = re.findall(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})", raw)
    if len(m) != 2:
        log("backfill.txt 형식 오류:", raw)
        return None
    a, b = [datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=KST).timestamp() for d, t in m]
    return raw, a, b


# ---------------------------------------------------------------- 공통

def log(*a):
    print(*a, flush=True)


def is_quiet_time(now):
    t = now.hour * 60 + now.minute
    return t >= 23 * 60 + 30 or t < 5 * 60


def load_accounts():
    accounts = []
    with open(ACCOUNTS_FILE, encoding="utf-8") as f:
        for line in f:
            url = line.split("#")[0].strip()
            if not url:
                continue
            u = url.rstrip("/")
            if "x.com/" in u or "twitter.com/" in u:
                handle = u.split("/")[-1].lstrip("@")
                accounts.append({"platform": "x", "handle": handle, "url": f"https://x.com/{handle}"})
            elif "threads.com/" in u or "threads.net/" in u:
                handle = u.split("/")[-1].lstrip("@")
                accounts.append({"platform": "threads", "handle": handle,
                                 "url": f"https://www.threads.com/@{handle}"})
            elif "facebook.com/" in u:
                m = re.search(r"id=(\d+)", u)
                handle = m.group(1) if m else u.split("/")[-1]
                accounts.append({"platform": "facebook", "handle": handle, "url": u})
            else:
                log("알 수 없는 주소, 건너뜀:", url)
    return accounts


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("seen", {})
    s.setdefault("fails", {})
    s.setdefault("warned", {})
    return s


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1, sort_keys=True)


def walk(obj):
    """JSON 안의 모든 dict를 순회."""
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            yield o
            stack.extend(reversed(list(o.values())))
        elif isinstance(o, list):
            stack.extend(reversed(o))


def parse_json_blobs(text):
    """응답 본문에서 JSON 덩어리들을 꺼낸다(여러 줄 JSON, for(;;); 접두어 처리)."""
    text = text.strip()
    if text.startswith("for (;;);"):
        text = text[len("for (;;);"):]
    out = []
    try:
        out.append(json.loads(text))
        return out
    except Exception:
        pass
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def scripts_json(html):
    out = []
    for m in re.finditer(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.S):
        try:
            out.append(json.loads(m.group(1)))
        except Exception:
            pass
    return out


def load_cookies(raw, default_domain):
    """Cookie-Editor로 내보낸 JSON 또는 'a=b; c=d' 문자열을 Playwright 형식으로."""
    if not raw:
        return []
    cookies = []
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("cookies", [data])
        same = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}
        for c in data:
            ck = {
                "name": c["name"], "value": c["value"],
                "domain": c.get("domain") or default_domain,
                "path": c.get("path") or "/",
                "secure": bool(c.get("secure", True)),
                "httpOnly": bool(c.get("httpOnly", False)),
            }
            ss = same.get(str(c.get("sameSite", "")).lower())
            if ss:
                ck["sameSite"] = ss
            exp = c.get("expirationDate") or c.get("expires")
            if exp and float(exp) > 0:
                ck["expires"] = float(exp)
            cookies.append(ck)
    except (ValueError, TypeError, KeyError):
        for part in raw.split(";"):
            if "=" in part:
                n, v = part.strip().split("=", 1)
                cookies.append({"name": n, "value": v, "domain": default_domain,
                                "path": "/", "secure": True})
    return cookies


# ---------------------------------------------------------------- 번역 / 텔레그램

def needs_translation(text):
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 5:
        return False
    hangul = sum(1 for c in letters if "가" <= c <= "힣" or "ㄱ" <= c <= "ㆎ")
    latin = sum(1 for c in letters if c.isascii())
    return hangul < len(letters) * 0.1 and latin > len(letters) * 0.5


def _tr_google_direct(text):
    r = requests.get("https://translate.googleapis.com/translate_a/single",
                     params={"client": "gtx", "sl": "auto", "tl": "ko", "dt": "t", "q": text[:4500]},
                     headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    return "".join(seg[0] for seg in r.json()[0] if seg and seg[0])


def _tr_deep_google(text):
    from deep_translator import GoogleTranslator
    return GoogleTranslator(source="auto", target="ko").translate(text[:4500])


def _tr_mymemory(text):
    r = requests.get("https://api.mymemory.translated.net/get",
                     params={"q": text[:480], "langpair": "en|ko"}, timeout=20)
    r.raise_for_status()
    return r.json()["responseData"]["translatedText"]


def translate(text):
    """무료 번역을 차례로 시도. 모두 실패하면 None(원문만 보냄)."""
    for fn in (_tr_google_direct, _tr_deep_google, _tr_mymemory):
        try:
            out = fn(text)
            if out and out.strip():
                return out
        except Exception as e:
            log(f"  번역 실패({fn.__name__}):", str(e)[:150])
    return None


def find_channel(state):
    """채널 ID를 따로 안 넣었으면, 봇이 관리자로 추가된 채널을 자동으로 찾는다."""
    global TG_CHAT
    if TG_CHAT or not TG_TOKEN:
        return
    if state.get("chat_id"):
        TG_CHAT = state["chat_id"]
        return
    if PC_MODE:
        try:
            TG_CHAT = json.load(open("state.json", encoding="utf-8")).get("chat_id", "")
        except Exception:
            pass
        if TG_CHAT:
            state["chat_id"] = TG_CHAT
            return
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                         params={"allowed_updates": json.dumps(
                             ["my_chat_member", "channel_post", "message"])},
                         timeout=30).json()
    except Exception as e:
        log("채널 찾기 실패:", e)
        return
    if not r.get("ok"):
        if r.get("error_code") in (401, 404):
            sys.exit("봇 토큰이 틀렸거나 폐기됨 — @BotFather에서 토큰을 다시 복사해 "
                     "TELEGRAM_BOT_TOKEN 값을 고쳐주세요 (" + str(r.get("description")) + ")")
        log("텔레그램 응답 오류(다른 프로그램이 이 봇을 쓰는 중일 수 있음):", r)
    chats = [(upd.get("my_chat_member") or upd.get("channel_post") or upd.get("message") or {})
             .get("chat", {}) for upd in r.get("result", [])]
    # 채널을 우선, 없으면 봇과의 1:1 대화방이나 그룹
    for kind in ("channel", "supergroup", "group", "private"):
        for chat in reversed(chats):
            if chat.get("type") == kind:
                TG_CHAT = str(chat["id"])
                state["chat_id"] = TG_CHAT
                log("받을 곳 자동 인식:", kind, chat.get("title") or chat.get("first_name"))
                return
    log("받을 곳을 못 찾음 — 채널(봇이 관리자)이나 봇 대화방에 아무 글이나 하나 올려주세요")


def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        log("[텔레그램 설정 없음]\n" + text)
        return True
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for _ in range(5):
        r = requests.post(url, data={"chat_id": TG_CHAT, "text": text[:4096],
                                     "disable_web_page_preview": "true"}, timeout=30)
        if r.status_code == 429:
            wait = r.json().get("parameters", {}).get("retry_after", 5)
            time.sleep(wait + 1)
            continue
        if not r.ok:
            log("  텔레그램 전송 실패:", r.status_code, r.text[:300])
            return False
        time.sleep(1.2)
        return True
    return False


ICON = {"x": "𝕏 X", "threads": "🧵 Threads", "facebook": "📘 Facebook"}


def format_post(acc, post):
    head = f"{ICON[acc['platform']]} · {post.get('author') or acc['handle']}"
    if post.get("ts"):
        head += "\n🕒 " + datetime.fromtimestamp(post["ts"], KST).strftime("%m/%d %H:%M")
    text = (post.get("text") or "").strip() or "(사진/영상 게시물)"
    body = text
    if needs_translation(text):
        ko = translate(text)
        if ko and ko.strip() != text.strip():
            body = f"{ko}\n\n― 원문 ―\n{text}"
    msg = f"{head}\n\n{body}\n\n🔗 {post['url']}"
    if len(msg) > 4096:
        tail = f"…\n\n🔗 {post['url']}"
        msg = msg[:4096 - len(tail)] + tail
    return msg


# ---------------------------------------------------------------- Threads

def fetch_threads(ctx, acc):
    handle = acc["handle"].lower()
    blobs = []
    page = ctx.new_page()

    def on_resp(resp):
        if "graphql" in resp.url:
            try:
                blobs.extend(parse_json_blobs(resp.text()))
            except Exception:
                pass

    page.on("response", on_resp)
    try:
        page.goto(acc["url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(2500)
        blobs.extend(scripts_json(page.content()))
    finally:
        page.close()

    posts = {}
    for b in blobs:
        for d in walk(b):
            if not ("code" in d and "pk" in d and "taken_at" in d):
                continue
            user = d.get("user") or {}
            if str(user.get("username", "")).lower() != handle:
                continue
            cap = d.get("caption") or {}
            text = cap.get("text", "") if isinstance(cap, dict) else ""
            pid = str(d["pk"])
            if pid not in posts:
                posts[pid] = {"id": pid, "text": text, "ts": int(d["taken_at"]),
                              "author": "@" + acc["handle"],
                              "url": f"https://www.threads.com/@{acc['handle']}/post/{d['code']}"}
    if not posts:
        raise RuntimeError("글을 찾지 못함(페이지 구조 변경 또는 차단)")
    return list(posts.values())


# ---------------------------------------------------------------- X

def _x_time(s):
    try:
        return int(parsedate_to_datetime(s).timestamp())
    except Exception:
        return None


def extract_x(blobs, handle):
    handle = handle.lower()
    posts = {}
    for b in blobs:
        for d in walk(b):
            # 로그인 GraphQL 응답 형식
            if "rest_id" in d and isinstance(d.get("legacy"), dict) and "full_text" in d["legacy"]:
                lg = d["legacy"]
                ures = ((d.get("core") or {}).get("user_results") or {}).get("result") or {}
                sn = ((ures.get("core") or {}).get("screen_name")
                      or (ures.get("legacy") or {}).get("screen_name") or "")
                note = (((d.get("note_tweet") or {}).get("note_tweet_results") or {})
                        .get("result") or {}).get("text")
                tid, text, ts = str(d["rest_id"]), note or lg["full_text"], _x_time(lg.get("created_at", ""))
            # 비로그인(syndication) 형식
            elif "id_str" in d and "created_at" in d and isinstance(d.get("user"), dict) \
                    and ("full_text" in d or "text" in d):
                sn = d["user"].get("screen_name", "")
                tid, text, ts = str(d["id_str"]), d.get("full_text") or d.get("text", ""), _x_time(d["created_at"])
            else:
                continue
            if sn.lower() != handle or tid in posts:
                continue
            text = re.sub(r"\s*https://t\.co/\w+$", "", text)
            posts[tid] = {"id": tid, "text": text, "ts": ts, "author": "@" + sn,
                          "url": f"https://x.com/{sn}/status/{tid}"}
    return list(posts.values())


def fetch_x(ctx, acc):
    if not X_COOKIES and not PC_MODE:
        # 로그인 없이 시도(최신 글이 안 나올 수 있음)
        r = requests.get("https://syndication.twitter.com/srv/timeline-profile/screen-name/"
                         + acc["handle"], headers={"User-Agent": UA}, timeout=30)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            raise RuntimeError(f"X 비로그인 조회 실패(HTTP {r.status_code}) — X_COOKIES 필요")
        posts = extract_x([json.loads(m.group(1))], acc["handle"])
    else:
        blobs, ops = [], []
        page = ctx.new_page()

        def on_resp(resp):
            if "/graphql/" in resp.url:
                op = resp.url.split("?")[0].rstrip("/").split("/")[-1]
                ops.append(f"{op}:{resp.status}")
                try:
                    blobs.append(resp.json())
                except Exception:
                    pass

        page.on("response", on_resp)
        dom_posts = []
        try:
            page.goto(acc["url"], wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector('article[data-testid="tweet"]', timeout=25000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(2500)
            if "/login" in page.url or "/i/flow" in page.url:
                raise RuntimeError("X 로그인 풀림 — X_COOKIES 다시 등록 필요")
            # 화면에 보이는 글에서 직접 읽기(예비용)
            dom_posts = page.evaluate("""() => [...document.querySelectorAll('article[data-testid="tweet"]')].map(a => {
                const t = a.querySelector('time');
                const link = t ? t.closest('a') : a.querySelector('a[href*="/status/"]');
                const txt = a.querySelector('[data-testid="tweetText"]');
                return {href: link ? link.getAttribute('href') : '', dt: t ? t.getAttribute('datetime') : '',
                        text: txt ? txt.innerText : ''};
            })""")
            title = page.title()
            logged_out = page.locator('[data-testid="loginButton"], a[href="/login"]').count() > 0
        finally:
            page.close()
        posts = extract_x(blobs, acc["handle"])
        if not posts:
            seen_ids = set()
            for d in dom_posts:
                m = re.match(r"^/([^/]+)/status/(\d+)", d.get("href") or "")
                if not m or m.group(1).lower() != acc["handle"].lower() or m.group(2) in seen_ids:
                    continue
                seen_ids.add(m.group(2))
                ts = None
                if d.get("dt"):
                    ts = int(datetime.fromisoformat(d["dt"].replace("Z", "+00:00")).timestamp())
                posts.append({"id": m.group(2), "text": d.get("text", ""), "ts": ts,
                              "author": "@" + m.group(1),
                              "url": f"https://x.com/{m.group(1)}/status/{m.group(2)}"})
        if not posts:
            if logged_out:
                raise RuntimeError("X 로그인 안 됨 — x.com 탭에서 쿠키를 다시 Export 해주세요")
            raise RuntimeError(f"글을 찾지 못함 [제목:{title[:40]} / 응답:{','.join(ops[:6]) or '없음'}"
                               f" / 화면글:{len(dom_posts)}]")
        return posts
    if not posts:
        raise RuntimeError("글을 찾지 못함")
    return posts


# ---------------------------------------------------------------- Facebook

def fetch_facebook(ctx, acc):
    if not FB_COOKIES:
        raise RuntimeError("FB_COOKIES 없음 — 페이스북은 로그인 쿠키가 필요")
    blobs = []
    page = ctx.new_page()

    def on_resp(resp):
        if "/api/graphql" in resp.url:
            try:
                blobs.extend(parse_json_blobs(resp.text()))
            except Exception:
                pass

    page.on("response", on_resp)
    try:
        page.goto(acc["url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)
        if "login" in page.url or "checkpoint" in page.url:
            raise RuntimeError("페이스북 로그인 풀림/보안확인 — FB_COOKIES 다시 등록 필요")
        for _ in range(3):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(2500)
        blobs.extend(scripts_json(page.content()))
    finally:
        page.close()

    posts = {}
    for b in blobs:
        for d in walk(b):
            pid = d.get("post_id")
            if not isinstance(pid, str) or not pid.isdigit() or pid in posts:
                continue
            text, ts, url, author = None, None, None, None
            for sub in walk(d):
                if text is None and isinstance(sub.get("message"), dict) \
                        and isinstance(sub["message"].get("text"), str):
                    text = sub["message"]["text"]
                if ts is None and isinstance(sub.get("creation_time"), int):
                    ts = sub["creation_time"]
                u = sub.get("url")
                if url is None and isinstance(u, str) and "facebook.com" in u and \
                        any(k in u for k in ("/posts/", "story_fbid", "permalink", "/videos/", "pfbid")):
                    url = u
                if author is None and sub.get("__typename") == "User" and isinstance(sub.get("name"), str):
                    author = sub["name"]
            if text is None and ts is None:
                continue
            posts[pid] = {"id": pid, "text": text or "", "ts": ts, "author": author,
                          "url": url or f"https://www.facebook.com/{pid}"}
    if not posts:
        raise RuntimeError("글을 찾지 못함(쿠키 만료 또는 페이지 구조 변경)")
    return list(posts.values())


def send_digest(items, title):
    """밤사이 글을 시간순으로 묶어서, 텔레그램 길이 제한(4096자)에 맞게 나눠 보냄."""
    if not items:
        log("밤사이 새 글 없음")
        return
    items.sort(key=lambda ap: ap[1].get("ts") or 0)
    head = f"{title} · {len(items)}개"
    sep = "\n\n━━━━━━━━━━\n\n"
    chunks, cur = [], head
    for i, (acc, post) in enumerate(items, 1):
        entry = f"[{i}/{len(items)}] " + format_post(acc, post)
        if len(entry) > 4000:
            entry = entry[:3990] + "…"
        if len(cur) + len(sep) + len(entry) > 4096:
            chunks.append(cur)
            cur = entry
        else:
            cur += sep + entry
    chunks.append(cur)
    for c in chunks:
        tg_send(c)


# ---------------------------------------------------------------- 메인

def main():
    now = datetime.now(KST)
    if is_quiet_time(now) and not IGNORE_QUIET:
        log(f"{now:%H:%M} KST — 조용한 시간이라 건너뜀(새벽 5시에 몰아서 보냄)")
        return

    from playwright.sync_api import sync_playwright

    accounts = load_accounts()
    state = load_state()
    find_channel(state)
    if not TG_CHAT:
        sys.exit("텔레그램 채널을 찾지 못해 중단합니다.")
    first_run = not state["seen"]
    backfill = read_backfill(state)
    if backfill:
        log("지난 글 다시 받기:", backfill[0])
    summary = {}
    errors = []
    # 새벽 5시~7시 사이 그날 첫 실행이면 밤사이 글을 한 번에 묶어서 보냄
    today = now.strftime("%Y-%m-%d")
    morning = 5 <= now.hour < 7 and state.get("digest_date") != today
    last_run = state.get("last_run", 0)
    was_off = PC_MODE and last_run and now.timestamp() - last_run > 90 * 60
    digest_mode = (morning or was_off) and not backfill and not SEND_LATEST
    if was_off and not morning:
        digest_title = (f"💤 PC 꺼진 동안 새 글 "
                        f"({datetime.fromtimestamp(last_run, KST):%m/%d %H:%M} ~ {now:%H:%M})")
    else:
        digest_title = f"🌙 밤사이 새 글 (어젯밤 11:30 ~ {now:%H:%M})"
    digest = []

    with sync_playwright() as p:
        launch_args = ["--disable-blink-features=AutomationControlled"]
        try:
            # 일반 크롬과 더 비슷한 방식(자동화 감지를 덜 당함)
            browser = p.chromium.launch(channel="chromium", args=launch_args)
        except Exception:
            browser = p.chromium.launch(args=launch_args)
        ctxs = {}

        def ctx_for(platform):
            if PC_MODE and platform == "x" and "x" not in ctxs and not X_COOKIES:
                ctxs["x"] = p.chromium.launch_persistent_context(
                    X_PROFILE_DIR, headless=True, user_agent=UA, locale="ko-KR",
                    timezone_id="Asia/Seoul", viewport={"width": 1280, "height": 900},
                    args=launch_args)
            if platform not in ctxs:
                c = browser.new_context(user_agent=UA, locale="ko-KR", timezone_id="Asia/Seoul",
                                        viewport={"width": 1280, "height": 900})
                if platform == "x":
                    c.add_cookies(load_cookies(X_COOKIES, ".x.com"))
                elif platform == "facebook":
                    c.add_cookies(load_cookies(FB_COOKIES, ".facebook.com"))
                ctxs[platform] = c
            return ctxs[platform]

        fetchers = {"x": fetch_x, "threads": fetch_threads, "facebook": fetch_facebook}

        for acc in accounts:
            key = f"{acc['platform']}:{acc['handle']}"
            ok_count, total = summary.get(acc["platform"], (0, 0))
            try:
                posts = fetchers[acc["platform"]](ctx_for(acc["platform"]), acc)
            except Exception as e:
                msg = str(e).splitlines()[0][:200]
                log(f"[실패] {key}: {msg}")
                errors.append(f"{key}: {msg}")
                state["fails"][key] = state["fails"].get(key, 0) + 1
                state.setdefault("last_error", {})[key] = f"{now:%m/%d %H:%M} {msg}"
                if state["fails"][key] >= WARN_AFTER_FAILS and not state["warned"].get(key):
                    tg_send(f"⚠️ {key} 글을 {state['fails'][key]}번 연속 못 가져왔어요.\n이유: {msg}")
                    state["warned"][key] = True
                summary[acc["platform"]] = (ok_count, total + 1)
                save_state(state)
                continue

            summary[acc["platform"]] = (ok_count + 1, total + 1)
            state["fails"][key] = 0
            state.setdefault("last_error", {}).pop(key, None)
            state["warned"].pop(key, None)
            seen = set(state["seen"].get(key, []))
            new_account = key not in state["seen"]
            posts.sort(key=lambda x: x["ts"] or 0)
            cutoff = (now - MAX_AGE).timestamp()

            if backfill:
                # 테스트 때 이미 보낸 최신 글(posts[-1])은 빼고 보냄
                to_send = [x for x in posts[:-1] if x["ts"] and backfill[1] <= x["ts"] < backfill[2]]
            elif SEND_LATEST and new_account and posts:
                to_send = [posts[-1]]  # 테스트: 처음 연결된 계정만 최신 글 1개
            elif new_account:
                to_send = []  # 처음 보는 계정: 기존 글은 보내지 않고 기억만
            else:
                to_send = [x for x in posts if x["id"] not in seen and (x["ts"] is None or x["ts"] >= cutoff)]

            log(f"[성공] {key}: {len(posts)}개 확인, 새 글 {len(to_send)}개")
            for post in to_send:
                if digest_mode:
                    digest.append((acc, post))
                    seen.add(post["id"])
                elif tg_send(format_post(acc, post)):
                    seen.add(post["id"])
            seen.update(x["id"] for x in posts if x not in to_send)
            # 최근 것만 기억(파일 크기 제한)
            state["seen"][key] = sorted(seen, key=lambda i: int(i) if i.isdigit() else 0)[-300:]
            save_state(state)

        browser.close()

    if digest_mode:
        send_digest(digest, digest_title)
        if morning:
            state["digest_date"] = today
    state["last_run"] = int(now.timestamp())
    save_state(state)

    if backfill:
        state["backfill_done"] = backfill[0]
        save_state(state)
        tg_send(f"📥 {backfill[0]} 사이 글 다시 보내기 완료")

    if (first_run or SEND_LATEST) and not backfill:
        lines = [f"✅ SNS 알림 설정 완료 ({now:%m/%d %H:%M})"]
        for pf, (ok, total) in summary.items():
            lines.append(f"{ICON[pf]}: {ok}/{total} 계정 수집 성공")
        if errors:
            lines.append("\n실패:\n" + "\n".join(errors[:15]))
        lines.append("\n이제부터 새 글이 올라오면 30분마다 보내드려요.")
        tg_send("\n".join(lines))


def pc_login():
    """처음 한 번: 창을 띄워 X에 직접 로그인. 로그인 정보는 x_profile 폴더에 저장."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(X_PROFILE_DIR, headless=False,
                                                   viewport={"width": 1100, "height": 850})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://x.com/login")
        print("\n열린 창에서 X에 로그인하세요. 로그인이 끝나면 그 창을 닫으면 됩니다.")
        try:
            while ctx.pages:
                ctx.pages[0].wait_for_timeout(1000)
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass
    print("X 로그인 저장 완료")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        pc_login()
    else:
        main()
