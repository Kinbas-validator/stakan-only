"""Единая точка доступа к REST Lighter с общим бюджетом запросов на площадку.

Лимит Lighter для запросов без входа считается по IP: около 60 в минуту на площадку.
Все REST-запросы приложения (сервер, страница через сервер, докачка сделок, позиции)
идут отсюда, поэтому вместе они не превышают BUDGET в минуту:
  * обычные запросы (стакан, позиции, сделки) ждут свободного места в окне;
  * фоновые (low=True: комиссии из транзакций) идут, только пока
    занято меньше LOW_SHARE бюджета, — у срочных всегда остаётся запас.
На ответ 429 площадка «замораживается» на Retry-After (не меньше 10 с) для всех.

Отправленные WebSocket-сообщения (подписки) тоже лимитированы по IP: 200 в минуту.
ws_budget() — общий счётчик для всех соединений сервера с одной площадкой.
"""
import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import deque

HOSTS = {"rh": "api.rh.lighter.xyz", "core": "mainnet.zklighter.elliot.ai"}
BUDGET = 48          # REST-запросов в минуту на площадку (из ~60 разрешённых)
LOW_SHARE = 0.6      # фоновые запросы — пока окно заполнено меньше чем на 60%
WS_BUDGET = 150      # отправленных WS-сообщений в минуту на площадку (из 200)


def ssl_context():
    """Сертификаты для HTTPS и WSS: системные + набор certifi.

    На Windows Python берёт корневые сертификаты из хранилища Windows, а оно пополняется лениво
    (сертификат появляется, когда его впервые запросит браузер) — без certifi бывает
    CERTIFICATE_VERIFY_FAILED. Системное хранилище тоже нужно: антивирус, проверяющий HTTPS,
    кладёт туда свой корневой сертификат.
    """
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except ImportError:
        pass
    return ctx


SSL = ssl_context()


class Limiter:
    def __init__(self, per_min, low_share=1.0):
        self.per_min, self.low_cap = per_min, int(per_min * low_share)
        self.sent = deque()
        self.frozen_until = 0.0
        self.cv = threading.Condition()
        self.used_total = 0

    def _trim(self, now):
        while self.sent and now - self.sent[0] >= 60:
            self.sent.popleft()

    def wait_time(self, low=False, n=1):
        now = time.time()
        self._trim(now)
        if now < self.frozen_until:
            return self.frozen_until - now
        cap = self.low_cap if low else self.per_min
        if len(self.sent) + n <= cap:
            return 0.0
        idx = len(self.sent) + n - cap - 1
        return max(0.05, 60 - (now - self.sent[idx]))

    def acquire(self, low=False, n=1, timeout=None):
        """Блокирует, пока не появится место. False — если не дождались timeout."""
        end = None if timeout is None else time.time() + timeout
        with self.cv:
            while True:
                w = self.wait_time(low, n)
                if w <= 0:
                    now = time.time()
                    self.sent.extend([now] * n)
                    self.used_total += n
                    return True
                if end is not None and time.time() + w > end:
                    return False
                self.cv.wait(min(w, 1.0))

    def freeze(self, seconds):
        with self.cv:
            self.frozen_until = max(self.frozen_until, time.time() + seconds)

    def usage(self):
        with self.cv:
            self._trim(time.time())
            return len(self.sent)


REST = {v: Limiter(BUDGET, LOW_SHARE) for v in HOSTS}
WS = {v: Limiter(WS_BUDGET) for v in HOSTS}


class ApiError(Exception):
    pass


def get(venue, path, low=False, timeout=None, tries=4):
    """GET https://<host>/api/v1/<path> через общий бюджет. timeout — сколько готовы ждать очередь."""
    url = f"https://{HOSTS[venue]}/api/v1/{path}"
    lim = REST[venue]
    last = None
    for attempt in range(tries):
        if not lim.acquire(low=low, timeout=timeout):
            raise ApiError("очередь запросов занята")
        try:
            with urllib.request.urlopen(url, timeout=30, context=SSL) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                try:
                    wait = float(e.headers.get("Retry-After") or 30)
                except ValueError:
                    wait = 30
                lim.freeze(max(10.0, min(wait, 300.0)))
            elif 400 <= e.code < 500:
                raise ApiError(f"HTTP {e.code}") from e
            else:
                time.sleep(3)
        except Exception as e:  # сеть
            last = e
            time.sleep(3 * (attempt + 1))
    raise ApiError(f"не удалось: {type(last).__name__} {last}")


def ws_url(venue):
    return f"wss://{HOSTS[venue]}/stream?readonly=true"


def stats():
    return {v: {"rest_last_min": REST[v].usage(), "rest_budget": BUDGET,
                "ws_sent_last_min": WS[v].usage(), "ws_budget": WS_BUDGET,
                "frozen": max(0, round(REST[v].frozen_until - time.time()))} for v in HOSTS}
