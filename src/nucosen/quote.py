"""
Copyright 2022 NUCOSen運営会議

This file is part of NUCOSen Broadcast.

NUCOSen Broadcast is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

NUCOSen Broadcast is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with NUCOSen Broadcast.  If not, see <https://www.gnu.org/licenses/>.
"""

from decouple import AutoConfig
from os import getcwd
from datetime import timedelta
from logging import getLogger
from typing import Optional, Tuple, Dict, Any
from time import sleep
from random import uniform

from requests import delete, get, post, patch
from requests.exceptions import ConnectionError as ConnError
from requests.exceptions import HTTPError
from retry import retry

from nucosen.sessionCookie import Session

from defusedxml import ElementTree as ET


class ReLoggedIn(Exception):
    pass


class RetryRequired(Exception):
    pass


config = AutoConfig(getcwd())
NetworkErrors = (HTTPError, ConnError, ReLoggedIn, RetryRequired)

def boolConfig(key, default=False):
    value = config(key, default=default)
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes", "on")


def floatConfig(key, default=0.0):
    try:
        return float(config(key, default=str(default)))
    except (TypeError, ValueError):
        return default

NICO_REQUEST_DELAY = max(0.0, floatConfig("NICO_REQUEST_DELAY", 1.0))

def nicovideo_delay():
    if NICO_REQUEST_DELAY <= 0:
        return
    sleep(NICO_REQUEST_DELAY * uniform(0.9, 1.1))

sourceList = (
    ("quote", "self") if boolConfig("QUOTE_MAIN", False)
    else ("self", "quote")
)
layoutSettings = {
    "main": {
        "source": sourceList[0],
        "volume": floatConfig("MAIN_VOLUME", 0.5)
    },
    "sub": {
        "source": sourceList[1],
        "volume": floatConfig("SUB_VOLUME", 0.5),
        "isSoundOnly": boolConfig("SUB_SOUND_ONLY", False)
    }
}
quoteBotUri = \
    "https://services-eapi.spi.nicovideo.jp/v1/services/quotation/contents/{0}/bots" \
    if config("USE_OLD_QUOTE_BOT",default=False) else \
    "https://services-eapi.spi.nicovideo.jp/v1/tools/live/contents/{0}/quotation"

@retry(NetworkErrors, tries=10, delay=1, backoff=2, logger=getLogger(__name__ + ".getCurrent"))
def getCurrent(liveId: str, session: Session) -> Optional[str]:
    url = quoteBotUri
    nicovideo_delay()
    resp = get(url.format(liveId), cookies=session.cookie)
    if resp.status_code == 401:
        session.login()
        raise ReLoggedIn("L10 ログインセッション更新")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    quotationData = dict(resp.json())
    quotationContent = quotationData.get("currentContent", {}).get("id", None)
    return quotationContent


@retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".stop"))
def stop(liveId: str, session: Session):
    url = quoteBotUri
    nicovideo_delay()
    resp = delete(url.format(liveId), cookies=session.cookie)
    if resp.status_code == 401:
        session.login()
        raise ReLoggedIn("L11 ログインセッション更新")
    if resp.status_code == 404:
        getLogger(__name__).info("停止すべき引用が存在しませんでした。")
        return
    resp.raise_for_status()


@retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".checkNgTag"))
def checkNgTag(videoId: str, ngTags: set) -> bool:
    url = "https://ext.nicovideo.jp/api/getthumbinfo/{0}"
    nicovideo_delay()
    resp = get(url.format(videoId))
    resp.raise_for_status()
    videoThumbInfo = ET.fromstring(resp.text)
    tagsElement = videoThumbInfo.findall(".//tag")
    tags = set(map(lambda x: x.text, tagsElement))
    return True if len(ngTags & tags) == 0 else False


def boolConfig(key, default):
    return bool(AutoConfig(getcwd())(key, default))


@retry(NetworkErrors, tries=3, delay=1, backoff=2, logger=getLogger(__name__ + ".getThumbInfo"))
def getThumbInfo(videoId: str) -> Dict[str, Any]:
    url = "https://ext.nicovideo.jp/api/getthumbinfo/{0}"
    nicovideo_delay()
    resp = get(url.format(videoId))
    resp.raise_for_status()
    videoThumbInfo = ET.fromstring(resp.text)
    error = videoThumbInfo.findtext("error")
    if error:
        raise ValueError("getthumbinfo error: {0}".format(error))

    thumb = videoThumbInfo.find("thumb")
    if thumb is None:
        raise ValueError("getthumbinfo response missing thumb element")

    def get_text(name: str, default: str = "") -> str:
        element = thumb.find(name)
        return element.text if element is not None and element.text is not None else default

    def get_int(name: str) -> int:
        try:
            return int(get_text(name, "0"))
        except ValueError:
            return 0

    tags = [tag.text for tag in thumb.findall("tags/tag") if tag.text]
    length_text = get_text("length")
    length_seconds = 0
    if length_text:
        parts = length_text.split(":")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            length_seconds = int(parts[0]) * 60 + int(parts[1])

    return {
        "id": get_text("video_id"),
        "title": get_text("title"),
        "description": get_text("description"),
        "thumbnail_url": get_text("thumbnail_url"),
        "first_retrieve": get_text("first_retrieve"),
        "length": length_text,
        "length_seconds": length_seconds,
        "view_counter": get_int("view_counter"),
        "comment_num": get_int("comment_num"),
        "mylist_counter": get_int("mylist_counter"),
        "user_id": get_text("user_id"),
        "user_nickname": get_text("user_nickname"),
        "tags": tags,
    }


