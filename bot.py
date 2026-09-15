import discord
from discord import app_commands
from discord.ext import commands
import random
import os
import json

# === НАСТРОЙКИ ===
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = 1547463400919928832   # ID канала #счет
ROLE_NAME = "Счетовод"             # Название роли для выдачи
TARGET_COUNT = 100                 # До скольких считаем (классический режим и рубеж)
STATE_FILE = "state.json"          # Файл для сохранения состояния

# ID сервера для мгновенной синхронизации слэш-команд (None = глобально, до 1 часа)
GUILD_ID = None                    # например: 123456789012345678

# ← НОВОЕ: могут ли админы участвовать в счёте?
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

# === ИНТЕНТЫ ===
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.reactions = True

# Префиксные команды не используются — фиктивный префикс
bot = commands.Bot(command_prefix="\u0000", intents=intents)

# === СОСТОЯНИЕ СЧЁТА ===
current_count = 0
last_user_id = None
participants = set()
mode = MODE_CLASSIC
milestones_reached = set()


# ============================================================
#                    ХЕЛПЕР: АДМИН?
# ============================================================

def is_admin(member: discord.Member) -> bool:
    """Проверяет, есть ли у участника права администратора."""
    if not isinstance(member, discord.Member):
        return False
    return member.guild_permissions.administrator


# ============================================================
#                    СОХРАНЕНИЕ / ЗАГРУЗКА
# ============================================================

