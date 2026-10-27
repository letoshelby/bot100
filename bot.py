import json
import logging
import os
import random
import re
import shutil
import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("countbot")

# === НАСТРОЙКИ ===
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = 1547463400919928832   # ID канала #счет
ROLE_NAME = "Счетовод"             # Название роли для выдачи (если ROLE_ID не задан)
ROLE_ID = int(os.getenv("ROLE_ID", "0") or 0)  # Надёжнее, чем имя: задайте через переменную окружения
TARGET_COUNT = 100                 # До скольких считаем (классический режим и рубеж)

# Состояние хранится в папке data/ рядом со скриптом: она не затрагивается обновлением из git.
# При необходимости пути можно переопределить переменными окружения DATA_DIR / STATE_FILE.
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
STATE_FILE = Path(os.getenv("STATE_FILE", DATA_DIR / "state.json"))
LEGACY_STATE_FILE = BASE_DIR / "state.json"  # старое расположение (рядом с bot.py)

# === ЗАЩИТА СЕРВЕРА ===
MAIN_GUILD_ID = 885532750822641675  # ID твоего сервера
MAIN_GUILD = discord.Object(id=MAIN_GUILD_ID)

# Поставьте True на ОДИН запуск после обновления: удалит у Discord старые
# глобальные команды (из-за них команды были видны на чужих серверах).
# Потом верните False.
CLEAR_OLD_GLOBAL_COMMANDS = True

# могут ли админы участвовать в счёте?
#   True  — админ считается как игрок, но бот НЕ удаляет его сообщения и НЕ ругает
#   False — бот полностью игнорирует сообщения админов в канале счёта
ADMINS_CAN_COUNT = True

# === РЕЖИМЫ ===
MODE_CLASSIC = "classic"                # сброс при ошибке, победа на 100
MODE_ENDLESS = "endless"                # без сброса, счёт до бесконечности
MODE_ENDLESS_SOFT = "endless_soft"      # откат к последнему рубежу при ошибке

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


bot = CountingBot()

# === СОСТОЯНИЕ СЧЁТА ===
# ВАЖНО: все изменения состояния делаются в синхронных функциях (без await внутри),
# поэтому в asyncio они атомарны и блокировка не нужна. Долгие операции (роли, сообщения)
# работают только с копиями данных, снятыми заранее.
current_count = 0
last_user_id = None
participants = set()
mode = MODE_CLASSIC
milestones_reached = set()


# ============================================================
#                    ХЕЛПЕРЫ
# ============================================================

def is_admin(member) -> bool:
    """Проверяет, есть ли у участника права администратора."""
    if not isinstance(member, discord.Member):
        return False
    return member.guild_permissions.administrator


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


def format_mentions(user_ids, limit: int) -> str:
    ids = list(user_ids)
    text = " ".join(f"<@{uid}>" for uid in ids[:limit])
    if len(ids) > limit:
        text += f" … и ещё {len(ids) - limit}"
    return text or "—"


# ============================================================
#                    СОХРАНЕНИЕ / ЗАГРУЗКА
# ============================================================

def save_state():
    data = {
        "current_count": current_count,
        "last_user_id": last_user_id,
        "participants": sorted(participants),
        "mode": mode,
        "milestones_reached": sorted(milestones_reached),
    }
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Пишем во временный файл и атомарно подменяем: сбой посреди записи не портит state.json.
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception:
        log.exception("Не удалось сохранить состояние")


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


def load_state():
    global current_count, last_user_id, participants, mode, milestones_reached
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        new_count = max(0, int(data.get("current_count", 0)))
        raw_last = data.get("last_user_id")
        new_last = int(raw_last) if raw_last is not None else None
        new_participants = {int(x) for x in data.get("participants", [])}
        new_milestones = {int(x) for x in data.get("milestones_reached", [])}
        new_mode = data.get("mode", MODE_CLASSIC)
        if new_mode not in MODE_NAMES:
            log.warning("Неизвестный режим %r в файле состояния, ставлю классический", new_mode)
            new_mode = MODE_CLASSIC
    except Exception:
        log.exception("Не удалось загрузить состояние, файл сохранён как .corrupt")
        try:
            os.replace(STATE_FILE, STATE_FILE.with_name(STATE_FILE.name + ".corrupt"))
        except OSError:
            pass
        return

    current_count, last_user_id = new_count, new_last
    participants, milestones_reached, mode = new_participants, new_milestones, new_mode


def reset_state():
    global current_count, last_user_id
    current_count = 0
    last_user_id = None
    participants.clear()
    milestones_reached.clear()
    save_state()


# ============================================================
#                    ЛОГИКА СЧЁТА (синхронная, атомарная)
# ============================================================

