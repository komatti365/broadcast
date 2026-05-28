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

from logging import getLogger
from random import randint, shuffle, uniform
from time import sleep
from typing import List, Optional, Tuple

from requests import get
from requests.exceptions import ConnectionError as ConnError
from requests.exceptions import HTTPError
from retry import retry
from decouple import AutoConfig
from os import getcwd, environ

from nucosen import quote
from nucosen.sessionCookie import Session


class RetryRequested(Exception):
    pass


_configLoader = AutoConfig(getcwd())

def config(key, default=""):
    if key in environ:
        return str(environ[key])
    return str(_configLoader(key, default=default))
NetworkErrors = (HTTPError, ConnError, RetryRequested)


def floatConfig(key, default=0.0):
    try:
        return float(config(key, default=str(default)))
    except (TypeError, ValueError):
        return default

def nicovideo_delay():
    delay = max(0.0, floatConfig("NICO_REQUEST_DELAY", 1.0))
    if delay <= 0:
        return
    sleep(delay * uniform(0.9, 1.1))


UserAgent = str(config("NUCOSEN_UA_PREFIX", default="anonymous")
                ) + " / NUCOSen Broadcast Personality System"


def choiceFromRequests(requests: List[str], choicesNum: int) -> Optional[List[str]]:
    shuffle(requests)
    winner = list()
    for request in requests:
        if request in winner:
            continue
        winner.append(request)
        if len(winner) >= choicesNum:
            break
    return winner if len(winner) else None


@retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".randomSelection"))
def randomSelection(tags: List[str], session: Session, ngTags: set, cooldownVideos: set = None, categoryTags: List[str] = None, genreTags: List[str] = None) -> Tuple[str, str]:
    if cooldownVideos is None:
        cooldownVideos = set()
    _tags = tags.copy()
    url = "https://snapshot.search.nicovideo.jp/api/v2/snapshot/video/contents/search"
    header = {
        "User-Agent": UserAgent
    }
    shuffle(_tags)
    tag = _tags.pop()
    offset = randint(0, 90)
    minimumAllowableDuration = \
        int(config("MIN_ALLOWABLE_DURATION", default=45))
    maximumAllowableDuration = \
        int(config("MAX_ALLOWABLE_DURATION", default=10 * 60))
    if maximumAllowableDuration < minimumAllowableDuration:
        maximumAllowableDuration = minimumAllowableDuration + (10 * 60)
    payload = {
        "q": tag,
        "targets": "tagsExact",
        "fields": "contentId",
        "filters[lengthSeconds][gte]": minimumAllowableDuration,
        "filters[lengthSeconds][lte]": maximumAllowableDuration,
        "_sort": "-lastCommentTime",
        "_context": UserAgent,
        "_limit": "30",
        "_offset": offset
    }

    if categoryTags:
        for i, cat in enumerate(categoryTags):
            payload[f"filters[categoryTags][{i}]"] = cat

    if genreTags:
        for i, genre in enumerate(genreTags):
            payload[f"filters[genre][{i}]"] = genre

    ngVideos = str(config("NG_VIDEO_IDS",default="")).split(",")

    nicovideo_delay()
    response = get(url, headers=header, params=payload)
    if response.status_code == 503:
        return str(config("MAINTENANCE_VIDEO_ID", default="sm17759202")), tag
    response.raise_for_status()
    try:
        result = dict(response.json())
    except Exception as e:
        raise RetryRequested("スナップショット検索のレスポンス解析に失敗しました: {0}".format(e))
    winners: List[str] = []
    cooldown_fallback: List[str] = []
    for target in result['data']:
        content_id = target["contentId"]
        if content_id not in ngVideos:
            if content_id not in cooldownVideos:
                winners.append(content_id)
            else:
                cooldown_fallback.append(content_id)
    shuffle(winners)
    shuffle(cooldown_fallback)
    winners.extend(cooldown_fallback)
    if len(winners) == 0:
        raise RetryRequested("V30 セレクション失敗 {0} {1}".format(tag, offset))
    for winner in winners:
        if quote.getVideoInfo(winner, session, ngTags)[0] is True:
            return winner, tag
        getLogger(__name__).info("セレクションリジェクト {0}".format(winner))
    raise RetryRequested("V31 セレクション失敗 {0} {1}".format(tag, offset))
