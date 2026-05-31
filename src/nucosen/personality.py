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
def randomSelection(tags: List[str], session: Session, ngTags: set, cooldownVideos: set = None, categoryTags: List[str] = None, genreTags: List[str] = None, exactTags: List[str] = None, ngTagsExact: set = None) -> Tuple[str, str]:
    if cooldownVideos is None:
        cooldownVideos = set()
    url = "https://snapshot.search.nicovideo.jp/api/v2/snapshot/video/contents/search"
    header = {
        "User-Agent": UserAgent
    }
    
    # タグと検索方式（部分一致 vs 完全一致）のペアをマージしてシャッフル
    search_targets = []
    if tags:
        for t in tags:
            if t.strip():
                search_targets.append((t.strip(), "tags"))
    if exactTags:
        for t in exactTags:
            if t.strip():
                search_targets.append((t.strip(), "tagsExact"))
                
    shuffle(search_targets)
    if not search_targets:
        raise ValueError("検索対象のタグが設定されていません")
        
    tag, target_type = search_targets.pop()
    offset = randint(0, 90)
    minimumAllowableDuration = \
        int(config("MIN_ALLOWABLE_DURATION", default=45))
    maximumAllowableDuration = \
        int(config("MAX_ALLOWABLE_DURATION", default=10 * 60))
    if maximumAllowableDuration < minimumAllowableDuration:
        maximumAllowableDuration = minimumAllowableDuration + (10 * 60)
    payload = {
        "q": tag,
        "targets": target_type,
        "fields": "contentId",
        "filters[lengthSeconds][gte]": minimumAllowableDuration,
        "filters[lengthSeconds][lte]": maximumAllowableDuration,
        "_sort": "-lastCommentTime",
        "_context": UserAgent,
        "_limit": "30",
        "_offset": offset
    }

    if categoryTags:
        unique_categories = [c.strip() for c in dict.fromkeys(categoryTags) if c.strip()]
        for i, cat in enumerate(unique_categories):
            payload[f"filters[categoryTags][{i}]"] = cat

    if genreTags:
        unique_genres = [g.strip() for g in dict.fromkeys(genreTags) if g.strip()]
        for i, genre in enumerate(unique_genres):
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
        if quote.getVideoInfo(winner, session, ngTags, ngTagsExact)[0] is True:
            return winner, tag
        getLogger(__name__).info("セレクションリジェクト {0}".format(winner))
    raise RetryRequested("V31 セレクション失敗 {0} {1}".format(tag, offset))


@retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".selectNewArrivals"))
def selectNewArrivals(tags: List[str], session: Session, limit: int, ngTags: set, cooldownVideos: set = None, categoryTags: List[str] = None, genreTags: List[str] = None, exactTags: List[str] = None, ngTagsExact: set = None, maxAgeHours: int = 24) -> List[str]:
    """新着動画を指定数 (limit) 抽出します。"""
    if cooldownVideos is None:
        cooldownVideos = set()
    url = "https://snapshot.search.nicovideo.jp/api/v2/snapshot/video/contents/search"
    header = {
        "User-Agent": UserAgent
    }
    
    # 対象のタグ（部分一致、完全一致）をリストアップ
    search_targets = []
    if tags:
        for t in tags:
            if t.strip():
                search_targets.append((t.strip(), "tags"))
    if exactTags:
        for t in exactTags:
            if t.strip():
                search_targets.append((t.strip(), "tagsExact"))
                
    if not search_targets:
        getLogger(__name__).warning("新着動画抽出の検索対象タグが設定されていません")
        return []
        
    minimumAllowableDuration = int(config("MIN_ALLOWABLE_DURATION", default=45))
    maximumAllowableDuration = int(config("MAX_ALLOWABLE_DURATION", default=10 * 60))
    if maximumAllowableDuration < minimumAllowableDuration:
        maximumAllowableDuration = minimumAllowableDuration + (10 * 60)

    ngVideos = set(str(config("NG_VIDEO_IDS", default="")).split(","))

    candidates = []
    
    from datetime import datetime, timezone, timedelta
    
    # 直近のAPI更新時刻（朝5:00）を算出
    now_local = datetime.now()
    latest_update_local = now_local.replace(hour=5, minute=0, second=0, microsecond=0)
    if now_local < latest_update_local:
        latest_update_local = latest_update_local - timedelta(days=1)
        
    lte_str = latest_update_local.astimezone(timezone.utc).isoformat()
    gte_time = latest_update_local - timedelta(hours=maxAgeHours)
    gte_str = gte_time.astimezone(timezone.utc).isoformat()

    for tag, target_type in search_targets:
        payload = {
            "q": tag,
            "targets": target_type,
            "fields": "contentId,startTime",
            "filters[lengthSeconds][gte]": minimumAllowableDuration,
            "filters[lengthSeconds][lte]": maximumAllowableDuration,
            "filters[startTime][gte]": gte_str,
            "filters[startTime][lte]": lte_str,
            "_sort": "-startTime",  # 投稿日時の降順
            "_context": UserAgent,
            "_limit": "30",
            "_offset": 0
        }
        
        if categoryTags:
            unique_categories = [c.strip() for c in dict.fromkeys(categoryTags) if c.strip()]
            for i, cat in enumerate(unique_categories):
                payload[f"filters[categoryTags][{i}]"] = cat

        if genreTags:
            unique_genres = [g.strip() for g in dict.fromkeys(genreTags) if g.strip()]
            for i, genre in enumerate(unique_genres):
                payload[f"filters[genre][{i}]"] = genre

        try:
            nicovideo_delay()
            response = get(url, headers=header, params=payload)
            if response.status_code == 503:
                continue
            response.raise_for_status()
            result = dict(response.json())
            for target in result.get('data', []):
                content_id = target["contentId"]
                start_time_str = target.get("startTime", "")
                if content_id not in ngVideos and content_id not in cooldownVideos:
                    candidates.append({
                        "contentId": content_id,
                        "startTime": start_time_str
                    })
        except Exception as e:
            getLogger(__name__).warning("タグ「{0}」での新着取得に失敗しました: {1}".format(tag, e))

    if not candidates:
        return []

    # 重複排除と投稿日時の新しい順にソート
    seen = set()
    unique_candidates = []
    for c in candidates:
        if c["contentId"] not in seen:
            seen.add(c["contentId"])
            unique_candidates.append(c)
            
    # startTime (例: 2023-10-24T12:00:00+09:00) の文字列で降順ソート
    unique_candidates.sort(key=lambda x: x["startTime"], reverse=True)

    # 引用可能な動画をバリデーションしながら limit 件集める
    selected_videos = []
    for c in unique_candidates:
        winner = c["contentId"]
        try:
            if quote.getVideoInfo(winner, session, ngTags, ngTagsExact)[0] is True:
                selected_videos.append(winner)
                if len(selected_videos) >= limit:
                    break
            else:
                getLogger(__name__).info("新着セレクションリジェクト (引用不可): {0}".format(winner))
        except Exception as e:
            getLogger(__name__).warning("新着動画 {0} の引用可否確認に失敗しました: {1}".format(winner, e))

    return selected_videos

