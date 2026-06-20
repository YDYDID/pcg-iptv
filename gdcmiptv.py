#!/usr/bin/env python3
"""
生成广东移动 IPTV 播放列表和 XMLTV 节目表。

运行后会在当前目录输出：
  - gdcmiptv.m3u      : 原始 RTP 组播播放列表
  - gdcmiptv2.m3u     : 组播代理播放列表和使用中兴回看
  - gdcmiptv3.m3u     : rtp2httpd RTP 转 HTTP 单播播放列表，中兴回看也走 rtp2httpd HTTP 代理
  - gdcmiptv4.m3u     : 组播代理播放列表，回看使用华为 RedirectPlay
  - gdcmiptv5.m3u     : rtp2httpd RTP 转 HTTP 单播播放列表，回看使用华为 RedirectPlay 并走 rtp2httpd HTTP 代理
  - gdcmepg.xml       : XMLTV 节目表
  - gdcmepg.xml.gz    : 压缩版 XMLTV 节目表

回看说明：
  - gdcmiptv.m3u / gdcmiptv2.m3u / gdcmiptv3.m3u 默认使用中兴 ztecode HLS 回看，
    URL 形如 183.235.162.80:6610/.../index.m3u8，时间参数使用 UTC。
  - gdcmiptv3.m3u 会把中兴 HLS 回看包成 rtp2httpd 的 /http/... 代理地址。
  - gdcmiptv4.m3u 使用华为 RedirectPlay.jsp 回看，默认只写最小必需参数。
  - gdcmiptv5.m3u 使用华为 RedirectPlay.jsp 回看，并包成 rtp2httpd 的 /http/... 代理地址。

流程来自用户广东移动 IPTV 抓包：
  1. 按机顶盒身份 POST /epg/aaa/v2/login.do，拿到 EPG 服务器信息和 Cookie。
  2. GET /epg/api/custom/getAllChannel2.json 获取频道列表。
  3. GET /epg/api/channel/{code}.json?begintime=YYYYMMDD 获取节目表。

只依赖 Python 标准库。
"""

from __future__ import annotations

import gzip
import http.cookiejar
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

try:
    from gdctiptv import tdes_ede3_ecb_encrypt_pkcs7
except Exception:
    try:
        from ctiptv import tdes_ede3_ecb_encrypt_pkcs7
    except Exception:
        tdes_ede3_ecb_encrypt_pkcs7 = None


# =========================
# 用户配置
# =========================

# 下面这些值来自自己的机顶盒抓包；其中华为途径回放IPTV账号、MAC、密码是必须的。
USER_ID = ""
MAC = ""
STB_ID = ""
STB_TYPE = ""
STB_IP = ""
AREA_CODE = ""
SOFTWARE_VERSION = ""
APK_VERSION_CODE = "585"
APK_VERSION_NAME = "5.0.9"

# 华为回看授权配置。启用 CATCHUP_MODE="huawei_redirect" 或 "auto" 时，
# 脚本会用下面这组信息换取 RedirectPlay.jsp 需要的 UserToken。
# 默认复用上面的盒子信息；如果华为回看链路使用另一组账号，在这里单独填写。
HUAWEI_AUTH_USER_ID = USER_ID
HUAWEI_AUTH_PASSWORD = ""
HUAWEI_AUTH_MAC = MAC
HUAWEI_AUTH_STB_ID = STB_ID
HUAWEI_AUTH_STB_TYPE = STB_TYPE
HUAWEI_AUTH_STB_IP = STB_IP
HUAWEI_AUTH_AREA_CODE = AREA_CODE
HUAWEI_AUTH_SOFTWARE_VERSION = SOFTWARE_VERSION

# 按抓包模拟移动新版 EPG 登录。频道/节目表接口可直接访问，默认跳过 AAA，速度更快。
DO_AAA_LOGIN = False
CONTINUE_WITHOUT_LOGIN = True
AAA_LOGIN_URL = "http://183.235.16.92:8082/epg/aaa/v2/login.do"

# 频道列表接口。抓包实际访问的是 getAllChannel2.json；失败时再回退 getAllChannel.json。
# OpenWrt 单请求实测 183.235.11.39 备站更稳定，强制在线刷新时优先请求备站。
CHANNEL_JSON_URLS = [
    "http://183.235.11.39:8082/epg/api/custom/getAllChannel2.json",
    "http://183.235.16.92:8082/epg/api/custom/getAllChannel2.json",
    "http://183.235.11.39:8082/epg/api/custom/getAllChannel.json",
    "http://183.235.16.92:8082/epg/api/custom/getAllChannel.json",
]

# 留空表示在线抓取；填本地 getAllChannel2.json 路径时，从文件生成频道列表。
CHANNEL_JSON_FILE = ""
# 默认先在线刷新频道接口；在线失败时自动回退缓存。
CHANNEL_JSON_CACHE = "mobile_getAllChannel2_cache.json"
PREFER_CHANNEL_JSON_CACHE = False

# 节目表接口。OpenWrt 实测 183.235.11.39 备站更稳定，优先使用；失败再回退抓包里的主站。
EPG_BASE_URLS = [
    "http://183.235.11.39:8082/epg/api/channel/",
    "http://183.235.16.92:8082/epg/api/channel/",
]

HTTP_TIMEOUT = 8
CHANNEL_RETRY_COUNT = 1
CHANNEL_RETRY_DELAY = 0.5
EPG_HTTP_TIMEOUT = 3.0
EPG_WORKERS = 6
EPG_RETRY_COUNT = 1
EPG_RETRY_DELAY = 0.3
EPG_REQUEST_DELAY = 0.0
# 节目表日期偏移。移动回看需要历史 EPG 才能准确定位，默认抓前 7 天到明天。
# 只想最快生成可设 EPG_DAYS = [0]；需要更大边界可设 list(range(-10, 7))。
EPG_DAYS = list(range(-7, 2))
EPG_VERBOSE_ERRORS = False
DOWNLOAD_EPG = True

# True 优先使用 hwurl，也就是抓包里 239.10/239.11 网段；False 优先使用 zteurl。
PREFER_HWURL = True

# 默认使用频道顶层 params。只有顶层地址缺失时才从 phychannels 里补地址。
# 若想优先拿 phychannels 里的更高清/4K 变体，可改成 True。
PREFER_PHYCHANNEL_STREAM = False

