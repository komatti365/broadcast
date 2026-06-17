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

from dataclasses import dataclass
from logging import getLogger
from typing import Optional
import json
from pathlib import Path


import httpx
from requests.cookies import RequestsCookieJar
from retry import retry
from decouple import AutoConfig
from os import getcwd

class ReLoginRequested(Exception):
    pass

config = AutoConfig(getcwd())
# レビュー3: トランスポート層の全エラーに対応するため httpx.RequestError を指定
NetworkErrors = (httpx.RequestError, httpx.HTTPStatusError, ReLoginRequested)
UserAgent = str(config("NUCOSEN_UA_PREFIX", default="anonymous")
                ) + " / NUCOSen Automatic Login"

@dataclass
class Session(object):
    mail_tel: str
    password: str
    mfa_token: str

    user_agent: str = UserAgent
    # レビュー2: requests を使う live.py / quote.py との完全なCookie受け渡し互換性を維持するため、RequestsCookieJar に戻します
    cookie: Optional[RequestsCookieJar] = None

    @retry(NetworkErrors, tries=3, delay=1, backoff=2, logger=getLogger(__name__ + ".login"))
    def login(self):
        getLogger(__name__).error("Cookie以外の旧来のログイン方法（ID/パスワード）は現在無効化されています。")
        raise RuntimeError("Cloudflare Turnstile対策のため、ID/パスワードでの自動ログインは無効化されています。ブラウザから新しいCookieをエクスポートし、Cookieファイルとして保存して再実行してください。")
        # --- 以下の旧来のログイン処理は無効化されました ---
        # header = {
        #     "User-Agent": self.user_agent,
        #     "Content-Type": "application/x-www-form-urlencoded"
        # }
        # with httpx.Client() as client:
        #     resp = client.post(
        #         "https://account.nicovideo.jp/login/redirector",
        #         data={
        #             "mail_tel": self.mail_tel,
        #             "password": self.password
        #         },
        #         headers=header,
        #         follow_redirects=False
        #     )
        #     resp.raise_for_status()
        #     if "user_session" in resp.cookies and resp.cookies.get("user_session") not in ("deleted", ""):
        #         jar = RequestsCookieJar()
        #         jar.update(client.cookies)
        #         self.cookie = jar
        #         getLogger(__name__).info("ユーザー名/パスワードによるログイン成功")
        #         self._auto_save_cookie()
        #         return
        #     if "mfa_session" in resp.cookies:
        #         self.__mfa_login(resp, header)
        #         getLogger(__name__).info("MFA成功")
        #         return
        #     raise ReLoginRequested("L15 ログイン失敗")

    def __mfa_login(self, resp: httpx.Response, header):
        raise RuntimeError("MFAログインも現在無効化されています。")

    def _auto_save_cookie(self):
        cookie_file = config("NICO_COOKIE_FILE", default="")
        if cookie_file:
            try:
                self.save_cookies(cookie_file)
            except Exception as e:
                getLogger(__name__).warning(f"クッキーの自動保存に失敗しました: {e}")

    def getSessionString(self) -> Optional[str]:
        # NOTE - X-niconico-sessionなどに使用
        if self.cookie is None:
            return None
        # RequestsCookieJar は __contains__ をサポートしているためそのまま判定可能です
        if "user_session" not in self.cookie:
            return None
        return self.cookie["user_session"]

    @classmethod
    def from_access_token(cls, access_token: str, mail_tel: str = "", password: str = "", mfa_token: str = "", user_agent: str = UserAgent):
        """Create a Session using an existing `user_session` access token.

        This avoids performing a login with username/password and MFA.
        The provided token is placed into a RequestsCookieJar so existing
        code that relies on `session.cookie` continues to work.
        """
        session = cls(mail_tel, password, mfa_token, user_agent=user_agent)
        jar = RequestsCookieJar()
        jar.set("user_session", access_token, domain=".nicovideo.jp", path="/")
        session.cookie = jar
        getLogger(__name__).info("認証済み情報によるログイン")
        return session

    def save_cookies(self, path: str):
        """Save current cookies to a file as a simple JSON mapping name->value.

        Only saves cookie name and value; this is sufficient for `user_session`.
        """
        if self.cookie is None:
            getLogger(__name__).warning("クッキーが空のため保存できませんでした")
            return
        # レビュー6: dict() によるシンプルでPythonicな辞書変換に変更します
        data = dict(self.cookie)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        getLogger(__name__).info(f"クッキーを保存しました: {path}")

    @classmethod
    def from_cookie_file(cls, path: str, mail_tel: str = "", password: str = "", mfa_token: str = "", user_agent: str = UserAgent):
        """Create a Session loading cookies from a JSON file saved by `save_cookies`.

        If the file contains name->value mapping, cookies are created with a
        default domain of `.nicovideo.jp` and path `/`.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(path)
        if p.stat().st_size == 0:
            raise ValueError(f"クッキーファイルが空です: {path}")
        jar = RequestsCookieJar()
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError(f"クッキーファイルは辞書形式である必要があります: {path}")
            for name, value in data.items():
                jar.set(name, value, domain=".nicovideo.jp", path="/")
        except json.JSONDecodeError as json_e:
            import http.cookiejar
            try:
                mozilla_jar = http.cookiejar.MozillaCookieJar(path)
                mozilla_jar.load()
                for cookie in mozilla_jar:
                    jar.set_cookie(cookie)
            except Exception as e:
                raise ValueError(f"クッキーファイルの形式が不正です。JSONでもNetscape形式でもありません: {path} (JSON Error: {json_e}, Netscape Error: {e})")
        session = cls(mail_tel, password, mfa_token, user_agent=user_agent)
        session.cookie = jar
        getLogger(__name__).info("認証済み情報によるログイン")
        return session


