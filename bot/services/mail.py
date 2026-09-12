"""Gmail: отправка запросов цены и приём ответов.

Приём построен на опросе ``history.list`` раз в 3–5 минут. ``users.watch`` с
Pub/Sub прикручивается, когда заявок станет много.

Самое важное здесь — **порядок матчинга ответа с заявкой**. Он строго такой:

1. ``In-Reply-To`` / ``References`` → наш ``Message-ID``;
2. ``threadId`` Gmail;
3. токен заявки в теме письма;
4. адрес отправителя — **последним**.

Адрес стоит последним не для красоты: отвечают из общей почты, через
секретаря, с личного ящика. Матч по адресу привяжет ответ не к той заявке, и
заметить это будет некому — отсюда тесты именно на этот модуль.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_settings
from bot.db import repo
from bot.db.models import QuoteRequest
from bot.logging_setup import log_extra
from bot.services.http import ApiClient

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — это адрес, не секрет
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

TOKEN_RE = re.compile(r"\b(RFQ-\d{4}-\d+)\b", re.IGNORECASE)
MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")


class MailError(RuntimeError):
    pass


@dataclass(slots=True)
class ReplyHeaders:
    """Разобранные заголовки входящего письма."""

    gmail_id: str = ""
    thread_id: str | None = None
    subject: str = ""
    from_email: str = ""
    from_name: str = ""
    message_ids: list[str] = field(default_factory=list)  # из In-Reply-To и References
    body: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class MatchResult:
    quote: QuoteRequest | None
    method: str  # message_id | thread | token | sender | none


def extract_token(subject: str) -> str | None:
    """Токен заявки из темы: ``[RFQ-2026-041] Re: Запрос цены`` → ``RFQ-2026-041``."""
    match = TOKEN_RE.search(subject or "")
    return match.group(1).upper() if match else None


def parse_message_ids(*header_values: str | None) -> list[str]:
    """``In-Reply-To`` и ``References`` → список Message-ID в порядке появления.

    В ``References`` их обычно несколько, и наш может быть любым из них: цепочка
    могла успеть пройти через пересылку.
    """
    found: list[str] = []
    for value in header_values:
        if not value:
            continue
        for match in MESSAGE_ID_RE.finditer(value):
            candidate = match.group(0)
            if candidate not in found:
                found.append(candidate)
    return found


def _header(headers: list[dict[str, str]], name: str) -> str:
    lowered = name.lower()
    for item in headers:
        if item.get("name", "").lower() == lowered:
            return item.get("value", "")
    return ""


def _decode_b64url(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding)
    except (binascii.Error, ValueError):
        return b""


def _walk_parts(part: dict[str, Any]) -> list[dict[str, Any]]:
    parts = [part]
    for child in part.get("parts", []) or []:
        parts.extend(_walk_parts(child))
    return parts


def parse_message(payload: dict[str, Any]) -> ReplyHeaders:
    """Ответ Gmail API → структура, с которой работает матчинг."""
    message_payload = payload.get("payload", {}) or {}
    headers = message_payload.get("headers", []) or []
    raw_from = _header(headers, "From")
    name, address = parseaddr(raw_from)

    body_text = ""
    attachments: list[dict[str, Any]] = []
    for part in _walk_parts(message_payload):
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        filename = part.get("filename") or ""
        if filename and body.get("attachmentId"):
            attachments.append(
                {
                    "filename": filename,
                    "attachment_id": body["attachmentId"],
                    "mime_type": mime,
                    "size": body.get("size", 0),
                }
            )
        elif mime == "text/plain" and body.get("data") and not body_text:
            body_text = _decode_b64url(body["data"]).decode("utf-8", errors="replace")

    if not body_text:
        body_text = str(payload.get("snippet") or "")

    return ReplyHeaders(
        gmail_id=str(payload.get("id") or ""),
        thread_id=str(payload.get("threadId") or "") or None,
        subject=_header(headers, "Subject"),
        from_email=address.lower(),
        from_name=name,
        message_ids=parse_message_ids(
            _header(headers, "In-Reply-To"), _header(headers, "References")
        ),
        body=body_text,
        attachments=attachments,
    )


async def match_quote(session: AsyncSession, headers: ReplyHeaders) -> MatchResult:
    """Привязать ответ к отправленному запросу. Порядок шагов менять нельзя."""
    if headers.message_ids:
        quote = await repo.find_quote_by_message_id(session, headers.message_ids)
        if quote is not None:
            return MatchResult(quote, "message_id")

    if headers.thread_id:
        quote = await repo.find_quote_by_thread(session, headers.thread_id)
        if quote is not None:
            return MatchResult(quote, "thread")

    token = extract_token(headers.subject)
    if token:
        quote = await repo.find_quote_by_token(session, token)
        if quote is not None:
            return MatchResult(quote, "token")

    # Последний шаг и самый ненадёжный. Берётся только незакрытый запрос —
    # иначе старая переписка перехватит свежий ответ.
    if headers.from_email:
        quote = await repo.find_quote_by_sender(session, headers.from_email)
        if quote is not None:
            return MatchResult(quote, "sender")

    return MatchResult(None, "none")


def new_message_id(sender: str) -> str:
    """Свой ``Message-ID``. Генерируется до отправки и до записи в базу:
    по нему потом находится ответ и проверяется, ушло ли письмо после сбоя."""
    return make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)


def build_message(
    *,
    sender: str,
    sender_name: str,
    to: str,
    token: str,
    subject_suffix: str,
    body: str,
    message_id: str | None = None,
) -> tuple[str, str]:
    """Собрать письмо. Возвращает ``(base64url MIME, Message-ID)``.

    Свой ``Message-ID`` ставится намеренно: по нему потом находится ответ, и
    знать его надо до отправки, а не выковыривать из ответа Gmail. Обычно он
    уже зарезервирован в ``quote_requests`` и передаётся сюда.
    Токен в теме обязателен — это третий шаг матчинга.
    """
    message = EmailMessage()
    message["Subject"] = f"[{token}] {subject_suffix}"
    message["From"] = formataddr((sender_name, sender)) if sender_name else sender
    message["To"] = to
    message_id = message_id or new_message_id(sender)
    message["Message-ID"] = message_id
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    return raw, message_id


class MailService:
    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._client = ApiClient("gmail", timeout_read=60.0)
        self._access_token: str | None = None
        self._expires_at: dt.datetime = dt.datetime.min.replace(tzinfo=dt.UTC)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _token(self, request_id: int | None = None) -> str:
        """Access-token с запасом в минуту до истечения."""
        settings = self._settings
        if not settings.gmail_enabled:
            raise MailError("Gmail не настроен")

        now = dt.datetime.now(dt.UTC)
        if self._access_token and now < self._expires_at:
            return self._access_token

        result = await self._client.post(
            TOKEN_URL,
            operation="oauth.refresh",
            request_id=request_id,
            # В ответе приходят access_token и refresh_token. Одна отладочная
            # строка с CallResult — и токен в файле лога.
            redact_body=True,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": settings.google_refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if not result.ok:
            raise MailError(f"не удалось обновить токен Google: {result.error}")

        payload = result.json or {}
        token = payload.get("access_token")
        if not token:
            raise MailError("Google не вернул access_token")
        self._access_token = str(token)
        self._expires_at = now + dt.timedelta(seconds=int(payload.get("expires_in", 3600)) - 60)
        return self._access_token

    async def _auth_headers(self, request_id: int | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._token(request_id)}"}

    async def find_sent_by_message_id(
        self, message_id: str, *, request_id: int | None = None
    ) -> str | None:
        """Есть ли в ящике уже отправленное письмо с таким ``Message-ID``.

        Возвращает ``threadId`` найденного письма, иначе ``None``. Нужно после
        неясного сбоя отправки: Gmail мог принять письмо и не успеть ответить.
        """
        result = await self._client.get(
            f"{GMAIL_BASE}/messages",
            operation="messages.list.byMsgId",
            request_id=request_id,
            headers=await self._auth_headers(request_id),
            params={"q": f"rfc822msgid:{message_id.strip('<>')}", "maxResults": 1},
        )
        if not result.ok:
            return None
        messages = (result.json or {}).get("messages") or []
        if not messages:
            return None
        found_id = str(messages[0].get("id") or "")
        if not found_id:
            return None

        details = await self._client.get(
            f"{GMAIL_BASE}/messages/{found_id}",
            operation="messages.get.byMsgId",
            request_id=request_id,
            headers=await self._auth_headers(request_id),
            params={"format": "minimal"},
        )
        if not details.ok:
            return None
        return str((details.json or {}).get("threadId") or "")

    async def send(
        self,
        *,
        to: str,
        token: str,
        subject_suffix: str,
        body: str,
        request_id: int | None = None,
        message_id: str | None = None,
    ) -> tuple[str, str]:
        """Отправить письмо. Возвращает ``(threadId, Message-ID)``.

        Ретраев здесь нет намеренно. Отправка не идемпотентна: Gmail мог
        принять письмо и не успеть ответить, и слепой повтор отправил бы
        поставщику второй такой же запрос. Вместо повтора — проверка по
        собственному ``Message-ID``, который ставится до отправки: если письмо
        в ящике уже есть, значит оно ушло, и повторять нечего.
        """
        settings = self._settings
        raw, message_id = build_message(
            sender=settings.gmail_sender,
            sender_name="",
            to=to,
            token=token,
            subject_suffix=subject_suffix,
            body=body,
            message_id=message_id,
        )
        result = await self._client.post(
            f"{GMAIL_BASE}/messages/send",
            operation="messages.send",
            request_id=request_id,
            headers=await self._auth_headers(request_id),
            json={"raw": raw},
            retries=0,
        )

        if not result.ok:
            # Сбой мог случиться и после того, как Gmail принял письмо.
            # Прежде чем сказать «не отправлено», смотрим, нет ли его в ящике.
            existing_thread = await self.find_sent_by_message_id(message_id, request_id=request_id)
            if existing_thread is not None:
                logger.warning(
                    "Отправка вернула ошибку (%s), но письмо в ящике есть — "
                    "считаем отправленным",
                    result.error,
                    extra=log_extra(request_id),
                )
                return existing_thread, message_id
            raise MailError(f"письмо не отправлено: {result.error}")

        payload = result.json or {}
        thread_id = str(payload.get("threadId") or "")
        logger.info(
            "Письмо отправлено на %s, thread=%s, msgid=%s",
            to,
            thread_id,
            message_id,
            extra=log_extra(request_id),
        )
        return thread_id, message_id

    async def forward_file(
        self,
        *,
        to: str,
        filename: str,
        content: bytes,
        mime_type: str,
        request_id: int | None = None,
    ) -> None:
        """Переслать приложенный файл без обработки (команда владельца)."""
        message = EmailMessage()
        message["Subject"] = f"Файл из Telegram: {filename}"
        message["From"] = self._settings.gmail_sender
        message["To"] = to
        # Тот же приём, что у send: свой Message-ID до отправки, чтобы после
        # неясного сбоя проверить ящик, а не пересылать файл второй раз.
        message_id = new_message_id(self._settings.gmail_sender)
        message["Message-ID"] = message_id
        message.set_content("Файл переслан ботом подбора поставщиков.")
        maintype, _, subtype = mime_type.partition("/")
        message.add_attachment(
            content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=filename,
        )
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        result = await self._client.post(
            f"{GMAIL_BASE}/messages/send",
            operation="messages.forward",
            request_id=request_id,
            headers=await self._auth_headers(request_id),
            json={"raw": raw},
            retries=0,  # отправка не идемпотентна
        )
        if not result.ok:
            if await self.find_sent_by_message_id(message_id, request_id=request_id) is not None:
                logger.warning(
                    "Пересылка вернула ошибку (%s), но письмо в ящике есть — считаем ушедшим",
                    result.error,
                    extra=log_extra(request_id),
                )
                return
            raise MailError(f"файл не переслан: {result.error}")

    async def current_history_id(self, request_id: int | None = None) -> str | None:
        result = await self._client.get(
            f"{GMAIL_BASE}/profile",
            operation="users.profile",
            request_id=request_id,
            headers=await self._auth_headers(request_id),
        )
        if not result.ok:
            return None
        return str((result.json or {}).get("historyId") or "") or None

    async def new_message_ids(
        self, start_history_id: str, *, request_id: int | None = None
    ) -> tuple[list[str], str | None]:
        """Новые входящие с момента ``start_history_id``.

        Возвращает ``(id писем, новый historyId)``. Если Gmail сообщил, что
        историю потеряли (404), новый historyId придётся взять из профиля —
        иначе опрос застрянет навсегда.
        """
        message_ids: list[str] = []
        page_token: str | None = None
        latest: str | None = None

        while True:
            params: dict[str, Any] = {
                "startHistoryId": start_history_id,
                "historyTypes": "messageAdded",
                "labelId": "INBOX",
            }
            if page_token:
                params["pageToken"] = page_token

            result = await self._client.get(
                f"{GMAIL_BASE}/history",
                operation="history.list",
                request_id=request_id,
                headers=await self._auth_headers(request_id),
                params=params,
            )
            if not result.ok:
                if result.status_code == 404:
                    logger.warning("Gmail потерял историю — беру historyId из профиля")
                    return [], await self.current_history_id(request_id)
                logger.warning("history.list не ответил: %s", result.error)
                return [], None

            payload = result.json or {}
            latest = str(payload.get("historyId") or "") or latest
            for record in payload.get("history", []) or []:
                for added in record.get("messagesAdded", []) or []:
                    message = added.get("message", {})
                    msg_id = str(message.get("id") or "")
                    if msg_id and msg_id not in message_ids:
                        message_ids.append(msg_id)

            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        return message_ids, latest

    async def get_message(
        self, message_id: str, *, request_id: int | None = None, full: bool = True
    ) -> ReplyHeaders | None:
        """Письмо целиком (``full=True``) или только заголовки и фрагмент.

        Матчингу нужны лишь заголовки и ``threadId`` — их даёт дешёвый
        ``format=metadata``. Тело со всеми частями качается только для
        письма, которое привязалось к заявке: в ящик приходит не только
        почта поставщиков.
        """
        params: dict[str, Any] = {"format": "full"}
        if not full:
            params = {
                "format": "metadata",
                "metadataHeaders": ["From", "Subject", "In-Reply-To", "References"],
            }
        result = await self._client.get(
            f"{GMAIL_BASE}/messages/{message_id}",
            operation="messages.get" if full else "messages.get.metadata",
            request_id=request_id,
            headers=await self._auth_headers(request_id),
            params=params,
        )
        if not result.ok:
            logger.warning("Не удалось прочитать письмо %s: %s", message_id, result.error)
            return None
        return parse_message(result.json or {})

    async def download_attachment(
        self, message_id: str, attachment_id: str, *, request_id: int | None = None
    ) -> bytes | None:
        result = await self._client.get(
            f"{GMAIL_BASE}/messages/{message_id}/attachments/{attachment_id}",
            operation="messages.attachments",
            request_id=request_id,
            headers=await self._auth_headers(request_id),
        )
        if not result.ok:
            return None
        data = (result.json or {}).get("data")
        return _decode_b64url(str(data)) if data else None


_service: MailService | None = None


def get_mail_service() -> MailService:
    global _service
    if _service is None:
        _service = MailService()
    return _service


async def close_mail_service() -> None:
    global _service
    if _service is not None:
        await _service.aclose()
    _service = None