def apply_count(author_id: int, number: int) -> dict:
    """Применяет число к состоянию и возвращает описание исхода.

    НЕ добавляйте сюда await и не делайте await перед вызовом этой функции
    в on_message: иначе порядок обработки сообщений может нарушиться.
    """
    global current_count, last_user_id

    if author_id == last_user_id:
        return {"kind": "double"}

    expected = current_count + 1

    # ---- Правильное число ----
    if number == expected:
        current_count = number
        last_user_id = author_id
        participants.add(author_id)
        result = {"kind": "ok", "win": False, "winners": [], "milestone": None, "milestone_users": []}

        if mode == MODE_CLASSIC:
            if current_count >= TARGET_COUNT:
                result["win"] = True
                result["winners"] = sorted(participants)
                # Игра сразу начинается заново — пока выдаются роли, счёт уже идёт с 1.
                current_count = 0
                last_user_id = None
                participants.clear()
                milestones_reached.clear()
        elif current_count % TARGET_COUNT == 0 and current_count not in milestones_reached:
            milestones_reached.add(current_count)
            result["milestone"] = current_count
            result["milestone_users"] = sorted(participants)

        save_state()
        return result

    # ---- Ошибка ----
    broken_at = current_count
    result = {
        "kind": "wrong",
        "mode": mode,
        "number": number,
        "expected": expected,
        "broken_at": broken_at,
        "rollback_to": None,
    }

    if mode == MODE_CLASSIC:
        current_count = 0
        last_user_id = None
        participants.clear()
        milestones_reached.clear()
    elif mode == MODE_ENDLESS_SOFT:
        rollback_to = (broken_at // TARGET_COUNT) * TARGET_COUNT
        result["rollback_to"] = rollback_to
        current_count = rollback_to
        last_user_id = None
    # MODE_ENDLESS: состояние не меняется

    save_state()
    return result


# ============================================================
#                    ВЫДАЧА РОЛИ
# ============================================================

def find_role(guild: discord.Guild):
    if ROLE_ID:
        return guild.get_role(ROLE_ID)
    return discord.utils.get(guild.roles, name=ROLE_NAME)


async def give_role_to_users(guild: discord.Guild, user_ids, reason: str):
    """Возвращает (role, выдано, [id не получивших]). role=None, если роль не найдена."""
    role = find_role(guild)
    if role is None:
        return None, 0, []

    given = 0
    failed = []
    for uid in user_ids:
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.HTTPException:  # вышел с сервера и т.п.
                failed.append(uid)
                continue
        if role in member.roles:
            given += 1
            continue
        try:
            await member.add_roles(role, reason=reason)
            given += 1
        except discord.HTTPException:  # Forbidden (иерархия/права) и прочие ошибки API
            failed.append(uid)
    return role, given, failed


def roles_report(role, given: int, failed) -> str:
    if role is None:
        return "⚠️ Роль для награды не найдена на сервере — проверьте ROLE_NAME / ROLE_ID."
    text = f"🏅 Роль `{role.name}` выдана: **{given}**"
    if failed:
        text += f"\n⚠️ Не удалось выдать: {format_mentions(failed, MAX_MENTIONS_FAILED)}"
    return text


# ============================================================
#                    ПОБЕДА / РУБЕЖИ
# ============================================================

async def announce_victory(channel, guild: discord.Guild, winners):
    # Состояние уже сброшено в apply_count, поэтому любая ошибка здесь не «замораживает» игру.
    try:
        role, given, failed = await give_role_to_users(
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
    except Exception:
        log.exception("Ошибка при объявлении победы")


async def announce_milestone(channel, guild: discord.Guild, milestone: int, users, current_mode: str):
    try:
        role, given, failed = await give_role_to_users(
            guild, users, f"Достигли рубежа {milestone} (endless)"
        )

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
            f"🏆 **Участники цепочки:**\n{format_mentions(users, MAX_MENTIONS_WINNERS)}\n\n"
            f"👥 Участников: **{len(users)}**\n"
            f"{roles_report(role, given, failed)}\n\n"
            f"♾️ **Счёт продолжается — следующее число {milestone + 1}!**"
            f"{mode_hint}"
        )

        for emoji in ("🎉", "🏆"):
            await safe_react(msg, emoji)
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
    # on_ready может вызываться несколько раз (переподключения), поэтому здесь только лёгкие действия.
    log.info("Бот %s запущен", bot.user)
    log.info("Канал: %s | режим: %s | рубеж: %s | админы считают: %s",
             CHANNEL_ID, MODE_NAMES.get(mode, mode), TARGET_COUNT, ADMINS_CAN_COUNT)
    for guild in list(bot.guilds):
        if guild.id != MAIN_GUILD_ID:
            await leave_foreign_guild(guild)


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

    author_is_admin = is_admin(message.author)

    # Если админ, а режим "не считать" — игнорируем полностью
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

    # Между проверками выше и apply_count нет await — порядок сообщений сохраняется.
    result = apply_count(message.author.id, int(content))

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

    # === Правильное число ===
    if result["kind"] == "ok":
        await safe_react(message, "✅")
        if result["win"]:
            await announce_victory(message.channel, message.guild, result["winners"])
        elif result["milestone"] is not None:
            await announce_milestone(
                message.channel, message.guild,
                result["milestone"], result["milestone_users"], mode,
            )
        return

    # === Ошибка: число не то ===
    broken_at = result["broken_at"]
    expected = result["expected"]
    number = result["number"]

    # ---------------- CLASSIC ----------------
    if result["mode"] == MODE_CLASSIC:
        # Админу не ставим реакцию ❌, но сообщение оставляем
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
        try:
            fail_msg = await message.channel.send(
                f"💥 {mention}"
                f"**{random.choice(fail_phrases)} {broken_at}!!**\n"
                f"Следующее число — **1**. Игра начинается сначала."
            )
            await safe_react(fail_msg, "😡")
        except discord.HTTPException:
            pass

    # ---------------- ENDLESS (без сброса) ----------------
    elif result["mode"] == MODE_ENDLESS:
        if not author_is_admin:
            await safe_react(message, "❌")

        mention = "" if author_is_admin else f"{message.author.mention}, "
        try:
            fail_msg = await message.channel.send(
                f"⚠️ {mention}сейчас ждали **{expected}**, а не **{number}**.\n"
                f"В бесконечном режиме счёт **не сбрасывается**. "
                f"Следующее число — **{expected}**."
            )
            await safe_react(fail_msg, "🤔")
        except discord.HTTPException:
            pass

    # ---------------- ENDLESS_SOFT (откат к рубежу) ----------------
    elif result["mode"] == MODE_ENDLESS_SOFT:
        rollback_to = result["rollback_to"]
        lost = broken_at - rollback_to

        if not author_is_admin:
            await safe_react(message, "💥")

        if rollback_to == 0:
            rollback_text = "**0** (рубежей ещё не было)"
        else:
            rollback_text = f"**{rollback_to}** (последний рубеж)"

        mention = "" if author_is_admin else f"{message.author.mention} "
        try:
            fail_msg = await message.channel.send(
                f"💥 {mention}ошибся на **{broken_at}**!\n"
                f"🔽 Откат до {rollback_text}. Потеряно чисел: **{lost}**.\n"
                f"Следующее число — **{rollback_to + 1}**."
            )
            await safe_react(fail_msg, "😤")
        except discord.HTTPException:
            pass


# ============================================================
#                    СЛЭШ-КОМАНДЫ (только главный сервер, только админы)
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
    mode = new_mode.value

    if reset:
        reset_state()
    else:
        save_state()

    await interaction.response.send_message(
        f"✅ Режим изменён: **{MODE_NAMES.get(old_mode, old_mode)}** → **{MODE_NAMES[mode]}**\n"
        f"Текущий счёт: **{current_count}**"
        + ("\n🔄 Счёт сброшен." if reset else ""),
        ephemeral=False,
    )


@bot.tree.command(name="reset_count", description="Сбросить счёт вручную", guild=MAIN_GUILD)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def reset_count(interaction: discord.Interaction):
    reset_state()
    await interaction.response.send_message(
        "🔄 Счёт сброшен вручную. Начинаем с **1**.",
        ephemeral=False,
    )


@bot.tree.command(name="set_count", description="Установить текущее число вручную", guild=MAIN_GUILD)
@app_commands.describe(number="Число, с которого продолжится счёт")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def set_count(interaction: discord.Interaction, number: app_commands.Range[int, 0, 1_000_000]):
    global current_count, last_user_id
    current_count = number
    last_user_id = None
    # Рубежи выше нового счёта должны уметь сработать снова.
    milestones_reached.difference_update({m for m in milestones_reached if m > number})
    save_state()
    await interaction.response.send_message(
        f"✅ Счёт установлен на **{number}**. Следующее число — **{number + 1}**.",
        ephemeral=False,
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
        f"Рубеж: **{TARGET_COUNT}**\n"
        f"Следующее число: **{current_count + 1}**\n"
        f"Откат при ошибке: {rollback_hint}\n"
        f"Админы считают: **{'да' if ADMINS_CAN_COUNT else 'нет'}**",
        ephemeral=True,
    )


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
        bot.run(TOKEN, log_handler=None)  # логирование уже настроено выше
    except discord.LoginFailure:
        log.critical("Неверный токен бота.")
        sys.exit(1)