@retry(NetworkErrors, tries=3, delay=1, backoff=2, logger=getLogger(__name__ + ".getVideoInfo"))
def getVideoInfo(videoId: str, session: Session, ngTags: set) -> Tuple[bool, timedelta, str]:
    # NOTE - 戻り値: (引用可能性, 動画長, 紹介メッセージ)
    url = "https://services-eapi.spi.nicovideo.jp/v1/tools/live/quote/services/video/contents/{0}"
    nicovideo_delay()
    resp = get(url.format(videoId), cookies=session.cookie)
    if resp.status_code == 401:
        session.login()
        raise ReLoggedIn("L12 ログインセッション更新")
    if resp.status_code == 500:
        return (False, timedelta(seconds=0), "ERROR")
    resp.raise_for_status()
    videoData: Dict[str, Any] = dict(resp.json()).get("data", {})
    if boolConfig("USE_OLD_VINFO_API", False):
        # NOTE - This is old api
        if not "N_Q_GVI_WARNED_OLD_API" in globals():
            getLogger(__name__).warning("旧APIの呼び出し")
            global N_Q_GVI_WARNED_OLD_API
            N_Q_GVI_WARNED_OLD_API = True
        quotable = boolConfig("IGNORE_QUOTABLE_CHECK", False)\
            or videoData.get("quotable", False)
    else:
        url = "https://services-eapi.spi.nicovideo.jp/v1/services/select_content/video/{0}"
        nicovideo_delay()
        resp = get(url.format(videoId), cookies=session.cookie)
        if resp.status_code == 401:
            session.login()
            raise ReLoggedIn("L15 ログインセッション更新")
        if resp.status_code == 500:
            return (False, timedelta(seconds=0), "ERROR")
        resp.raise_for_status()
        newApiVideoData: Dict[str, Any] = dict(resp.json())\
            .get("data", {}).get("content", {})
        quotable = boolConfig("IGNORE_QUOTABLE_CHECK", False)\
            or newApiVideoData.get("isQuotableByOtherContents", False)
    # NOTE : 重いので引用可能動画のみNGタグの処理を行う
    if quotable:
        quotable = checkNgTag(videoId, ngTags)
    length = timedelta(seconds=videoData.get("length", 0))
    introducing = "{0} / {1}".format(
        videoData.get("title", "（無題）"),
        videoData.get("id", "sm0")
    )
    return (quotable, length, introducing)


@retry(NetworkErrors, tries=10, delay=5, backoff=2, logger=getLogger(__name__ + ".once"))
def once(liveId: str, videoId: str, session: Session) -> timedelta:
    stop(liveId, session)

    url = quoteBotUri
    payload = {
        "layout": layoutSettings,
        "contents": [
            {
                "id": videoId,
                "type": "video"
            }
        ]
    }
    sleep(1.5)
    nicovideo_delay()
    resp = post(url.format(liveId), json=payload, cookies=session.cookie)
    if resp.status_code == 409:
        resp = patch(
            (url + "/contents").format(liveId),
            json={"contents": [{"id": videoId, "type": "video"}]},
            cookies=session.cookie
        )
    if resp.status_code == 401:
        session.login()
        raise ReLoggedIn("L13 ログインセッション更新")

    if resp.status_code == 400:
        getLogger(__name__).info((
            "400 Error Log ==========\n" +
            "Target : {0}\n" +
            "Cookie : {1}\n" +
            "Response body ; \n{2}").format(
                url.format(liveId),
                "Bad" if session.getSessionString is None else "Good",
                resp.text
        ))
        raise RetryRequired("W01 引用拒否発生")

    resp.raise_for_status()
    postedVideoLength = getVideoInfo(videoId, session, set())[1]
    return postedVideoLength


def loop(liveId: str, videoId: str, session: Session):
    once(liveId, videoId, session)
    setLoop(liveId, session)


@retry(NetworkErrors, tries=10, delay=1, backoff=2, logger=getLogger(__name__ + ".setLoop"))
def setLoop(liveId: str, session: Session):
    sleep(1)
    nicovideo_delay()
    url = quoteBotUri + "/layout"
    payload = {
        "layout": layoutSettings,
        "repeat": True
    }
    resp = patch(url.format(liveId), json=payload, cookies=session.cookie)
    if resp.status_code == 401:
        session.login()
        raise ReLoggedIn("L14 ログインセッション更新")
    resp.raise_for_status()
