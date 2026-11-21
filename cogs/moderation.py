"""Automatic moderation for slurs and targeted harassment."""
from __future__ import annotations

import logging
import re
import unicodedata

import discord
from discord.ext import commands

log = logging.getLogger("shelby.moderation")

# Confusable characters and common leetspeak. Normalization is used only for
# matching; original message content is retained for moderation logs.
FOLD = str.maketrans({
    "а": "a", "е": "e", "ё": "e", "о": "o", "р": "p", "с": "c",
    "у": "y", "х": "x", "і": "i", "ї": "i", "ј": "j", "к": "k",
    "м": "m", "т": "t", "в": "b", "н": "h", "г": "r", "д": "d",
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "6": "g",
    "@": "a", "$": "s",
})
ZERO_WIDTH = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
NON_LETTER = re.compile(r"[^a-zа-яё]+", re.IGNORECASE)

# Terms are matched after punctuation/spacing removal, so n-i-g-g-e-r and
# similar obfuscations are caught. Generic identity words are intentionally
# not global bans; add tag-specific validation if a server tag feature is used.
BLOCKED = (
    "nigger", "nigga", "naga", "niga", "ниггер", "нигга", "нига", "нага",
    "пидор", "пидорас", "педик", "гомик", "faggot",
    "хохол", "хач", "жид", "москаль", "ватник", "сионист",
    "куколд", "конча", "даун", "аутист", "дебил", "retard",
    "virgin", "девственник", "simp", "симп", "incel", "инцел",
    "cunt", "пизда", "вагина",
)
BLOCKED_PATTERNS = tuple(
    re.compile(r"(?<![a-zа-яё])" + r"[^a-zа-яё]*".join(re.escape(ch) for ch in term.translate(FOLD)) + r"(?![a-zа-яё])", re.I)
    for term in BLOCKED
)
TAG_ONLY_PATTERNS = tuple(
    re.compile(r"(?<![a-zа-яё])" + r"[^a-zа-яё]*".join(re.escape(ch) for ch in term.translate(FOLD)) + r"(?![a-zа-яё])", re.I)
    for term in ("белый", "натурал", "гетеросексуал")
)

# Neutral usage of «петух» as poultry is allowed when the surrounding text
# clearly refers to a bird/farm/food. Otherwise it is treated as an insult.
BIRD_CONTEXT = re.compile(r"(?:куриц\w*|птиц\w*|петух\w*|петуш\w*|птич\w*|птичник\w*|яиц\w*|ферм\w*|курятин\w*|петушин\w*)", re.I)
PETUKH = re.compile(r"петух\w*", re.I)

# Hijab is moderated only in a clearly hostile context, not in neutral
# discussion. Proximity is intentionally limited to the same short phrase.
HIJAB = re.compile(r"хиджаб\w*", re.I)
NEGATIVE = re.compile(r"(?:ненавиж\w*|мерзк\w*|урод\w*|грязн\w*|туп\w*|позор\w*|сним\w*|запрет\w*|убир\w*|оскорб\w*)", re.I)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().translate(FOLD)
    text = ZERO_WIDTH.sub("", text)
    text = NON_LETTER.sub(" ", text)
    # Repeated-character stretching is a common filter bypass.
    return re.sub(r"(.)\1{2,}", r"\1\1", text)


def find_violation(text: str) -> str | None:
    normalized = normalize(text)
    for pattern in BLOCKED_PATTERNS:
        if pattern.search(normalized):
            return "оскорбительная лексика"

    # These terms are checked in original text because Cyrillic folding changes
    # their spelling; separator-obfuscated versions are covered by normalized.
    compact_original = "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalpha())
    if "петух" in compact_original:
        if not BIRD_CONTEXT.search(text):
            return "оскорбительное употребление слова «петух»"
    if "хиджаб" in compact_original and HIJAB.search(text) and NEGATIVE.search(text):
        return "оскорбительное высказывание о религиозной одежде"
    return None


def find_tag_violation(text: str) -> bool:
    normalized = normalize(text)
    return any(pattern.search(normalized) for pattern in TAG_ONLY_PATTERNS)


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot, log_channel_id: int, embed_factory):
        self.bot = bot
        self.log_channel_id = log_channel_id
        self.embed_factory = embed_factory

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self._moderate(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.content != after.content:
            await self._moderate(after)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        # Discord bots cannot control account usernames or native server tags;
        # enforce tag-only vocabulary in the editable server nickname.
        if before.nick == after.nick or not after.nick or not find_tag_violation(after.nick):
            return
        try:
            await after.edit(nick=None, reason="Запрещённое обозначение в серверном теге/никнейме")
            log.info("Reset restricted nickname for member %s", after.id)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not reset restricted nickname for member %s", after.id)

    async def _moderate(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild or not message.content:
            return
        reason = find_violation(message.content)
        if not reason:
            return
        try:
            await message.delete()
        except discord.NotFound:
            return
        except discord.Forbidden:
            log.warning("Missing Manage Messages in channel %s", message.channel.id)
        except discord.HTTPException:
            log.exception("Could not remove a violating message")
            return

        # Short channel notice; no message content is echoed back.
        try:
            await message.channel.send(
                f"{message.author.mention}, сообщение удалено за нарушение правил общения.",
                delete_after=8,
                allowed_mentions=discord.AllowedMentions(users=[message.author], roles=False, everyone=False),
            )
        except discord.HTTPException:
            log.warning("Could not post moderation notice in channel %s", message.channel.id)

        await self._log(message, reason)

    async def _log(self, message: discord.Message, reason: str) -> None:
        if not self.log_channel_id:
            log.warning("MOD_LOG_CHANNEL_ID is not configured; moderation event for user %s", message.author.id)
            return
        channel = self.bot.get_channel(self.log_channel_id)
        if not isinstance(channel, discord.TextChannel):
            log.warning("Moderation log channel %s not found", self.log_channel_id)
            return
        embed = self.embed_factory("Автомодерация", color=discord.Colour.dark_red())
        embed.add_field(name="Участник", value=f"{message.author.mention} (`{message.author.id}`)", inline=False)
        embed.add_field(name="Канал", value=message.channel.mention, inline=True)
        embed.add_field(name="Причина", value=reason, inline=True)
        embed.add_field(name="Сообщение", value=discord.utils.escape_markdown(message.content[:900]), inline=False)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("Could not write moderation log")


async def setup(bot: commands.Bot) -> None:
    # Main loads the cog directly so it can pass the configured log channel.
    return None
