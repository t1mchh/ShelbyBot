"""Shelby Family management bot for Nova RP. Python 3.11+, discord.py 2.x."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from cogs.moderation import ModerationCog

# ── Server configuration: replace 0 with your Discord IDs ──────────────────
GUILD_ID = 0
STARTER_ROLE_ID = 0
WELCOME_CHANNEL_ID = 0
SBOR_CHANNEL_ID = 0
STAFF_ROLE_ID = 0
REPORTS_CHANNEL_ID = 0             # private senior staff channel
VOICE_TRIGGER_CHANNEL_ID = 0
TEMP_VOICE_CATEGORY_ID = 0
MOD_LOG_CHANNEL_ID = 0            # канал для журнала авто-модерации

DB_PATH = Path(__file__).resolve().parent / "data" / "shelby.sqlite3"
COLOR_GRAPHITE = discord.Colour.from_rgb(40, 40, 40)  # #282828
COLOR_GOLD = discord.Colour.from_rgb(198, 160, 82)
FOOTER = "Shelby Family • Nova RP"
EVENTS = ("Капт / Бизвар", "Контракты / Фарм", "ВЗХ / МП", "Общий сбор")
log = logging.getLogger("shelby")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def shelby_embed(title: str, description: str | None = None, *, color: discord.Colour = COLOR_GRAPHITE) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    e.set_footer(text=FOOTER)
    return e


def is_staff(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or (STAFF_ROLE_ID != 0 and any(r.id == STAFF_ROLE_ID for r in member.roles))


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await self.db.execute("PRAGMA journal_mode = WAL")
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sbor_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL, message_id INTEGER, role_id INTEGER NOT NULL,
                event TEXT NOT NULL, event_time TEXT NOT NULL, created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS sbor_responses (
                event_id INTEGER NOT NULL REFERENCES sbor_events(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL, response TEXT NOT NULL CHECK(response IN ('yes','no')),
                updated_at TEXT NOT NULL, PRIMARY KEY(event_id,user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sbor_events_message ON sbor_events(message_id);
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL, nickname TEXT NOT NULL, work TEXT NOT NULL,
                details TEXT NOT NULL, created_at TEXT NOT NULL, message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','paid')),
                paid_by INTEGER, paid_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status);
            CREATE TABLE IF NOT EXISTS temporary_voice_channels (
                channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL, created_at TEXT NOT NULL
            );
        """)
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None

    async def execute(self, sql: str, params: tuple = ()) -> int:
        assert self.db
        async with self.db.execute(sql, params) as cur:
            await self.db.commit()
            return cur.lastrowid or 0

    async def one(self, sql: str, params: tuple = ()):
        assert self.db
        async with self.db.execute(sql, params) as cur:
            return await cur.fetchone()

    async def all(self, sql: str, params: tuple = ()):
        assert self.db
        async with self.db.execute(sql, params) as cur:
            return await cur.fetchall()

    async def set_response(self, event_id: int, user_id: int, response: str) -> None:
        assert self.db
        async with self.db.execute("""INSERT INTO sbor_responses(event_id,user_id,response,updated_at)
            VALUES(?,?,?,?) ON CONFLICT(event_id,user_id) DO UPDATE SET response=excluded.response,updated_at=excluded.updated_at""",
            (event_id, user_id, response, now_iso())):
            await self.db.commit()

    async def pay_report(self, report_id: int, staff_id: int) -> bool:
        assert self.db
        async with self.db.execute("UPDATE reports SET status='paid',paid_by=?,paid_at=? WHERE id=? AND status='open'",
                                   (staff_id, now_iso(), report_id)) as cur:
            await self.db.commit()
            return cur.rowcount == 1


db = Database(DB_PATH)


async def render_sbor(event_id: int) -> discord.Embed:
    event = await db.one("SELECT * FROM sbor_events WHERE id=?", (event_id,))
    if not event:
        return shelby_embed("Сбор закрыт", "Запись о сборе недоступна.")
    rows = await db.all("SELECT user_id,response FROM sbor_responses WHERE event_id=? ORDER BY updated_at", (event_id,))
    yes = [f"<@{r['user_id']}>" for r in rows if r["response"] == "yes"]
    no = [f"<@{r['user_id']}>" for r in rows if r["response"] == "no"]
    e = shelby_embed(f"Семейный сбор • {event['event']}",
        f"**Время:** {discord.utils.escape_markdown(event['event_time'])}\n"
        f"**Участие:** <@&{event['role_id']}>\n\nПрисоединяйтесь к делу семьи.\n\n"
        f"**Идут — {len(yes)}**\n" + ("\n".join(yes) if yes else "—") + "\n\n"
        f"**Не смогут — {len(no)}**\n" + ("\n".join(no) if no else "—"), color=COLOR_GOLD)
    e.add_field(name="Сбор №", value=str(event_id), inline=True)
    return e


