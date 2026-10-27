import asyncio
import json
import logging
import os
import random
import re
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("countbot")

# === НАСТРОЙКИ ===
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = 1547463400919928832        # ID канала #счет
LOG_CHANNEL_ID = 1548649643275980840    # ID канала для логов
ROLE_NAME = "Счетовод"                  # Роль за победу/рубеж
ROLE_ID = int(os.getenv("ROLE_ID", "0") or 0)              # надёжнее имени (необязательно)
TOP1_ROLE_NAME = "Лучший счетовод"      # Роль за 1-е место в топе
TOP1_ROLE_ID = int(os.getenv("TOP1_ROLE_ID", "0") or 0)    # надёжнее имени (необязательно)
TOP1_ROLE_ENABLED = True                # Включить авто-выдачу роли топ-1
TARGET_COUNT = 100                      # До скольких считаем (классический режим и рубеж)
MAX_MILESTONES = 50                     # Сколько последних рубежей хранить (endless)

# Состояние хранится в data/ рядом со скриптом: эта папка не затрагивается обновлением из git.
# Пути можно переопределить переменными окружения DATA_DIR / STATE_FILE.
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
STATE_FILE = Path(os.getenv("STATE_FILE", DATA_DIR / "state.json"))
LEGACY_STATE_FILE = BASE_DIR / "state.json"  # старое расположение (рядом с bot.py)

# === ЗАЩИТА СЕРВЕРА ===
MAIN_GUILD_ID = 885532750822641675      # ID твоего сервера
MAIN_GUILD = discord.Object(id=MAIN_GUILD_ID)

# Поставьте True на ОДИН запуск, если у Discord остались старые глобальные команды
# (из-за них команды были видны на чужих серверах). Потом верните False.
CLEAR_OLD_GLOBAL_COMMANDS = False

# Могут ли админы участвовать в счёте?
ADMINS_CAN_COUNT = True

# === РЕЖИМЫ ===
MODE_CLASSIC = "classic"
MODE_ENDLESS = "endless"
MODE_ENDLESS_SOFT = "endless_soft"

MODE_NAMES = {
    MODE_CLASSIC: "🎯 Классический",
    MODE_ENDLESS: "♾️ Бесконечный (без сброса)",
    MODE_ENDLESS_SOFT: "🔄 Бесконечный (откат к рубежу)",
}

# Только ASCII-цифры, без ведущих нулей, не длиннее 15 знаков.
# (str.isdigit() пропускает «²», «①» и арабские цифры, а int("²") падает с ValueError.)
NUMBER_RE = re.compile(r"0|[1-9][0-9]{0,14}")

# Сколько упоминаний помещаем в одно сообщение (лимит Discord — 2000 символов).
MAX_MENTIONS_WINNERS = 40
MAX_MENTIONS_FAILED = 15

# === ИНТЕНТЫ ===
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.reactions = True


# ============================================================
#                    БОТ И ДЕРЕВО КОМАНД
# ============================================================

