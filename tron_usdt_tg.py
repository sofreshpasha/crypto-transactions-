#!/usr/bin/env python3
"""
tron_usdt_tg.py — уведомления в Telegram о поступлениях USDT (TRC-20) на кошелек TRON.

Что показывает в уведомлении:
    • сумма
    • валюта (символ токена)
    • полный адрес отправителя
    • дата и время
    • число подтверждений (сообщение редактируется по мере роста)

Команды в чате с ботом:
    /wallet            — показать текущий отслеживаемый кошелек
    /wallet T...       — сменить кошелек (история слежения обнуляется)
    /restart           — полный перезапуск процесса (перечитывает .env)
    /week [дней]       — выгрузка всех переводов за N дней (приход + расход), по умолчанию 7
    /status, /help     — краткая справка и текущие настройки

Источники данных (TronGrid V1 API + FullNode HTTP API):
    GET  /v1/accounts/{address}/transactions/trc20  — переводы TRC-20
    POST /wallet/getnowblock                        — текущая высота блока
    POST /wallet/gettransactioninfobyid             — блок конкретной транзакции

Зависимостей нет — только стандартная библиотека Python 3.7+.

Токен бота, chat_id, ключ TronGrid и адрес кошелька лежат в файле .env
рядом со скриптом. Остальное настраивается флагами.

Запуск:
    python3 tron_usdt_tg.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# ------------------------------------------------------------------ константы

API_BASE = "https://api.trongrid.io"
TG_BASE = "https://api.telegram.org"

# Контракт USDT (TRC-20) в основной сети TRON
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# Сколько подтверждений считаем окончательными (блок становится необратимым ~на 19-20)
DEFAULT_TARGET_CONF = 20

TRONSCAN_TX = "https://tronscan.org/#/transaction/"

# Лимит на одно сообщение в Telegram — 4096 символов, режем с запасом
TG_CHUNK = 3500

B58_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")

BOT_COMMANDS = [
    ("wallet", "показать или сменить отслеживаемый кошелек"),
    ("week", "переводы за N дней (приход и расход)"),
    ("restart", "перезапустить бота"),
    ("status", "текущие настройки"),
]


# ------------------------------------------------------------------ .env


def load_env():
    """Читает .env рядом со скриптом и кладет значения в os.environ."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


# ------------------------------------------------------------------ HTTP