# M3U/XML 输出。
OUTPUT_RTP_M3U = "gdcmiptv.m3u"
OUTPUT_PROXY_M3U = "gdcmiptv2.m3u"
OUTPUT_RTP2HTTPD_M3U = "gdcmiptv3.m3u"
OUTPUT_HUAWEI_MULTICAST_M3U = "gdcmiptv4.m3u"
OUTPUT_RTP2HTTPD_HUAWEI_M3U = "gdcmiptv5.m3u"
OUTPUT_XMLTV = "gdcmepg.xml"
OUTPUT_XMLTV_GZ = "gdcmepg.xml.gz"
M3U_XMLTV_URL = "http://10.10.10.250:33333/gdcmepg.xml"

GENERATE_RTP_M3U = True
GENERATE_PROXY_M3U = True
GENERATE_RTP2HTTPD_M3U = True
GENERATE_HUAWEI_MULTICAST_M3U = True
GENERATE_HUAWEI_RTP2HTTPD_M3U = True
GENERATE_XMLTV = True
GENERATE_XMLTV_GZ = True

# gdcmiptv2.m3u 默认组播代理地址。
MULTICAST_PROXY_BASE_URL = "http://10.10.10.253:33335"
MULTICAST_PROXY_PATH = "rtp"

# gdcmiptv3.m3u / gdcmiptv5.m3u 默认 rtp2httpd 地址。
RTP2HTTPD_BASE_URL = "http://10.10.10.253:55141"
RTP2HTTPD_PATH = "rtp"
RTP2HTTPD_HTTP_PATH = "http"
RTP2HTTPD_PROXY_CATCHUP_FOR_RTP2HTTPD_M3U = True

# 移动回看。
# OpenWrt 实测：中兴 6610 回看源不需要账号密码授权，使用 UTC 时间模板即可取到 HLS/TS。
# 华为 RedirectPlay.jsp/TVOD 链路需要有效 UserToken/accountinfo；gdcmiptv4/5 会强制使用华为直连回看。
INCLUDE_CATCHUP = True
CATCHUP_MODE = "zte"  # 可选：zte、huawei_redirect、auto

# 华为回看入口。默认会按抓包里的 EDS/OAuth 流程自动获取 access_token；
# 也可以把已有 token 填到 LEGACY_USER_TOKEN。
LEGACY_USER_TOKEN = ""
HUAWEI_AUTH_AUTO_LOGIN = True
HUAWEI_AUTH_EDS_URL = "http://183.235.3.110:8082/EDS/jsp/AuthenticationURL"
HUAWEI_AUTH_CLIENT_ID = "jltv"
HUAWEI_REDIRECT_BASE_URL = "http://183.235.124.250:33200"
HUAWEI_CPID = "nfcmiptv"
HUAWEI_PLAYSEEK_TEMPLATE = "${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}"
HUAWEI_REDIRECT_INCLUDE_OPTIONAL_PARAMS = False
HUAWEI_RUNTIME_USER_TOKEN = ""
HUAWEI_RUNTIME_REDIRECT_BASE_URL = ""

# 中兴 ztecode HLS 模板。注意这里必须使用 UTC 时间；
# 用北京时间直接请求时一级 m3u8 可能返回，但二级 m3u8 会 404。
FALLBACK_TO_ZTE_CATCHUP = False
ZTE_CATCHUP_SOURCE_PREFIX = "http://183.235.162.80:6610/190000002005"
ZTE_CATCHUP_SOURCE_TEMPLATE = (
    "{prefix}/{ztecode}/index.m3u8?"
    "starttime=${{utc:yyyyMMddHHmmss}}&endtime=${{utcend:yyyyMMddHHmmss}}"
)

# 外部 M3U 合并。默认提取参考项目使用的远程源里 group-title 包含“港澳台”的频道。
# 离线快速生成时可改成 False。
MERGE_EXTERNAL_M3U = True
EXTERNAL_M3U_URL = "https://raw.githubusercontent.com/Jsnzkpg/Jsnzkpg/Jsnzkpg/Jsnzkpg1.m3u"
EXTERNAL_M3U_CACHE = "exports/external_hkat_cache.m3u"
EXTERNAL_GROUP_TITLES = {"港澳台": "港澳台"}
EXTERNAL_M3U_TIMEOUT = 15
EXTERNAL_GROUP_MIN_MATCH = 3

# 源站不给节目表时生成占位，避免客户端空白。
FILL_EMPTY_EPG_WITH_PLACEHOLDER = True
PLACEHOLDER_EPG_TITLE = "暂无节目表"
PLACEHOLDER_EPG_DESC = "运营商接口未提供该频道节目表"
PLACEHOLDER_EPG_INTERVAL_HOURS = 6

# 分组和排序。
SPLIT_SD_TO_NORMAL_GROUP = True
GROUP_ORDER = ["央视", "广东", "卫视", "其他", "普通"]
GUANGDONG_KEYWORDS = ["广东", "大湾区", "嘉佳", "南方", "岭南", "广州", "深圳"]
HD_KEYWORDS = ["高清", "超清", "4K", "4k", "8K", "8k"]


# =========================
# 数据结构
# =========================


@dataclass
class Program:
    start: str
    stop: str
    title: str
    desc: str = ""


@dataclass
class Channel:
    id: str
    name: str
    number: str
    icon: str
    rtp: str
    ztecode: str = ""
    hwmediaid: str = ""
    hwcode: str = ""
    supports_catchup: bool = False
    fields: dict[str, Any] = field(default_factory=dict)
    epg: list[Program] = field(default_factory=list)


@dataclass
class ExternalChannel:
    title: str
    url: str
    group: str
    attrs: dict[str, str] = field(default_factory=dict)
    extra_lines: list[str] = field(default_factory=list)


# =========================
# HTTP 工具
# =========================


def build_opener() -> urllib.request.OpenerDirector:
    cookie_jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))


def base_headers(user_agent: str = "Chrome/102") -> dict[str, str]:
    return {
        "User-Agent": user_agent,
        "UserID": USER_ID,
        "MAC": MAC,
        "STBID": STB_ID,
        "StbType": STB_TYPE,
    }


