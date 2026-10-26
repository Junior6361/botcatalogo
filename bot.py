"""
Bot de catálogo de jogos para Discord (discord.py 2.x, comandos slash).

USUÁRIOS
  /catalogo            -> navega pelo catálogo, com filtro por plataforma e paginação
  /buscar nome         -> busca pelo nome (com autocomplete)

ADMIN (quem tem a permissão "Gerenciar Servidor")
  /jogo_adicionar      -> nome, plataforma, link, capa (imagem), descrição e tutorial (opcionais)
  /jogo_editar         -> altera qualquer campo de um jogo
  /jogo_remover        -> remove um jogo

Configuração (variáveis de ambiente):
  DISCORD_TOKEN = token do bot (Discord Developer Portal)
  GUILD_ID      = (opcional) ID do seu servidor; faz os comandos aparecerem na hora

Instalação:  pip install -U discord.py
Execução:    python bot_catalogo_discord.py
"""
import math
import os
import sqlite3
import uuid

import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = os.getenv("GUILD_ID")
DB_PATH = "jogos.db"
CAPAS_DIR = "capas"
PAGE_SIZE = 8

# Plataformas de origem (edite à vontade, máximo 24)
PLATAFORMAS = {
    "Steam": "🟦",
    "Epic Games": "⬛",
    "GOG": "🟪",
    "EA App": "🟥",
    "Ubisoft Connect": "🔷",
    "Xbox / Microsoft Store": "🟩",
    "PlayStation": "🎮",
    "Nintendo": "🔴",
    "Itch.io": "🟧",
    "Mobile": "📱",
    "Outra": "🌐",
}
PLAT_CHOICES = [app_commands.Choice(name=f"{e} {n}", value=n) for n, e in PLATAFORMAS.items()]

