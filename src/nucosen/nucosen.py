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
from datetime import datetime, timedelta, timezone
from logging import getLogger
from os import getcwd
from pathlib import Path
from traceback import format_exc

import requests
from decouple import AutoConfig

from nucosen import clock, db, live, personality, quote, sessionCookie


def run():
    logger = getLogger(__name__)
    watcher = None

    try:
        database = db.RestDbIo()
        settings_keys = {
            "LIVE_TITLE", "COMMUNITY", "TAGS", "REQTAGS", "LOGGING_DISCORD_WEBHOOK",
            "DISCORD_VIDEOINFO_WEBHOOK", "DISCORD_ON_VIDEOINFO", "DISCORD_VIDEOINFO_TEXT",
            "BROADCASTER_ON_VIDEOINFO", "BROADCASTER_VIDEOINFO_TEXT", "QUEUE_URL",
            "REQUEST_URL", "QUEUE_PRELOAD_SIZE", "QUOTED_URL", "NG_TAGS",
            "USE_OLD_VIDEO_API", "USE_OLD_QUOTE_BOT", "IGNORE_QUOTABLE_CHECK",
            "MAINTENANCE_VIDEO_ID", "CLOSING_VIDEO_ID", "NUCOSEN_UA_PREFIX",
            "NUCOSEN_LIVE_DESCRIPTION", "NUCOSEN_TIMESHIFT_ENABLED",
            "NUCOSEN_USER_AD_DISABLED", "NUCOSEN_MAINTENANCE_MESSAGE",
            "NUCOSEN_CLOSING_MESSAGE", "OPENING_VIDEO_ID", "NUCOSEN_OPENING_MESSAGE",
            "MIN_ALLOWABLE_DURATION",
            "MAX_ALLOWABLE_DURATION", "NG_VIDEO_IDS",
            "MAIN_VOLUME", "SUB_VOLUME", "DURATION_OVERWRITE",
            "QUOTE_LAYOUT",
            "NICO_REQUEST_DELAY", "NUCOSEN_AUTO_RESERVE", "NOWPLAYING_URL",
            "COOLDOWN_SIZE", "COOLDOWN_AFFECTS_REQUESTS"
        }
        db_settings = database.get_settings()
        for key, value in db_settings.items():
            if key in settings_keys and value != "":
                os.environ[key] = value

        configLoader = AutoConfig(getcwd())

        def config(key, default=""):
            # Wrapper around decouple.AutoConfig to accept a default value.
            return str(configLoader(key, default=default))

        def config_bool(key, default=False):
            return configLoader(key, default=default, cast=bool)

        def config_int(key, default=0):
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

        queuePreloadSize = max(1, config_int("QUEUE_PRELOAD_SIZE", 10))

        autoReserveEnabled = config_bool("NUCOSEN_AUTO_RESERVE", default=True)

        cooldownSize = max(0, config_int("COOLDOWN_SIZE", 50))
        cooldownAffectsRequests = config_bool("COOLDOWN_AFFECTS_REQUESTS", default=False)
        cooldownHistory = collections.deque(maxlen=cooldownSize)

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

        SPECIFIC_VIDEO_IDS = [
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

        ngTags = set(config("NG_TAGS").split(","))
        is_fresh_frame = False

        while True:
            logger.debug("現枠・次枠の確保開始")
            liveIDs = live.getLives(session)
            if liveIDs[0] is None:
                if liveIDs[1] is None:
                    logger.warning("W0L 枠未検出")
                    if autoReserveEnabled:
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
                if autoReserveEnabled:
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
                if currentQuote == SPECIFIC_VIDEO_IDS[MAINTENANCE]:
                    logger.info("メンテナンス動画の引用を検知しました")
                    database.clearNowPlaying()
                elif currentQuote == SPECIFIC_VIDEO_IDS[CLOSING]:
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
                        if autoReserveEnabled:
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
                        liveIDs[0], SPECIFIC_VIDEO_IDS[MAINTENANCE], session)
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

                def ensure_preloaded_queue():
                    current_queue_count = database.getQueueCount()
                    if current_queue_count >= queuePreloadSize:
                        return
                    logger.info(
                        "キューを事前登録します: 現在 %d 件, 目標 %d 件",
                        current_queue_count,
                        queuePreloadSize,
                    )
                    missing = queuePreloadSize - current_queue_count
                    max_attempts = 5
                    while missing > 0 and max_attempts > 0:
                        try:
                            selection, selected_tag = personality.randomSelection(
                                config("REQTAGS").split(","), session, ngTags, set(cooldownHistory))
                            database.enqueueByList([selection])
                            current_queue_count = database.getQueueCount()
                            missing = queuePreloadSize - current_queue_count
                        except Exception as err:
                            logger.warning("ランダム補充に失敗しました: %s", err)
                            break
                        finally:
                            max_attempts -= 1

                    if missing > 0:
                        logger.info("事前キューが目標数に達しませんでした: %d 件不足", missing)

                is_requested = False
                is_opening = is_first_video
                if is_first_video and config("OPENING_VIDEO_ID"):
                    nextVideoId = config("OPENING_VIDEO_ID")
                else:
                    db_requests = database.getAndResetRequests()
                    if db_requests is not None and cooldownAffectsRequests:
                        cooldown_set = set(cooldownHistory)
                        db_requests = [req for req in db_requests if req not in cooldown_set]
                        if not db_requests:
                            db_requests = None
                    if db_requests is not None:
                        winners = personality.choiceFromRequests(db_requests, 5)
                        if winners is None:
                            logger.error("E40 抽選アボート {0}".format(db_requests))
                            selection, selected_tag = personality.randomSelection(
                                config("REQTAGS").split(","), session, ngTags, set(cooldownHistory))
                        else:
                            selection = winners.pop()
                            database.enqueueByList(winners)
                            is_requested = True
                        nextVideoId = selection
                    else:
                        ensure_preloaded_queue()
                        nextVideoId = database.dequeue()
                        if nextVideoId is None:
                            logger.debug("キューが空なので補充を行います")
                            request_ids = database.getAndResetRequests()
                            if request_ids is not None and cooldownAffectsRequests:
                                cooldown_set = set(cooldownHistory)
                                request_ids = [req for req in request_ids if req not in cooldown_set]
                                if not request_ids:
                                    request_ids = None
                            if request_ids is not None:
                                winners = personality.choiceFromRequests(request_ids, 5)
                                if winners is None:
                                    logger.error("E40 抽選アボート {0}".format(request_ids))
                                    selection, selected_tag = personality.randomSelection(
                                        config("REQTAGS").split(","), session, ngTags, set(cooldownHistory))
                                else:
                                    selection = winners.pop()
                                    database.enqueueByList(winners)
                                    is_requested = True
                            else:
                                selection, selected_tag = personality.randomSelection(
                                    config("REQTAGS").split(","), session, ngTags, set(cooldownHistory))
                            nextVideoId = selection

                logger.info("引用を開始します: {0}".format(nextVideoId))
                currentLiveEnd = live.getEndTime(currentLiveId, session)
                videoInfo = quote.getVideoInfo(nextVideoId, session, ngTags)
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
                    database.priorityEnqueue(nextVideoId)
                    quote.loop(
                        currentLiveId, SPECIFIC_VIDEO_IDS[CLOSING], session)
                    database.clearNowPlaying()
                    live.showMessage(
                        currentLiveId,
                        config("NUCOSEN_CLOSING_MESSAGE") or
                        "この枠の放送は終了しました。\nご視聴ありがとうございました。",
                        session, permanent=True)
                    clock.waitUntil(currentLiveEnd)
                    break
                quote.once(currentLiveId, nextVideoId, session)
                database.recordQuotedVideo(nextVideoId, currentLiveId)
                if cooldownSize > 0 and nextVideoId not in SPECIFIC_VIDEO_IDS:
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
                        logger.info(update_msg)
                        webhook = config("LOGGING_DISCORD_WEBHOOK", default="")
                        if webhook:
                            try:
                                _send_discord_notification(webhook, update_msg)
                            except Exception as e:
                                logger.warning("設定更新のDiscord通知に失敗しました: %s", e)
                    else:
                        logger.debug("DB設定の再確認が完了しました (変更なし)")
                except Exception as err:
                    logger.warning("DB設定の再確認に失敗しました: %s", err)

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