def http_request(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float | None = None,
) -> bytes:
    req = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with opener.open(req, timeout=HTTP_TIMEOUT if timeout is None else timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        data = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} for {url}: {data[:300]}") from e


def http_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float | None = None,
) -> Any:
    data = http_request(opener, url, method=method, headers=headers, body=body, timeout=timeout)
    return json.loads(data.decode("utf-8", errors="replace"))


def http_json_retry(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    retries: int,
    delay: float,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return http_json(opener, url, method=method, headers=headers, body=body)
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(delay * attempt)
    raise RuntimeError(str(last_error))


def make_query_url(base: str, params: list[tuple[str, str]] | dict[str, str]) -> str:
    return base + "?" + urllib.parse.urlencode(params)


def huawei_auth_key() -> bytes:
    return (HUAWEI_AUTH_PASSWORD + "0" * 24).encode("utf-8")[:24]


def build_huawei_authinfo(encry_token: str) -> str:
    if tdes_ede3_ecb_encrypt_pkcs7 is None:
        raise RuntimeError("缺少 3DES 实现：请把 gdctiptv.py 和 gdcmiptv.py 放在同一目录")
    plain = (
        f"{random.randrange(0, 100000000)}$"
        f"{encry_token}$"
        f"{HUAWEI_AUTH_USER_ID}$"
        f"{HUAWEI_AUTH_STB_ID}$"
        f"{HUAWEI_AUTH_STB_IP}$"
        f"{HUAWEI_AUTH_MAC}$"
        f"{HUAWEI_AUTH_AREA_CODE}$OTT"
    )
    encrypted = tdes_ede3_ecb_encrypt_pkcs7(plain.encode("utf-8"), huawei_auth_key())
    return encrypted.hex().upper()


def huawei_oauth_login(opener: urllib.request.OpenerDirector) -> tuple[str, str, dict[str, Any]]:
    headers = {"User-Agent": "okhttp/3.4.1", "Cookie": "JSESSIONID="}
    eds_url = make_query_url(
        HUAWEI_AUTH_EDS_URL,
        [
            ("UserID", HUAWEI_AUTH_USER_ID),
            ("Action", "Login"),
            ("return_type", "1"),
        ],
    )
    eds_data = http_json(opener, eds_url, headers=headers)
    epgurl = str(eds_data.get("epgurl") or "")
    parsed = urllib.parse.urlparse(epgurl)
    if not parsed.scheme or not parsed.hostname:
        raise RuntimeError(f"EDS 未返回有效 epgurl：{eds_data}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    base_url = f"{parsed.scheme}://{parsed.hostname}:{port}"

    authorize_url = make_query_url(
        f"{base_url}/EPG/oauth/v2/authorize",
        [
            ("response_type", "EncryToken"),
            ("client_id", HUAWEI_AUTH_CLIENT_ID),
            ("userid", HUAWEI_AUTH_USER_ID),
        ],
    )
    encry_token = str(http_json(opener, authorize_url, headers=headers).get("EncryToken") or "")
    if not encry_token:
        raise RuntimeError("OAuth authorize 未返回 EncryToken")

    token_url = make_query_url(
        f"{base_url}/EPG/oauth/v2/token",
        [
            ("grant_type", "EncryToken"),
            ("client_id", HUAWEI_AUTH_CLIENT_ID),
            ("UserID", HUAWEI_AUTH_USER_ID),
            ("DeviceType", HUAWEI_AUTH_STB_TYPE),
            ("DeviceVersion", HUAWEI_AUTH_SOFTWARE_VERSION),
            ("authinfo", build_huawei_authinfo(encry_token)),
        ],
    )
    token_data = http_json(opener, token_url, headers=headers)
    access_token = str(token_data.get("access_token") or "")
    if not access_token:
        raise RuntimeError(f"OAuth token 未返回 access_token：{token_data}")
    return access_token, base_url, token_data


def should_prepare_huawei_redirect() -> bool:
    if not INCLUDE_CATCHUP:
        return False
    mode = CATCHUP_MODE.strip().lower()
    if mode in {"huawei_redirect", "huawei", "redirect", "auto"}:
        return True
    return GENERATE_HUAWEI_MULTICAST_M3U or GENERATE_HUAWEI_RTP2HTTPD_M3U


def prepare_huawei_redirect(opener: urllib.request.OpenerDirector) -> None:
    global HUAWEI_RUNTIME_USER_TOKEN, HUAWEI_RUNTIME_REDIRECT_BASE_URL
    if not should_prepare_huawei_redirect():
        return
    if effective_legacy_user_token() or not HUAWEI_AUTH_AUTO_LOGIN:
        return
    try:
        token, base_url, token_data = huawei_oauth_login(opener)
    except Exception as e:
        print(f"警告：华为 OAuth 登录失败：{e}")
        return
    HUAWEI_RUNTIME_USER_TOKEN = token
    HUAWEI_RUNTIME_REDIRECT_BASE_URL = base_url
    expires = token_data.get("expires_in", "")
    suffix = f"，有效期={expires} 秒" if expires else ""
    print(f"华为 OAuth 登录成功，入口={base_url}{suffix}")


def aaa_login(opener: urllib.request.OpenerDirector) -> dict[str, Any]:
    payload = {
        "apkPlatform": "HW",
        "apkVersionCode": APK_VERSION_CODE,
        "apkVersionName": APK_VERSION_NAME,
        "ipAddr": STB_IP,
        "macAddr": MAC,
        "patchVersion": "2",
        "softwareVersion": SOFTWARE_VERSION,
        "stbId": STB_ID,
        "stbType": STB_TYPE,
        "userId": USER_ID,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = base_headers("okhttp/3.12.11")
    headers["Content-Type"] = "application/json; charset=utf-8"
    data = http_json(opener, AAA_LOGIN_URL, method="POST", headers=headers, body=body)
    status = str(data.get("status", ""))
    if status and status != "200":
        raise RuntimeError(f"AAA 登录状态异常：{status}")
    print(f"AAA 登录成功，区域={data.get('areaCode', '')}，子区域={data.get('subAreaCode', '')}")

    epg_server = data.get("epgServer") or data.get("servers", {}).get("epgServer")
    epg_backup = data.get("epgServerBackup") or data.get("servers", {}).get("epgServerBackup")
    if epg_server:
        print(f"EPG 主服务器：{epg_server}")
    if epg_backup:
        print(f"EPG 备用服务器：{epg_backup}")
    return data


# =========================
# 频道提取
# =========================


def first_non_empty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def stream_fields(params: dict[str, Any]) -> tuple[str, str, str, str]:
    hwurl = first_non_empty(params.get("hwurl"))
    zteurl = first_non_empty(params.get("zteurl"))
    if PREFER_HWURL:
        rtp = first_non_empty(hwurl, zteurl)
    else:
        rtp = first_non_empty(zteurl, hwurl)
    return (
        rtp,
        first_non_empty(params.get("ztecode")),
        first_non_empty(params.get("hwmediaid")),
        first_non_empty(params.get("hwcode")),
    )


def bitrate_score(phychannel: dict[str, Any]) -> tuple[int, int]:
    text = f"{phychannel.get('bitrateType', '')} {phychannel.get('bitrateTypeName', '')}"
    nums = [int(n) for n in re.findall(r"\d+", text)]
    score = max(nums) if nums else 0
    if "4K" in text.upper():
        score += 1000
    if "高清" in text:
        score += 100
    return score, len(str(phychannel))


def choose_params(item: dict[str, Any]) -> dict[str, Any]:
    params = item.get("params") if isinstance(item.get("params"), dict) else {}
    if not PREFER_PHYCHANNEL_STREAM and first_non_empty(params.get("hwurl"), params.get("zteurl")):
        return params

    candidates: list[dict[str, Any]] = []
    for phychannel in item.get("phychannels", []) or []:
        if isinstance(phychannel, dict) and isinstance(phychannel.get("params"), dict):
            candidates.append(phychannel)

    if candidates:
        match_number = [
            ch for ch in candidates
            if first_non_empty(ch.get("virtualChannelnum")) == first_non_empty(item.get("channelnum"))
        ]
        pool = match_number or candidates
        best = max(pool, key=bitrate_score)
        best_params = best.get("params", {})
        if first_non_empty(best_params.get("hwurl"), best_params.get("zteurl")):
            return best_params
    return params


def parse_channel(item: dict[str, Any]) -> Channel | None:
    channel_id = first_non_empty(item.get("code"))
    name = first_non_empty(item.get("title"), item.get("subTitle"), channel_id)
    number = first_non_empty(item.get("channelnum"), item.get("virtualChannelnum"), channel_id)
    icon = first_non_empty(item.get("icon"), item.get("icon2"))
    params = choose_params(item)
    rtp, ztecode, hwmediaid, hwcode = stream_fields(params)
    if not channel_id or not name or not rtp:
        return None

    supports_catchup = (
        str(item.get("timeshiftAvailable", "")).lower() == "true"
        or str(item.get("lookbackAvailable", "")).lower() == "true"
    )
    return Channel(
        id=channel_id,
        name=name,
        number=number,
        icon=icon,
        rtp=rtp,
        ztecode=ztecode,
        hwmediaid=hwmediaid,
        hwcode=hwcode,
        supports_catchup=supports_catchup,
        fields=item,
    )


def parse_channel_payload(data: dict[str, Any], source: str) -> list[Channel]:
    raw_channels = data.get("channels", [])
    channels = [ch for item in raw_channels if (ch := parse_channel(item))]
    if channels:
        print(f"已从 {source} 获取 {len(channels)} 个频道")
        return channels
    raise RuntimeError(f"未在 {source} 中解析到可用频道")


def load_channel_cache() -> list[Channel] | None:
    if not CHANNEL_JSON_CACHE:
        return None
    path = Path(CHANNEL_JSON_CACHE)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return parse_channel_payload(data, f"频道缓存 {path}")
    except Exception as e:
        print(f"警告：频道缓存不可用 {path}: {e}")
        return None


def save_channel_cache(data: dict[str, Any]) -> None:
    if not CHANNEL_JSON_CACHE:
        return
    path = Path(CHANNEL_JSON_CACHE)
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        print(f"已更新频道缓存：{path}")
    except Exception as e:
        print(f"警告：写入频道缓存失败 {path}: {e}")


def download_channels(opener: urllib.request.OpenerDirector) -> list[Channel]:
    if CHANNEL_JSON_FILE:
        path = Path(CHANNEL_JSON_FILE)
        data = json.loads(path.read_text(encoding="utf-8"))
        return parse_channel_payload(data, str(path))

    if PREFER_CHANNEL_JSON_CACHE:
        cached = load_channel_cache()
        if cached:
            return cached

    headers = base_headers("Chrome/102")
    headers["Referer"] = "http://183.237.203.89:59001/epg/rubiks-v2-2504221038/js/main.jsv.f62c0590.mjs#/"

    last_error: Exception | None = None
    for url in CHANNEL_JSON_URLS:
        try:
            data = http_json_retry(
                opener,
                url,
                retries=CHANNEL_RETRY_COUNT,
                delay=CHANNEL_RETRY_DELAY,
                headers=headers,
            )
            channels = parse_channel_payload(data, url)
            if isinstance(data, dict):
                save_channel_cache(data)
            return channels
        except Exception as e:
            last_error = e
            print(f"警告：请求频道 JSON 失败 {url}: {e}")
    cached = load_channel_cache()
    if cached:
        print(f"警告：在线频道接口失败，已使用本地缓存继续：{last_error}")
        return cached
    raise RuntimeError(f"所有频道 JSON 地址请求失败：{last_error}")


# =========================
# 节目表
# =========================


def epg_dates() -> list[str]:
    now = datetime.now(timezone(timedelta(hours=8)))
    return [(now + timedelta(days=offset)).strftime("%Y%m%d") for offset in EPG_DAYS]


def schedule_key(schedule: dict[str, Any]) -> tuple[str, str, str]:
    return (
        first_non_empty(schedule.get("starttime"), schedule.get("startTime"), schedule.get("start")),
        first_non_empty(schedule.get("endtime"), schedule.get("endTime"), schedule.get("end")),
        first_non_empty(schedule.get("title"), schedule.get("name")),
    )


def parse_program(schedule: dict[str, Any]) -> Program | None:
    start, stop, title = schedule_key(schedule)
    if not start or not stop or not title:
        return None
    desc = first_non_empty(schedule.get("description"), schedule.get("desc"), title)
    return Program(start=start, stop=stop, title=title, desc=desc)


def fetch_epg_json(url: str, headers: dict[str, str]) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, EPG_RETRY_COUNT + 1):
        try:
            return http_json(build_opener(), url, headers=headers, timeout=EPG_HTTP_TIMEOUT)
        except Exception as e:
            last_error = e
            if attempt < EPG_RETRY_COUNT:
                time.sleep(EPG_RETRY_DELAY * attempt)
    raise RuntimeError(str(last_error))


def fetch_channel_epg(channel: Channel, base_urls: list[str]) -> tuple[str, list[Program]]:
    headers = base_headers("Chrome/102")
    headers["Accept"] = "application/json,text/plain,*/*"
    headers["Connection"] = "close"
    headers["Referer"] = "http://183.237.203.89:59001/epg/rubiks-v2-2504221038/js/main.jsv.f62c0590.mjs#/"
    programs: list[Program] = []
    seen: set[tuple[str, str, str]] = set()

    for date in epg_dates():
        data = None
        empty_data = None
        last_error: Exception | None = None
        for base_url in base_urls:
            url = f"{base_url}{urllib.parse.quote(channel.id)}.json?begintime={date}"
            try:
                fetched = fetch_epg_json(url, headers)
                if not isinstance(fetched, dict):
                    continue
                schedules = fetched.get("schedules", [])
                if schedules:
                    data = fetched
                    break
                empty_data = fetched
            except Exception as e:
                last_error = e
        if data is None and empty_data is not None:
            data = empty_data
        if data is None:
            if EPG_VERBOSE_ERRORS:
                print(f"警告：未获取到节目表 {channel.id} {channel.name} {date}: {last_error}")
            continue
        for schedule in data.get("schedules", []):
            if not isinstance(schedule, dict):
                continue
            key = schedule_key(schedule)
            if key in seen:
                continue
            program = parse_program(schedule)
            if program:
                programs.append(program)
                seen.add(key)
        if EPG_REQUEST_DELAY > 0:
            time.sleep(EPG_REQUEST_DELAY)
    return channel.id, programs


def fetch_all_epg(channels: list[Channel]) -> None:
    if not channels:
        return
    base_urls = [url if url.endswith("/") else url + "/" for url in EPG_BASE_URLS]
    done = 0
    total_programs = 0

    print(
        "节目表参数："
        f"并发={EPG_WORKERS}，单次超时={EPG_HTTP_TIMEOUT:g}秒，"
        f"单地址重试={EPG_RETRY_COUNT}，日期数={len(epg_dates())}"
    )
    with ThreadPoolExecutor(max_workers=EPG_WORKERS) as pool:
        futures = {}
        for channel in channels:
            futures[pool.submit(fetch_channel_epg, channel, base_urls)] = channel

        for future in as_completed(futures):
            channel = futures[future]
            try:
                _, programs = future.result()
                channel.epg = programs
            except Exception as e:
                print(f"警告：节目表任务失败 {channel.id} {channel.name}: {e}")
            done += 1
            total_programs += len(channel.epg)
            if done % 20 == 0 or done == len(channels):
                print(f"节目表进度：{done}/{len(channels)}，节目数={total_programs}")
    print(f"已获取 {total_programs} 条节目")


def fill_empty_epg_with_placeholder(channels: list[Channel]) -> None:
    if not FILL_EMPTY_EPG_WITH_PLACEHOLDER:
        return
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = (now + timedelta(days=max(EPG_DAYS or [0]) + 1)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    step = timedelta(hours=max(1, int(PLACEHOLDER_EPG_INTERVAL_HOURS)))

    filled = 0
    for channel in channels:
        if channel.epg:
            continue
        cursor = start
        while cursor < end:
            stop = min(cursor + step, end)
            channel.epg.append(
                Program(
                    start=cursor.strftime("%Y%m%d%H%M%S"),
                    stop=stop.strftime("%Y%m%d%H%M%S"),
                    title=PLACEHOLDER_EPG_TITLE,
                    desc=PLACEHOLDER_EPG_DESC,
                )
            )
            cursor = stop
        filled += 1
    if filled:
        print(f"已为 {filled} 个频道填充占位节目表")


# =========================
# 输出
# =========================


def is_hd_source(name: str) -> bool:
    return any(keyword in name for keyword in HD_KEYWORDS)


def group_title(name: str) -> str:
    if SPLIT_SD_TO_NORMAL_GROUP and not is_hd_source(name):
        return "普通"
    if name.startswith("CCTV") or name.startswith("CGTN") or "央视" in name:
        return "央视"
    if any(keyword in name for keyword in GUANGDONG_KEYWORDS):
        return "广东"
    if "卫视" in name:
        return "卫视"
    return "其他"


def natural_sort_key(text: str) -> list[tuple[int, int | str]]:
    parts = re.findall(r"\d+|\D+", text)
    key: list[tuple[int, int | str]] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            normalized = re.sub(r"[\s\-＋+_/]+", "", part).casefold()
            if normalized:
                key.append((1, normalized))
    return key


def channel_sort_key(channel: Channel) -> tuple[int, list[tuple[int, int | str]], int | str]:
    group = group_title(channel.name)
    group_index = GROUP_ORDER.index(group) if group in GROUP_ORDER else len(GROUP_ORDER)
    number: int | str = int(channel.number) if channel.number.isdigit() else channel.number
    return group_index, natural_sort_key(channel.name), number


def sort_channels(channels: list[Channel]) -> list[Channel]:
    return sorted(channels, key=channel_sort_key)


def valid_m3u_content(text: str) -> bool:
    return bool(text and ("#EXTM3U" in text or "#EXTINF" in text))


def download_text(url: str, timeout: int = HTTP_TIMEOUT) -> str:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def load_external_m3u_text() -> tuple[str, str]:
    cache_path = Path(EXTERNAL_M3U_CACHE)
    try:
        text = download_text(EXTERNAL_M3U_URL, EXTERNAL_M3U_TIMEOUT)
        if valid_m3u_content(text):
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(text, encoding="utf-8")
            print(f"已从网络获取外部 M3U：{EXTERNAL_M3U_URL}")
            return text, "network"
    except Exception as e:
        print(f"警告：获取外部 M3U 失败：{e}")

    if cache_path.exists():
        text = cache_path.read_text(encoding="utf-8", errors="replace")
        if valid_m3u_content(text):
            print(f"使用缓存的外部 M3U：{cache_path}")
            return text, "cache"
    return "", "none"


def normalize_external_group(text: str) -> str:
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text.lower()))


def shared_group_fragment(source: str, target: str) -> bool:
    source_norm = normalize_external_group(source)
    target_norm = normalize_external_group(target)
    if not source_norm or not target_norm:
        return False
    if source_norm == target_norm or source_norm in target_norm or target_norm in source_norm:
        return True
    shorter, longer = (source_norm, target_norm) if len(source_norm) <= len(target_norm) else (target_norm, source_norm)
    if len(shorter) < EXTERNAL_GROUP_MIN_MATCH:
        return False
    return any(shorter[i:i + EXTERNAL_GROUP_MIN_MATCH] in longer for i in range(len(shorter) - EXTERNAL_GROUP_MIN_MATCH + 1))


def external_target_group(source_group: str) -> str:
    for key, target in EXTERNAL_GROUP_TITLES.items():
        if key in source_group or shared_group_fragment(source_group, key):
            return target
    return ""


def normalize_external_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme or parsed.netloc:
        return parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower()).geturl()
    return url.strip()


