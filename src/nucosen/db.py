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
from os import getcwd
from re import match
from typing import Any, Dict, Iterable, List, Optional
from datetime import datetime, timezone

from decouple import AutoConfig
from requests import delete, get, patch, post
from requests.exceptions import ConnectionError as ConnError
from requests.exceptions import HTTPError
from retry import retry

NetworkErrors = (HTTPError, ConnError)


class RestDbIo(object):
    # TODO - 非同期実行ができるリクエストにスレッドを使って高速化
    def __init__(self):
        config = AutoConfig(getcwd())

        def get_secret(key: str, default=None):
            from pathlib import Path
            file_path = str(config(f"{key}_FILE", default=""))
            if file_path:
                p = Path(file_path)
                if p.exists():
                    try:
                        with p.open("r", encoding="utf-8") as f:
                            return f.read().strip()
                    except Exception as e:
                        getLogger(__name__).warning(f"{key}_FILE の読み込みに失敗しました: {e}")
                else:
                    getLogger(__name__).warning(f"{key}_FILE に指定されたパスが見つかりません: {file_path}")
            return config(key, default=default)

        queueUrl = config("QUEUE_URL", default=None)
        requestUrl = config("REQUEST_URL", default=None)
        quotedUrl = config("QUOTED_URL", default=None)
        key = get_secret("DB_KEY", default=None)
        if None in (queueUrl, requestUrl, key):
            raise Exception("V0E 環境変数エラー {0} {1} {2}".format(
                queueUrl, requestUrl, key))
        header = {'x-apikey': str(key), 'cache-control': "no-cache"}

        self.isQueueUpdated: bool = True
        self.__queueUrl = str(queueUrl)
        self.__requestUrl = str(requestUrl)
        self.__quotedUrl = quotedUrl
        self.__settingsUrl = config("SETTINGS_URL", default=None)
        self.__nowplayingUrl = config("NOWPLAYING_URL", default=None)
        self.__header = header
        self.__dequeueCache: List[Dict[str, str]] = []

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".dequeue"))
    def dequeue(self) -> str | None:
        if self.isQueueUpdated:
            # 優先・エンキュー逆順
            query = '?q={}&h={"$orderby": {"priority": 1,"_id":-1}}'
            resp = get(self.__queueUrl + query, headers=self.__header)
            resp.raise_for_status()
            queues: List[Dict[str, str]] = resp.json()
            self.__dequeueCache = queues
            self.isQueueUpdated = False

        if len(self.__dequeueCache) < 1:
            return None
        result = self.__dequeueCache.pop()
        self.__deleteQueueItem(result["_id"])
        return result["videoId"]

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".getQueueVideoIds"))
    def getQueueVideoIds(self) -> set[str]:
        query = '?h={"$fields":{"videoId":1}}'
        resp = get(self.__queueUrl + query, headers=self.__header)
        resp.raise_for_status()
        queues: List[Dict[str, str]] = resp.json()
        return {item["videoId"] for item in queues if item.get("videoId")}

    def getQueueCount(self) -> int:
        return len(self.getQueueVideoIds())

    @retry(NetworkErrors, tries=10, delay=1, backoff=2, logger=getLogger(__name__ + ".__deleteQueueItem"))
    def __deleteQueueItem(self, itemId: str):
        resp = delete(self.__queueUrl+"/"+itemId, headers=self.__header)
        resp.raise_for_status()

    def get_settings(self) -> Dict[str, str]:
        if self.__settingsUrl is None:
            return {}

        resp = get(self.__settingsUrl + "?q={}", headers=self.__header)
        resp.raise_for_status()
        documents: List[Dict[str, Any]] = resp.json()
        
        settings = {}
        for doc in documents:
            if "key" in doc and "value" in doc and doc["value"] is not None:
                settings[doc["key"]] = str(doc["value"])
        return settings

    def publish_settings(self, settings: Dict[str, str]):
        if self.__settingsUrl is None or len(settings) < 1:
            return

        resp = get(self.__settingsUrl + "?q={}", headers=self.__header)
        resp.raise_for_status()
        documents: List[Dict[str, Any]] = resp.json()
        
        existing_map = {doc["key"]: doc["_id"] for doc in documents if "key" in doc}
        
        for k, v in settings.items():
            payload = {"key": k, "value": str(v)}
            if k in existing_map:
                doc_id = existing_map[k]
                patch_resp = patch(self.__settingsUrl + "/" + str(doc_id), json=payload, headers=self.__header)
                patch_resp.raise_for_status()
            else:
                post_resp = post(self.__settingsUrl, json=payload, headers=self.__header)
                post_resp.raise_for_status()

    @retry(NetworkErrors, tries=10, delay=1, backoff=2, logger=getLogger(__name__ + ".enqueueByList"))
    def enqueueByList(self, items: Iterable[str]):
        existingVideoIds = self.getQueueVideoIds()
        payload = list()
        for item in items:
            if not match("^[a-z][a-z][0-9]+$", item):
                getLogger(__name__).error("E09 通常エンキューのアボート {0}".format(item))
                continue
            if item in existingVideoIds:
                getLogger(__name__).debug("重複動画をスキップしました: {0}".format(item))
                continue
            payload.append({"videoId": item})
            existingVideoIds.add(item)
        if len(payload) < 1:
            return
        resp = post(self.__queueUrl, json=payload, headers=self.__header)
        resp.raise_for_status()
        self.isQueueUpdated = True

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".priorityEnqueue"))
    def priorityEnqueue(self, item: str):
        if not match("^[a-z][a-z][0-9]+$", item):
            getLogger(__name__).error("E01 優先エンキューのアボート {0}".format(item))
            return
        payload = {"videoId": item, "priority": True}
        resp = post(self.__queueUrl, json=payload, headers=self.__header)
        resp.raise_for_status()
        self.isQueueUpdated = True

    @retry(NetworkErrors, tries=10, delay=1, backoff=2, logger=getLogger(__name__ + ".getAndResetRequests"))
    def getAndResetRequests(self) -> Optional[List[str]]:
        resp = get(self.__requestUrl, headers=self.__header)
        resp.raise_for_status()
        results: List[Dict[str, str]] = resp.json()
        if len(results) < 1:
            return None
        deletionIds = []
        requestVideoIds = []
        for result in results:
            deletionIds.append(result["_id"])
            requestVideoIds.append(result["videoId"])
        self.__deleteRequestItems(deletionIds)
        return requestVideoIds

    @retry(NetworkErrors, tries=10, delay=1, backoff=2, logger=getLogger(__name__ + ".__deleteRequestItems"))
    def __deleteRequestItems(self, items: List[str]):
        resp = delete(
            self.__requestUrl+"/*", json=items, headers=self.__header)
        resp.raise_for_status()

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".recordQuotedVideo"))
    def recordQuotedVideo(self, videoId: str, liveId: str) -> bool:
        """Record a quoted/broadcasted video to the database.
        
        Args:
            videoId: The video ID (e.g., 'sm12345678')
            liveId: The live broadcast ID (e.g., 'lv123456789')
            
        Returns:
            bool: True if recording was successful, False if database URL is not configured
        """
        if self.__quotedUrl is None:
            getLogger(__name__).debug("QUOTED_URL が設定されていないため、引用済み動画の記録をスキップします")
            return False
        
        if not match("^[a-z][a-z][0-9]+$", videoId):
            getLogger(__name__).error("E02 引用済み動画記録のアボート 無効な動画ID {0}".format(videoId))
            return False
        
        try:
            payload = {
                "videoId": videoId,
                "liveId": liveId,
                "quotedAt": datetime.now(timezone.utc).isoformat()
            }
            resp = post(self.__quotedUrl, json=payload, headers=self.__header)
            resp.raise_for_status()
            getLogger(__name__).debug("引用済み動画を記録しました: {0} (Live: {1})".format(videoId, liveId))
            return True
        except Exception as e:
            getLogger(__name__).error("引用済み動画の記録に失敗しました: {0}".format(e))
            return False

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".updateNowPlaying"))
    def updateNowPlaying(self, videoId: str, title: str, duration: int = 0) -> Optional[str]:
        if self.__nowplayingUrl is None:
            return None
        
        try:
            # 常に最新の1件にするため、既存の情報を全クリア
            delete_resp = delete(self.__nowplayingUrl + "/*", headers=self.__header)
            delete_resp.raise_for_status()
            
            payload = {
                "videoId": videoId,
                "title": title,
                "duration": duration,
                "remainingTime": duration
            }
            post_resp = post(self.__nowplayingUrl, json=payload, headers=self.__header)
            post_resp.raise_for_status()
            
            data = post_resp.json()
            if isinstance(data, list) and len(data) > 0:
                return data[0].get("_id")
            elif isinstance(data, dict):
                return data.get("_id")
            return None
        except Exception as e:
            getLogger(__name__).error("nowplayingの更新に失敗しました: {0}".format(e))
            return None

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".patchNowPlayingTime"))
    def patchNowPlayingTime(self, doc_id: str, remainingTime: int) -> bool:
        if self.__nowplayingUrl is None or not doc_id:
            return False
            
        try:
            patch_resp = patch(self.__nowplayingUrl + "/" + str(doc_id), json={"remainingTime": remainingTime}, headers=self.__header)
            patch_resp.raise_for_status()
            return True
        except Exception as e:
            getLogger(__name__).error("nowplayingの残り時間更新に失敗しました: {0}".format(e))
            return False

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".clearNowPlaying"))
    def clearNowPlaying(self) -> bool:
        if self.__nowplayingUrl is None:
            return False
        
        try:
            delete_resp = delete(self.__nowplayingUrl + "/*", headers=self.__header)
            delete_resp.raise_for_status()
            return True
        except Exception as e:
            getLogger(__name__).error("nowplayingのクリアに失敗しました: {0}".format(e))
            return False