def save_state():
    data = {
        "current_count": current_count,
        "last_user_id": last_user_id,
        "participants": list(participants),
        "mode": mode,
        "milestones_reached": list(milestones_reached),
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Не удалось сохранить состояние: {e}")


def load_state():
    global current_count, last_user_id, participants, mode, milestones_reached
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        current_count = data.get("current_count", 0)
        last_user_id = data.get("last_user_id")
        participants = set(data.get("participants", []))
        mode = data.get("mode", MODE_CLASSIC)
        milestones_reached = set(data.get("milestones_reached", []))
    except Exception as e:
        print(f"⚠️ Не удалось загрузить состояние: {e}")


def reset_state():
    global current_count, last_user_id, participants, milestones_reached
    current_count = 0
    last_user_id = None
    participants.clear()
    milestones_reached.clear()
    save_state()


# ============================================================
#                    СОБЫТИЯ
# ============================================================

@bot.event
async def on_ready():
    print(f"✅ Бот {bot.user} запущен!")
    print(f"📋 Канал для счёта: {CHANNEL_ID}")
    print(f"🎮 Режим: {MODE_NAMES.get(mode, mode)}")
    print(f"🎯 Рубеж (TARGET_COUNT): {TARGET_COUNT}")
    print(f"👑 Админы могут считать: {ADMINS_CAN_COUNT}")

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


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.id != CHANNEL_ID:
        return

    global current_count, last_user_id, participants

    content = message.content.strip()
    author_is_admin = is_admin(message.author)

    # === Если админ, а режим "не считать" — игнорируем полностью ===
    if author_is_admin and not ADMINS_CAN_COUNT:
        return

    # === Не число ===
    if not content.isdigit():
        if author_is_admin:
            # Админу не удаляем сообщение и не пишем предупреждение — просто молчим
            return
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        warning = await message.channel.send(
            f"{message.author.mention}, здесь нужно писать **только числа**! "
            f"Сообщение удалено."
        )
        await warning.delete(delay=5)
        return

    number = int(content)

    # === Нельзя считать дважды подряд ===
    if message.author.id == last_user_id:
        if author_is_admin:
            # Админу не мешаем — просто не засчитываем
            return
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        warning = await message.channel.send(
            f"{message.author.mention}, нельзя считать дважды подряд! "
            f"Дождись другого участника."
        )
        await warning.delete(delay=5)
        return

    expected = current_count + 1

    # === Правильное число ===
    if number == expected:
        current_count = number
        last_user_id = message.author.id
        participants.add(message.author.id)
        save_state()

        try:
            await message.add_reaction("✅")
        except discord.Forbidden:
            pass

        if mode == MODE_CLASSIC:
            if current_count == TARGET_COUNT:
                await handle_classic_success(message)
                return

        elif mode in (MODE_ENDLESS, MODE_ENDLESS_SOFT):
            if current_count % TARGET_COUNT == 0:
                milestone = current_count
                if milestone not in milestones_reached:
                    milestones_reached.add(milestone)
                    save_state()
                    await handle_endless_milestone(message, milestone)

    # === Ошибка: число не то ===
    else:
        broken_at = current_count

        # ---------------- CLASSIC ----------------
        if mode == MODE_CLASSIC:
            current_count = 0
            last_user_id = None
            participants.clear()
            milestones_reached.clear()
            save_state()

            # Админу не ставим реакцию ❌ (чтобы не привлекать внимания), но сообщение оставляем
            if not author_is_admin:
                try:
                    await message.add_reaction("❌")
                except discord.Forbidden:
                    pass

            fail_phrases = [
                "испортил счёт на",
                "всё сломал на",
                "обнулил цепочку на",
                "заруинил счёт на",
                "уронил счёт на",
            ]

            # Если ошибся админ — сообщение публикуем, но без упоминания (mention)
            mention = "" if author_is_admin else f"{message.author.mention} "
            fail_msg = await message.channel.send(
                f"💥 {mention}"
                f"**{random.choice(fail_phrases)} {broken_at}!!**\n"
                f"Следующее число — **1**. Игра начинается сначала."
            )
            try:
                await fail_msg.add_reaction("😡")
            except discord.Forbidden:
                pass

        # ---------------- ENDLESS (без сброса) ----------------
        elif mode == MODE_ENDLESS:
            if not author_is_admin:
                try:
                    await message.add_reaction("❌")
                except discord.Forbidden:
                    pass

            mention = "" if author_is_admin else f"{message.author.mention}, "
            fail_msg = await message.channel.send(
                f"⚠️ {mention}сейчас ждали **{expected}**, а не **{number}**.\n"
                f"В бесконечном режиме счёт **не сбрасывается**. "
                f"Следующее число — **{expected}**."
            )
            try:
                await fail_msg.add_reaction("🤔")
            except discord.Forbidden:
                pass

        # ---------------- ENDLESS_SOFT (откат к рубежу) ----------------
        elif mode == MODE_ENDLESS_SOFT:
            rollback_to = (broken_at // TARGET_COUNT) * TARGET_COUNT
            lost = broken_at - rollback_to

            current_count = rollback_to
            last_user_id = None

            save_state()

            if not author_is_admin:
                try:
                    await message.add_reaction("💥")
                except discord.Forbidden:
                    pass

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
            try:
                await fail_msg.add_reaction("😤")
            except discord.Forbidden:
                pass

        return


# ============================================================
#                    ВЫДАЧА РОЛИ
# ============================================================

async def give_role_to_participants(guild, reason: str):
    role = discord.utils.get(guild.roles, name=ROLE_NAME)
    if role is None:
        return None, []

    success_count = 0
    failed = []

    for user_id in list(participants):
        member = guild.get_member(user_id)
        if member is None:
            failed.append(f"<@{user_id}>")
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
    mentions = " ".join(f"<@{uid}>" for uid in winners)

    result = await give_role_to_participants(guild, "Досчитали до 100 (classic)")

    if result[0] is None:
        await message.channel.send(
            f"⚠️ Роль **{ROLE_NAME}** не найдена на сервере. "
            f"Создайте её или измените ROLE_NAME в настройках бота."
        )
        reset_state()
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

    try:
        await victory_msg.add_reaction("🎉")
        await victory_msg.add_reaction("🏆")
        await victory_msg.add_reaction("🥳")
    except discord.Forbidden:
        pass

    reset_state()


async def handle_endless_milestone(message, milestone):
    guild = message.guild
    mentions = " ".join(f"<@{uid}>" for uid in participants) or "—"

    result = await give_role_to_participants(
        guild, f"Достигли рубежа {milestone} (endless)"
    )

    if result[0] is None:
        await message.channel.send(
            f"🎯 Рубеж **{milestone}** достигнут! "
            f"(Роль **{ROLE_NAME}** не найдена, но это не важно — продолжаем!)"
        )
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
        f"🏆 **Участники цепочки:**\n{mentions}\n\n"
        f"👥 Участников: **{len(participants)}**\n"
        f"🏅 Роль `{ROLE_NAME}` выдана: **{success_count}**"
        + (f"\n⚠️ Не удалось выдать: {', '.join(failed)}" if failed else "")
        + f"\n\n♾️ **Счёт продолжается — следующее число {milestone + 1}!**"
        + mode_hint
    )

    try:
        await msg.add_reaction("🎉")
        await msg.add_reaction("🏆")
    except discord.Forbidden:
        pass


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
    mode = new_mode.value

    if reset:
        reset_state()
    else:
        save_state()

    await interaction.response.send_message(
        f"✅ Режим изменён: **{MODE_NAMES[old_mode]}** → **{MODE_NAMES[mode]}**\n"
        f"Текущий счёт: **{current_count}**"
        + (f"\n🔄 Счёт сброшен." if reset else ""),
        ephemeral=False,
    )


@bot.tree.command(name="reset_count", description="Сбросить счёт вручную")
@app_commands.default_permissions(administrator=True)
async def reset_count(interaction: discord.Interaction):
    reset_state()
    await interaction.response.send_message(
        "🔄 Счёт сброшен вручную. Начинаем с **1**.",
        ephemeral=False,
    )


@bot.tree.command(name="set_count", description="Установить текущее число вручную")
@app_commands.describe(number="Число, с которого продолжится счёт")
@app_commands.default_permissions(administrator=True)
async def set_count(interaction: discord.Interaction, number: int):
    global current_count
    current_count = number
    save_state()
    await interaction.response.send_message(
        f"✅ Счёт установлен на **{number}**. Следующее число — **{number + 1}**.",
        ephemeral=False,
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
    load_state()
    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f"❌ Ошибка: {e}")
    finally:
        try:
            input("\nНажмите Enter, чтобы закрыть окно...")
        except EOFError:
            pass