def parse_external_m3u(text: str) -> list[ExternalChannel]:
    channels: list[ExternalChannel] = []
    seen_urls: set[str] = set()
    current: dict[str, Any] | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attrs = {k: v for k, v in re.findall(r'(\S+?)="([^"]*)"', line)}
            current = {
                "title": line.split(",", 1)[-1].strip() if "," in line else "",
                "attrs": attrs,
                "extra_lines": [],
            }
        elif line.startswith("#") and current is not None:
            current["extra_lines"].append(line)
        elif current is not None:
            source_group = current["attrs"].get("group-title", "")
            target_group = external_target_group(source_group)
            norm_url = normalize_external_url(line)
            if target_group and norm_url not in seen_urls:
                seen_urls.add(norm_url)
                attrs = dict(current["attrs"])
                attrs["group-title"] = target_group
                title = current["title"] or attrs.get("tvg-name", "") or attrs.get("tvg-id", "")
                channels.append(
                    ExternalChannel(
                        title=title,
                        url=line,
                        group=target_group,
                        attrs=attrs,
                        extra_lines=list(current["extra_lines"]),
                    )
                )
            current = None
    return channels


def load_external_channels() -> list[ExternalChannel]:
    if not MERGE_EXTERNAL_M3U or not EXTERNAL_M3U_URL or not EXTERNAL_GROUP_TITLES:
        return []
    text, source = load_external_m3u_text()
    if not text:
        return []
    channels = parse_external_m3u(text)
    source_label = {"network": "网络", "cache": "缓存", "none": "无"}.get(source, source)
    print(f"已提取 {len(channels)} 个外部频道，来源={source_label}")
    return channels


