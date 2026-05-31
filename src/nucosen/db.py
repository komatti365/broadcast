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
from concurrent.futures import ThreadPoolExecutor

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

        pickupQueueUrl = config("PICKUP_QUEUE_URL", default=None)
        if not pickupQueueUrl:
            pickupQueueUrl = queueUrl.replace("/queue", "/pickup_queue")

        backupQueueUrl = config("BACKUP_QUEUE_URL", default=None)
        if not backupQueueUrl:
            backupQueueUrl = queueUrl.replace("/queue", "/backup_queue")

        # 防御的バリデーション: 自動生成されたURLが本番キューURLと同一になっていないかチェック
        if str(pickupQueueUrl) == str(queueUrl):
            raise Exception("V0E 環境変数エラー: PICKUP_QUEUE_URL が本番キューURLと同一です。カスタムコレクション名を使用している場合は、個別に環境変数 PICKUP_QUEUE_URL を指定してください。")
        if str(backupQueueUrl) == str(queueUrl):
            raise Exception("V0E 環境変数エラー: BACKUP_QUEUE_URL が本番キューURLと同一です。カスタムコレクション名を使用している場合は、個別に環境変数 BACKUP_QUEUE_URL を指定してください。")

        self.__queueUrl = str(queueUrl)
        self.__requestUrl = str(requestUrl)
        self.__quotedUrl = quotedUrl
        self.__settingsUrl = config("SETTINGS_URL", default=None)
        self.__nowplayingUrl = config("NOWPLAYING_URL", default=None)
        self.__pickupQueueUrl = str(pickupQueueUrl)
        self.__backupQueueUrl = str(backupQueueUrl)
        self.__header = header
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="db_async")

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".dequeue"))
    def dequeue(self) -> str | None:
        # キャッシュ競合を防ぐため、常にDBから最優先レコードを1件だけ取得してデキュー
        # priority: -1 (True優先の降順), _id: 1 (古い順の昇順)
        query = '?q={}&h={"$orderby": {"priority": -1, "_id": 1}}&max=1'
        resp = get(self.__queueUrl + query, headers=self.__header)
        resp.raise_for_status()
        queues: List[Dict[str, str]] = resp.json()
        
        if not queues:
            return None
        result = queues[0]
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

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".get_settings"))
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

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".publish_settings"))
    def publish_settings(self, settings: Dict[str, str]):
        if self.__settingsUrl is None or len(settings) < 1:
            return

        resp = get(self.__settingsUrl + "?q={}", headers=self.__header)
        resp.raise_for_status()
        documents: List[Dict[str, Any]] = resp.json()
        
        # 値が変更されたかどうかを検知するため、IDと既存の値をマッピング
        existing_map = {doc["key"]: (doc["_id"], doc.get("value")) for doc in documents if "key" in doc}
        
        for k, v in settings.items():
            payload = {"key": k, "value": str(v)}
            if k in existing_map:
                doc_id, current_val = existing_map[k]
                # 値がすでに同一なら余計なPATCHリクエストを送信せずスキップ
                if str(current_val) == str(v):
                    continue
                patch_resp = patch(self.__settingsUrl + "/" + str(doc_id), json=payload, headers=self.__header)
                patch_resp.raise_for_status()
            else:
                post_resp = post(self.__settingsUrl, json=payload, headers=self.__header)
                post_resp.raise_for_status()

    @retry(NetworkErrors, tries=10, delay=1, backoff=2, logger=getLogger(__name__ + ".enqueueByList"))
    def enqueueByList(self, items: Iterable[str]):
        existingVideoIds = self.getQueueVideoIds()
        payload = list()
        from nucosen.quote import getThumbInfo
        for item in items:
            if not match("^[a-z][a-z][0-9]+$", item):
                getLogger(__name__).error("E09 通常エンキューのアボート {0}".format(item))
                continue
            if item in existingVideoIds:
                getLogger(__name__).debug("重複動画をスキップしました: {0}".format(item))
                continue
            
            title = None
            thumbnailUrl = None
            try:
                videoDetail = getThumbInfo(item)
                title = videoDetail.get("title")
                thumbnailUrl = videoDetail.get("thumbnail_url")
            except Exception as e:
                getLogger(__name__).warning("通常追加用の動画情報取得に失敗しました ({0}): {1}".format(item, e))
                
            data = {"videoId": item}
            if title:
                data["title"] = title
            if thumbnailUrl:
                data["thumbnailUrl"] = thumbnailUrl
            payload.append(data)
            existingVideoIds.add(item)
        if len(payload) < 1:
            return
        resp = post(self.__queueUrl, json=payload, headers=self.__header)
        resp.raise_for_status()

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".priorityEnqueue"))
    def priorityEnqueue(self, item: str):
        if not match("^[a-z][a-z][0-9]+$", item):
            getLogger(__name__).error("E01 優先エンキューのアボート {0}".format(item))
            return
        
        title = None
        thumbnailUrl = None
        try:
            from nucosen.quote import getThumbInfo
            videoDetail = getThumbInfo(item)
            title = videoDetail.get("title")
            thumbnailUrl = videoDetail.get("thumbnail_url")
        except Exception as e:
            getLogger(__name__).warning("優先追加用の動画情報取得に失敗しました: {0}".format(e))
            
        payload = {"videoId": item, "priority": True}
        if title:
            payload["title"] = title
        if thumbnailUrl:
            payload["thumbnailUrl"] = thumbnailUrl
            
        resp = post(self.__queueUrl, json=payload, headers=self.__header)
        resp.raise_for_status()

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
        if not items:
            return
        # RestDBの仕様に基づき、/*?q={"_id":{"$in":[...]}} 形式でクエリパラメータによる一括削除を実行
        import json
        query = '?q={"_id":{"$in":' + json.dumps(items) + '}}'
        resp = delete(
            self.__requestUrl+"/*" + query, headers=self.__header)
        resp.raise_for_status()


    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".recordQuotedVideo"))
    def _recordQuotedVideo(self, videoId: str, liveId: str, title: str = None, thumbnailUrl: str = None) -> bool:
        payload = {
            "videoId": videoId,
            "liveId": liveId,
            "quotedAt": datetime.now(timezone.utc).isoformat()
        }
        if title:
            payload["title"] = title
        if thumbnailUrl:
            payload["thumbnailUrl"] = thumbnailUrl
            
        resp = post(self.__quotedUrl, json=payload, headers=self.__header)
        resp.raise_for_status()
        getLogger(__name__).debug("引用済み動画を記録しました: {0} (Live: {1})".format(videoId, liveId))
        return True

    def recordQuotedVideo(self, videoId: str, liveId: str, title: str = None, thumbnailUrl: str = None) -> bool:
        """Record a quoted/broadcasted video to the database.
        
        Args:
            videoId: The video ID (e.g., 'sm12345678')
            liveId: The live broadcast ID (e.g., 'lv123456789')
            title: Optional video title
            thumbnailUrl: Optional video thumbnail url
            
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
            return self._recordQuotedVideo(videoId, liveId, title=title, thumbnailUrl=thumbnailUrl)
        except Exception as e:
            getLogger(__name__).error("引用済み動画の記録に失敗しました: {0}".format(e))
            return False

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".updateNowPlaying"))
    def _updateNowPlaying(self, videoId: str, title: str, duration: int) -> Optional[str]:
        # 常に最新の1件にするため、既存の情報を全クリア
        delete_resp = delete(self.__nowplayingUrl + "/*?q={}", headers=self.__header)
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

    def updateNowPlaying(self, videoId: str, title: str, duration: int = 0) -> Optional[str]:
        if self.__nowplayingUrl is None:
            return None
        
        try:
            return self._updateNowPlaying(videoId, title, duration)
        except Exception as e:
            getLogger(__name__).error("nowplayingの更新に失敗しました: {0}".format(e))
            return None

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".patchNowPlayingTime"))
    def _patchNowPlayingTime(self, doc_id: str, remainingTime: int) -> bool:
        patch_resp = patch(self.__nowplayingUrl + "/" + str(doc_id), json={"remainingTime": remainingTime}, headers=self.__header)
        patch_resp.raise_for_status()
        return True

    def patchNowPlayingTime(self, doc_id: str, remainingTime: int) -> bool:
        if self.__nowplayingUrl is None or not doc_id:
            return False
            
        try:
            return self._patchNowPlayingTime(doc_id, remainingTime)
        except Exception as e:
            getLogger(__name__).error("nowplayingの残り時間更新に失敗しました: {0}".format(e))
            return False

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".clearNowPlaying"))
    def _clearNowPlaying(self) -> bool:
        delete_resp = delete(self.__nowplayingUrl + "/*?q={}", headers=self.__header)
        delete_resp.raise_for_status()
        return True

    def clearNowPlaying(self) -> bool:
        if self.__nowplayingUrl is None:
            return False
        
        try:
            return self._clearNowPlaying()
        except Exception as e:
            getLogger(__name__).error("nowplayingのクリアに失敗しました: {0}".format(e))
            return False

    def _async_callback(self, future):
        """非同期スレッドで発生した未キャッチ例外を検知してログ出力します。"""
        try:
            exception = future.exception()
            if exception:
                getLogger(__name__ + ".async").error(
                    "バックグラウンド非同期処理で例外が発生しました:",
                    exc_info=exception
                )
        except Exception as e:
            getLogger(__name__ + ".async").error(
                "非同期コールバックの実行中にエラーが発生しました:",
                exc_info=e
            )

    def enqueueByListAsync(self, items: Iterable[str]):
        """非同期でリストからエンキュー処理を行います。"""
        future = self._executor.submit(self.enqueueByList, items)
        future.add_done_callback(self._async_callback)

    def priorityEnqueueAsync(self, item: str):
        """非同期で優先エンキュー処理を行います。"""
        future = self._executor.submit(self.priorityEnqueue, item)
        future.add_done_callback(self._async_callback)

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".getPickupQueueCount"))
    def getPickupQueueCount(self) -> int:
        resp = get(self.__pickupQueueUrl + '?h={"$fields":{"videoId":1}}', headers=self.__header)
        resp.raise_for_status()
        items = resp.json()
        return len(items)

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".clearPickupQueue"))
    def clearPickupQueue(self):
        resp = delete(self.__pickupQueueUrl + "/*?q={}", headers=self.__header)
        resp.raise_for_status()

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".addPickupQueueItems"))
    def addPickupQueueItems(self, items: List[str]):
        """新着ピックアップキューに動画を登録します。"""
        if not items:
            return
        payload = []
        from nucosen.quote import getThumbInfo
        for item in items:
            title = None
            thumbnailUrl = None
            try:
                videoDetail = getThumbInfo(item)
                title = videoDetail.get("title")
                thumbnailUrl = videoDetail.get("thumbnail_url")
            except Exception as e:
                getLogger(__name__).warning("ピックアップ動画情報取得失敗 ({0}): {1}".format(item, e))
                
            data = {"videoId": item, "priority": False}
            if title:
                data["title"] = title
            if thumbnailUrl:
                data["thumbnailUrl"] = thumbnailUrl
            payload.append(data)
            
        resp = post(self.__pickupQueueUrl, json=payload, headers=self.__header)
        resp.raise_for_status()

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".backupCurrentQueue"))
    def backupCurrentQueue(self):
        """現在の queue テーブルを全退避し、queue テーブルを空にします。"""
        # 1. バックアップ先のクリア
        delete_backup_resp = delete(self.__backupQueueUrl + "/*?q={}", headers=self.__header)
        delete_backup_resp.raise_for_status()

        # 2. 現在のキューを全取得
        get_queue_resp = get(self.__queueUrl + '?h={"$orderby": {"priority": -1, "_id": 1}}', headers=self.__header)
        get_queue_resp.raise_for_status()
        queues = get_queue_resp.json()

        if not queues:
            return

        # 3. 取得したキューを backup_queue へコピー
        payload = []
        for q in queues:
            video_id = q.get("videoId")
            if not video_id:
                continue
            data = {
                "videoId": video_id,
                "priority": q.get("priority", False)
            }
            if q.get("title"):
                data["title"] = q["title"]
            if q.get("thumbnailUrl"):
                data["thumbnailUrl"] = q["thumbnailUrl"]
            payload.append(data)

        post_backup_resp = post(self.__backupQueueUrl, json=payload, headers=self.__header)
        post_backup_resp.raise_for_status()

        # 4. 元のキューを空にする
        delete_queue_resp = delete(self.__queueUrl + "/*?q={}", headers=self.__header)
        delete_queue_resp.raise_for_status()

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".restoreBackupQueue"))
    def restoreBackupQueue(self):
        """backup_queue テーブルの内容を queue テーブルに戻し、backup_queue を空にします。"""
        # 1. バックアップされているキューの取得
        get_backup_resp = get(self.__backupQueueUrl + '?h={"$orderby": {"_id": 1}}', headers=self.__header)
        get_backup_resp.raise_for_status()
        backups = get_backup_resp.json()

        # 2. queue テーブルをクリア（すでに一部消化されて空になっているはずですが、念のため）
        delete_queue_resp = delete(self.__queueUrl + "/*?q={}", headers=self.__header)
        delete_queue_resp.raise_for_status()

        if backups:
            # 3. queue テーブルへ書き戻し
            payload = []
            for b in backups:
                video_id = b.get("videoId")
                if not video_id:
                    continue
                data = {
                    "videoId": video_id,
                    "priority": b.get("priority", False)
                }
                if b.get("title"):
                    data["title"] = b["title"]
                if b.get("thumbnailUrl"):
                    data["thumbnailUrl"] = b["thumbnailUrl"]
                payload.append(data)

            post_queue_resp = post(self.__queueUrl, json=payload, headers=self.__header)
            post_queue_resp.raise_for_status()

        # 4. backup_queue をクリア
        delete_backup_resp = delete(self.__backupQueueUrl + "/*?q={}", headers=self.__header)
        delete_backup_resp.raise_for_status()

    @retry(NetworkErrors, tries=5, delay=1, backoff=2, logger=getLogger(__name__ + ".replaceQueueWithPickup"))
    def replaceQueueWithPickup(self):
        """pickup_queue テーブルの内容を queue テーブルにランダムな順序でコピーします。"""
        # 1. ピックアップキューを取得
        get_pickup_resp = get(self.__pickupQueueUrl + '?h={"$orderby": {"_id": 1}}', headers=self.__header)
        get_pickup_resp.raise_for_status()
        pickups = get_pickup_resp.json()

        # 2. queue テーブルをクリア
        delete_queue_resp = delete(self.__queueUrl + "/*?q={}", headers=self.__header)
        delete_queue_resp.raise_for_status()

        if pickups:
            # 毎回ランダムな順番でコピーするため、リストをシャッフルする
            import random
            random.shuffle(pickups)

            # 3. queue テーブルへコピー
            payload = []
            for p in pickups:
                video_id = p.get("videoId")
                if not video_id:
                    continue
                data = {
                    "videoId": video_id,
                    "priority": p.get("priority", False)
                }
                if p.get("title"):
                    data["title"] = p["title"]
                if p.get("thumbnailUrl"):
                    data["thumbnailUrl"] = p["thumbnailUrl"]
                payload.append(data)

            post_queue_resp = post(self.__queueUrl, json=payload, headers=self.__header)
            post_queue_resp.raise_for_status()

