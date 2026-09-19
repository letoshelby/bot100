import discord
from discord import app_commands
from discord.ext import commands
import random
import os
import json
import asyncio
import signal
import shutil
import time
import tempfile
import sys

# === ПРИНУДИТЕЛЬНЫЙ ВЫВОД В STDOUT (для Bothost) ===
sys.stdout.reconfigure(line_buffering=True)

# === НАСТРОЙКИ ===
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = 1547463400919928832        # ID канала #счет
LOG_CHANNEL_ID = 1548649643275980840    # ID канала для логов
ROLE_NAME = "Счетовод"                  # Роль за победу/рубеж
TOP1_ROLE_NAME = "Лучший счетовод"      # Роль за 1-е место в топе
TOP1_ROLE_ENABLED = True                # Включить авто-выдачу роли топ-1
TARGET_COUNT = 100                      # До скольких считаем (классический режим и рубеж)
STATE_FILE = "state.json"               # Файл для сохранения состояния
MAX_MILESTONES = 50                     # Сколько последних рубежей хранить (endless)

# ID сервера для мгновенной синхронизации слэш-команд (None = глобально, до 1 часа)
GUILD_ID = None                         # например: 123456789012345678

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

# === ИНТЕНТЫ ===
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix="\u0000", intents=intents)

# === СОСТОЯНИЕ СЧЁТА ===
current_count = 0
last_user_id = None
participants = set()
milestone_participants = set()
mode = MODE_CLASSIC
milestones_reached = set()

# === СТАТИСТИКА ===
player_stats = {}
cur_streak = 0

# === ДЛЯ ЛОГИРОВАНИЯ ===
_last_state = {}
_load_error = None


# ============================================================
#                    ХЕЛПЕРЫ
# ============================================================