def attr_escape(value: Any) -> str:
    return escape(str(value), {'"': "&quot;"})


def external_m3u_lines(channel: ExternalChannel) -> list[str]:
    attrs = dict(channel.attrs)
    attrs["group-title"] = channel.group
    attrs.setdefault("tvg-name", channel.title)
    attrs.setdefault("tvg-id", channel.title)
    parts = ["#EXTINF:-1"]
    parts.extend(f'{key}="{attr_escape(value)}"' for key, value in attrs.items())
    return [" ".join(parts) + f",{channel.title}", *channel.extra_lines, channel.url]


def parse_rtp_addr(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port
    if host and port:
        return f"{host}:{port}"
    if url.startswith("rtp://"):
        return url[len("rtp://"):]
    return url


def join_base_path(base: str, path: str, tail: str) -> str:
    base = base.rstrip("/")
    path = path.strip("/")
    tail = tail.lstrip("/")
    return f"{base}/{path}/{tail}" if path else f"{base}/{tail}"


def encode_query(params: list[tuple[str, str]]) -> str:
    safe = "${}:,|-()"
    return "&".join(
        f"{urllib.parse.quote(key, safe='')}={urllib.parse.quote(str(value), safe=safe)}"
        for key, value in params
    )


def playlist_url(channel: Channel, mode: str) -> str:
    if mode == "rtp":
        return channel.rtp
    addr = parse_rtp_addr(channel.rtp)
    if mode == "proxy":
        return join_base_path(MULTICAST_PROXY_BASE_URL, MULTICAST_PROXY_PATH, addr)
    if mode == "rtp2httpd":
        return join_base_path(RTP2HTTPD_BASE_URL, RTP2HTTPD_PATH, addr)
    raise ValueError(f"未知播放列表模式：{mode}")


def rtp2httpd_http_proxy_url(url: str) -> str:
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return url
    tail = f"{parsed.netloc}{parsed.path or '/'}"
    proxied = join_base_path(RTP2HTTPD_BASE_URL, RTP2HTTPD_HTTP_PATH, tail)
    if parsed.query:
        proxied = f"{proxied}?{parsed.query}"
    if parsed.fragment:
        proxied = f"{proxied}#{parsed.fragment}"
    return proxied


def effective_legacy_user_token() -> str:
    return first_non_empty(LEGACY_USER_TOKEN, HUAWEI_RUNTIME_USER_TOKEN)


def effective_huawei_redirect_base_url() -> str:
    return first_non_empty(HUAWEI_RUNTIME_REDIRECT_BASE_URL, HUAWEI_REDIRECT_BASE_URL)


def zte_catchup_source(channel: Channel) -> str:
    if not ZTE_CATCHUP_SOURCE_TEMPLATE:
        return ""
    if "{ztecode}" in ZTE_CATCHUP_SOURCE_TEMPLATE and not channel.ztecode:
        return ""
    try:
        return ZTE_CATCHUP_SOURCE_TEMPLATE.format(
            prefix=ZTE_CATCHUP_SOURCE_PREFIX.rstrip("/"),
            ztecode=urllib.parse.quote(channel.ztecode, safe=""),
            hwmediaid=urllib.parse.quote(channel.hwmediaid, safe=""),
            hwcode=urllib.parse.quote(channel.hwcode, safe=""),
            channel_id=urllib.parse.quote(channel.id, safe=""),
        )
    except Exception:
        return ""


def huawei_redirect_catchup_source(channel: Channel) -> str:
    token = effective_legacy_user_token()
    base_url = effective_huawei_redirect_base_url()
    if not token or not base_url:
        return ""
    if not channel.hwcode or not channel.hwmediaid:
        return ""
    base_url = base_url.rstrip("/")
    url = f"{base_url}/EPG/MediaService/RedirectPlay.jsp"
    params = [
        ("UserToken", token),
        ("UserName", HUAWEI_AUTH_USER_ID),
        ("ContentType", "lookback"),
        ("ContentID", channel.hwcode),
        ("PlaySeek", HUAWEI_PLAYSEEK_TEMPLATE),
    ]
    if HUAWEI_REDIRECT_INCLUDE_OPTIONAL_PARAMS:
        params.extend(
            [
                ("CPID", HUAWEI_CPID),
                ("MediaID", channel.hwmediaid),
                ("NPT", "-X"),
            ]
        )
    query = encode_query(params)
    return f"{url}?{query}"


def catchup_source(channel: Channel, mode_override: str | None = None) -> str:
    if not INCLUDE_CATCHUP:
        return ""
    if not channel.supports_catchup:
        return ""
    mode = (mode_override or CATCHUP_MODE).strip().lower()
    if mode == "zte":
        return zte_catchup_source(channel)
    if mode in {"huawei_only", "huawei_redirect_only"}:
        return huawei_redirect_catchup_source(channel)
    if mode in {"huawei_redirect", "huawei", "redirect"}:
        source = huawei_redirect_catchup_source(channel)
        return source or (zte_catchup_source(channel) if FALLBACK_TO_ZTE_CATCHUP else "")
    if mode == "auto":
        return huawei_redirect_catchup_source(channel) or zte_catchup_source(channel)
    return ""


def print_catchup_summary(channels: list[Channel]) -> None:
    if not INCLUDE_CATCHUP:
        print("移动回看：已关闭")
        return

    supported = [channel for channel in channels if channel.supports_catchup]
    mode = CATCHUP_MODE.strip().lower()
    if mode in {"huawei_redirect", "huawei", "redirect", "auto"}:
        token = effective_legacy_user_token()
        capable = [channel for channel in supported if channel.hwcode and channel.hwmediaid]
        if token:
            print(
                "移动回看：华为 RedirectPlay 模式，"
                f"可写入 {len(capable)} 个频道，入口={effective_huawei_redirect_base_url()}"
            )
        elif mode == "auto":
            print(
                "移动回看：未配置 LEGACY_USER_TOKEN，"
                "本次会尝试回退中兴模板"
            )
        elif FALLBACK_TO_ZTE_CATCHUP:
            print(
                "移动回看：未配置 LEGACY_USER_TOKEN，"
                "本次会按配置回退中兴模板"
            )
        else:
            print(
                "移动回看：未配置 LEGACY_USER_TOKEN，"
                "本次不会写入华为回看地址"
            )
    elif mode == "zte":
        capable = [channel for channel in supported if channel.ztecode]
        print(f"移动回看：中兴 ztecode 模式，可写入 {len(capable)} 个频道")
    else:
        print(f"移动回看：未知模式 {CATCHUP_MODE!r}，本次不会写入回看地址")


def count_channels_with_catchup(channels: list[Channel], catchup_mode: str | None = None) -> int:
    return sum(1 for channel in channels if catchup_source(channel, catchup_mode))


def count_external_channels_with_catchup(channels: list[ExternalChannel]) -> int:
    return sum(1 for channel in channels if "catchup-source" in channel.attrs)


def m3u_attr(value: str) -> str:
    # M3U 不是 XML，URL 里的 & 必须保持原样；这里只避免属性引号截断。
    return str(value).replace('"', "'")


def extinf_line(
    channel: Channel,
    catchup_mode: str | None = None,
    proxy_catchup_with_rtp2httpd: bool = False,
) -> str:
    attrs = [
        "#EXTINF:-1",
        f'tvg-id="{m3u_attr(channel.id)}"',
        f'tvg-name="{m3u_attr(channel.name)}"',
        f'tvg-chno="{m3u_attr(channel.number)}"',
        f'tvg-logo="{m3u_attr(channel.icon)}"',
    ]
    if channel.ztecode:
        attrs.append(f'ztecode="{m3u_attr(channel.ztecode)}"')
    if channel.hwcode:
        attrs.append(f'hwcode="{m3u_attr(channel.hwcode)}"')
    if channel.hwmediaid:
        attrs.append(f'hwmediaid="{m3u_attr(channel.hwmediaid)}"')
    source = catchup_source(channel, catchup_mode)
    if source:
        if proxy_catchup_with_rtp2httpd:
            source = rtp2httpd_http_proxy_url(source)
        attrs.append('catchup="default"')
        attrs.append(f'catchup-source="{m3u_attr(source)}"')
    attrs.append(f'group-title="{m3u_attr(group_title(channel.name))}",{channel.name}')
    return " ".join(attrs)


def write_m3u(
    path: Path,
    channels: list[Channel],
    mode: str,
    external_channels: list[ExternalChannel],
    catchup_mode: str | None = None,
    proxy_catchup_with_rtp2httpd: bool = False,
) -> None:
    lines = [f'#EXTM3U x-tvg-url="{M3U_XMLTV_URL}"']
    catchup_count = 0
    for channel in channels:
        url = playlist_url(channel, mode)
        if not url:
            continue
        if catchup_source(channel, catchup_mode):
            catchup_count += 1
        lines.append(extinf_line(channel, catchup_mode, proxy_catchup_with_rtp2httpd))
        lines.append(url)
    for channel in external_channels:
        lines.extend(external_m3u_lines(channel))
    catchup_count += count_external_channels_with_catchup(external_channels)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"已写入 {path}（{sum(1 for line in lines if line.startswith('#EXTINF'))} 个频道，"
        f"{catchup_count} 个回看）"
    )


