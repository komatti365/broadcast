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

import os
import sys
import collections
import threading
import time
from datetime import datetime, timedelta, timezone
from logging import getLogger
from os import getcwd
from pathlib import Path
from traceback import format_exc

import requests
from decouple import AutoConfig

from nucosen import clock, db, live, personality, quote, sessionCookie


def start_queue_preloader(database, session, cooldownHistory, cooldown_lock, config):
    """キューの残り登録数をバックグラウンドスレッドで常に監視・補充する preloader を開始します。"""
    def preloader_loop():
        logger = getLogger(__name__ + ".preloader")
        logger.info("バックグラウンドのキュー監視スレッドを開始しました")
        
        while True:
            try:
                # QUEUE_PRELOAD_SIZE の動的取得
                try:
                    queuePreloadSize = max(1, int(config("QUEUE_PRELOAD_SIZE", "10")))
                except (TypeError, ValueError):
                    queuePreloadSize = 10

                current_queue_count = database.getQueueCount()
                if current_queue_count < queuePreloadSize:
                    logger.info(
                        "キュー不足を検知しました: 現在 %d 件, 目標 %d 件。補充を開始します。",
                        current_queue_count,
                        queuePreloadSize,
                    )
                    missing = queuePreloadSize - current_queue_count
                    selections = []
                    max_attempts = 5
                    
                    # 現在キューにあるIDとcooldownHistoryをマージして重複を防止
                    existing_video_ids = database.getQueueVideoIds()
                    with cooldown_lock:
                        history_copy = list(cooldownHistory)
                    temp_cooldown = set(history_copy) | existing_video_ids
                    
                    # NG_TAGS の動的取得
                    ng_tags_set = set(config("NG_TAGS", "").split(","))
                    
                    while missing > 0 and max_attempts > 0:
                        try:
                            # 補充用動画IDの選定
                            selection, _ = personality.randomSelection(
                                config("REQTAGS").split(","), session, ng_tags_set, temp_cooldown)
                            selections.append(selection)
                            temp_cooldown.add(selection)
                            missing -= 1
                        except Exception as err:
                            logger.warning("ランダム選定に失敗しました: %s", err)
                            max_attempts -= 1
                    
                    if selections:
                        logger.info("%d 件の動画をキューに補充します: %s", len(selections), selections)
                        # preloader 自体がバックグラウンドスレッドで動くため、通常の同期的な enqueueByList を実行
                        database.enqueueByList(selections)
            except Exception as err:
                logger.error("キュー補充監視スレッドで予期せぬエラーが発生しました: %s", err)
            
            time.sleep(30) # 30秒ごとに監視

    t = threading.Thread(target=preloader_loop, name="QueuePreloader", daemon=True)
    t.start()
    return t


def start_settings_reloader(database, settings_keys, config, logger):
    """DB設定をバックグラウンドスレッドで定期的に再ロードし、環境変数に反映する reloader を開始します。"""
    def reloader_loop():
        reloader_logger = getLogger(__name__ + ".settings_reloader")
        reloader_logger.info("バックグラウンドの設定再ロードスレッドを開始しました (確認間隔: 60秒)")
        
        while True:
            time.sleep(60) # 60秒ごとに確認
            try:
                latest_settings = database.get_settings()
                updated_keys = []
                for key, value in latest_settings.items():
                    if key in settings_keys and value != "":
                        old_val = os.environ.get(key, "")
                        if old_val != value:
                            os.environ[key] = value
                            updated_keys.append(f"{key}: {old_val} -> {value}")
                if updated_keys:
                    update_msg = "DB設定が更新され、環境変数に反映されました:\n" + "\n".join(updated_keys)
                    reloader_logger.info(update_msg)
                    webhook = config("LOGGING_DISCORD_WEBHOOK", default="")
                    if webhook:
                        try:
                            resp = requests.post(webhook, json={"content": update_msg})
                            resp.raise_for_status()
                        except Exception as e:
                            reloader_logger.warning("設定更新のDiscord通知に失敗しました: %s", e)
                else:
                    reloader_logger.debug("DB設定の再確認が完了しました (変更なし)")
            except Exception as err:
                reloader_logger.warning("DB設定の再確認に失敗しました: %s", err)

    t = threading.Thread(target=reloader_loop, name="SettingsReloader", daemon=True)
    t.start()
    return t