class GuardedTree(app_commands.CommandTree):
    """Дерево команд, которое работает только на главном сервере."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # False -> CheckFailure -> on_error ниже; команда НЕ выполняется.
        return interaction.guild_id == MAIN_GUILD_ID

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            text = "⛔ Нужны права администратора."
        elif isinstance(error, app_commands.CheckFailure):
            text = "⛔ Эта команда здесь недоступна."
        else:
            log.error("Ошибка в слэш-команде", exc_info=error)
            text = "⚠️ Произошла ошибка. Попробуйте позже."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


class CountingBot(commands.Bot):
    def __init__(self):
        # Префиксные команды не используются.
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=GuardedTree,
        )

    async def setup_hook(self):
        # Вызывается один раз за запуск (в отличие от on_ready, который срабатывает при каждом переподключении).
        try:
            synced = await self.tree.sync(guild=MAIN_GUILD)
            log.info("Синхронизировано %d слэш-команд на сервере %s", len(synced), MAIN_GUILD_ID)
            if CLEAR_OLD_GLOBAL_COMMANDS:
                self.tree.clear_commands(guild=None)
                await self.tree.sync()
                log.info("Старые глобальные команды удалены. Верните CLEAR_OLD_GLOBAL_COMMANDS = False.")
        except discord.HTTPException:
            log.exception("Ошибка синхронизации команд")

        # Корректное завершение по SIGTERM (останов/рестарт на хостинге): закрываем бота,
        # при закрытии состояние сохраняется (см. close()).
        # (signal.signal + create_task не будит event loop, поэтому используем add_signal_handler.)
        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGTERM, self._request_shutdown)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # например, Windows

    def _request_shutdown(self):
        log.info("Получен SIGTERM, завершаю работу...")
        self._shutdown_task = asyncio.get_running_loop().create_task(self.close())

    async def close(self):
        try:
            save_state()
            log.info("Состояние сохранено перед выходом.")
        except Exception:
            log.exception("Не удалось сохранить состояние при выходе")
        await super().close()


bot = CountingBot()

# === СОСТОЯНИЕ СЧЁТА ===
# ВАЖНО: все изменения счёта и статистики делаются в синхронных функциях (без await внутри),
# поэтому в asyncio они атомарны. Долгие операции (роли, сообщения, запись на диск)
# выполняются ПОСЛЕ изменения и работают только с копиями данных.
current_count = 0
last_user_id = None
participants = set()
milestone_participants = set()
mode = MODE_CLASSIC
milestones_reached = set()

# === СТАТИСТИКА ===
player_stats = {}
cur_streak = 0

# === СЛУЖЕБНОЕ ===
_last_state = {}
_load_error = None
_started = False
_top1_applied = None  # кому в последний раз пересчитывали роль топ-1 (в памяти)
_save_lock = None


# ============================================================
#                    ХЕЛПЕРЫ
# ============================================================

def is_admin(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return member.guild_permissions.administrator


def get_stats(user_id: int) -> dict:
    key = str(user_id)
    if key not in player_stats:
        player_stats[key] = {"score": 0, "best_streak": 0, "broken": 0, "games": 0}
    return player_stats[key]


def ranking():
    """Список (user_id, stats) по убыванию: баллы, лучший стрик, затем меньший id. Только score > 0."""
    items = [(int(uid), st) for uid, st in player_stats.items() if st.get("score", 0) > 0]
    items.sort(key=lambda kv: (-kv[1].get("score", 0), -kv[1].get("best_streak", 0), kv[0]))
    return items


def get_top1_user_id():
    items = ranking()
    return items[0][0] if items else None


def format_mentions(user_ids, limit: int) -> str:
    ids = list(user_ids)
    text = " ".join(f"<@{uid}>" for uid in ids[:limit])
    if len(ids) > limit:
        text += f" … и ещё {len(ids) - limit}"
    return text or "—"


def find_role(guild: discord.Guild, role_id: int, role_name: str):
    if role_id:
        return guild.get_role(role_id)
    return discord.utils.get(guild.roles, name=role_name)


async def safe_react(message: discord.Message, emoji: str):
    try:
        await message.add_reaction(emoji)
    except discord.HTTPException:  # Forbidden, NotFound (сообщение удалено) и т.д.
        pass


async def safe_delete(message: discord.Message):
    try:
        await message.delete()
    except discord.HTTPException:
        pass


async def reject(message: discord.Message, text: str):
    """Удаляет сообщение и показывает временное предупреждение."""
    await safe_delete(message)
    try:
        warning = await message.channel.send(text)
        await warning.delete(delay=5)
    except discord.HTTPException:
        pass


async def log_to_channel(text: str):
    """Отправляет сообщение в канал логов. Ошибки только пишутся в лог."""
    try:
        channel = bot.get_channel(LOG_CHANNEL_ID)
        if channel is None:
            log.warning("Канал логов %s не найден.", LOG_CHANNEL_ID)
            return
        # без пингов: в логах упоминания нужны только для чтения
        await channel.send(text[:2000], allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        log.exception("Не удалось отправить лог в канал")


# ============================================================
#                    СОХРАНЕНИЕ / ЗАГРУЗКА
# ============================================================

def _log_state_diff(data: dict):
    global _last_state
    changed = []
    for key in ("current_count", "mode", "cur_streak"):
        old, new = _last_state.get(key), data[key]
        if old != new:
            changed.append(f"{key}: {old} → {new}")
        _last_state[key] = new
    for key in ("participants", "milestone_participants", "milestones_reached"):
        old_len, new_len = _last_state.get(key + "_len"), len(data[key])
        if old_len != new_len:
            changed.append(f"{key}: {old_len} → {new_len}")
        _last_state[key + "_len"] = new_len
    if changed:
        log.info("💾 save_state: %s", " | ".join(changed))


def serialize_state() -> str:
    """Снимок состояния в виде строки. Вызывается в потоке event loop, поэтому данные согласованы."""
    data = {
        "current_count": current_count,
        "last_user_id": last_user_id,
        "participants": sorted(participants),
        "milestone_participants": sorted(milestone_participants),
        "mode": mode,
        "milestones_reached": sorted(milestones_reached),
        "player_stats": player_stats,
        "cur_streak": cur_streak,
    }
    _log_state_diff(data)
    return json.dumps(data, ensure_ascii=False, indent=2)


def write_state_file(payload: str):
    """Атомарная запись: временный файл + os.replace. Безопасно вызывать из потока."""
    tmp_path = None
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(STATE_FILE.parent), prefix="state_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, STATE_FILE)
        tmp_path = None
    except Exception:
        log.exception("Не удалось сохранить состояние")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def save_state():
    """Синхронное сохранение (для выхода из программы)."""
    write_state_file(serialize_state())


async def save_state_async():
    """Снимок берётся сразу (синхронно), запись идёт в потоке строго по очереди:
    порядок файлов на диске совпадает с порядком изменений, устаревшее не перезапишет новое."""
    global _save_lock
    if _save_lock is None:
        _save_lock = asyncio.Lock()
    payload = serialize_state()
    async with _save_lock:
        await asyncio.to_thread(write_state_file, payload)


def migrate_legacy_state():
    """Один раз копирует старый state.json из корня проекта в data/ (если там ещё ничего нет)."""
    if STATE_FILE.exists() or not LEGACY_STATE_FILE.exists():
        return
    if LEGACY_STATE_FILE.resolve() == STATE_FILE.resolve():
        return
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LEGACY_STATE_FILE, STATE_FILE)
        log.info("Состояние перенесено: %s -> %s", LEGACY_STATE_FILE, STATE_FILE)
    except OSError:
        log.exception("Не удалось перенести старый state.json")


def _clean_stats(raw) -> dict:
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        try:
            uid = str(int(key))
            out[uid] = {
                "score": int(value.get("score", 0)),
                "best_streak": int(value.get("best_streak", 0)),
                "broken": int(value.get("broken", 0)),
                "games": int(value.get("games", 0)),
            }
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def load_state():
    global current_count, last_user_id, participants, milestone_participants
    global mode, milestones_reached, player_stats, cur_streak, _load_error

    if not STATE_FILE.exists():
        log.info("ℹ️ Файл состояния %s не найден — стартуем с дефолтными значениями.", STATE_FILE)
        return

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        new_count = max(0, int(data.get("current_count", 0)))
        raw_last = data.get("last_user_id")
        new_last = int(raw_last) if raw_last is not None else None
        new_participants = {int(x) for x in data.get("participants", [])}
        new_segment = {int(x) for x in data.get("milestone_participants", [])}
        new_milestones = {int(x) for x in data.get("milestones_reached", [])}
        new_stats = _clean_stats(data.get("player_stats", {}))
        new_streak = max(0, int(data.get("cur_streak", 0)))
        new_mode = data.get("mode", MODE_CLASSIC)
        if new_mode not in MODE_NAMES:
            log.warning("Неизвестный режим %r в файле состояния, ставлю классический", new_mode)
            new_mode = MODE_CLASSIC
    except Exception as e:
        log.exception("❌ КРИТИЧНО: не удалось загрузить %s", STATE_FILE)
        _load_error = (
            f"❌ **КРИТИЧНО: не удалось загрузить state.json**\n"
            f"Ошибка: `{e}`\n"
            f"Бот стартует с дефолтными значениями (mode=classic, счёт=0)."
        )
        try:
            backup = STATE_FILE.with_name(f"state.broken.{int(time.time())}.json")
            shutil.copy(STATE_FILE, backup)
            log.warning("💾 Битый файл сохранён как %s", backup)
        except Exception:
            log.exception("Не удалось сохранить бэкап битого файла")
        return

    current_count, last_user_id = new_count, new_last
    participants, milestone_participants = new_participants, new_segment
    milestones_reached, player_stats = new_milestones, new_stats
    cur_streak, mode = new_streak, new_mode

    log.info(
        "💾 Состояние загружено: счёт=%s, режим=%s, рубежей=%s, игроков=%s, участников=%s, отрезок=%s, стрик=%s",
        current_count, mode, len(milestones_reached), len(player_stats),
        len(participants), len(milestone_participants), cur_streak,
    )

    if mode == MODE_CLASSIC and current_count > 150:
        log.warning(
            "⚠️ Режим classic, но счёт = %s. Возможно, режим должен быть endless/endless_soft.",
            current_count,
        )
    if current_count == 0 and not milestones_reached and not participants and not player_stats:
        log.warning("⚠️ Состояние полностью пустое.")


def _clear_run():
    """Сброс текущей игры (статистика игроков не трогается)."""
    global current_count, last_user_id, cur_streak
    current_count = 0
    last_user_id = None
    cur_streak = 0
    participants.clear()
    milestone_participants.clear()
    milestones_reached.clear()


# ============================================================
#                    ЛОГИКА СЧЁТА (синхронная, атомарная)
# ============================================================

def apply_count(author_id: int, number: int, author_is_admin: bool) -> dict:
    """Применяет число к состоянию и статистике, возвращает описание исхода.

    НЕ добавляйте сюда await и не делайте await перед вызовом этой функции в обработчике
    сообщения: иначе порядок обработки сообщений может нарушиться.
    """
    global current_count, last_user_id, cur_streak

    if author_id == last_user_id:
        return {"kind": "double"}

    expected = current_count + 1

    # ============ ПРАВИЛЬНОЕ ЧИСЛО ============
    if number == expected:
        current_count = number
        last_user_id = author_id
        participants.add(author_id)
        milestone_participants.add(author_id)

        st = get_stats(author_id)
        st["score"] += 1
        cur_streak += 1
        if cur_streak > st["best_streak"]:
            st["best_streak"] = cur_streak

        result = {
            "kind": "ok", "win": False, "winners": [],
            "milestone": None, "segment": [], "total": 0,
        }

        if mode == MODE_CLASSIC:
            if number >= TARGET_COUNT:
                winners = sorted(participants)
                for uid in winners:
                    get_stats(uid)["games"] += 1
                result["win"] = True
                result["winners"] = winners
                # Новая игра начинается сразу: пока выдаются роли, счёт уже идёт с 1.
                _clear_run()
        else:
            if number % TARGET_COUNT == 0 and number not in milestones_reached:
                milestones_reached.add(number)
                while len(milestones_reached) > MAX_MILESTONES:
                    milestones_reached.discard(min(milestones_reached))
                result["milestone"] = number
                result["segment"] = sorted(milestone_participants)
                result["total"] = len(participants)
                milestone_participants.clear()
        return result

    # ============ ОШИБКА ============
    broken_at = current_count
    streak_before = cur_streak
    result = {
        "kind": "wrong",
        "mode": mode,
        "number": number,
        "expected": expected,
        "broken_at": broken_at,
        "streak": streak_before,
        "rollback_to": None,
    }

    if not author_is_admin and broken_at > 0:  # при счёте 0 ломать нечего
        get_stats(author_id)["broken"] += 1
    cur_streak = 0

    if mode == MODE_CLASSIC:
        _clear_run()
    elif mode == MODE_ENDLESS_SOFT:
        rollback_to = (broken_at // TARGET_COUNT) * TARGET_COUNT
        result["rollback_to"] = rollback_to
        current_count = rollback_to
        last_user_id = None
        milestone_participants.clear()
    # MODE_ENDLESS: счёт не меняется
    return result


# ============================================================
#                    ВЫДАЧА РОЛЕЙ
# ============================================================

async def give_role_to_participants(guild: discord.Guild, user_ids, reason: str):
    """Возвращает (role, выдано_новых, [id не получивших]). role=None, если роль не найдена."""
    role = find_role(guild, ROLE_ID, ROLE_NAME)
    if role is None:
        return None, 0, []

    given = 0
    failed = []
    for uid in list(user_ids):
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.HTTPException:  # вышел с сервера и т.п.
                failed.append(uid)
                continue
        if role in member.roles:
            continue
        try:
            await member.add_roles(role, reason=reason)
            given += 1
        except discord.HTTPException:  # Forbidden (иерархия/права) и прочие ошибки API
            failed.append(uid)
    return role, given, failed


def roles_report(role, given: int, failed) -> str:
    if role is None:
        return f"⚠️ Роль **{ROLE_NAME}** не найдена на сервере — проверьте ROLE_NAME / ROLE_ID."
    text = f"🏅 Роль `{role.name}` выдана: **{given}**"
    if failed:
        text += f"\n⚠️ Не удалось выдать: {format_mentions(failed, MAX_MENTIONS_FAILED)}"
    return text


async def _strip_role(members, role, reason: str):
    for m in members:
        try:
            await m.remove_roles(role, reason=reason)
        except discord.HTTPException:
            log.warning("Не удалось снять роль %s у %s", role.name, m.id)


async def update_top1_role(guild: discord.Guild):
    if not TOP1_ROLE_ENABLED or guild is None:
        return
    role = find_role(guild, TOP1_ROLE_ID, TOP1_ROLE_NAME)
    if role is None:
        return

    top_id = get_top1_user_id()
    holders = list(role.members)

    if top_id is None:
        await _strip_role(holders, role, "Топ-1: статистика пуста")
        return

    leader = guild.get_member(top_id)
    if leader is None:
        try:
            leader = await guild.fetch_member(top_id)
        except discord.HTTPException:
            leader = None
    if leader is None:
        await _strip_role(holders, role, "Топ-1: лидер покинул сервер")
        return

    await _strip_role([m for m in holders if m.id != top_id], role, "Топ-1: потерял 1-е место")

    if role not in leader.roles:
        try:
            await leader.add_roles(role, reason="Топ-1: вышел на 1-е место")
        except discord.HTTPException:
            log.warning("Не удалось выдать роль топ-1 пользователю %s", leader.id)


async def refresh_top1_if_changed(guild: discord.Guild):
    """Роль пересчитывается только когда сменился лидер (а не после каждого числа)."""
    global _top1_applied
    if not TOP1_ROLE_ENABLED or guild is None:
        return
    top_id = get_top1_user_id()
    if top_id == _top1_applied:
        return
    _top1_applied = top_id
    try:
        await update_top1_role(guild)
    except Exception:
        log.exception("Ошибка обновления роли топ-1")


# ============================================================
#                    ПОБЕДА / РУБЕЖИ
# ============================================================

async def announce_victory(channel, guild: discord.Guild, winners):
    # Состояние уже сброшено и сохранено, поэтому любая ошибка здесь не «замораживает» игру.
    try:
        role, given, failed = await give_role_to_participants(
            guild, winners, f"Досчитали до {TARGET_COUNT} (classic)"
        )

        victory_phrases = [
            "🎉 **НЕВЕРОЯТНО!**",
            "🏆 **ЛЕГЕНДАРНО!**",
            "🔥 **ЭТО ПОБЕДА!**",
            "💎 **ИДЕАЛЬНАЯ ЦЕПОЧКА!**",
            "⚡ **МОЛНИЕНОСНО!**",
            "🌟 **ВЫ СДЕЛАЛИ ЭТО!**",
        ]

        victory_msg = await channel.send(
            f"{random.choice(victory_phrases)}\n\n"
            f"🎯 Цепочка **1 → {TARGET_COUNT}** пройдена!\n\n"
            f"🏆 **Герои этой игры:**\n{format_mentions(winners, MAX_MENTIONS_WINNERS)}\n\n"
            f"👥 Участников: **{len(winners)}**\n"
            f"{roles_report(role, given, failed)}\n\n"
            f"🔄 **Новая игра начинается — следующее число 1!**"
        )

        try:
            await victory_msg.pin()
        except discord.HTTPException:
            pass

        for emoji in ("🎉", "🏆", "🥳"):
            await safe_react(victory_msg, emoji)

        await log_to_channel(
            f"🏆 **ПОБЕДА в classic!**\n"
            f"🎯 Цепочка 1 → {TARGET_COUNT} пройдена\n"
            f"👥 Участников: **{len(winners)}**\n"
            f"🏅 Роль выдана: **{given}**"
            + (f"\n⚠️ Не удалось выдать: {len(failed)}" if failed else "")
        )
    except Exception:
        log.exception("Ошибка при объявлении победы")


async def announce_milestone(channel, guild: discord.Guild, milestone: int, segment, total: int, current_mode: str):
    try:
        role, given, failed = await give_role_to_participants(
            guild, segment, f"Достигли рубежа {milestone}"
        )

        if role is None:
            await channel.send(
                f"🎯 Рубеж **{milestone}** достигнут! "
                f"(Роль **{ROLE_NAME}** не найдена, но это не важно — продолжаем!)"
            )
            return

        phrases = [
            "🎉 **НОВЫЙ РУБЕЖ!**",
            "🏆 **ОТЛИЧНО!**",
            "🔥 **ТАК ДЕРЖАТЬ!**",
            "💎 **КРУТО!**",
            "⚡ **МОЩНО!**",
            "🌟 **ПРОДОЛЖАЕМ!**",
        ]

        mode_hint = ""
        if current_mode == MODE_ENDLESS_SOFT:
            mode_hint = "\n⚠️ *Ошибка откатит счёт к этому рубежу.*"

        msg = await channel.send(
            f"{random.choice(phrases)}\n\n"
            f"🎯 Достигнут рубеж **{milestone}**!\n\n"
            f"🏆 **Участники последнего отрезка:**\n{format_mentions(segment, MAX_MENTIONS_WINNERS)}\n\n"
            f"👥 Участников: **{total}**\n"
            f"\n♾️ **Счёт продолжается — следующее число {milestone + 1}!**"
            f"{mode_hint}"
        )

        for emoji in ("🎉", "🏆"):
            await safe_react(msg, emoji)

        await log_to_channel(
            f"🎯 **Достигнут рубеж {milestone}**\n"
            f"🎮 Режим: **{MODE_NAMES.get(current_mode, current_mode)}**\n"
            f"👥 В отрезке: **{len(segment)}**\n"
            f"👥 Всего в забеге: **{total}**\n"
            f"🏅 Роль выдана: **{given}**"
            + (f"\n⚠️ Не удалось выдать: {len(failed)}" if failed else "")
        )
    except Exception:
        log.exception("Ошибка при объявлении рубежа")


# ============================================================
#                    СОБЫТИЯ
# ============================================================

async def leave_foreign_guild(guild: discord.Guild):
    log.warning("Бот на чужом сервере: %s (%s) — выхожу.", guild.name, guild.id)
    try:
        await guild.leave()
    except discord.HTTPException:
        log.exception("Не удалось выйти с %s", guild.name)


@bot.event
async def on_ready():
    # on_ready может вызываться несколько раз (переподключения): разовые действия — под флагом.
    global _started, _top1_applied

    log.info("✅ Бот %s на связи", bot.user)

    for guild in list(bot.guilds):
        if guild.id != MAIN_GUILD_ID:
            await leave_foreign_guild(guild)

    if _started:
        return
    _started = True

    log.info("📋 Канал счёта: %s | 📝 канал логов: %s | 🎮 режим: %s | 🎯 рубеж: %s | 👑 админы считают: %s | 🏆 роль топ-1: %s",
             CHANNEL_ID, LOG_CHANNEL_ID, MODE_NAMES.get(mode, mode), TARGET_COUNT,
             ADMINS_CAN_COUNT, TOP1_ROLE_NAME if TOP1_ROLE_ENABLED else "отключена")

    main_guild = bot.get_guild(MAIN_GUILD_ID)
    if main_guild is None:
        log.error("Главный сервер %s недоступен боту — проверьте MAIN_GUILD_ID и что бот приглашён.", MAIN_GUILD_ID)
    else:
        _top1_applied = get_top1_user_id()
        try:
            await update_top1_role(main_guild)
        except Exception:
            log.exception("Ошибка синхронизации роли топ-1")

    if _load_error:
        await log_to_channel(_load_error)

    await log_to_channel(
        f"🚀 **Бот запущен**\n"
        f"🎮 Режим: **{MODE_NAMES.get(mode, mode)}**\n"
        f"🔢 Счёт: **{current_count}**\n"
        f"🎯 Рубежей пройдено: **{len(milestones_reached)}**\n"
        f"👥 Участников в забеге: **{len(participants)}**\n"
        f"📊 Игроков в статистике: **{len(player_stats)}**"
    )


@bot.event
async def on_guild_join(guild: discord.Guild):
    if guild.id != MAIN_GUILD_ID:
        await leave_foreign_guild(guild)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # === ЗАЩИТА: игнорируем чужие серверы и ЛС ===
    if message.guild is None or message.guild.id != MAIN_GUILD_ID:
        return

    if message.channel.id != CHANNEL_ID:
        return

    await handle_count_message(message)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    """Редактирование сообщений в канале счёта запрещено."""
    if after.author.bot:
        return
    if after.guild is None or after.guild.id != MAIN_GUILD_ID:
        return
    if after.channel.id != CHANNEL_ID:
        return
    if before.content == after.content:  # например, подгрузилось превью ссылки
        return

    author_is_admin = is_admin(after.author)
    if author_is_admin and not ADMINS_CAN_COUNT:
        return

    await reject(
        after,
        f"{after.author.mention}, редактировать сообщения в канале счёта нельзя! "
        f"Сообщение удалено.",
    )


# ============================================================
#                    ОСНОВНАЯ ЛОГИКА СЧЁТА
# ============================================================

async def handle_count_message(message: discord.Message):
    # ВАЖНО: до вызова apply_count здесь нет ни одного await (порядок сообщений сохраняется).
    author_is_admin = is_admin(message.author)

    if author_is_admin and not ADMINS_CAN_COUNT:
        return

    content = message.content.strip()

    # === Не число ===
    if not NUMBER_RE.fullmatch(content):
        if author_is_admin:
            return  # админу не удаляем сообщение и не пишем предупреждение
        await reject(
            message,
            f"{message.author.mention}, здесь нужно писать **только числа** "
            f"(без лишних символов и нулей в начале)! Сообщение удалено.",
        )
        return

    result = apply_count(message.author.id, int(content), author_is_admin)

    # === Нельзя считать дважды подряд ===
    if result["kind"] == "double":
        if author_is_admin:
            return  # админу не мешаем — просто не засчитываем
        await reject(
            message,
            f"{message.author.mention}, нельзя считать дважды подряд! "
            f"Дождись другого участника.",
        )
        return

    # Состояние уже изменено; дальше только ввод-вывод — гонки с другими сообщениями не страшны.
    await save_state_async()

    # === Правильное число ===
    if result["kind"] == "ok":
        await safe_react(message, "✅")
        if result["win"]:
            await announce_victory(message.channel, message.guild, result["winners"])
        elif result["milestone"] is not None:
            await announce_milestone(
                message.channel, message.guild,
                result["milestone"], result["segment"], result["total"], mode,
            )
        await refresh_top1_if_changed(message.guild)
        return

    # === Ошибка: число не то ===
    await handle_wrong(message, result, author_is_admin)


async def handle_wrong(message: discord.Message, result: dict, author_is_admin: bool):
    broken_at = result["broken_at"]
    expected = result["expected"]
    number = result["number"]
    result_mode = result["mode"]

    try:
        # ---------------- CLASSIC ----------------
        if result_mode == MODE_CLASSIC:
            if not author_is_admin:
                await safe_react(message, "❌")

            fail_phrases = [
                "испортил счёт на",
                "всё сломал на",
                "обнулил цепочку на",
                "заруинил счёт на",
                "уронил счёт на",
            ]
            mention = "" if author_is_admin else f"{message.author.mention} "
            if broken_at == 0:
                # Например, число, написанное сразу после победы, когда новая игра уже началась.
                fail_msg = await message.channel.send(
                    f"⚠️ {mention}новая игра уже идёт — следующее число **1**."
                )
                await safe_react(fail_msg, "🤔")
            else:
                fail_msg = await message.channel.send(
                    f"💥 {mention}"
                    f"**{random.choice(fail_phrases)} {broken_at}!!**\n"
                    f"Следующее число — **1**. Игра начинается сначала."
                )
                await safe_react(fail_msg, "😡")

        # ---------------- ENDLESS (без сброса) ----------------
        elif result_mode == MODE_ENDLESS:
            if not author_is_admin:
                await safe_react(message, "❌")

            mention = "" if author_is_admin else f"{message.author.mention}, "
            fail_msg = await message.channel.send(
                f"⚠️ {mention}сейчас ждали **{expected}**, а не **{number}**.\n"
                f"В бесконечном режиме счёт **не сбрасывается**. "
                f"Следующее число — **{expected}**."
            )
            await safe_react(fail_msg, "🤔")

        # ---------------- ENDLESS_SOFT (откат к рубежу) ----------------
        elif result_mode == MODE_ENDLESS_SOFT:
            rollback_to = result["rollback_to"]
            lost = broken_at - rollback_to

            if not author_is_admin:
                await safe_react(message, "💥")

            if rollback_to == 0:
                rollback_text = "**0** (рубежей ещё не было)"
            else:
                rollback_text = f"**{rollback_to}** (последний рубеж)"

            mention = "" if author_is_admin else f"{message.author.mention} "
            fail_msg = await message.channel.send(
                f"💥 {mention}ошибся на **{broken_at}**!\n"
                f"🔽 Откат до {rollback_text}. Потеряно чисел: **{lost}**.\n"
                f"Следующее число — **{rollback_to + 1}**."
            )
            await safe_react(fail_msg, "😤")
    except discord.HTTPException:
        log.warning("Не удалось отправить сообщение об ошибке в счёте")

    await log_to_channel(
        f"❌ **Ошибка в счёте**\n"
        f"👤 Кто: {message.author.mention} (`{message.author.id}`)\n"
        f"✍️ Написал: **{number}**\n"
        f"🎯 Ждали: **{expected}**\n"
        f"🎮 Режим: **{MODE_NAMES.get(result_mode, result_mode)}**\n"
        f"🔢 Счёт до ошибки: **{broken_at}**\n"
        f"🔥 Стрик: **{result['streak']}**"
    )


# ============================================================
#                    СЛЭШ-КОМАНДЫ (только главный сервер)
# ============================================================

@bot.tree.command(name="change_mode", description="Сменить режим счёта", guild=MAIN_GUILD)
@app_commands.describe(
    new_mode="Выберите режим",
    reset="Сбросить текущий счёт при смене режима?"
)
@app_commands.choices(new_mode=[
    app_commands.Choice(name="Классический (сброс при ошибке, победа на 100)", value=MODE_CLASSIC),
    app_commands.Choice(name="Бесконечный (ошибка не сбрасывает)", value=MODE_ENDLESS),
    app_commands.Choice(name="Бесконечный мягкий (откат к последнему рубежу)", value=MODE_ENDLESS_SOFT),
])
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def change_mode(
    interaction: discord.Interaction,
    new_mode: app_commands.Choice[str],
    reset: bool = False,
):
    global mode

    old_mode = mode

    if new_mode.value == old_mode and not reset:
        await interaction.response.send_message(
            f"ℹ️ Режим уже **{MODE_NAMES[old_mode]}**. Ничего не изменилось.",
            ephemeral=True,
        )
        return

    mode = new_mode.value
    if reset:
        _clear_run()

    if new_mode.value == old_mode:
        text = (
            f"🔄 Счёт сброшен. Режим остался прежним: **{MODE_NAMES[mode]}**\n"
            f"Текущий счёт: **{current_count}**"
        )
    else:
        text = (
            f"✅ Режим изменён: **{MODE_NAMES.get(old_mode, old_mode)}** → **{MODE_NAMES[mode]}**\n"
            f"Текущий счёт: **{current_count}**"
            + ("\n🔄 Счёт сброшен." if reset else "")
        )

    await save_state_async()
    await interaction.response.send_message(text, ephemeral=False)

    await log_to_channel(
        f"⚙️ **Смена режима**\n"
        f"👤 Кто: {interaction.user.mention} (`{interaction.user.id}`)\n"
        f"🔁 Было: **{MODE_NAMES.get(old_mode, old_mode)}**\n"
        f"➡️ Стало: **{MODE_NAMES[mode]}**\n"
        f"🔄 Сброс счёта: **{'да' if reset else 'нет'}**\n"
        f"📊 Текущий счёт: **{current_count}**"
    )


@bot.tree.command(name="reset_count", description="Сбросить счёт вручную", guild=MAIN_GUILD)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def reset_count(interaction: discord.Interaction):
    old_count = current_count
    _clear_run()
    await save_state_async()
    await interaction.response.send_message(
        "🔄 Счёт сброшен вручную. Начинаем с **1**.",
        ephemeral=False,
    )
    await log_to_channel(
        f"🛠 **Ручной сброс счёта**\n"
        f"👤 Кто: {interaction.user.mention} (`{interaction.user.id}`)\n"
        f"🔢 Было: **{old_count}** → **0**"
    )


@bot.tree.command(name="set_count", description="Установить текущее число вручную", guild=MAIN_GUILD)
@app_commands.describe(number="Число, с которого продолжится счёт")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def set_count(interaction: discord.Interaction, number: app_commands.Range[int, 0, 1_000_000]):
    global current_count, last_user_id
    old_count = current_count
    current_count = number
    last_user_id = None
    # Рубежи выше нового счёта должны уметь сработать снова.
    milestones_reached.difference_update({m for m in milestones_reached if m > number})
    await save_state_async()
    await interaction.response.send_message(
        f"✅ Счёт установлен на **{number}**. Следующее число — **{number + 1}**.",
        ephemeral=False,
    )
    await log_to_channel(
        f"🛠 **Ручная установка счёта**\n"
        f"👤 Кто: {interaction.user.mention} (`{interaction.user.id}`)\n"
        f"🔢 Было: **{old_count}** → **{number}**"
    )


@bot.tree.command(name="count_status", description="Показать текущий статус счёта", guild=MAIN_GUILD)
@app_commands.guild_only()
async def count_status(interaction: discord.Interaction):
    if mode == MODE_ENDLESS_SOFT:
        last_milestone = (current_count // TARGET_COUNT) * TARGET_COUNT
        rollback_hint = f"**{last_milestone}**"
    else:
        rollback_hint = "—"

    await interaction.response.send_message(
        f"📊 **Статус счёта:**\n"
        f"Режим: **{MODE_NAMES.get(mode, mode)}**\n"
        f"Текущее число: **{current_count}**\n"
        f"Участников в цепочке: **{len(participants)}**\n"
        f"Участников в отрезке: **{len(milestone_participants)}**\n"
        f"Рубеж: **{TARGET_COUNT}**\n"
        f"Следующее число: **{current_count + 1}**\n"
        f"Откат при ошибке: {rollback_hint}\n"
        f"Админы считают: **{'да' if ADMINS_CAN_COUNT else 'нет'}**",
        ephemeral=True,
    )


@bot.tree.command(name="stats", description="Показать статистику игрока", guild=MAIN_GUILD)
@app_commands.describe(user="Чей профиль показать (по умолчанию — свой)")
@app_commands.guild_only()
async def stats(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    target = user or interaction.user

    if target.bot:
        await interaction.response.send_message("🤖 У ботов нет статистики.", ephemeral=True)
        return

    st = player_stats.get(str(target.id))
    if st is None or st.get("score", 0) == 0:
        await interaction.response.send_message(
            f"📭 У {target.mention} пока нет ни одного засчитанного числа.",
            ephemeral=True,
        )
        return

    rank = next((i + 1 for i, (uid, _) in enumerate(ranking()) if uid == target.id), None)

    total_score = st.get("score", 0)
    broken = st.get("broken", 0)
    games = st.get("games", 0)
    best = st.get("best_streak", 0)
    accuracy = total_score / (total_score + broken) * 100 if (total_score + broken) else 0

    embed = discord.Embed(
        title=f"📊 Статистика — {target.display_name}",
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="🏅 Баллы", value=f"**{total_score}**", inline=True)
    embed.add_field(name="🏆 Место в топе", value=f"**#{rank}**" if rank else "—", inline=True)
    embed.add_field(name="🔥 Лучший стрик", value=f"**{best}**", inline=True)
    embed.add_field(name="💥 Сломал цепочек", value=f"**{broken}**", inline=True)
    embed.add_field(name="🎮 Победных игр", value=f"**{games}**", inline=True)
    embed.add_field(name="🎯 Точность", value=f"**{accuracy:.1f}%**", inline=True)
    embed.set_footer(text=f"ID: {target.id}")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="top", description="Топ-10 лучших счетоводов", guild=MAIN_GUILD)
@app_commands.guild_only()
async def top(interaction: discord.Interaction):
    items = ranking()[:10]
    if not items:
        await interaction.response.send_message("📭 Пока никто не набрал баллов.", ephemeral=True)
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, st) in enumerate(items):
        prefix = medals[i] if i < 3 else f"`#{i + 1}`"
        member = interaction.guild.get_member(uid) if interaction.guild else None
        name = discord.utils.escape_markdown(member.display_name) if member else f"<@{uid}>"
        lines.append(
            f"{prefix} **{name}** — `{st.get('score', 0)}` баллов "
            f"(стрик: {st.get('best_streak', 0)})"
        )

    embed = discord.Embed(
        title="🏆 Топ-10 счетоводов",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="update_top_role", description="Пересчитать роль топ-1 вручную", guild=MAIN_GUILD)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def update_top_role(interaction: discord.Interaction):
    global _top1_applied

    if not TOP1_ROLE_ENABLED:
        await interaction.response.send_message(
            "⚠️ Авто-выдача роли топ-1 отключена (TOP1_ROLE_ENABLED = False).",
            ephemeral=True,
        )
        return

    role = find_role(interaction.guild, TOP1_ROLE_ID, TOP1_ROLE_NAME)
    if role is None:
        await interaction.response.send_message(
            f"⚠️ Роль **{TOP1_ROLE_NAME}** не найдена на сервере. "
            f"Создайте её или измените TOP1_ROLE_NAME / TOP1_ROLE_ID в настройках.",
            ephemeral=True,
        )
        return

    # Пересчёт ролей может занять больше 3 секунд — сначала подтверждаем получение команды.
    await interaction.response.defer(ephemeral=True)
    _top1_applied = get_top1_user_id()
    await update_top1_role(interaction.guild)

    if _top1_applied is None:
        text = "🔄 Роль топ-1 пересчитана. Лидера пока нет — роль снята со всех."
    else:
        text = f"🔄 Роль топ-1 пересчитана. Лидер: <@{_top1_applied}>"
    await interaction.followup.send(text, ephemeral=True)


# ============================================================
#                    ЗАПУСК
# ============================================================

if __name__ == "__main__":
    discord.utils.setup_logging()  # логи с временными метками

    if not TOKEN:
        log.critical("Переменная окружения BOT_TOKEN не задана.")
        sys.exit(1)

    migrate_legacy_state()
    load_state()

    try:
        # Состояние сохраняется в CountingBot.close() — и при SIGTERM, и при Ctrl+C.
        bot.run(TOKEN, log_handler=None)  # логирование уже настроено выше
    except discord.LoginFailure:
        log.critical("Неверный токен бота.")
        sys.exit(1)