def xmltv_time(value: str) -> str:
    value = re.sub(r"\D", "", value)
    if len(value) >= 14:
        return f"{value[:14]} +0800"
    if len(value) == 12:
        return f"{value}00 +0800"
    return f"{value:<14} +0800"


def write_xmltv(path: Path, channels: list[Channel]) -> bytes:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append('<tv generator-info-name="gdcmiptv.py" source-info-name="gdcmiptv.py">')
    for channel in channels:
        lines.append(f'  <channel id="{escape(channel.id)}">')
        lines.append(f"    <display-name>{escape(channel.name)}</display-name>")
        if channel.icon:
            lines.append(f'    <icon src="{escape(channel.icon)}" />')
        lines.append("  </channel>")
    count = 0
    for channel in channels:
        for program in channel.epg:
            lines.append(
                f'  <programme start="{xmltv_time(program.start)}" '
                f'stop="{xmltv_time(program.stop)}" channel="{escape(channel.id)}">'
            )
            lines.append(f'    <title lang="zh">{escape(program.title)}</title>')
            if program.desc:
                lines.append(f'    <desc lang="zh">{escape(program.desc)}</desc>')
            lines.append("  </programme>")
            count += 1
    lines.append("</tv>")
    data = ("\n".join(lines) + "\n").encode("utf-8")
    path.write_bytes(data)
    print(f"已写入 {path}（{len(channels)} 个频道，{count} 条节目）")
    return data