# ------------------------------------------------------------------ banco de dados


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(CAPAS_DIR, exist_ok=True)
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS jogos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                plataforma TEXT NOT NULL,
                descricao TEXT,
                link TEXT NOT NULL,
                tutorial TEXT,
                capa TEXT
            )"""
        )


def listar(plataforma=None, query=None, page=0):
    where, params = [], []
    if plataforma:
        where.append("plataforma = ?")
        params.append(plataforma)
    if query:
        where.append("nome LIKE ?")
        params.append(f"%{query}%")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM jogos {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT id, nome, plataforma FROM jogos {clause} "
            "ORDER BY nome COLLATE NOCASE LIMIT ? OFFSET ?",
            params + [PAGE_SIZE, page * PAGE_SIZE],
        ).fetchall()
    return rows, total


def get_jogo(jogo_id):
    with db() as conn:
        return conn.execute("SELECT * FROM jogos WHERE id = ?", (jogo_id,)).fetchone()


def link_valido(s):
    return s.strip().lower().startswith(("http://", "https://"))


async def salvar_capa(att: discord.Attachment):
    if not (att.content_type or "").startswith("image/"):
        return None
    ext = os.path.splitext(att.filename)[1].lower() or ".png"
    path = os.path.join(CAPAS_DIR, f"{uuid.uuid4().hex}{ext}")
    await att.save(path)
    return path


def apagar_arquivo(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


# ------------------------------------------------------------------ embeds


def jogo_embed(j):
    emb = discord.Embed(
        title=j["nome"],
        description=(j["descricao"] or "")[:1500] or None,
        color=discord.Color.blurple(),
    )
    emoji = PLATAFORMAS.get(j["plataforma"], "🎮")
    emb.add_field(name="Plataforma de origem", value=f"{emoji} {j['plataforma']}", inline=True)
    if j["tutorial"]:
        emb.add_field(name="Tutorial", value="✅ Disponível", inline=True)
    file = None
    if j["capa"] and os.path.exists(j["capa"]):
        fname = os.path.basename(j["capa"])
        file = discord.File(j["capa"], filename=fname)
        emb.set_image(url=f"attachment://{fname}")  # imagem grande
    return emb, file


# ------------------------------------------------------------------ views (catálogo)


class PlataformaSelect(discord.ui.Select):
    def __init__(self, atual):
        opts = [discord.SelectOption(label="Todas as plataformas", value="all", emoji="📚", default=atual is None)]
        for n, e in PLATAFORMAS.items():
            opts.append(discord.SelectOption(label=n, value=n, emoji=e, default=(n == atual)))
        super().__init__(placeholder="Filtrar por plataforma", options=opts, row=0)

    async def callback(self, interaction: discord.Interaction):
        v: CatalogView = self.view
        v.plataforma = None if self.values[0] == "all" else self.values[0]
        v.page = 0
        await v.atualizar(interaction)


class JogoSelect(discord.ui.Select):
    def __init__(self, rows):
        opts = [
            discord.SelectOption(
                label=r["nome"][:100], value=str(r["id"]),
                description=r["plataforma"], emoji=PLATAFORMAS.get(r["plataforma"], "🎮"),
            )
            for r in rows
        ]
        super().__init__(placeholder="Abrir um jogo...", options=opts, row=1)

    async def callback(self, interaction: discord.Interaction):
        v: CatalogView = self.view
        jogo = get_jogo(int(self.values[0]))
        if not jogo:
            await interaction.response.send_message("Esse jogo não existe mais.", ephemeral=True)
            return
        emb, file = jogo_embed(jogo)
        view = DetalheView(v.user_id, jogo, v.plataforma, v.query, v.page)
        await interaction.response.edit_message(embed=emb, attachments=[file] if file else [], view=view)


class PageButton(discord.ui.Button):
    def __init__(self, delta, disabled):
        super().__init__(
            label="Anterior" if delta < 0 else "Próxima",
            emoji="⬅️" if delta < 0 else "➡️",
            style=discord.ButtonStyle.secondary, disabled=disabled, row=2,
        )
        self.delta = delta

    async def callback(self, interaction: discord.Interaction):
        v: CatalogView = self.view
        v.page += self.delta
        await v.atualizar(interaction)


class CatalogView(discord.ui.View):
    def __init__(self, user_id, plataforma=None, query=None, page=0):
        super().__init__(timeout=900)
        self.user_id, self.plataforma, self.query, self.page = user_id, plataforma, query, page
        self.rows, self.total = [], 0
        self.build()

    def build(self):
        self.clear_items()
        self.rows, self.total = listar(self.plataforma, self.query, self.page)
        pages = max(1, math.ceil(self.total / PAGE_SIZE))
        self.add_item(PlataformaSelect(self.plataforma))
        if self.rows:
            self.add_item(JogoSelect(self.rows))
        self.add_item(PageButton(-1, self.page <= 0))
        self.add_item(PageButton(+1, self.page >= pages - 1))

    def embed(self):
        pages = max(1, math.ceil(self.total / PAGE_SIZE))
        titulo = "📚 Catálogo de jogos"
        if self.query:
            titulo += f" • busca: {self.query}"
        if self.plataforma:
            titulo += f" • {self.plataforma}"
        if self.rows:
            linhas = [
                f"{PLATAFORMAS.get(r['plataforma'], '🎮')} **{r['nome']}** — {r['plataforma']}"
                for r in self.rows
            ]
            desc = "\n".join(linhas) + "\n\nEscolha um jogo no menu abaixo para ver a capa e o download."
        else:
            desc = "Nenhum jogo encontrado."
        emb = discord.Embed(title=titulo, description=desc, color=discord.Color.blurple())
        emb.set_footer(text=f"Página {self.page + 1}/{pages} • {self.total} jogo(s)")
        return emb

    async def atualizar(self, interaction: discord.Interaction):
        self.build()
        await interaction.response.edit_message(embed=self.embed(), attachments=[], view=self)

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Use /catalogo para abrir o seu próprio catálogo 😉", ephemeral=True)
            return False
        return True


class DetalheView(discord.ui.View):
    def __init__(self, user_id, jogo, plataforma, query, page):
        super().__init__(timeout=900)
        self.user_id, self.plataforma, self.query, self.page = user_id, plataforma, query, page
        self.add_item(discord.ui.Button(label="Baixar", emoji="⬇️", style=discord.ButtonStyle.link, url=jogo["link"]))
        if jogo["tutorial"]:
            self.add_item(discord.ui.Button(label="Tutorial", emoji="📖", style=discord.ButtonStyle.link, url=jogo["tutorial"]))
        voltar = discord.ui.Button(label="Voltar", emoji="⬅️", style=discord.ButtonStyle.secondary)
        voltar.callback = self.voltar
        self.add_item(voltar)

    async def voltar(self, interaction: discord.Interaction):
        v = CatalogView(self.user_id, self.plataforma, self.query, self.page)
        await interaction.response.edit_message(embed=v.embed(), attachments=[], view=v)

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Use /catalogo para abrir o seu próprio catálogo 😉", ephemeral=True)
            return False
        return True


# ------------------------------------------------------------------ bot


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = Bot()
tree = bot.tree


@tree.error
async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "⛔ Você precisa da permissão **Gerenciar Servidor** para usar esse comando."
    else:
        msg = "Ocorreu um erro inesperado."
        print("Erro:", repr(error))
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


async def jogo_autocomplete_id(interaction: discord.Interaction, current: str):
    with db() as conn:
        rows = conn.execute(
            "SELECT id, nome, plataforma FROM jogos WHERE nome LIKE ? ORDER BY nome LIMIT 25",
            (f"%{current}%",),
        ).fetchall()
    return [app_commands.Choice(name=f"{r['nome']} ({r['plataforma']})"[:100], value=str(r["id"])) for r in rows]


async def jogo_autocomplete_nome(interaction: discord.Interaction, current: str):
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT nome FROM jogos WHERE nome LIKE ? ORDER BY nome LIMIT 25",
            (f"%{current}%",),
        ).fetchall()
    return [app_commands.Choice(name=r["nome"][:100], value=r["nome"][:100]) for r in rows]


# ------------------------------------------------------------------ comandos de usuário


@tree.command(name="catalogo", description="Navegue pelo catálogo de jogos")
async def catalogo(interaction: discord.Interaction):
    v = CatalogView(interaction.user.id)
    await interaction.response.send_message(embed=v.embed(), view=v)


@tree.command(name="buscar", description="Busque um jogo pelo nome")
@app_commands.describe(nome="Parte do nome do jogo")
@app_commands.autocomplete(nome=jogo_autocomplete_nome)
async def buscar(interaction: discord.Interaction, nome: str):
    v = CatalogView(interaction.user.id, query=nome)
    if v.total == 1:  # só um resultado: abre direto o jogo
        jogo = get_jogo(v.rows[0]["id"])
        emb, file = jogo_embed(jogo)
        view = DetalheView(interaction.user.id, jogo, None, nome, 0)
        await interaction.response.send_message(embed=emb, file=file or discord.utils.MISSING, view=view)
    else:
        await interaction.response.send_message(embed=v.embed(), view=v)


# ------------------------------------------------------------------ comandos de admin

admin = dict(
    default=app_commands.default_permissions(manage_guild=True),
    check=app_commands.checks.has_permissions(manage_guild=True),
)


@tree.command(name="jogo_adicionar", description="[Admin] Adiciona um jogo ao catálogo")
@app_commands.describe(
    nome="Nome do jogo",
    plataforma="Plataforma de origem do jogo",
    link="Link de download",
    capa="Imagem de capa (quanto maior, melhor)",
    descricao="Descrição do jogo (opcional)",
    tutorial="Link de tutorial de instalação (opcional)",
)
@app_commands.choices(plataforma=PLAT_CHOICES)
@app_commands.guild_only()
@admin["default"]
@admin["check"]
async def jogo_adicionar(
    interaction: discord.Interaction,
    nome: str,
    plataforma: app_commands.Choice[str],
    link: str,
    capa: discord.Attachment,
    descricao: str = None,
    tutorial: str = None,
):
    await interaction.response.defer(ephemeral=True)
    if not link_valido(link) or (tutorial and not link_valido(tutorial)):
        await interaction.followup.send("❌ Os links precisam começar com http:// ou https://")
        return
    path = await salvar_capa(capa)
    if not path:
        await interaction.followup.send("❌ O arquivo da capa precisa ser uma imagem (png, jpg, webp...).")
        return
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO jogos (nome, plataforma, descricao, link, tutorial, capa) VALUES (?, ?, ?, ?, ?, ?)",
            (nome.strip(), plataforma.value, descricao, link.strip(), tutorial.strip() if tutorial else None, path),
        )
    jogo = get_jogo(cur.lastrowid)
    emb, file = jogo_embed(jogo)
    await interaction.followup.send("✅ Jogo adicionado! Prévia:", embed=emb, file=file or discord.utils.MISSING)


@tree.command(name="jogo_editar", description="[Admin] Edita um jogo do catálogo")
@app_commands.describe(
    jogo="Jogo a editar",
    tutorial="Novo link de tutorial (use - para remover)",
    capa="Nova imagem de capa",
)
@app_commands.choices(plataforma=PLAT_CHOICES)
@app_commands.autocomplete(jogo=jogo_autocomplete_id)
@app_commands.guild_only()
@admin["default"]
@admin["check"]
async def jogo_editar(
    interaction: discord.Interaction,
    jogo: str,
    nome: str = None,
    plataforma: app_commands.Choice[str] = None,
    descricao: str = None,
    link: str = None,
    tutorial: str = None,
    capa: discord.Attachment = None,
):
    await interaction.response.defer(ephemeral=True)
    atual = get_jogo(int(jogo)) if jogo.isdigit() else None
    if not atual:
        await interaction.followup.send("❌ Escolha um jogo da lista de sugestões.")
        return
    campos = {}
    if nome:
        campos["nome"] = nome.strip()
    if plataforma:
        campos["plataforma"] = plataforma.value
    if descricao:
        campos["descricao"] = descricao
    if link:
        if not link_valido(link):
            await interaction.followup.send("❌ Link de download inválido.")
            return
        campos["link"] = link.strip()
    if tutorial:
        if tutorial.strip() == "-":
            campos["tutorial"] = None
        elif link_valido(tutorial):
            campos["tutorial"] = tutorial.strip()
        else:
            await interaction.followup.send("❌ Link de tutorial inválido.")
            return
    if capa:
        novo = await salvar_capa(capa)
        if not novo:
            await interaction.followup.send("❌ A capa precisa ser uma imagem.")
            return
        apagar_arquivo(atual["capa"])
        campos["capa"] = novo
    if not campos:
        await interaction.followup.send("Nada para alterar.")
        return
    sets = ", ".join(f"{k} = ?" for k in campos)  # chaves vêm do código, não do usuário
    with db() as conn:
        conn.execute(f"UPDATE jogos SET {sets} WHERE id = ?", list(campos.values()) + [atual["id"]])
    emb, file = jogo_embed(get_jogo(atual["id"]))
    await interaction.followup.send("✅ Jogo atualizado! Prévia:", embed=emb, file=file or discord.utils.MISSING)


@tree.command(name="jogo_remover", description="[Admin] Remove um jogo do catálogo")
@app_commands.describe(jogo="Jogo a remover")
@app_commands.autocomplete(jogo=jogo_autocomplete_id)
@app_commands.guild_only()
@admin["default"]
@admin["check"]
async def jogo_remover(interaction: discord.Interaction, jogo: str):
    atual = get_jogo(int(jogo)) if jogo.isdigit() else None
    if not atual:
        await interaction.response.send_message("❌ Escolha um jogo da lista de sugestões.", ephemeral=True)
        return
    with db() as conn:
        conn.execute("DELETE FROM jogos WHERE id = ?", (atual["id"],))
    apagar_arquivo(atual["capa"])
    await interaction.response.send_message(f"🗑 **{atual['nome']}** removido do catálogo.", ephemeral=True)


@bot.event
async def on_ready():
    print(f"Bot online como {bot.user}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Defina a variável de ambiente DISCORD_TOKEN.")
    init_db()
    bot.run(TOKEN)
