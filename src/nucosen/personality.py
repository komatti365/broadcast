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
from random import choice, randint, shuffle, uniform
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

def intConfig(key, default=0):
    try:
        val = config(key, default="")
        if not val or not val.strip():
            return default
        return int(val)
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
    
    # 設定値の動的取得 (安全なintConfigを使用)
    minor_min_view = intConfig("MINOR_MIN_VIEW", default=100)
    minor_min_mylist = intConfig("MINOR_MIN_MYLIST", default=10)
    minor_min_like = intConfig("MINOR_MIN_LIKE", default=10)

    # ソート順の多様化
    sort_options = [
        "-lastCommentTime",  # 最終コメント順（最近アクティブ）
        "-startTime",        # 投稿日時の新しい順（比較的新しい）
        "+startTime",        # 投稿日時の古い順（懐かしい）
        "-viewCounter",      # 再生数の多い順（人気・定番）
        "-mylistCounter",    # マイリスト数の多い順（支持されている名曲含む）
        "-commentCounter",   # コメント数の多い順（賑やか）
        "-likeCounter",      # いいね！数の多い順（評価が高い）
        "+viewCounter",      # 【マイナー発掘】再生数の少ない順
        "+mylistCounter",    # 【マイナー発掘】マイリスト数の少ない順
        "+likeCounter"       # 【マイナー発掘】いいね！数の少ない順
    ]
    selected_sort = choice(sort_options)

    # ソート順に応じたオフセット調整および足切りフィルタの追加
    extra_filters = {}
    if selected_sort in ("-viewCounter", "-mylistCounter", "-commentCounter", "-likeCounter"):
        # 人気順などは上位すぎる部分を避けて中堅も拾えるように広めに設定
        offset = randint(0, 500)
    elif selected_sort == "+startTime":
        # 古い順は最初期すぎるエラー（最古の動画など）を避けつつ発掘
        offset = randint(0, 300)
    elif selected_sort == "+viewCounter":
        # マイナー発掘: 設定された最低再生数以上の動画から少ない順で取得
        extra_filters["filters[viewCounter][gte]"] = minor_min_view
        offset = randint(0, 100)
    elif selected_sort == "+mylistCounter":
        # マイナー発掘: 設定された最低マイリスト以上の動画から少ない順で取得
        extra_filters["filters[mylistCounter][gte]"] = minor_min_mylist
        offset = randint(0, 100)
    elif selected_sort == "+likeCounter":
        # マイナー発掘: 設定された最低いいね！以上の動画から少ない順で取得
        extra_filters["filters[likeCounter][gte]"] = minor_min_like
        offset = randint(0, 100)
    else:
        offset = randint(0, 400)

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
        "_sort": selected_sort,
        "_context": UserAgent,
        "_limit": "30",
        "_offset": offset
    }
    # マイナー発掘用足切りフィルタを反映
    payload.update(extra_filters)

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

    # 【レビュー反映】オフセット超過により検索結果が空になった場合の自動リカバリー
    total_count = result.get("meta", {}).get("totalCount", 0)
    if not result.get("data") and total_count > 0:
        # 総件数を超えない安全なオフセットをその場で算出（_limit=30分を考慮、1600の上限にも配慮）
        safe_max_offset = max(0, min(total_count - 30, 1500))
        new_offset = randint(0, safe_max_offset) if safe_max_offset > 0 else 0
        
        getLogger(__name__).info(
            "【リカバリー】オフセット超過を検知しました (総件数: %d, 指定オフセット: %d)。安全なオフセット (%d) で再検索します。タグ: %s",
            total_count, offset, new_offset, tag
        )
        
        payload["_offset"] = new_offset
        nicovideo_delay()
        response = get(url, headers=header, params=payload)
        response.raise_for_status()
        try:
            result = dict(response.json())
        except Exception as e:
            raise RetryRequested("スナップショット再検索のレスポンス解析に失敗しました: {0}".format(e))
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
    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst)
    latest_update_jst = now_jst.replace(hour=5, minute=0, second=0, microsecond=0)
    if now_jst < latest_update_jst:
        latest_update_jst = latest_update_jst - timedelta(days=1)
        
    lte_str = latest_update_jst.astimezone(timezone.utc).isoformat()
    gte_time = latest_update_jst - timedelta(hours=maxAgeHours)
    gte_str = gte_time.astimezone(timezone.utc).isoformat()

    # 設定値の動的取得 (安全なintConfigを使用)
    minor_min_view = intConfig("MINOR_MIN_VIEW", default=100)

    for tag, target_type in search_targets:
        # 新着向けのソート順多様化
        new_arrival_sort_options = [
            "-startTime",      # 投稿日時が新しい順
            "-viewCounter",    # 再生数が多い順
            "-mylistCounter",  # マイリスト数が多い順
            "-commentCounter", # コメント数が多い順
            "-likeCounter",    # いいね！数が多い順
            "+viewCounter"     # 再生数が少ない順（未発掘新着）
        ]
        selected_sort = choice(new_arrival_sort_options)

        # レビュー反映：新着取りこぼし防止のため、オフセットは常に 0 に固定
        offset = 0

        payload = {
            "q": tag,
            "targets": target_type,
            "fields": "contentId,startTime",
            "filters[lengthSeconds][gte]": minimumAllowableDuration,
            "filters[lengthSeconds][lte]": maximumAllowableDuration,
            "filters[startTime][gte]": gte_str,
            "filters[startTime][lte]": lte_str,
            "_sort": selected_sort,
            "_context": UserAgent,
            "_limit": "30",
            "_offset": offset
        }

        # 新着の昇順時は母数が少ないため、動的最低再生数の10%（最低でも10再生）を安全に足切り設定
        if selected_sort == "+viewCounter":
            payload["filters[viewCounter][gte]"] = max(10, int(minor_min_view * 0.1))
        
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
            
    # 投稿日時でのソートを廃止し、シャッフルすることで時間的な偏りをなくす
    shuffle(unique_candidates)

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