def write_gzip(path: Path, data: bytes) -> None:
    with gzip.open(path, "wb") as f:
        f.write(data)
    print(f"已写入 {path}")


def mask_value(value: str) -> str:
    value = value.strip()
    if not value:
        return "<未配置>"
    if len(value) <= 6:
        return value[0] + "***"
    return value[:3] + "***" + value[-3:]


def validate_config() -> None:
    required = [
        ("USER_ID", USER_ID),
        ("MAC", MAC),
        ("STB_ID", STB_ID),
        ("STB_TYPE", STB_TYPE),
        ("STB_IP", STB_IP),
        ("AREA_CODE", AREA_CODE),
        ("SOFTWARE_VERSION", SOFTWARE_VERSION),
    ]
    missing = [name for name, value in required if not value.strip()]
    if not missing and should_prepare_huawei_redirect() and HUAWEI_AUTH_AUTO_LOGIN and not effective_legacy_user_token():
        huawei_required = [
            ("HUAWEI_AUTH_USER_ID", HUAWEI_AUTH_USER_ID),
            ("HUAWEI_AUTH_PASSWORD", HUAWEI_AUTH_PASSWORD),
            ("HUAWEI_AUTH_MAC", HUAWEI_AUTH_MAC),
            ("HUAWEI_AUTH_STB_ID", HUAWEI_AUTH_STB_ID),
            ("HUAWEI_AUTH_STB_TYPE", HUAWEI_AUTH_STB_TYPE),
            ("HUAWEI_AUTH_STB_IP", HUAWEI_AUTH_STB_IP),
            ("HUAWEI_AUTH_AREA_CODE", HUAWEI_AUTH_AREA_CODE),
            ("HUAWEI_AUTH_SOFTWARE_VERSION", HUAWEI_AUTH_SOFTWARE_VERSION),
        ]
        missing.extend(name for name, value in huawei_required if not value.strip())
    if missing:
        raise RuntimeError(
            "缺少广东移动配置："
            + ", ".join(dict.fromkeys(missing))
            + "。请在脚本顶部“用户配置”区域填写后再运行。"
        )


