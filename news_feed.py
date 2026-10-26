"""Complemento independente: notícias de anúncios e lançamentos via RSS para Discord.

Configuração por comandos slash de administrador:
  /noticias_canal
  /noticias_adicionar_feed
  /noticias_listar_feeds
  /noticias_remover_feed
  /noticias_status
  /noticias_testar

O módulo não altera a lógica do catálogo de jogos/mods; usa tabelas próprias no SQLite.
"""
import asyncio
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

DEFAULT_FEEDS = [
    ("Gematsu", "https://www.gematsu.com/feed"),
]
POLL_MINUTES = 5
MAX_DESCRIPTION = 700
ANNOUNCEMENT_TERMS = (
    "announce", "announced", "announcement", "reveal", "revealed",
    "release date", "launch date", "launches", "launching", "release date",
    "coming to", "out now", "available now", "new game", "newly announced",
    "data de lançamento", "data de lancamento", "lançamento", "lancamento",
    "anunciado", "anunciada", "anúncio", "anuncio", "revelado", "revelada",
    "chega em", "já disponível", "ja disponivel", "estreia",
)
USER_AGENT = "GameNewsDiscordBot/1.0 (+RSS reader)"


def _db(bot):
    # Usa o mesmo arquivo SQLite do bot, mas tabelas independentes.
    return bot.db_connection() if hasattr(bot, "db_connection") else None


class GameNewsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._session = None

    async def cog_load(self):
        self._init_tables()
        self.poll_news.start()

    async def cog_unload(self):
        self.poll_news.cancel()
        if self._session and not self._session.closed:
            await self._session.close()

    def _connect(self):
        # Usa o mesmo arquivo SQLite do catálogo sem importar/recriar a instância do bot.
        import sqlite3
        conn = sqlite3.connect("jogos.db")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS news_feeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                ativo INTEGER NOT NULL DEFAULT 1,
                inicializado INTEGER NOT NULL DEFAULT 0,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS news_seen (
                item_key TEXT PRIMARY KEY,
                feed_id INTEGER NOT NULL,
                visto_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS news_config (
                chave TEXT PRIMARY KEY,
                valor TEXT
            )""")
            for nome, url in DEFAULT_FEEDS:
                conn.execute(
                    "INSERT OR IGNORE INTO news_feeds (nome, url) VALUES (?, ?)",
                    (nome, url),
                )

    def _get_config(self, key):
        with self._connect() as conn:
            row = conn.execute("SELECT valor FROM news_config WHERE chave=?", (key,)).fetchone()
        return row["valor"] if row else None

    def _set_config(self, key, value):
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO news_config (chave, valor) VALUES (?, ?)", (key, str(value)))

    @staticmethod
    def _is_admin(interaction):
        return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild

    @staticmethod
    def _normalise_url(url):
        return (url or "").strip()

    @staticmethod
    def _local_name(tag):
        return tag.rsplit("}", 1)[-1].lower()

    @classmethod
    def _parse_feed(cls, xml_bytes):
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError:
            return []
        items = []
        for node in root.iter():
            if cls._local_name(node.tag) not in ("item", "entry"):
                continue
            data = {}
            for child in list(node):
                key = cls._local_name(child.tag)
                value = (child.text or "").strip()
                if key == "link" and not value:
                    value = child.attrib.get("href", "").strip()
                if key in ("title", "link", "description", "summary", "content", "guid", "id", "pubdate", "published", "updated"):
                    data.setdefault(key, value)
                    if key == "link" and value:
                        data["link"] = value
            title = re.sub(r"\s+", " ", data.get("title", "")).strip()
            link = data.get("link", "").strip()
            if not link and data.get("id", "").startswith(("http://", "https://")):
                link = data["id"]
            description = data.get("description") or data.get("summary") or data.get("content") or ""
            description = re.sub(r"<[^>]+>", " ", description)
            description = re.sub(r"\s+", " ", description).strip()
            date_text = data.get("pubdate") or data.get("published") or data.get("updated") or ""
            if title and link.startswith(("http://", "https://")):
                items.append({
                    "title": title[:250],
                    "link": link,
                    "description": description[:MAX_DESCRIPTION],
                    "date": date_text[:100],
                    "key": hashlib.sha256((link or title).encode("utf-8")).hexdigest(),
                })
        return items

    @staticmethod
    def _is_relevant(item):
        text = (item["title"] + " " + item["description"]).casefold()
        return any(term in text for term in ANNOUNCEMENT_TERMS)

    async def _fetch_feed(self, session, url):
        timeout = aiohttp.ClientTimeout(total=20)
        async with session.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT}) as resp:
            resp.raise_for_status()
            body = await resp.read()
        return self._parse_feed(body)

    def _feeds(self):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM news_feeds WHERE ativo=1 ORDER BY id").fetchall()

    def _seen(self, key):
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM news_seen WHERE item_key=?", (key,)).fetchone() is not None

    def _mark_seen(self, feed_id, key):
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO news_seen (item_key, feed_id) VALUES (?, ?)", (key, feed_id))

    def _feed_initialised(self, feed_id):
        with self._connect() as conn:
            row = conn.execute("SELECT inicializado FROM news_feeds WHERE id=?", (feed_id,)).fetchone()
        return bool(row and row["inicializado"])

    def _set_feed_initialised(self, feed_id):
        with self._connect() as conn:
            conn.execute("UPDATE news_feeds SET inicializado=1 WHERE id=?", (feed_id,))

    async def _publish(self, channel, feed_name, item):
        embed = discord.Embed(
            title=item["title"][:256],
            url=item["link"],
            description=(item["description"][:MAX_DESCRIPTION] if item["description"] else "Abra a fonte original para ler a notícia completa."),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Categoria", value="Anúncio / lançamento", inline=True)
        embed.set_footer(text=f"Fonte: {feed_name} • monitoramento RSS")
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @tasks.loop(minutes=POLL_MINUTES)
    async def poll_news(self):
        channel_id = self._get_config("channel_id")
        if not channel_id:
            return
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                return
        if not hasattr(channel, "send"):
            return
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        for feed in self._feeds():
            try:
                items = await self._fetch_feed(self._session, feed["url"])
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                print(f"[news] Feed {feed['nome']} indisponível: {exc}")
                continue
            # Na primeira leitura, registra o conteúdo já existente para não despejar notícias antigas.
            if not self._feed_initialised(feed["id"]):
                for item in items:
                    self._mark_seen(feed["id"], item["key"])
                self._set_feed_initialised(feed["id"])
                continue
            # RSS geralmente retorna os mais recentes primeiro; publica em ordem cronológica.
            new_items = [item for item in items if not self._seen(item["key"])]
            for item in reversed(new_items):
                self._mark_seen(feed["id"], item["key"])
                if not self._is_relevant(item):
                    continue
                try:
                    await self._publish(channel, feed["nome"], item)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    print(f"[news] Falha ao publicar no Discord: {exc}")
                    break

    @poll_news.before_loop
    async def before_poll_news(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="noticias_canal", description="[Admin] Define o canal que receberá notícias de games")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(canal="Canal de texto para receber notícias")
    async def noticias_canal(self, interaction: discord.Interaction, canal: discord.TextChannel):
        self._set_config("channel_id", canal.id)
        await interaction.response.send_message(
            f"Canal de notícias configurado: {canal.mention}. A verificação ocorre a cada {POLL_MINUTES} minutos.",
            ephemeral=True,
        )

    @app_commands.command(name="noticias_adicionar_feed", description="[Admin] Adiciona uma fonte RSS de games")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(nome="Nome curto da fonte", url="URL HTTPS do feed RSS ou Atom")
    async def noticias_adicionar_feed(self, interaction: discord.Interaction, nome: str, url: str):
        url = self._normalise_url(url)
        if not url.startswith("https://") or len(url) > 500:
            await interaction.response.send_message("Informe uma URL HTTPS válida para o feed RSS/Atom.", ephemeral=True)
            return
        if len(nome.strip()) < 2 or len(nome) > 80:
            await interaction.response.send_message("O nome deve ter entre 2 e 80 caracteres.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            items = await self._fetch_feed(self._session, url)
            if not items:
                await interaction.edit_original_response(content="Não encontrei itens válidos nesse feed. Verifique se a URL é realmente RSS/Atom.")
                return
        except Exception as exc:
            await interaction.edit_original_response(content=f"Não consegui ler esse feed: {str(exc)[:250]}")
            return
        try:
            with self._connect() as conn:
                cur = conn.execute("INSERT INTO news_feeds (nome, url) VALUES (?, ?)", (nome.strip(), url))
                feed_id = cur.lastrowid
                # Inicializa sem publicar arquivo antigo.
                for item in items:
                    conn.execute("INSERT OR IGNORE INTO news_seen (item_key, feed_id) VALUES (?, ?)", (item["key"], feed_id))
                conn.execute("UPDATE news_feeds SET inicializado=1 WHERE id=?", (feed_id,))
        except Exception:
            await interaction.edit_original_response(content="Esse feed já foi cadastrado ou ocorreu um erro no banco de dados.")
            return
        await interaction.edit_original_response(content=f"Feed **{nome.strip()}** adicionado e validado. Notícias futuras serão monitoradas.")

    @app_commands.command(name="noticias_listar_feeds", description="[Admin] Lista as fontes RSS configuradas")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def noticias_listar_feeds(self, interaction: discord.Interaction):
        feeds = self._feeds()
        if not feeds:
            texto = "Nenhuma fonte RSS cadastrada."
        else:
            texto = "\n".join(f"`{r['id']}` — **{r['nome']}** — {r['url']}" for r in feeds)
        await interaction.response.send_message(texto[:1900], ephemeral=True)

    @app_commands.command(name="noticias_remover_feed", description="[Admin] Remove uma fonte RSS pelo ID")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(feed_id="ID mostrado em /noticias_listar_feeds")
    async def noticias_remover_feed(self, interaction: discord.Interaction, feed_id: int):
        with self._connect() as conn:
            row = conn.execute("SELECT nome FROM news_feeds WHERE id=?", (feed_id,)).fetchone()
            if row:
                conn.execute("DELETE FROM news_seen WHERE feed_id=?", (feed_id,))
                conn.execute("DELETE FROM news_feeds WHERE id=?", (feed_id,))
        if not row:
            await interaction.response.send_message("Não encontrei esse feed.", ephemeral=True)
        else:
            await interaction.response.send_message(f"Feed **{row['nome']}** removido.", ephemeral=True)

    @app_commands.command(name="noticias_status", description="[Admin] Mostra o estado do monitor de notícias")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def noticias_status(self, interaction: discord.Interaction):
        channel_id = self._get_config("channel_id")
        channel_text = f"<#{channel_id}>" if channel_id else "não configurado"
        feeds = self._feeds()
        texto = (
            "## Monitor de notícias de games\n"
            f"**Canal:** {channel_text}\n"
            f"**Feeds ativos:** {len(feeds)}\n"
            f"**Intervalo:** {POLL_MINUTES} minutos\n"
            "**Filtro:** anúncios, revelações e datas de lançamento\n"
            "**Publicação:** automática após a primeira sincronização de cada feed"
        )
        await interaction.response.send_message(texto, ephemeral=True)

    @app_commands.command(name="noticias_testar", description="[Admin] Envia uma mensagem de teste ao canal configurado")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def noticias_testar(self, interaction: discord.Interaction):
        channel_id = self._get_config("channel_id")
        if not channel_id:
            await interaction.response.send_message("Configure primeiro o canal com /noticias_canal.", ephemeral=True)
            return
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                await interaction.response.send_message("Não consegui acessar o canal configurado.", ephemeral=True)
                return
        embed = discord.Embed(
            title="Teste do Radar de Games",
            description="O bot consegue publicar neste canal. As notícias novas serão enviadas quando forem detectadas nos feeds RSS.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        await interaction.response.send_message(f"Mensagem de teste enviada em {channel.mention}.", ephemeral=True)
