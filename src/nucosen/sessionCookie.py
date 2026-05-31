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

from pyotp import TOTP
import httpx
from retry import retry
from decouple import AutoConfig
from os import getcwd

class ReLoginRequested(Exception):
    pass

config = AutoConfig(getcwd())
NetworkErrors = (httpx.ConnectError, httpx.HTTPStatusError, ReLoginRequested)
UserAgent = str(config("NUCOSEN_UA_PREFIX", default="anonymous")
                ) + " / NUCOSen Automatic Login"

@dataclass
class Session(object):
    mail_tel: str
    password: str
    mfa_token: str

    user_agent: str = UserAgent
    cookie: Optional[httpx.Cookies] = None

    @retry(NetworkErrors, tries=3, delay=1, backoff=2, logger=getLogger(__name__ + ".login"))
    def login(self):
        header = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/x-www-form-urlencoded"
        }
        with httpx.Client() as client:
            resp = client.post(
                "https://account.nicovideo.jp/login/redirector",
                data={
                    "mail_tel": self.mail_tel,
                    "password": self.password
                },
                headers=header,
                follow_redirects=False
            )
            resp.raise_for_status()
            if "user_session" in resp.cookies and resp.cookies.get("user_session") not in ("deleted", ""):
                self.cookie = httpx.Cookies(resp.cookies)
                getLogger(__name__).info("ユーザー名/パスワードによるログイン成功")
                self._auto_save_cookie()
                return
            if "mfa_session" in resp.cookies:
                self.__mfa_login(resp, header)
                getLogger(__name__).info("MFA成功")
                return
            raise ReLoginRequested("L15 ログイン失敗")

    def __mfa_login(self, resp: httpx.Response, header):
        if not self.mfa_token:
            getLogger(__name__).error("ニコニコ動画で2段階認証が要求されましたが、NICO_TFA (MFAトークン) が設定されていません。")
            raise ReLoginRequested("V40 MFA失敗 (トークン未設定)")
        try:
            import base64
            # pyotpのデコード処理と同様に、スペースを除去した上でBase32としてデコード可能か検証します
            base64.b32decode(self.mfa_token.replace(" ", ""), casefold=True)
        except Exception as e:
            getLogger(__name__).error(f"MFAトークン (NICO_TFA) のデコードに失敗しました。Base32形式が正しいか確認してください: {e}")
            raise ReLoginRequested("V40 MFA失敗 (トークン不正)")
        tfac = TOTP(self.mfa_token)
        current_cookies = httpx.Cookies(resp.cookies)

        with httpx.Client(cookies=current_cookies) as client:
            mfaResp = client.post(
                resp.headers["Location"],
                data={
                    "otp": tfac.now(),
                    "is_mfa_trusted_device": "false",
                },
                headers=header,
                follow_redirects=False,
            )
            mfaResp.raise_for_status()
            current_cookies.update(mfaResp.cookies)
            
            final_resp = client.get(
                mfaResp.headers["Location"],
                headers={"User-Agent": self.user_agent},
                follow_redirects=False
            )
            final_resp.raise_for_status()
            current_cookies.update(final_resp.cookies)

        if "user_session" in current_cookies and current_cookies.get("user_session") not in ("deleted", ""):
            self.cookie = current_cookies
            getLogger(__name__).info("ユーザー名/パスワード（MFA付き）によるログイン成功")
            self._auto_save_cookie()
            return
        raise ReLoginRequested("V40 MFA失敗")

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
        if "user_session" not in self.cookie:
            return None
        return self.cookie["user_session"]

    @classmethod
    def from_access_token(cls, access_token: str, mail_tel: str = "", password: str = "", mfa_token: str = "", user_agent: str = UserAgent):
        """Create a Session using an existing `user_session` access token.

        This avoids performing a login with username/password and MFA.
        The provided token is placed into a httpx.Cookies so existing
        code that relies on `session.cookie` continues to work.
        """
        session = cls(mail_tel, password, mfa_token, user_agent=user_agent)
        jar = httpx.Cookies()
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
        data = {name: value for name, value in self.cookie.items()}
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
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"クッキーファイルのJSON形式が不正です: {path} ({e})")
        if not isinstance(data, dict):
            raise ValueError(f"クッキーファイルは辞書形式である必要があります: {path}")
        jar = httpx.Cookies()
        for name, value in data.items():
            jar.set(name, value, domain=".nicovideo.jp", path="/")
        session = cls(mail_tel, password, mfa_token, user_agent=user_agent)
        session.cookie = jar
        getLogger(__name__).info("認証済み情報によるログイン")
        return session