def is_admin(member: discord.Member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return member.guild_permissions.administrator


def get_stats(user_id: int) -> dict:
    key = str(user_id)
    if key not in player_stats:
        player_stats[key] = {"score": 0, "best_streak": 0, "broken": 0, "games": 0}
    return player_stats[key]


async def safe_add_reaction(message: discord.Message, emoji: str):
    try:
        await message.add_reaction(emoji)
    except (discord.Forbidden, discord.NotFound):
        pass


async def log_to_channel(text: str):
    """Отправляет сообщение в канал логов. Игнорирует ошибки."""
    try:
        channel = bot.get_channel(LOG_CHANNEL_ID)
        if channel is None:
            print(f"⚠️ Канал логов {LOG_CHANNEL_ID} не найден.")
            return
        await channel.send(text)
    except Exception as e:
        print(f"⚠️ Не удалось отправить лог в канал: {e}")


# ============================================================
#                    СОХРАНЕНИЕ / ЗАГРУЗКА
# ============================================================

def save_state():
    global _last_state

    data = {
        "current_count": current_count,
        "last_user_id": last_user_id,
        "participants": list(participants),
        "milestone_participants": list(milestone_participants),
        "mode": mode,
        "milestones_reached": list(milestones_reached),
        "player_stats": player_stats,
        "cur_streak": cur_streak,
    }

    changed = []
    for key in ("current_count", "mode", "cur_streak"):
        old = _last_state.get(key)
        new = data[key]
        if old != new:
            changed.append(f"{key}: {old} → {new}")
        _last_state[key] = new
    for key in ("participants", "milestone_participants", "milestones_reached"):
        old_len = _last_state.get(key + "_len")
        new_len = len(data[key])
        if old_len != new_len:
            changed.append(f"{key}: {old_len} → {new_len}")
        _last_state[key + "_len"] = new_len
    if changed:
        print(f"💾 save_state: {' | '.join(changed)}")

    dir_ = os.path.dirname(os.path.abspath(STATE_FILE)) or "."
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix="state_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, STATE_FILE)
        tmp_path = None
    except Exception as e:
        print(f"⚠️ Не удалось сохранить состояние: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


async def save_state_async():
    await asyncio.to_thread(save_state)


def load_state():
    global current_count, last_user_id, participants, milestone_participants, mode, milestones_reached, player_stats, cur_streak, _load_error

    if not os.path.exists(STATE_FILE):
        print("ℹ️ state.json не найден — стартуем с дефолтными значениями.")
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        current_count = data.get("current_count", 0)
        last_user_id = data.get("last_user_id")
        participants = set(data.get("participants", []))
        milestone_participants = set(data.get("milestone_participants", []))
        mode = data.get("mode", MODE_CLASSIC)
        milestones_reached = set(data.get("milestones_reached", []))
        player_stats = data.get("player_stats", {})
        cur_streak = data.get("cur_streak", 0)

        print(
            f"💾 state.json загружен: счёт={current_count}, режим={mode}, "
            f"рубежей={len(milestones_reached)}, игроков={len(player_stats)}, "
            f"участников={len(participants)}, отрезок={len(milestone_participants)}, "
            f"стрик={cur_streak}"
        )

        if mode == MODE_CLASSIC and current_count > 150:
            print(
                f"⚠️ ВНИМАНИЕ: режим classic, но счёт = {current_count}. "
                f"Возможно, режим должен быть endless/endless_soft."
            )

        if current_count == 0 and not milestones_reached and not participants and not player_stats:
            print("⚠️ ВНИМАНИЕ: state.json полностью пустой.")

    except Exception as e:
        print(f"❌ КРИТИЧНО: не удалось загрузить state.json: {e}")
        print("⚠️ Бот стартует с ДЕФОЛТНЫМИ значениями (mode=classic, счёт=0)!")
        _load_error = (
            f"❌ **КРИТИЧНО: не удалось загрузить state.json**\n"
            f"Ошибка: `{e}`\n"
            f"Бот стартует с дефолтными значениями (mode=classic, счёт=0)."
        )
        try:
            backup = f"state.broken.{int(time.time())}.json"
            shutil.copy(STATE_FILE, backup)
            print(f"💾 Битый файл сохранён как {backup}")
        except Exception as e2:
            print(f"⚠️ Не удалось сохранить бэкап: {e2}")


def reset_state():
    global current_count, last_user_id, participants, milestone_participants, milestones_reached, cur_streak
    current_count = 0
    last_user_id = None
    participants.clear()
    milestone_participants.clear()
    milestones_reached.clear()
    cur_streak = 0
    save_state()


async def reset_state_async():
    global current_count, last_user_id, participants, milestone_participants, milestones_reached, cur_streak
    current_count = 0
    last_user_id = None
    participants.clear()
    milestone_participants.clear()
    milestones_reached.clear()
    cur_streak = 0
    await save_state_async()


# ============================================================
#                    РОЛЬ ТОП-1
# ============================================================

def get_top1_user_id():
    if not player_stats:
        return None
    sorted_players = sorted(
        player_stats.items(),
        key=lambda kv: (kv[1].get("score", 0), kv[1].get("best_streak", 0), -int(kv[0])),
        reverse=True,
    )
    top_id, top_st = sorted_players[0]
    if top_st.get("score", 0) <= 0:
        return None
    return int(top_id)


async def update_top1_role(guild: discord.Guild):
    if not TOP1_ROLE_ENABLED:
        return
    if guild is None:
        return

    role = discord.utils.get(guild.roles, name=TOP1_ROLE_NAME)
    if role is None:
        return

    top_id = get_top1_user_id()
    current_holders = [m for m in role.members]

    if top_id is None:
        for m in current_holders:
            try:
                await m.remove_roles(role, reason="Топ-1: статистика пуста")
            except discord.Forbidden:
                pass
        return

    if len(current_holders) == 1 and current_holders[0].id == top_id:
        return

    new_leader = guild.get_member(top_id)
    if new_leader is None:
        for m in current_holders:
            try:
                await m.remove_roles(role, reason="Топ-1: лидер покинул сервер")
            except discord.Forbidden:
                pass
        return

    for m in current_holders:
        if m.id != top_id:
            try:
                await m.remove_roles(role, reason="Топ-1: потерял 1-е место")
            except discord.Forbidden:
                pass

    if role not in new_leader.roles:
        try:
            await new_leader.add_roles(role, reason="Топ-1: вышел на 1-е место")
        except discord.Forbidden:
            pass


# ============================================================
#                    СОБЫТИЯ
# ============================================================

@bot.event
async def on_ready():
    print(f"✅ Бот {bot.user} запущен!")
    print(f"📋 Канал для счёта: {CHANNEL_ID}")
    print(f"📝 Канал логов: {LOG_CHANNEL_ID}")
    print(f"🎮 Режим: {MODE_NAMES.get(mode, mode)}")
    print(f"🎯 Рубеж (TARGET_COUNT): {TARGET_COUNT}")
    print(f"👑 Админы могут считать: {ADMINS_CAN_COUNT}")
    print(f"🏆 Роль топ-1: {TOP1_ROLE_NAME if TOP1_ROLE_ENABLED else 'отключена'}")

    for guild in bot.guilds:
        try:
            await update_top1_role(guild)
        except Exception as e:
            print(f"⚠️ Ошибка синхронизации роли топ-1: {e}")

    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"🔁 Синхронизировано {len(synced)} слэш-команд на сервере {GUILD_ID}")
        else:
            synced = await bot.tree.sync()
            print(f"🔁 Синхронизировано {len(synced)} слэш-команд глобально")
    except Exception as e:
        print(f"⚠️ Ошибка синхронизации команд: {e}")

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
async def on_message(message):
    if message.author.bot:
        return

    await handle_count_message(message)
    await bot.process_commands(message)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    """Реакция на редактирование сообщения в канале счёта."""
    if after.author.bot:
        return
    if after.channel.id != CHANNEL_ID:
        return
    if before.content == after.content:
        return

    author_is_admin = is_admin(after.author)
    if author_is_admin and not ADMINS_CAN_COUNT:
        return

    try:
        await after.delete()
    except (discord.Forbidden, discord.NotFound):
        pass

    warning = await after.channel.send(
        f"{after.author.mention}, редактировать сообщения в канале счёта нельзя! "
        f"Сообщение удалено."
    )
    await warning.delete(delay=5)


# ============================================================
#                    ОСНОВНАЯ ЛОГИКА СЧЁТА
# ============================================================

async def handle_count_message(message):
    if message.channel.id != CHANNEL_ID:
        return

    global current_count, last_user_id, participants, milestone_participants, cur_streak

    content = message.content.strip()
    author_is_admin = is_admin(message.author)

    if author_is_admin and not ADMINS_CAN_COUNT:
        return

    if not content.isdigit():
        if author_is_admin:
            return
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass
        warning = await message.channel.send(
            f"{message.author.mention}, здесь нужно писать **только числа**! "
            f"Сообщение удалено."
        )
        await warning.delete(delay=5)
        return

    number = int(content)

    if message.author.id == last_user_id:
        if author_is_admin:
            return
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass
        warning = await message.channel.send(
            f"{message.author.mention}, нельзя считать дважды подряд! "
            f"Дождись другого участника."
        )
        await warning.delete(delay=5)
        return

    expected = current_count + 1

    # ============ ПРАВИЛЬНОЕ ЧИСЛО ============
    if number == expected:
        current_count = number
        last_user_id = message.author.id
        participants.add(message.author.id)
        milestone_participants.add(message.author.id)

        st = get_stats(message.author.id)
        st["score"] += 1
        cur_streak += 1
        if cur_streak > st["best_streak"]:
            st["best_streak"] = cur_streak

        await save_state_async()

        if TOP1_ROLE_ENABLED and message.guild:
            try:
                await update_top1_role(message.guild)
            except Exception as e:
                print(f"⚠️ Ошибка обновления роли топ-1: {e}")

        await safe_add_reaction(message, "✅")

        if mode == MODE_CLASSIC:
            if current_count == TARGET_COUNT:
                await handle_classic_success(message)
                return
        elif mode in (MODE_ENDLESS, MODE_ENDLESS_SOFT):
            if current_count % TARGET_COUNT == 0:
                milestone = current_count
                if milestone not in milestones_reached:
                    milestones_reached.add(milestone)
                    while len(milestones_reached) > MAX_MILESTONES:
                        milestones_reached.discard(min(milestones_reached))
                    await save_state_async()
                    await handle_endless_milestone(message, milestone)

    # ============ ОШИБКА ============
    else:
        broken_at = current_count

        await log_to_channel(
            f"❌ **Ошибка в счёте**\n"
            f"👤 Кто: {message.author.mention} (`{message.author.id}`)\n"
            f"✍️ Написал: **{number}**\n"
            f"🎯 Ждали: **{expected}**\n"
            f"🎮 Режим: **{MODE_NAMES.get(mode, mode)}**\n"
            f"🔢 Счёт до ошибки: **{broken_at}**\n"
            f"🔥 Стрик: **{cur_streak}**"
        )

        if not author_is_admin:
            get_stats(message.author.id)["broken"] += 1
        cur_streak = 0

        if mode == MODE_CLASSIC:
            current_count = 0
            last_user_id = None
            participants.clear()
            milestone_participants.clear()
            milestones_reached.clear()
            await save_state_async()

            if not author_is_admin:
                await safe_add_reaction(message, "❌")

            fail_phrases = [
                "испортил счёт на",
                "всё сломал на",
                "обнулил цепочку на",
                "заруинил счёт на",
                "уронил счёт на",
            ]

            mention = "" if author_is_admin else f"{message.author.mention} "
            fail_msg = await message.channel.send(
                f"💥 {mention}"
                f"**{random.choice(fail_phrases)} {broken_at}!!**\n"
                f"Следующее число — **1**. Игра начинается сначала."
            )
            await safe_add_reaction(fail_msg, "😡")

        elif mode == MODE_ENDLESS:
            if not author_is_admin:
                await safe_add_reaction(message, "❌")

            mention = "" if author_is_admin else f"{message.author.mention}, "
            fail_msg = await message.channel.send(
                f"⚠️ {mention}сейчас ждали **{expected}**, а не **{number}**.\n"
                f"В бесконечном режиме счёт **не сбрасывается**. "
                f"Следующее число — **{expected}**."
            )
            await safe_add_reaction(fail_msg, "🤔")

        elif mode == MODE_ENDLESS_SOFT:
            rollback_to = (broken_at // TARGET_COUNT) * TARGET_COUNT
            lost = broken_at - rollback_to

            current_count = rollback_to
            last_user_id = None
            milestone_participants.clear()
            await save_state_async()

            if not author_is_admin:
                await safe_add_reaction(message, "💥")

            if rollback_to == 0:
                rollback_text = "**0** (рубежей ещё не было)"
            else:
                rollback_text = f"**{rollback_to}** (последний рубеж)"

            mention = "" if author_is_admin else f"{message.author.mention} "
            fail_msg = await message.channel.send(
                f"💥 {mention}ошибся на **{broken_at}**!\n"
                f"🔽 Откат до {rollback_text}. Потеряно чисел: **{lost}**.\n"
                f"Следующее число — **{current_count + 1}**."
            )
            await safe_add_reaction(fail_msg, "😤")

        return


# ============================================================
#                    ВЫДАЧА РОЛИ
# ============================================================

async def give_role_to_participants(guild, user_ids, reason: str):
    role = discord.utils.get(guild.roles, name=ROLE_NAME)
    if role is None:
        return None, []

    success_count = 0
    failed = []

    for user_id in list(user_ids):
        member = guild.get_member(user_id)
        if member is None:
            failed.append(f"<@{user_id}>")
            continue
        if role in member.roles:
            continue
        try:
            await member.add_roles(role, reason=reason)
            success_count += 1
        except discord.Forbidden:
            failed.append(member.mention)

    return success_count, failed


# ============================================================
#                    ПОБЕДА / РУБЕЖИ
# ============================================================

async def handle_classic_success(message):
    global current_count, last_user_id, participants

    guild = message.guild
    winners = list(participants)
    mentions = " ".join(f"<@{uid}>" for uid in winners) if winners else "—"

    try:
        result = await give_role_to_participants(
            guild, participants, "Досчитали до 100 (classic)"
        )

        if result[0] is None:
            await message.channel.send(
                f"⚠️ Роль **{ROLE_NAME}** не найдена на сервере."
            )
            return

        success_count, failed = result

        victory_phrases = [
            "🎉 **НЕВЕРОЯТНО!**",
            "🏆 **ЛЕГЕНДАРНО!**",
            "🔥 **ЭТО ПОБЕДА!**",
            "💎 **ИДЕАЛЬНАЯ ЦЕПОЧКА!**",
            "⚡ **МОЛНИЕНОСНО!**",
            "🌟 **ВЫ СДЕЛАЛИ ЭТО!**",
        ]

        victory_msg = await message.channel.send(
            f"{random.choice(victory_phrases)}\n\n"
            f"🎯 Цепочка **1 → {TARGET_COUNT}** пройдена!\n\n"
            f"🏆 **Герои этой игры:**\n{mentions}\n\n"
            f"👥 Участников: **{len(winners)}**\n"
            f"🏅 Роль `{ROLE_NAME}` выдана: **{success_count}**"
            + (f"\n⚠️ Не удалось выдать: {', '.join(failed)}" if failed else "")
            + f"\n\n🔄 **Новая игра начинается — следующее число 1!**"
        )

        try:
            await victory_msg.pin()
        except (discord.Forbidden, discord.HTTPException):
            pass

        await safe_add_reaction(victory_msg, "🎉")
        await safe_add_reaction(victory_msg, "🏆")
        await safe_add_reaction(victory_msg, "🥳")

        for uid in winners:
            get_stats(uid)["games"] += 1

        await log_to_channel(
            f"🏆 **ПОБЕДА в classic!**\n"
            f"🎯 Цепочка 1 → {TARGET_COUNT} пройдена\n"
            f"👥 Участников: **{len(winners)}**\n"
            f"🏅 Роль выдана: **{success_count}**"
            + (f"\n⚠️ Не удалось выдать: {len(failed)}" if failed else "")
        )

    except Exception as e:
        print(f"⚠️ Ошибка при обработке победы: {e}")

    finally:
        await reset_state_async()


async def handle_endless_milestone(message, milestone):
    guild = message.guild

    segment = list(milestone_participants)
    mentions = " ".join(f"<@{uid}>" for uid in segment) if segment else "—"

    result = await give_role_to_participants(
        guild, segment, f"Достигли рубежа {milestone}"
    )

    if result[0] is None:
        await message.channel.send(
            f"🎯 Рубеж **{milestone}** достигнут! "
            f"(Роль **{ROLE_NAME}** не найдена, но это не важно — продолжаем!)"
        )
        milestone_participants.clear()
        await save_state_async()
        return

    success_count, failed = result

    phrases = [
        "🎉 **НОВЫЙ РУБЕЖ!**",
        "🏆 **ОТЛИЧНО!**",
        "🔥 **ТАК ДЕРЖАТЬ!**",
        "💎 **КРУТО!**",
        "⚡ **МОЩНО!**",
        "🌟 **ПРОДОЛЖАЕМ!**",
    ]

    mode_hint = ""
    if mode == MODE_ENDLESS_SOFT:
        mode_hint = "\n⚠️ *Ошибка откатит счёт к этому рубежу.*"

    msg = await message.channel.send(
        f"{random.choice(phrases)}\n\n"
        f"🎯 Достигнут рубеж **{milestone}**!\n\n"
        f"🏆 **Участники последнего отрезка:**\n{mentions}\n\n"
        f"👥 Участников: **{len(participants)}**\n"
        f"\n♾️ **Счёт продолжается — следующее число {milestone + 1}!**"
        + mode_hint
    )

    await safe_add_reaction(msg, "🎉")
    await safe_add_reaction(msg, "🏆")

    await log_to_channel(
        f"🎯 **Достигнут рубеж {milestone}**\n"
        f"🎮 Режим: **{MODE_NAMES.get(mode, mode)}**\n"
        f"👥 В отрезке: **{len(segment)}**\n"
        f"👥 Всего в забеге: **{len(participants)}**\n"
        f"🏅 Роль выдана: **{success_count}**"
        + (f"\n⚠️ Не удалось выдать: {len(failed)}" if failed else "")
    )

    milestone_participants.clear()
    await save_state_async()


# ============================================================
#                    СЛЭШ-КОМАНДЫ
# ============================================================

@bot.tree.command(name="change_mode", description="Сменить режим счёта")
@app_commands.describe(
    new_mode="Выберите режим",
    reset="Сбросить текущий счёт при смене режима?"
)
@app_commands.choices(new_mode=[
    app_commands.Choice(name="Классический (сброс при ошибке, победа на 100)", value=MODE_CLASSIC),
    app_commands.Choice(name="Бесконечный (ошибка не сбрасывает)", value=MODE_ENDLESS),
    app_commands.Choice(name="Бесконечный мягкий (откат к последнему рубежу)", value=MODE_ENDLESS_SOFT),
])
@app_commands.default_permissions(administrator=True)
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
        await reset_state_async()
    else:
        await save_state_async()

    await log_to_channel(
        f"⚙️ **Смена режима**\n"
        f"👤 Кто: {interaction.user.mention} (`{interaction.user.id}`)\n"
        f"🔁 Было: **{MODE_NAMES[old_mode]}**\n"
        f"➡️ Стало: **{MODE_NAMES[mode]}**\n"
        f"🔄 Сброс счёта: **{'да' if reset else 'нет'}**\n"
        f"📊 Текущий счёт: **{current_count}**"
    )

    if new_mode.value == old_mode:
        await interaction.response.send_message(
            f"🔄 Счёт сброшен. Режим остался прежним: **{MODE_NAMES[mode]}**\n"
            f"Текущий счёт: **{current_count}**",
            ephemeral=False,
        )
    else:
        await interaction.response.send_message(
            f"✅ Режим изменён: **{MODE_NAMES[old_mode]}** → **{MODE_NAMES[mode]}**\n"
            f"Текущий счёт: **{current_count}**"
            + (f"\n🔄 Счёт сброшен." if reset else ""),
            ephemeral=False,
        )


@bot.tree.command(name="reset_count", description="Сбросить счёт вручную")
@app_commands.default_permissions(administrator=True)
async def reset_count(interaction: discord.Interaction):
    old_count = current_count
    await reset_state_async()
    await interaction.response.send_message(
        "🔄 Счёт сброшен вручную. Начинаем с **1**.",
        ephemeral=False,
    )
    await log_to_channel(
        f"🛠 **Ручной сброс счёта**\n"
        f"👤 Кто: {interaction.user.mention} (`{interaction.user.id}`)\n"
        f"🔢 Было: **{old_count}** → **0**"
    )


@bot.tree.command(name="set_count", description="Установить текущее число вручную")
@app_commands.describe(number="Число, с которого продолжится счёт")
@app_commands.default_permissions(administrator=True)
async def set_count(interaction: discord.Interaction, number: int):
    global current_count, last_user_id
    old_count = current_count
    current_count = number
    last_user_id = None
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


@bot.tree.command(name="count_status", description="Показать текущий статус счёта")
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


@bot.tree.command(name="stats", description="Показать статистику игрока")
@app_commands.describe(user="Чей профиль показать (по умолчанию — свой)")
async def stats(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user

    if target.bot:
        await interaction.response.send_message(
            "🤖 У ботов нет статистики.", ephemeral=True
        )
        return

    st = player_stats.get(str(target.id))
    if st is None or st.get("score", 0) == 0:
        await interaction.response.send_message(
            f"📭 У {target.mention} пока нет ни одного засчитанного числа.",
            ephemeral=True,
        )
        return

    sorted_ids = sorted(
        player_stats.items(),
        key=lambda kv: kv[1].get("score", 0),
        reverse=True,
    )
    rank = next(
        (i + 1 for i, (uid, _) in enumerate(sorted_ids) if uid == str(target.id)),
        None,
    )

    total_score = st.get("score", 0)
    broken = st.get("broken", 0)
    games = st.get("games", 0)
    best = st.get("best_streak", 0)

    accuracy = (
        total_score / (total_score + broken) * 100 if (total_score + broken) else 0
    )

    embed = discord.Embed(
        title=f"📊 Статистика — {target.display_name}",
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="🏅 Баллы", value=f"**{total_score}**", inline=True)
    embed.add_field(
        name="🏆 Место в топе",
        value=f"**#{rank}**" if rank else "—",
        inline=True,
    )
    embed.add_field(name="🔥 Лучший стрик", value=f"**{best}**", inline=True)
    embed.add_field(name="💥 Сломал цепочек", value=f"**{broken}**", inline=True)
    embed.add_field(name="🎮 Победных игр", value=f"**{games}**", inline=True)
    embed.add_field(name="🎯 Точность", value=f"**{accuracy:.1f}%**", inline=True)
    embed.set_footer(text=f"ID: {target.id}")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="top", description="Топ-10 лучших счетоводов")
async def top(interaction: discord.Interaction):
    if not player_stats:
        await interaction.response.send_message(
            "📭 Пока никто не набрал баллов.", ephemeral=True
        )
        return

    sorted_players = sorted(
        player_stats.items(),
        key=lambda kv: (kv[1].get("score", 0), kv[1].get("best_streak", 0)),
        reverse=True,
    )[:10]

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, st) in enumerate(sorted_players):
        prefix = medals[i] if i < 3 else f"`#{i + 1}`"
        member = interaction.guild.get_member(int(uid)) if interaction.guild else None
        name = member.display_name if member else f"<@{uid}>"
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


@bot.tree.command(name="update_top_role", description="Пересчитать роль топ-1 вручную")
@app_commands.default_permissions(administrator=True)
async def update_top_role(interaction: discord.Interaction):
    if not TOP1_ROLE_ENABLED:
        await interaction.response.send_message(
            "⚠️ Авто-выдача роли топ-1 отключена (TOP1_ROLE_ENABLED = False).",
            ephemeral=True,
        )
        return

    role = discord.utils.get(interaction.guild.roles, name=TOP1_ROLE_NAME)
    if role is None:
        await interaction.response.send_message(
            f"⚠️ Роль **{TOP1_ROLE_NAME}** не найдена на сервере. "
            f"Создайте её или измените TOP1_ROLE_NAME в настройках.",
            ephemeral=True,
        )
        return

    await update_top1_role(interaction.guild)

    top_id = get_top1_user_id()
    if top_id is None:
        await interaction.response.send_message(
            "🔄 Роль топ-1 пересчитана. Лидера пока нет — роль снята со всех.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"🔄 Роль топ-1 пересчитана. Лидер: <@{top_id}>",
            ephemeral=True,
        )


# ============================================================
#                    GRACEFUL SHUTDOWN
# ============================================================

async def shutdown():
    print("\n🛑 Завершение работы. Сохраняю состояние...")
    try:
        save_state()
        print("💾 Состояние сохранено.")
    except Exception as e:
        print(f"⚠️ Не удалось сохранить состояние: {e}")

    try:
        await bot.close()
        print("🔌 Соединение с Discord закрыто.")
    except Exception as e:
        print(f"⚠️ Ошибка при закрытии: {e}")


def handle_signal(sig, frame):
    name = sig.name if hasattr(sig, "name") else str(sig)
    print(f"\n📴 Получен сигнал {name}")
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(shutdown())
    except RuntimeError:
        save_state()


# ============================================================
#                    ЗАПУСК
# ============================================================

if __name__ == "__main__":
    load_state()

    try:
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
    except (ValueError, AttributeError):
        pass

    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        print("\n🛑 Прервано пользователем. Сохраняю состояние...")
        save_state()
        print("💾 Состояние сохранено.")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        save_state()