class SborView(discord.ui.View):
    def __init__(self, event_id: int):
        super().__init__(timeout=None)
        self.event_id = event_id
        self.yes.custom_id = f"shelby:sbor:{event_id}:yes"
        self.no.custom_id = f"shelby:sbor:{event_id}:no"

    async def vote(self, interaction: discord.Interaction, answer: str):
        event = await db.one("SELECT active FROM sbor_events WHERE id=?", (self.event_id,))
        if not event or not event["active"]:
            await interaction.response.send_message("Этот сбор уже недоступен.", ephemeral=True)
            return
        await db.set_response(self.event_id, interaction.user.id, answer)
        await interaction.response.defer()
        try:
            await interaction.message.edit(embed=await render_sbor(self.event_id), view=self)
        except discord.NotFound:
            log.warning("Sbor message missing for event %s", self.event_id)
        except discord.HTTPException:
            log.exception("Could not update sbor message %s", self.event_id)

    @discord.ui.button(label="Буду", emoji="✅", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.vote(interaction, "yes")

    @discord.ui.button(label="Не смогу", emoji="❌", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.vote(interaction, "no")


class ReportPaidView(discord.ui.View):
    def __init__(self, report_id: int):
        super().__init__(timeout=None)
        self.report_id = report_id
        self.paid.custom_id = f"shelby:report:{report_id}:paid"

    @discord.ui.button(label="Выплачено", emoji="✅", style=discord.ButtonStyle.success)
    async def paid(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            await interaction.response.send_message("Доступно только старшему составу.", ephemeral=True)
            return
        if not await db.pay_report(self.report_id, interaction.user.id):
            await interaction.response.send_message("Отчёт уже закрыт или не найден.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            embed = interaction.message.embeds[0]
            embed.color = COLOR_GOLD
            embed.add_field(name="Статус", value=f"Выплачено • <@{interaction.user.id}>\n{now_iso()}", inline=False)
            await interaction.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            log.exception("Could not update paid report %s", self.report_id)
        report = await db.one("SELECT author_id FROM reports WHERE id=?", (self.report_id,))
        if report:
            try:
                user = interaction.client.get_user(report["author_id"]) or await interaction.client.fetch_user(report["author_id"])
                await user.send(embed=shelby_embed("Отчёт выплачен", f"Ваш отчёт №{self.report_id} отмечен как выплаченный.", color=COLOR_GOLD))
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                log.info("Could not DM report author for report %s", self.report_id)


class ReportModal(discord.ui.Modal, title="Отчёт Shelby Family"):
    nickname = discord.ui.TextInput(label="Игровой ник", max_length=100)
    work = discord.ui.TextInput(label="Что сделано", style=discord.TextStyle.paragraph, max_length=1000)
    details = discord.ui.TextInput(label="Сумма / дополнительные детали", style=discord.TextStyle.paragraph, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Форма доступна на сервере.", ephemeral=True)
            return
        if REPORTS_CHANNEL_ID == 0:
            await interaction.response.send_message("Канал отчётов не настроен. Сообщите администрации.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(REPORTS_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Канал отчётов не найден.", ephemeral=True)
            return
        rid = await db.execute("INSERT INTO reports(guild_id,author_id,nickname,work,details,created_at) VALUES(?,?,?,?,?,?)",
            (interaction.guild.id, interaction.user.id, str(self.nickname), str(self.work), str(self.details), now_iso()))
        embed = shelby_embed(f"Отчёт на премию №{rid}", color=COLOR_GOLD)
        embed.add_field(name="Автор", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
        embed.add_field(name="Игровой ник", value=str(self.nickname), inline=True)
        embed.add_field(name="Выполненная работа", value=str(self.work), inline=False)
        embed.add_field(name="Сумма / детали", value=str(self.details), inline=False)
        embed.add_field(name="Отправлен", value=now_iso(), inline=False)
        try:
            msg = await channel.send(embed=embed, view=ReportPaidView(rid))
            await db.execute("UPDATE reports SET message_id=? WHERE id=?", (msg.id, rid))
        except discord.HTTPException:
            log.exception("Failed to publish report %s", rid)
            await interaction.response.send_message("Не удалось передать отчёт старшему составу. Попробуйте позже.", ephemeral=True)
            return
        await interaction.response.send_message("Отчёт передан старшему составу.", ephemeral=True)


class ReportPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Отправить отчет", emoji="📦", style=discord.ButtonStyle.primary, custom_id="shelby:report:submit")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ReportModal())


class ShelbyBot(commands.Bot):
    async def setup_hook(self):
        await db.open()
        await self.add_cog(ModerationCog(self, MOD_LOG_CHANNEL_ID, shelby_embed))
        # Register fixed persistent panel and reconstruct DB-backed persistent views.
        self.add_view(ReportPanelView())
        for row in await db.all("SELECT id FROM sbor_events WHERE active=1 AND message_id IS NOT NULL"):
            self.add_view(SborView(row["id"]))
        for row in await db.all("SELECT id FROM reports WHERE status='open' AND message_id IS NOT NULL"):
            self.add_view(ReportPaidView(row["id"]))
        try:
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()
        except discord.HTTPException:
            log.exception("Slash command sync failed")

    async def close(self):
        await db.close()
        await super().close()


intents = discord.Intents.none()
intents.guilds = True
intents.members = True
intents.message_content = True
intents.voice_states = True
bot = ShelbyBot(command_prefix="!", intents=intents)
voice_locks: dict[int, asyncio.Lock] = {}
voice_cleanup_done = False


@bot.event
async def on_ready():
    log.info("Shelby bot ready as %s (%s)", bot.user, bot.user.id if bot.user else "?")
    global voice_cleanup_done
    if not voice_cleanup_done:
        await restore_voice_channels()
        voice_cleanup_done = True


@bot.event
async def on_member_join(member: discord.Member):
    if STARTER_ROLE_ID:
        role = member.guild.get_role(STARTER_ROLE_ID)
        if role:
            try:
                await member.add_roles(role, reason="Автоматическая стартовая роль Shelby Family")
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Could not assign starter role to %s", member.id)
        else:
            log.warning("Starter role %s not found", STARTER_ROLE_ID)
    else:
        log.warning("STARTER_ROLE_ID is not configured")
    if WELCOME_CHANNEL_ID:
        channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            e = shelby_embed("Новый член семьи", f"Добро пожаловать в Shelby Family, {member.mention}.\n\nТеперь в семье **{member.guild.member_count}** участников.", color=COLOR_GOLD)
            e.set_thumbnail(url=member.display_avatar.url)
            try:
                await channel.send(content=member.mention, embed=e)
            except discord.HTTPException:
                log.exception("Could not send welcome for %s", member.id)
        else:
            log.warning("Welcome channel %s not found", WELCOME_CHANNEL_ID)
    else:
        log.warning("WELCOME_CHANNEL_ID is not configured")


@bot.tree.command(name="sbor", description="Опубликовать объявление о семейном сборе")
@app_commands.rename(role="роль", event="событие", time="время", duplicate_dm="дублировать_в_лс")
@app_commands.describe(role="Роль для упоминания", event="Тип события", time="Время проведения", duplicate_dm="Разослать участникам роли в ЛС")
@app_commands.choices(event=[app_commands.Choice(name=e, value=e) for e in EVENTS], duplicate_dm=[app_commands.Choice(name="Да", value="yes"), app_commands.Choice(name="Нет", value="no")])
async def sbor(interaction: discord.Interaction, role: discord.Role, event: app_commands.Choice[str], time: str, duplicate_dm: app_commands.Choice[str]):
    if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
        await interaction.response.send_message("Команда доступна старшему составу и администраторам.", ephemeral=True)
        return
    if not SBOR_CHANNEL_ID or not interaction.guild:
        await interaction.response.send_message("Канал сборов не настроен.", ephemeral=True)
        return
    channel = interaction.guild.get_channel(SBOR_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("Канал сборов не найден.", ephemeral=True)
        return
    eid = await db.execute("INSERT INTO sbor_events(guild_id,channel_id,role_id,event,event_time,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
        (interaction.guild.id, channel.id, role.id, event.value, time[:200], interaction.user.id, now_iso()))
    view = SborView(eid)
    try:
        msg = await channel.send(embed=await render_sbor(eid), view=view)
        await db.execute("UPDATE sbor_events SET message_id=? WHERE id=?", (msg.id, eid))
    except discord.HTTPException:
        log.exception("Could not publish sbor %s", eid)
        await db.execute("UPDATE sbor_events SET active=0 WHERE id=?", (eid,))
        await interaction.response.send_message("Не удалось опубликовать сбор.", ephemeral=True)
        return
    await interaction.response.send_message(f"Сбор опубликован: {msg.jump_url}", ephemeral=True)
    if duplicate_dm.value == "yes":
        sent = failed = 0
        for member in role.members:
            if member.bot:
                continue
            try:
                await member.send(embed=shelby_embed(f"Семейный сбор • {event.value}", f"Время: **{discord.utils.escape_markdown(time[:200])}**\n[Открыть сбор]({msg.jump_url})", color=COLOR_GOLD))
                sent += 1
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                failed += 1
            await asyncio.sleep(1.2)
        log.info("Sbor %s DM distribution complete: sent=%s failed=%s", eid, sent, failed)


@bot.tree.command(name="setup_reports", description="Создать панель отправки отчётов")
async def setup_reports(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Команда доступна администраторам.", ephemeral=True)
        return
    e = shelby_embed("Отчёты на премию", "Нажмите кнопку ниже и заполните форму выполненной работы.", color=COLOR_GOLD)
    await interaction.response.send_message(embed=e, view=ReportPanelView())


@bot.tree.command(name="clear", description="Удалить сообщения в текущем канале")
@app_commands.describe(amount="Количество сообщений от 1 до 100")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("Команда доступна только в текстовом канале.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=amount, bulk=True)
        await interaction.followup.send(f"Удалено сообщений: **{len(deleted)}**.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("У бота нет права Manage Messages.", ephemeral=True)
    except discord.HTTPException:
        log.exception("Clear failed in channel %s", interaction.channel_id)
        await interaction.followup.send("Discord не выполнил очистку. Попробуйте позже.", ephemeral=True)


@clear.error
async def clear_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "Для команды требуется право Manage Messages."
    else:
        log.exception("Clear command error", exc_info=error)
        msg = "Не удалось выполнить команду."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    guild = member.guild
    lock = voice_locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        if after.channel and after.channel.id == VOICE_TRIGGER_CHANNEL_ID and VOICE_TRIGGER_CHANNEL_ID:
            category = guild.get_channel(TEMP_VOICE_CATEGORY_ID) if TEMP_VOICE_CATEGORY_ID else None
            if not isinstance(category, discord.CategoryChannel):
                log.warning("Temporary voice category is not configured or missing")
                return
            try:
                vc = await guild.create_voice_channel(f"🔊 Пачка | {member.display_name}"[:100], category=category, reason="Временная голосовая Shelby")
                await db.execute("INSERT OR REPLACE INTO temporary_voice_channels(channel_id,guild_id,owner_id,created_at) VALUES(?,?,?,?)", (vc.id, guild.id, member.id, now_iso()))
                try:
                    await member.move_to(vc, reason="Создана временная комната")
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Could not move member %s to temporary voice", member.id)
                    try:
                        await vc.delete(reason="Не удалось переместить создателя")
                        await db.execute("DELETE FROM temporary_voice_channels WHERE channel_id=?", (vc.id,))
                    except discord.HTTPException:
                        log.exception("Could not clean up temporary voice %s", vc.id)
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Could not create temporary voice for %s", member.id)
        # Only delete channels recorded in our table, never ordinary voice channels.
        if before.channel and (not after.channel or before.channel.id != after.channel.id):
            row = await db.one("SELECT channel_id FROM temporary_voice_channels WHERE channel_id=? AND guild_id=?", (before.channel.id, guild.id))
            if row and len(before.channel.members) == 0:
                try:
                    await before.channel.delete(reason="Временная комната опустела")
                    await db.execute("DELETE FROM temporary_voice_channels WHERE channel_id=?", (before.channel.id,))
                except discord.NotFound:
                    await db.execute("DELETE FROM temporary_voice_channels WHERE channel_id=?", (before.channel.id,))
                except discord.HTTPException:
                    log.exception("Could not delete empty temporary voice %s", before.channel.id)


async def restore_voice_channels():
    """Prune stale DB references and remove empty bot-owned temporary rooms after restart."""
    for row in await db.all("SELECT channel_id,guild_id FROM temporary_voice_channels"):
        channel = bot.get_channel(row["channel_id"])
        if isinstance(channel, discord.VoiceChannel) and not channel.members:
            try:
                await channel.delete(reason="Очистка пустой временной комнаты после перезапуска")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.warning("Could not clean temporary voice %s", row["channel_id"], exc_info=True)
        await db.execute("DELETE FROM temporary_voice_channels WHERE channel_id=?", (row["channel_id"],))


async def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        log.error("DISCORD_TOKEN is not set. Add it to the hosting environment variables.")
        return
    try:
        await bot.start(token)
    finally:
        if db.db:
            await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