def http_get(url: str, params: dict, headers: dict, timeout: int = 20):
    full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    req = urllib.request.Request(full, headers={"Accept": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post(url: str, payload: dict, headers: dict, timeout: int = 20):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ------------------------------------------------------------------ TRON


class Tron:
    def __init__(self, api_key: str | None):
        self.headers = {"TRON-PRO-API-KEY": api_key} if api_key else {}

    @staticmethod
    def _parse(it: dict) -> dict:
        info = it.get("token_info") or {}
        decimals = int(info.get("decimals", 6))
        return {
            "txid": it.get("transaction_id"),
            "ts": int(it.get("block_timestamp", 0)),
            "symbol": info.get("symbol") or "USDT",
            "raw_value": str(it.get("value", "0")),
            "decimals": decimals,
            "amount": fmt_units(it.get("value"), decimals),
            "from": it.get("from"),
            "to": it.get("to"),
        }

    def incoming_trc20(self, address: str, contract: str, min_ts: int, confirmed_only: bool):
        params = {
            "limit": 50,
            "only_to": "true",
            "contract_address": contract,
            "order_by": "block_timestamp,asc",
            "min_timestamp": min_ts,
        }
        if confirmed_only:
            params["only_confirmed"] = "true"
        data = http_get(f"{API_BASE}/v1/accounts/{address}/transactions/trc20", params, self.headers)

        out = []
        for it in data.get("data", []):
            if it.get("type") and it["type"] != "Transfer":
                continue
            out.append(self._parse(it))
        return out

    def all_trc20(self, address: str, contract: str, min_ts: int, max_pages: int = 10):
        """Все переводы токена за период — и входящие, и исходящие."""
        url = f"{API_BASE}/v1/accounts/{address}/transactions/trc20"
        params = {
            "limit": 200,
            "contract_address": contract,
            "order_by": "block_timestamp,desc",
            "min_timestamp": min_ts,
        }
        out = []
        for _ in range(max_pages):
            data = http_get(url, params, self.headers)
            rows = data.get("data", [])
            for it in rows:
                if it.get("type") and it["type"] != "Transfer":
                    continue
                out.append(self._parse(it))
            fingerprint = (data.get("meta") or {}).get("fingerprint")
            if not rows or not fingerprint:
                break
            params["fingerprint"] = fingerprint
        return out

    def now_block(self) -> int:
        d = http_post(f"{API_BASE}/wallet/getnowblock", {}, self.headers)
        return int((((d.get("block_header") or {}).get("raw_data")) or {}).get("number", 0))

    def tx_block(self, txid: str) -> int | None:
        d = http_post(f"{API_BASE}/wallet/gettransactioninfobyid", {"value": txid}, self.headers)
        n = d.get("blockNumber")
        return int(n) if n is not None else None


# ------------------------------------------------------------------ Telegram


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self.offset = None

    def _call(self, method: str, payload: dict, timeout: int = 20):
        url = f"{TG_BASE}/bot{self.token}/{method}"
        try:
            return http_post(url, payload, {}, timeout=timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            print(f"! Telegram {method}: HTTP {e.code} {detail}", file=sys.stderr, flush=True)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"! Telegram {method}: {e}", file=sys.stderr, flush=True)
        return None

    def send(self, text: str):
        r = self._call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        if r and r.get("ok"):
            return r["result"]["message_id"]
        return None

    def edit(self, message_id: int, text: str):
        self._call(
            "editMessageText",
            {
                "chat_id": self.chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    # --- команды

    def set_menu(self):
        self._call(
            "setMyCommands",
            {"commands": [{"command": c, "description": d} for c, d in BOT_COMMANDS]},
        )

    def drop_backlog(self):
        """Пропускаем все, что накопилось до старта, иначе старый /restart зациклит бота."""
        r = self._call("getUpdates", {"offset": -1, "timeout": 0})
        if r and r.get("ok") and r.get("result"):
            self.offset = r["result"][-1]["update_id"] + 1

    def confirm_offset(self):
        """Подтверждаем серверу обработанные апдейты (важно перед перезапуском)."""
        if self.offset is not None:
            self._call("getUpdates", {"offset": self.offset, "timeout": 0})

    def poll(self, wait: int = 0):
        payload = {"timeout": wait, "allowed_updates": ["message"]}
        if self.offset is not None:
            payload["offset"] = self.offset
        r = self._call("getUpdates", payload, timeout=wait + 15)
        texts = []
        if r and r.get("ok"):
            for u in r.get("result", []):
                self.offset = u["update_id"] + 1
                m = u.get("message") or {}
                if str((m.get("chat") or {}).get("id")) != self.chat_id:
                    continue
                text = (m.get("text") or "").strip()
                if text:
                    texts.append(text)
        return texts


# ------------------------------------------------------------------ формат


def fmt_units(raw, decimals: int) -> str:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return str(raw)
    if decimals <= 0:
        return f"{v:,}".replace(",", " ")
    s = str(v).rjust(decimals + 1, "0")
    whole, frac = s[:-decimals], s[-decimals:]
    whole = f"{int(whole):,}".replace(",", " ")
    frac = frac.rstrip("0")
    return f"{whole}.{frac}" if frac else whole


def to_float(raw, decimals: int) -> float:
    try:
        return int(raw) / (10 ** decimals)
    except (TypeError, ValueError):
        return 0.0


def fmt_amount(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ")


def valid_address(a: str) -> bool:
    return bool(a) and len(a) == 34 and a.startswith("T") and all(c in B58_ALPHABET for c in a)


def build_message(tx: dict, confirmations, target: int) -> str:
    dt = datetime.fromtimestamp(tx["ts"] / 1000).strftime("%d.%m.%Y %H:%M:%S")

    if confirmations is None:
        conf_line = "⏳ в мемпуле, блок еще не назначен"
    elif confirmations >= target:
        conf_line = f"✅ {confirmations} (подтверждена окончательно)"
    else:
        conf_line = f"⏳ {confirmations} из {target}"

    return (
        f"💵 <b>Поступление {tx['symbol']}</b>\n\n"
        f"<b>Сумма:</b> {tx['amount']} {tx['symbol']}\n"
        f"<b>Валюта:</b> {tx['symbol']} (TRC-20)\n"
        f"<b>От кого:</b>\n<code>{tx['from']}</code>\n"
        f"<b>Дата:</b> {dt}\n"
        f"<b>Подтверждений:</b> {conf_line}\n\n"
        f"<a href=\"{TRONSCAN_TX}{tx['txid']}\">Транзакция в Tronscan</a>"
    )


def build_report(address: str, rows: list, days: int) -> list:
    """Возвращает список сообщений (режем по лимиту Telegram)."""
    if not rows:
        return [f"За последние {days} дн. переводов не найдено."]

    rows = sorted(rows, key=lambda r: r["ts"], reverse=True)
    total_in = total_out = 0.0
    lines = []

    for tx in rows:
        value = to_float(tx["raw_value"], tx["decimals"])
        dt = datetime.fromtimestamp(tx["ts"] / 1000).strftime("%d.%m.%Y %H:%M")
        if tx.get("to") == address:
            total_in += value
            arrow, sign, label, peer = "🟢", "+", "от", tx.get("from")
        elif tx.get("from") == address:
            total_out += value
            arrow, sign, label, peer = "🔴", "−", "на", tx.get("to")
        else:
            arrow, sign, label, peer = "⚪️", "", "от", tx.get("from")
        lines.append(
            f"{arrow} <b>{sign}{fmt_amount(value)}</b> {tx['symbol']} · {dt} · {label}\n"
            f"<code>{peer or '—'}</code>"
        )

    header = (
        f"📄 <b>Переводы за {days} дн.</b>\n"
        f"<code>{address}</code>\n\n"
        f"Всего операций: {len(rows)}\n"
        f"🟢 Приход: {fmt_amount(total_in)}\n"
        f"🔴 Расход: {fmt_amount(total_out)}\n"
        f"Итог: {fmt_amount(total_in - total_out)}\n"
        + "—" * 20
    )

    chunks, buf = [], header
    for line in lines:
        if len(buf) + len(line) + 2 > TG_CHUNK:
            chunks.append(buf)
            buf = ""
        buf += "\n\n" + line
    if buf.strip():
        chunks.append(buf)
    return chunks


# ------------------------------------------------------------------ состояние


def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, json.JSONDecodeError):
        st = {}
    st.setdefault("last_ts", 0)
    st.setdefault("seen", [])
    st.setdefault("pending", {})
    st.setdefault("address", None)
    return st


def save_state(path: str, state: dict):
    state["seen"] = state["seen"][-1000:]
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        print(f"! Не удалось сохранить состояние: {e}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------ основной цикл


def main():
    load_env()

    # чувствительные данные — только из .env
    bot_token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")
    api_key = os.getenv("TRONGRID_API_KEY")
    address_env = os.getenv("TRON_ADDRESS")

    p = argparse.ArgumentParser(description="Уведомления в Telegram о поступлениях USDT на TRON-кошелек")
    p.add_argument("address", nargs="?", default=None, help="кошелек T... (перекрывает .env и сохраненный)")
    p.add_argument("--interval", type=float, default=10.0, help="пауза между опросами, сек (по умолчанию 10)")
    p.add_argument("--contract", default=USDT_CONTRACT, help="адрес контракта токена (по умолчанию USDT)")
    p.add_argument("--min-amount", type=float, default=0.0, help="игнорировать поступления меньше этой суммы")
    p.add_argument("--confirmations", type=int, default=DEFAULT_TARGET_CONF,
                   help=f"сколько подтверждений считать окончательными (по умолчанию {DEFAULT_TARGET_CONF})")
    p.add_argument("--confirmed-only", action="store_true",
                   help="уведомлять только по уже подтвержденным транзакциям (медленнее, но без правок сообщения)")
    p.add_argument("--state-file", default="tron_usdt_state.json", help="файл состояния между перезапусками")
    p.add_argument("--history", type=int, default=0, help="при первом запуске захватить последние N минут")
    args = p.parse_args()

    state = load_state(args.state_file)
    # приоритет: аргумент командной строки → сохраненный командой /wallet → .env
    address = args.address or state.get("address") or address_env

    missing = [n for n, v in (("TRON_ADDRESS", address),
                              ("TG_BOT_TOKEN", bot_token),
                              ("TG_CHAT_ID", chat_id)) if not v]
    if missing:
        p.error("не заполнены в .env: " + ", ".join(missing))

    if args.address and args.address != state.get("address"):
        state["address"] = args.address
        state["last_ts"] = 0
        state["seen"] = []
        state["pending"] = {}

    tron = Tron(api_key)
    tg = Telegram(bot_token, chat_id)

    if not state["last_ts"]:
        state["last_ts"] = int(time.time() * 1000) - args.history * 60 * 1000

    seen = set(state["seen"])
    pending = dict(state["pending"])

    print(f"Слежу за {address}")
    print(f"Токен: {args.contract} | интервал: {args.interval} c | цель подтверждений: {args.confirmations}")
    if not api_key:
        print("Без TronGrid API-ключа лимиты низкие — при 429 увеличьте --interval.")
    print("Ctrl+C для выхода.\n" + "-" * 60, flush=True)

    tg.set_menu()
    tg.drop_backlog()
    tg.send(
        f"🤖 <b>Бот запущен</b>\nСлежу за <code>{address}</code>\n\n"
        "/wallet — кошелек\n/week — переводы за неделю\n/restart — перезапуск"
    )

    # ---------------------------------------------------------- команды

    def cmd_status():
        tg.send(
            f"⚙️ <b>Настройки</b>\n"
            f"Кошелек: <code>{address}</code>\n"
            f"Контракт: <code>{args.contract}</code>\n"
            f"Интервал опроса: {args.interval} c\n"
            f"Цель подтверждений: {args.confirmations}\n"
            f"Мин. сумма: {args.min_amount}\n"
            f"В обработке: {len(pending)}\n\n"
            "<b>Команды</b>\n"
            "/wallet — показать кошелек\n"
            "/wallet T... — сменить кошелек\n"
            "/week [дней] — все переводы за период\n"
            "/restart — перезапустить бота"
        )

    def cmd_wallet(arg: str):
        nonlocal address
        if not arg:
            tg.send(
                f"👛 Сейчас слежу за:\n<code>{address}</code>\n\n"
                "Чтобы сменить: <code>/wallet TXXXXXXXX…</code>"
            )
            return
        new = arg.split()[0].strip()
        if not valid_address(new):
            tg.send("⚠️ Не похоже на TRON-адрес. Нужен формат <code>T...</code>, 34 символа.")
            return
        if new == address:
            tg.send("Это и есть текущий кошелек, ничего не меняю.")
            return

        address = new
        seen.clear()
        pending.clear()
        state["address"] = new
        state["seen"] = []
        state["pending"] = {}
        state["last_ts"] = int(time.time() * 1000)
        save_state(args.state_file, state)
        print(f"= кошелек изменен на {new}", flush=True)
        tg.send(
            f"✅ Кошелек изменен:\n<code>{new}</code>\n\n"
            "Слежу за новыми поступлениями с этого момента. "
            "Прошлое смотрите через /week."
        )

    def cmd_week(arg: str):
        days = 7
        if arg:
            try:
                days = max(1, min(60, int(arg.split()[0])))
            except ValueError:
                tg.send("⚠️ Укажите число дней, например <code>/week 14</code>")
                return
        tg.send(f"⏳ Собираю переводы за {days} дн. …")
        since = int(time.time() * 1000) - days * 86400 * 1000
        try:
            rows = tron.all_trc20(address, args.contract, since)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            tg.send(f"⚠️ TronGrid не ответил: {e}")
            return
        for chunk in build_report(address, rows, days):
            tg.send(chunk)

    def cmd_restart():
        state["seen"] = list(seen)
        state["pending"] = pending
        state["address"] = address
        save_state(args.state_file, state)
        tg.send("♻️ Перезапускаюсь…")
        tg.confirm_offset()  # чтобы команда не выполнилась повторно после старта
        print("= перезапуск по команде", flush=True)
        sys.stdout.flush()
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])

    def handle(text: str):
        if not text.startswith("/"):
            return
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/wallet":
            cmd_wallet(arg)
        elif cmd == "/week":
            cmd_week(arg)
        elif cmd == "/restart":
            cmd_restart()
        elif cmd in ("/status", "/help", "/start"):
            cmd_status()
        else:
            tg.send("Не знаю такую команду. /help")

    def idle(seconds: float):
        """Пауза, во время которой бот слушает команды."""
        deadline = time.time() + seconds
        while True:
            left = deadline - time.time()
            if left <= 0:
                return
            for text in tg.poll(wait=int(min(left, 15))):
                handle(text)

    # ---------------------------------------------------------- цикл

    while True:
        try:
            new_txs = tron.incoming_trc20(
                address, args.contract, state["last_ts"], args.confirmed_only
            )
        except urllib.error.HTTPError as e:
            hint = " — лимит запросов, увеличьте --interval" if e.code == 429 else ""
            print(f"! TronGrid HTTP {e.code}{hint}", file=sys.stderr, flush=True)
            idle(max(args.interval, 10))
            continue
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"! Сеть: {e}", file=sys.stderr, flush=True)
            idle(max(args.interval, 10))
            continue

        head = None
        if new_txs or pending:
            try:
                head = tron.now_block()
            except Exception as e:  # noqa: BLE001 — высота блока не критична для цикла
                print(f"! getnowblock: {e}", file=sys.stderr, flush=True)

        # --- новые поступления
        for tx in new_txs:
            if not tx["txid"] or tx["txid"] in seen:
                continue
            if to_float(tx["raw_value"], tx["decimals"]) < args.min_amount:
                seen.add(tx["txid"])
                state["last_ts"] = max(state["last_ts"], tx["ts"])
                continue

            conf = None
            block = None
            try:
                block = tron.tx_block(tx["txid"])
                if block and head:
                    conf = max(0, head - block)
            except Exception as e:  # noqa: BLE001
                print(f"! gettransactioninfobyid: {e}", file=sys.stderr, flush=True)

            msg_id = tg.send(build_message(tx, conf, args.confirmations))
            print(f"+ {tx['amount']} {tx['symbol']} от {tx['from']} (conf={conf})", flush=True)

            seen.add(tx["txid"])
            state["last_ts"] = max(state["last_ts"], tx["ts"])

            if msg_id and (conf is None or conf < args.confirmations):
                pending[tx["txid"]] = {"tx": tx, "message_id": msg_id, "block": block}

        # --- дожимаем подтверждения по уже отправленным сообщениям
        if head:
            for txid in list(pending.keys()):
                item = pending[txid]
                block = item.get("block")
                if not block:
                    try:
                        block = tron.tx_block(txid)
                        item["block"] = block
                    except Exception:  # noqa: BLE001
                        continue
                    if not block:
                        continue
                conf = max(0, head - block)
                tg.edit(item["message_id"], build_message(item["tx"], conf, args.confirmations))
                if conf >= args.confirmations:
                    pending.pop(txid, None)

        state["seen"] = list(seen)
        state["pending"] = pending
        state["address"] = address
        save_state(args.state_file, state)

        idle(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено.")
