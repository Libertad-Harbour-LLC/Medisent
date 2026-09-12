#!/usr/bin/env python3
"""Получить GOOGLE_REFRESH_TOKEN, не выходя из терминала.

Альтернатива OAuth Playground для тех, у кого локально есть Python. Скрипт
поднимает страничку на localhost, открывает браузер, ловит код и меняет его
на refresh-токен. Ничего не устанавливает и никуда не отправляет: обмен идёт
напрямую с oauth2.googleapis.com.

    python3 scripts/get_gmail_token.py

Понадобятся Client ID и Client secret из Google Cloud Console. В настройках
OAuth-клиента в «Authorized redirect URIs» должен быть добавлен адрес
http://localhost:8765/ — иначе Google откажется возвращать код.
"""

from __future__ import annotations

import http.server
import json
import secrets
import socketserver
import sys
import urllib.parse
import urllib.request
import webbrowser

PORT = 8765
REDIRECT_URI = f"http://localhost:{PORT}/"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — адрес, не секрет
SCOPES = (
    "https://www.googleapis.com/auth/gmail.send "
    "https://www.googleapis.com/auth/gmail.readonly"
)

_received: dict[str, str] = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        _received.update({k: v[0] for k, v in params.items()})

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        message = (
            "Готово. Возвращайтесь в терминал."
            if "code" in _received
            else f"Google вернул ошибку: {_received.get('error', 'неизвестно')}"
        )
        self.wfile.write(f"<html><body><h2>{message}</h2></body></html>".encode())

    def log_message(self, *args: object) -> None:
        return  # не засорять вывод


def main() -> int:
    print("Получение refresh-токена для Gmail\n")
    client_id = input("Client ID: ").strip()
    client_secret = input("Client secret: ").strip()
    if not client_id or not client_secret:
        print("Оба значения обязательны.")
        return 1

    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        # Без этих двух Google вернёт только access-токен, а нам нужен
        # долгоживущий refresh: бот работает без участия человека.
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    print(f"\nОткрываю браузер. Если не открылся — перейдите вручную:\n{url}\n")
    webbrowser.open(url)

    print(f"Жду ответа на {REDIRECT_URI} …")
    with socketserver.TCPServer(("", PORT), Handler) as server:
        server.handle_request()

    if _received.get("state") != state:
        print("Ответ пришёл с чужим state — прерываю.")
        return 1
    code = _received.get("code")
    if not code:
        print(f"Код не получен: {_received.get('error', 'причина неизвестна')}")
        return 1

    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        }
    ).encode()

    request = urllib.request.Request(TOKEN_URL, data=data)  # noqa: S310 — адрес зафиксирован
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            payload = json.loads(response.read())
    except Exception as exc:
        print(f"Обмен кода на токен не удался: {exc}")
        return 1

    refresh = payload.get("refresh_token")
    if not refresh:
        print(
            "Google не вернул refresh_token.\n"
            "Обычно это значит, что доступ уже выдавался раньше. Отзовите его на\n"
            "https://myaccount.google.com/permissions и запустите скрипт заново."
        )
        return 1

    print("\n" + "=" * 60)
    print("Готово. Впишите в переменные окружения:\n")
    print(f"GOOGLE_CLIENT_ID={client_id}")
    print(f"GOOGLE_CLIENT_SECRET={client_secret}")
    print(f"GOOGLE_REFRESH_TOKEN={refresh}")
    print("=" * 60)
    print(
        "\nВажно: пока приложение в Google Cloud стоит в статусе «Testing»,\n"
        "этот токен протухнет через 7 дней. Чтобы он жил постоянно, опубликуйте\n"
        "приложение: OAuth consent screen → Publishing status → Publish app."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
