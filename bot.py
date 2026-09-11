import discord
from discord.ext import commands
import random
import os

# === НАСТРОЙКИ ===
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = 1547463400919928832   # ID канала #счет
ROLE_NAME = "Счетовод"             # Название роли для выдачи
TARGET_COUNT = 100                 # До скольких считаем

# === ИНТЕНТЫ ===
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

# === СОСТОЯНИЕ СЧЁТА ===
current_count = 0
last_user_id = None
participants = set()


@bot.event
async def on_ready():
    print(f"✅ Бот {bot.user} запущен!")
    print(f"📋 Канал для счёта: {CHANNEL_ID}")
    print(f"🎯 Цель: досчитать до {TARGET_COUNT}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    if message.channel.id != CHANNEL_ID:
        return

    if message.content.startswith(bot.command_prefix):
        return

    global current_count, last_user_id, participants

    content = message.content.strip()

    # === Не число — удаляем и предупреждаем ===
    if not content.isdigit():
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

        try:
            await message.add_reaction("✅")
        except discord.Forbidden:
            pass

        if current_count == TARGET_COUNT:
            await handle_success(message)
            return

    # === Ошибка: число не то ===
    else:
        broken_at = current_count
        current_count = 0
        last_user_id = None
        participants.clear()

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

        fail_msg = await message.channel.send(
            f"💥 {message.author.mention} "
            f"**{random.choice(fail_phrases)} {broken_at}!!**\n"
            f"Следующее число — **1**. Игра начинается сначала."
        )
        
        try:
            await fail_msg.add_reaction("😡")
        except discord.Forbidden:
            pass
            
        return


async def handle_success(message):
    """Вызывается, когда досчитали до TARGET_COUNT."""
    global current_count, last_user_id, participants

    guild = message.guild
    role = discord.utils.get(guild.roles, name=ROLE_NAME)

    if role is None:
        await message.channel.send(
            f"⚠️ Роль **{ROLE_NAME}** не найдена на сервере. "
            f"Создайте её или измените ROLE_NAME в настройках бота."
        )
        return

    # Запоминаем участников до очистки
    winners = list(participants)

    success_count = 0
    failed = []

    for user_id in winners:
        member = guild.get_member(user_id)
        if member is None:
            failed.append(str(user_id))
            continue
        try:
            await member.add_roles(role, reason="Досчитали до 100")
            success_count += 1
        except discord.Forbidden:
            failed.append(member.name)

    # Рандомная победная фраза
    victory_phrases = [
        "🎉 **НЕВЕРОЯТНО!**",
        "🏆 **ЛЕГЕНДАРНО!**",
        "🔥 **ЭТО ПОБЕДА!**",
        "💎 **ИДЕАЛЬНАЯ ЦЕПОЧКА!**",
        "⚡ **МОЛНИЕНОСНО!**",
        "🌟 **ВЫ СДЕЛАЛИ ЭТО!**",
    ]

    # Упоминания всех победителей
    mentions = " ".join(f"<@{uid}>" for uid in winners)

    victory_msg = await message.channel.send(
        f"{random.choice(victory_phrases)}\n\n"
        f"🎯 Цепочка **1 → {TARGET_COUNT}** пройдена!\n\n"
        f"🏆 **Герои этой игры:**\n{mentions}\n\n"
        f"👥 Участников: **{len(winners)}**\n"
        f"🏅 Роль `{ROLE_NAME}` выдана: **{success_count}**"
        + (f"\n⚠️ Не удалось выдать: {', '.join(failed)}" if failed else "")
        + f"\n\n🔄 **Новая игра начинается — следующее число 1!**"
    )

    # Закрепляем сообщение с победой
    try:
        await victory_msg.pin()
    except discord.Forbidden:
        pass  # Нет прав Manage Messages — молча пропускаем
        
    try:
        await victory_msg.add_reaction("🎉")
        await victory_msg.add_reaction("🏆")
        await victory_msg.add_reaction("🥳")
    except discord.Forbidden:
        pass    
        
    # Сброс
    current_count = 0
    last_user_id = None
    participants.clear()


# ============================================================
#                    АДМИН-КОМАНДЫ
# ============================================================

@bot.command(name="reset_count")
@commands.has_permissions(administrator=True)
async def reset_count(ctx):
    """Сбросить счёт вручную. Только для администраторов."""
    global current_count, last_user_id, participants
    current_count = 0
    last_user_id = None
    participants.clear()
    await ctx.send("🔄 Счёт сброшен вручную. Начинаем с **1**.")


@reset_count.error
async def reset_count_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ У вас нет прав на эту команду. Нужны права **администратора**.")


@bot.command(name="set_count")
@commands.has_permissions(administrator=True)
async def set_count(ctx, number: int):
    """Установить текущее число вручную. Только для администраторов."""
    global current_count
    current_count = number
    await ctx.send(f"✅ Счёт установлен на **{number}**. Следующее число — **{number + 1}**.")


@set_count.error
async def set_count_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ У вас нет прав на эту команду. Нужны права **администратора**.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Укажите число. Пример: `!set_count 50`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Нужно указать целое число. Пример: `!set_count 50`")


@bot.command(name="count_status")
async def count_status(ctx):
    """Показать текущий статус счёта. Доступно всем."""
    await ctx.send(
        f"📊 **Статус счёта:**\n"
        f"Текущее число: **{current_count}**\n"
        f"Участников в цепочке: **{len(participants)}**\n"
        f"Цель: **{TARGET_COUNT}**"
    )


# === ЗАПУСК ===
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f"❌ Ошибка: {e}")
    finally:
        input("\nНажмите Enter, чтобы закрыть окно...")