def run():
    logger = getLogger(__name__)
    watcher = None

    try:
        database = db.RestDbIo()
        settings_keys = {
            "LIVE_TITLE", "COMMUNITY", "TAGS", "REQTAGS", "LOGGING_DISCORD_WEBHOOK",
            "DISCORD_VIDEOINFO_WEBHOOK", "DISCORD_ON_VIDEOINFO", "DISCORD_VIDEOINFO_TEXT",
            "BROADCASTER_ON_VIDEOINFO", "BROADCASTER_VIDEOINFO_TEXT",
            "QUEUE_PRELOAD_SIZE", "NG_TAGS",
            "USE_OLD_VIDEO_API", "USE_OLD_QUOTE_BOT", "IGNORE_QUOTABLE_CHECK",
            "MAINTENANCE_VIDEO_ID", "CLOSING_VIDEO_ID", "NUCOSEN_UA_PREFIX",
            "NUCOSEN_LIVE_DESCRIPTION", "NUCOSEN_TIMESHIFT_ENABLED",
            "NUCOSEN_USER_AD_DISABLED", "NUCOSEN_MAINTENANCE_MESSAGE",
            "NUCOSEN_CLOSING_MESSAGE", "OPENING_VIDEO_ID", "NUCOSEN_OPENING_MESSAGE",
            "MIN_ALLOWABLE_DURATION",
            "MAX_ALLOWABLE_DURATION", "NG_VIDEO_IDS",
            "MAIN_VOLUME", "SUB_VOLUME", "DURATION_OVERWRITE",
            "QUOTE_LAYOUT",
            "NICO_REQUEST_DELAY", "NUCOSEN_AUTO_RESERVE",
            "COOLDOWN_SIZE", "COOLDOWN_AFFECTS_REQUESTS"
        }
        db_settings = database.get_settings()
        for key, value in db_settings.items():
            if key in settings_keys and value != "":
                os.environ[key] = value

        configLoader = AutoConfig(getcwd())

        def config(key, default=""):
            # os.environ を最優先にし、なければ configLoader にフォールバックする
            if key in os.environ:
                return str(os.environ[key])
            return str(configLoader(key, default=default))

        def config_bool(key, default=False):
            if key in os.environ:
                val = os.environ[key]
                return val.lower() in ("true", "1", "t", "y", "yes")
            return configLoader(key, default=default, cast=bool)

        def config_int(key, default=0):
            if key in os.environ:
                try:
                    return int(os.environ[key])
                except (TypeError, ValueError):
                    pass
            try:
                return int(configLoader(key, default=default))
            except (TypeError, ValueError):
                return default

        current_settings = {
            key: config(key, default="")
            for key in settings_keys
        }
        current_settings = {key: value for key, value in current_settings.items() if value != ""}
        database.publish_settings(current_settings)

        cooldownHistory = collections.deque(maxlen=max(0, config_int("COOLDOWN_SIZE", 50)))
        cooldown_lock = threading.Lock()

        def _build_video_info_message(template: str, info: dict) -> str:
            text = template.replace("\\n", "\n")
            mapping = {
                "id": info.get("id", ""),
                "title": info.get("title", ""),
                "length": info.get("length", ""),
                "view": info.get("view_counter", 0),
                "comment": info.get("comment_num", 0),
                "mylist": info.get("mylist_counter", 0),
                "description": info.get("description", ""),
                "username": info.get("user_nickname", ""),
                "url": f"http://nico.ms/{info.get('id', '')}"
            }
            for key, value in mapping.items():
                text = text.replace("{" + key + "}", str(value))
            return text

        def _send_discord_notification(url: str, content: str):
            resp = requests.post(url, json={"content": content})
            resp.raise_for_status()

        def get_specific_video_ids() -> list[str]:
            return [
                (config("MAINTENANCE_VIDEO_ID") or "sm17759202"),
                (config("CLOSING_VIDEO_ID") or "sm17572946")
            ]
        MAINTENANCE, CLOSING = 0, 1

        def get_secret(key: str, default: str = "") -> str:
            """Read secret from {key}_FILE or fallback to {key} environment variable."""
            file_path = config(f"{key}_FILE", default="")
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

        # Allow authentication via an access token extracted from cookies
        # (set the environment variable `NICO_TOKEN` or `NICO_TOKEN_FILE` to the `user_session` value),
        # or by reusing a previously saved cookie file (`NICO_COOKIE_FILE`).
        token = get_secret("NICO_TOKEN")
        cookie_file = config("NICO_COOKIE_FILE", default="")
        
        def _get_login_info():
            return get_secret("NICO_ID"), get_secret("NICO_PW"), get_secret("NICO_TFA")

        logininfo = _get_login_info()

        if token:
            session = sessionCookie.Session.from_access_token(token, *logininfo)
        elif cookie_file:
            p = Path(cookie_file)
            if p.exists():
                try:
                    session = sessionCookie.Session.from_cookie_file(cookie_file, *logininfo)
                except (ValueError, FileNotFoundError) as e:
                    logger.warning(f"クッキーファイルが無効です: {e}")
                    logger.info("ユーザー名/パスワードで再度ログインします")
                    if "" in logininfo[:2]:
                        getLogger(__name__).info(
                            "現在のログイン情報: NICO_ID設定済み=%s, NICO_PW設定済み=%s, NICO_TFA設定済み=%s",
                            bool(logininfo[0]),
                            bool(logininfo[1]),
                            bool(logininfo[2]),
                        )
                        raise Exception("V00 ログイン情報が不十分です。現在の情報はinfoに出力済み。")
                    session = sessionCookie.Session(*logininfo)
                    session.login()
                    # Save cookies after successful login for future reuse
                    try:
                        session.save_cookies(cookie_file)
                    except Exception:
                        getLogger(__name__).warning("クッキーの保存に失敗しました")
            else:
                logger.debug(f"クッキーファイルが指定されていますが存在しません: {cookie_file}")
                if "" in logininfo[:2]:
                    getLogger(__name__).info(
                        "現在のログイン情報: NICO_ID設定済み=%s, NICO_PW設定済み=%s, NICO_TFA設定済み=%s",
                        bool(logininfo[0]),
                        bool(logininfo[1]),
                        bool(logininfo[2]),
                    )
                    raise Exception("V00 ログイン情報が不十分です。現在の情報はinfoに出力済み。")
                session = sessionCookie.Session(*logininfo)
                session.login()
        else:
            if "" in logininfo[:2]:
                getLogger(__name__).info(
                    "現在のログイン情報: NICO_ID設定済み=%s, NICO_PW設定済み=%s, NICO_TFA設定済み=%s",
                    bool(logininfo[0]),
                    bool(logininfo[1]),
                    bool(logininfo[2]),
                )
                raise Exception("V00 ログイン情報が不十分です。現在の情報はinfoに出力済み。")
            session = sessionCookie.Session(*logininfo)
            session.login()
        logger.debug("チャンネルループ開始")

        # バックグラウンドでキュー監視・補充スレッドを起動
        start_queue_preloader(database, session, cooldownHistory, cooldown_lock, config)

        # バックグラウンドでDB設定の定期更新スレッドを起動
        start_settings_reloader(database, settings_keys, config, logger)

        while True:
            is_fresh_frame = False
            logger.debug("現枠・次枠の確保開始")
            liveIDs = live.getLives(session)
            if liveIDs[0] is None:
                if liveIDs[1] is None:
                    logger.warning("W0L 枠未検出")
                    if config_bool("NUCOSEN_AUTO_RESERVE", default=True):
                        live.reserveLive(
                            title=config("LIVE_TITLE"),
                            communityId=config("COMMUNITY"),
                            tags=config("TAGS").split(","),
                            session=session
                        )
                        liveIDs = live.getLives(session)
                    else:
                        logger.info(
                            "自動枠取りは無効です。現在および次の枠が存在しないため、60秒後に再試行します。"
                        )
                        clock.waitUntil(datetime.now(timezone.utc) + timedelta(minutes=1))
                        continue
                nextLive: str | None = liveIDs[0] or liveIDs[1]
                if nextLive is None:
                    raise Exception("V10 予約確認エラー")
                nextLiveBegin = live.getStartTime(nextLive, session)
                clock.waitUntil(nextLiveBegin)
                is_fresh_frame = True
                for i in range(6):
                    liveIDs = live.getLives(session)
                    if liveIDs[0] is not None:
                        break
                    if i < 5:
                        logger.info("現枠の開始を待機しています。10秒後に再試行します。")
                        clock.waitUntil(datetime.now(timezone.utc) + timedelta(seconds=10))
            elif liveIDs[1] is None:
                if config_bool("NUCOSEN_AUTO_RESERVE", default=True):
                    live.reserveLive(
                        title=config("LIVE_TITLE"),
                        communityId=config("COMMUNITY"),
                        tags=config("TAGS").split(","),
                        session=session
                    )
                    liveIDs = live.getLives(session)
                else:
                    logger.info("自動枠取りは無効です。次枠の予約はスキップします。")

            if liveIDs[0] is not None and liveIDs[1] is not None:
                liveIDs = live.sGetLives(session)
            logger.info("現枠: {0}, 次枠: {1}".format(liveIDs[0], liveIDs[1]))

            logger.debug("現存する引用状態の処理")
            currentLiveEnd = live.getEndTime(liveIDs[0], session)
            currentQuote = quote.getCurrent(liveIDs[0], session)
            if currentQuote is not None:
                if currentQuote == get_specific_video_ids()[MAINTENANCE]:
                    logger.info("メンテナンス動画の引用を検知しました")
                    database.clearNowPlaying()
                elif currentQuote == get_specific_video_ids()[CLOSING]:
                    logger.info("エンディング動画の引用を検知しました")
                    if liveIDs[1] is None:
                        logger.info(
                            "次枠が未予約のため、自動枠取りは行いません。現在の枠終了まで待機します。"
                        )
                        clock.waitUntil(currentLiveEnd)
                        is_fresh_frame = True
                    else:
                        nextLiveBegin = live.getStartTime(liveIDs[1], session)
                        clock.waitUntil(currentLiveEnd)
                        if config_bool("NUCOSEN_AUTO_RESERVE", default=True):
                            live.reserveLive(
                                title=config("LIVE_TITLE"),
                                communityId=config("COMMUNITY"),
                                tags=config("TAGS").split(","),
                                session=session
                            )
                        else:
                            logger.info(
                                "自動枠取りは無効です。次枠は手動で予約してください。"
                            )
                        database.clearNowPlaying()
                        clock.waitUntil(nextLiveBegin)
                        is_fresh_frame = True
                        liveIDs = live.getLives(session)
                else:
                    logger.info("一般動画の引用を検知しました: {0}".format(currentQuote))
                    quote.stop(liveIDs[0], session)
                    maintenanceSpan = quote.once(
                        liveIDs[0], get_specific_video_ids()[MAINTENANCE], session)
                    maintenanceEnd = datetime.now(
                        timezone.utc) + maintenanceSpan
                    logger.error("E30 引用停止 {0}".format(currentQuote))
                    emergencyStopMessage = \
                        str(config("NUCOSEN_MAINTENANCE_MESSAGE"))\
                        or "システムが異常停止したため、自動回復機能により復旧しました。\n" +\
                        "ご迷惑をおかけし大変申し訳ございません。まもなく再開いたします。"
                    database.clearNowPlaying()
                    live.showMessage(
                        liveIDs[0], emergencyStopMessage, session)
                    clock.waitUntil(maintenanceEnd)

            currentLiveId = liveIDs[0]
            logger.info("放送の準備が整いました: {0}".format(currentLiveId))
            
            is_first_video = (currentQuote is None) and is_fresh_frame
            is_fresh_frame = False
            while True:

                nextVideoId = None
                is_requested = False
                is_opening = is_first_video

                # 動画が決定するまで繰り返すセーフティループ
                last_wait_comment_time = datetime.min
                while nextVideoId is None:
                    try:
                        if is_first_video and config("OPENING_VIDEO_ID"):
                            nextVideoId = config("OPENING_VIDEO_ID")
                        else:
                            db_requests = database.getAndResetRequests()
                            if db_requests is not None and config_bool("COOLDOWN_AFFECTS_REQUESTS", default=False):
                                with cooldown_lock:
                                    history_copy = list(cooldownHistory)
                                cooldown_set = set(history_copy)
                                db_requests = [req for req in db_requests if req not in cooldown_set]
                                if not db_requests:
                                    db_requests = None
                            if db_requests is not None:
                                winners = personality.choiceFromRequests(db_requests, 5)
                                if winners is None:
                                    logger.error("E40 抽選アボート {0}".format(db_requests))
                                    with cooldown_lock:
                                        history_copy = list(cooldownHistory)
                                    selection, _ = personality.randomSelection(
                                        config("REQTAGS").split(","), session, set(config("NG_TAGS").split(",")), set(history_copy))
                                    nextVideoId = selection
                                else:
                                    selection = winners.pop()
                                    database.enqueueByListAsync(winners)
                                    is_requested = True
                                    nextVideoId = selection
                            else:
                                nextVideoId = database.dequeue()
                                if nextVideoId is None:
                                    logger.debug("キューが空なので補充を行います")
                                    request_ids = database.getAndResetRequests()
                                    if request_ids is not None and config_bool("COOLDOWN_AFFECTS_REQUESTS", default=False):
                                        with cooldown_lock:
                                            history_copy = list(cooldownHistory)
                                        cooldown_set = set(history_copy)
                                        request_ids = [req for req in request_ids if req not in cooldown_set]
                                        if not request_ids:
                                            request_ids = None
                                    if request_ids is not None:
                                        winners = personality.choiceFromRequests(request_ids, 5)
                                        if winners is None:
                                            logger.error("E40 抽選アボート {0}".format(request_ids))
                                            with cooldown_lock:
                                                history_copy = list(cooldownHistory)
                                            selection, _ = personality.randomSelection(
                                                config("REQTAGS").split(","), session, set(config("NG_TAGS").split(",")), set(history_copy))
                                            nextVideoId = selection
                                        else:
                                            selection = winners.pop()
                                            database.enqueueByListAsync(winners)
                                            is_requested = True
                                            nextVideoId = selection
                                    else:
                                        with cooldown_lock:
                                            history_copy = list(cooldownHistory)
                                        selection, _ = personality.randomSelection(
                                            config("REQTAGS").split(","), session, set(config("NG_TAGS").split(",")), set(history_copy))
                                        nextVideoId = selection
                    except Exception as err:
                        logger.warning("動画の取得または緊急補充に失敗しました: %s", err)
                        nextVideoId = None

                    # それでも動画が見つからない（空）場合のセーフティ
                    if nextVideoId is None:
                        # 1. オープニング動画が設定されていれば、時間稼ぎとして引用
                        opening_video = config("OPENING_VIDEO_ID")
                        if opening_video:
                            logger.info("キューが空のため、時間稼ぎとしてオープニング動画 (%s) を再生します", opening_video)
                            nextVideoId = opening_video
                            break

                        # 2. オープニング動画もない場合は、キュー補充を待機
                        now = datetime.now()
                        if (now - last_wait_comment_time).total_seconds() >= 60:
                            wait_msg = "【お知らせ】現在リクエスト動画の補充を待機しています... (リクエスト歓迎！)"
                            try:
                                live.showMessage(currentLiveId, wait_msg, session)
                            except Exception as e:
                                logger.warning("待機メッセージのコメント送信に失敗しました: %s", e)
                            last_wait_comment_time = now

                        logger.info("再生可能な動画がないため、10秒間待機します...")
                        clock.waitUntil(datetime.now(timezone.utc) + timedelta(seconds=10))
                            
                videoDetail = None
                logger.info("引用を開始します: {0}".format(nextVideoId))
                currentLiveEnd = live.getEndTime(currentLiveId, session)
                videoInfo = quote.getVideoInfo(nextVideoId, session, set(config("NG_TAGS").split(",")))
                if videoInfo[0] is False:
                    logger.warning("V20 引用不能エラーのためスキップします: {0} {1}".format(nextVideoId, currentLiveId))

                    if is_requested:
                        reject_msg_nico = "リクエスト動画({0})は引用不可のためスキップしました。".format(nextVideoId)
                        reject_msg_discord = "⚠️ リクエストスキップ: 動画 `{0}` は引用不許可またはNGタグが含まれているためスキップされました。".format(nextVideoId)

                        try:
                            live.showMessage(currentLiveId, reject_msg_nico, session)
                        except Exception as err:
                            logger.warning("スキップ通知のニコ生コメント送信に失敗しました: %s", err)

                        webhook = config("DISCORD_VIDEOINFO_WEBHOOK", default="") or config("LOGGING_DISCORD_WEBHOOK", default="")
                        if webhook:
                            try:
                                _send_discord_notification(webhook, reject_msg_discord)
                            except Exception as err:
                                logger.warning("スキップ通知のDiscord送信に失敗しました: %s", err)

                    continue
                if config_bool("DISCORD_ON_VIDEOINFO", default=False):
                    webhook = config("DISCORD_VIDEOINFO_WEBHOOK", default="") or config("LOGGING_DISCORD_WEBHOOK", default="")
                    if webhook:
                        try:
                            videoDetail = quote.getThumbInfo(nextVideoId)
                            discordMessage = _build_video_info_message(
                                config("DISCORD_VIDEOINFO_TEXT", default="再生中:{title}\nhttp://nico.ms/{id} #{id}\n再生時間:{length} 再生数:{view} コメント:{comment} マイリスト:{mylist}"),
                                videoDetail
                            )
                            _send_discord_notification(webhook, discordMessage)
                        except Exception as err:
                            logger.warning("Discord動画情報送信に失敗しました: %s", err)
                    else:
                        logger.warning("DISCORD_ON_VIDEOINFO が有効ですが、DISCORD_VIDEOINFO_WEBHOOK または LOGGING_DISCORD_WEBHOOK が設定されていません。")

                if config_bool("BROADCASTER_ON_VIDEOINFO", default=False):
                    try:
                        videoDetail = quote.getThumbInfo(nextVideoId)
                        broadcasterMessage = _build_video_info_message(
                            config("BROADCASTER_VIDEOINFO_TEXT", default="放送者コメント:{title}\n再生時間:{length} 再生数:{view} コメント:{comment} マイリスト:{mylist}"),
                            videoDetail
                        )
                        live.showMessage(currentLiveId, broadcasterMessage, session)
                    except Exception as err:
                        logger.warning("放送者コメント動画情報送信に失敗しました: %s", err)
                if datetime.now(timezone.utc) + videoInfo[1] > currentLiveEnd - timedelta(minutes=1):
                    logger.info("引用アボート: 時間内に引用が終了しない見込みです")
                    database.priorityEnqueueAsync(nextVideoId)
                    quote.loop(
                        currentLiveId, get_specific_video_ids()[CLOSING], session)
                    database.clearNowPlaying()
                    live.showMessage(
                        currentLiveId,
                        config("NUCOSEN_CLOSING_MESSAGE") or
                        "この枠の放送は終了しました。\nご視聴ありがとうございました。",
                        session, permanent=True)
                    clock.waitUntil(currentLiveEnd)
                    break
                quote.once(currentLiveId, nextVideoId, session)
                title = None
                thumbnail_url = None
                try:
                    if videoDetail is None or videoDetail.get("id") != nextVideoId:
                        videoDetail = quote.getThumbInfo(nextVideoId)
                    title = videoDetail.get("title")
                    thumbnail_url = videoDetail.get("thumbnail_url")
                except Exception as err:
                    logger.warning("引用履歴用の動画情報取得に失敗しました: %s", err)
                    if videoInfo and len(videoInfo) > 2 and videoInfo[2]:
                        parts = videoInfo[2].split(" / ")
                        if len(parts) > 1:
                            title = parts[0]
                
                database.recordQuotedVideo(nextVideoId, currentLiveId, title=title, thumbnailUrl=thumbnail_url)
                with cooldown_lock:
                    current_cooldown_size = max(0, config_int("COOLDOWN_SIZE", 50))
                    if current_cooldown_size != cooldownHistory.maxlen:
                        cooldownHistory = collections.deque(list(cooldownHistory), maxlen=current_cooldown_size)
                        
                    if current_cooldown_size > 0 and nextVideoId not in get_specific_video_ids():
                        cooldownHistory.append(nextVideoId)
                duration_seconds = int(videoInfo[1].total_seconds())
                nowplaying_doc_id = database.updateNowPlaying(nextVideoId, videoInfo[2], duration_seconds)
                live.showMessage(currentLiveId, videoInfo[2], session)

                if is_opening:
                    if config("NUCOSEN_OPENING_MESSAGE"):
                        live.showMessage(currentLiveId, config("NUCOSEN_OPENING_MESSAGE"), session)
                    
                    # 初回動画の再生時に、設定されているタグのリスト全体を運営コメントに流す
                    try:
                        tags_message = f"【設定タグ】{config('TAGS')}\n【リクエスト対象タグ】{config('REQTAGS')}"
                        live.showMessage(currentLiveId, tags_message, session)
                    except Exception as err:
                        logger.warning("タグ一覧の運営コメント送信に失敗しました: %s", err)
                
                is_first_video = False


                # 引用終了まで一気に待機せず、最大10秒刻みで残り時間をDBに更新し続ける
                target_end_time = datetime.now(timezone.utc) + videoInfo[1]
                while True:
                    now = datetime.now(timezone.utc)
                    if now >= target_end_time:
                        break
                    
                    remaining_seconds = int((target_end_time - now).total_seconds())
                    if nowplaying_doc_id:
                        database.patchNowPlayingTime(nowplaying_doc_id, remaining_seconds)
                        
                    sleep_duration = min(10.0, (target_end_time - now).total_seconds())
                    clock.waitUntil(now + timedelta(seconds=sleep_duration))

                logger.info("引用終了見込み時刻になりました")
                database.clearNowPlaying()
                
            if watcher is not None:
                watcher.stop()
                watcher = None
            logger.info("放送が終了しました: {0}".format(currentLiveId))
    except Exception:
        if watcher is not None:
            watcher.stop()
        t = format_exc()
        logger.critical("例外がキャッチされませんでした\n```\n{0}\n```".format(t))
        sys.exit(0)