# =========================
# 主流程
# =========================


def main() -> int:
    validate_config()
    out_dir = Path.cwd()
    print(f"输出目录：{out_dir}")
    print(
        "用户信息："
        f"UserID={mask_value(USER_ID)}，"
        f"MAC={mask_value(MAC)}，"
        f"STBID={mask_value(STB_ID)}，"
        f"STBType={STB_TYPE or '<未配置>'}"
    )
    if should_prepare_huawei_redirect():
        print(
            "华为回看授权："
            f"UserID={mask_value(HUAWEI_AUTH_USER_ID)}，"
            f"MAC={mask_value(HUAWEI_AUTH_MAC)}，"
            f"STBID={mask_value(HUAWEI_AUTH_STB_ID)}，"
            f"STBType={HUAWEI_AUTH_STB_TYPE}，"
            f"AreaCode={HUAWEI_AUTH_AREA_CODE}"
        )

    opener = build_opener()
    if DO_AAA_LOGIN:
        try:
            aaa_login(opener)
        except Exception as e:
            if not CONTINUE_WITHOUT_LOGIN:
                raise
            print(f"警告：AAA 登录失败，继续尝试频道接口：{e}")
    prepare_huawei_redirect(opener)

    channels = download_channels(opener)
    channels = sort_channels(channels)
    print_catchup_summary(channels)
    if DOWNLOAD_EPG:
        fetch_all_epg(channels)
        fill_empty_epg_with_placeholder(channels)
    else:
        print("已跳过节目表下载")
    external_channels = load_external_channels()

    if GENERATE_RTP_M3U:
        write_m3u(out_dir / OUTPUT_RTP_M3U, channels, "rtp", external_channels)
    if GENERATE_PROXY_M3U:
        write_m3u(out_dir / OUTPUT_PROXY_M3U, channels, "proxy", external_channels)
    if GENERATE_RTP2HTTPD_M3U:
        write_m3u(
            out_dir / OUTPUT_RTP2HTTPD_M3U,
            channels,
            "rtp2httpd",
            external_channels,
            proxy_catchup_with_rtp2httpd=RTP2HTTPD_PROXY_CATCHUP_FOR_RTP2HTTPD_M3U,
        )
    if GENERATE_HUAWEI_MULTICAST_M3U:
        write_m3u(
            out_dir / OUTPUT_HUAWEI_MULTICAST_M3U,
            channels,
            "proxy",
            external_channels,
            "huawei_only",
        )
    if GENERATE_HUAWEI_RTP2HTTPD_M3U:
        write_m3u(
            out_dir / OUTPUT_RTP2HTTPD_HUAWEI_M3U,
            channels,
            "rtp2httpd",
            external_channels,
            "huawei_only",
            RTP2HTTPD_PROXY_CATCHUP_FOR_RTP2HTTPD_M3U,
        )
    if GENERATE_XMLTV:
        xml_data = write_xmltv(out_dir / OUTPUT_XMLTV, channels)
        if GENERATE_XMLTV_GZ:
            write_gzip(out_dir / OUTPUT_XMLTV_GZ, xml_data)
    else:
        print("已跳过 XMLTV 写入")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
