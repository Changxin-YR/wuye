"""登录用户 → 智能体会话令牌。

令牌只放在 MCP 子进程的环境变量里，**不会进入模型上下文**。
令牌绑定 ``user_id`` 与 ``auth_version``：用户登出、角色或数据范围变化后自动失效。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SALT = "wuye-agent-token"
DEFAULT_TTL_SECONDS = 8 * 3600


class TokenError(Exception):
    """令牌缺失、过期或被篡改。"""


@dataclass(frozen=True)
class TokenIdentity:
    user_id: int
    auth_version: int


def _serializer(secret_key: str | None = None) -> URLSafeTimedSerializer:
    secret = secret_key or os.environ.get("SECRET_KEY")
    if not secret:
        raise TokenError("缺少 SECRET_KEY，无法签发智能体会话令牌")
    return URLSafeTimedSerializer(secret, salt=SALT)


def mint_token(user_id: int, auth_version: int, *, secret_key: str | None = None) -> str:
    """为某个登录用户签发短期令牌。"""
    return _serializer(secret_key).dumps({"uid": int(user_id), "av": int(auth_version)})


def read_token(token: str, *, secret_key: str | None = None, max_age: int = DEFAULT_TTL_SECONDS) -> TokenIdentity:
    """校验令牌并取回身份；失败一律抛 :class:`TokenError`。"""
    if not token or not token.strip():
        raise TokenError("缺少智能体会话令牌")
    try:
        payload = _serializer(secret_key).loads(token, max_age=max_age)
    except SignatureExpired as exc:  # 过期
        raise TokenError("智能体会话令牌已过期，请重新登录后再试") from exc
    except BadSignature as exc:  # 篡改
        raise TokenError("智能体会话令牌无效") from exc
    if not isinstance(payload, dict) or "uid" not in payload:
        raise TokenError("智能体会话令牌内容不完整")
    return TokenIdentity(user_id=int(payload["uid"]), auth_version=int(payload.get("av", 0)))
