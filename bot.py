"""
Bot de catálogo de jogos para Discord, tudo por botões.

ÚNICO COMANDO:  /catalogo  (admin) -> publica o catálogo no canal onde for usado.

A mensagem publicada tem 3 botões:
  📚 Ver catálogo  -> filtro por plataforma, paginação e página do jogo (capa grande, download, tutorial)
  🔎 Buscar jogo   -> busca pelo nome
  🛠 Admin         -> só para quem tem "Gerenciar Servidor": adicionar, editar e remover jogos

Variáveis de ambiente:
  DISCORD_TOKEN = token do bot
  GUILD_ID      = (opcional) ID do servidor, para o comando aparecer na hora

Instalação:  pip install -U discord.py   (já inclui o aiohttp)
Execução:    python bot.py
"""
import math
import os
import sqlite3
import uuid

import aiohttp
import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = os.getenv("GUILD_ID")
DB_PATH = "jogos.db"
CAPAS_DIR = "capas"
PAGE_SIZE = 8
MAX_CAPA_BYTES = 8 * 1024 * 1024

# Console de origem do jogo (máximo 24 itens, limite do Discord para menus)
PLATAFORMAS = {
    "PlayStation 1": "🟦",
    "PlayStation 2": "🟦",
    "PlayStation 3": "🟦",
    "PlayStation 4": "🟦",
    "PlayStation 5": "🟦",
    "PSP": "🟦",
    "PS Vita": "🟦",
    "Xbox": "🟩",
    "Xbox 360": "🟩",
    "Xbox One / Series": "🟩",
    "NES": "🟥",
    "Super Nintendo": "🟥",
    "Nintendo 64": "🟥",
    "GameCube": "🟥",
    "Wii": "🟥",
    "Wii U": "🟥",
    "Switch": "🟥",
    "Game Boy / GBA": "🟥",
    "DS / 3DS": "🟥",
    "Mega Drive": "🟪",
    "Sega Saturn": "🟪",
    "Dreamcast": "🟪",
    "Outra": "🌐",
}

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


def apagar_arquivo(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


EXT_POR_TIPO = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}


async def baixar_capa(url):
    """Baixa a imagem do link e guarda uma cópia local. Retorna (caminho, erro)."""
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                if r.status != 200:
                    return None, f"o link da capa respondeu com erro {r.status}"
                tipo = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if not tipo.startswith("image/"):
                    return None, "o link da capa não é uma imagem direta (precisa terminar em .jpg, .png, .webp...)"
                data = await r.read()
    except Exception:
        return None, "não consegui baixar a imagem desse link"
    if len(data) > MAX_CAPA_BYTES:
        return None, "a imagem é grande demais (máximo 8 MB)"
    path = os.path.join(CAPAS_DIR, f"{uuid.uuid4().hex}{EXT_POR_TIPO.get(tipo, '.png')}")
    with open(path, "wb") as f:
        f.write(data)
    return path, None


def eh_admin(interaction: discord.Interaction):
    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild


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
        emb.set_image(url=f"attachment://{fname}")  # capa grande
    return emb, file


def main_embed():
    return discord.Embed(
        title="🎮 Catálogo de Jogos",
        description=(
            "Use os botões abaixo:\n\n"
            "📚 **Ver catálogo** – navegue e filtre por plataforma\n"
            "🔎 **Buscar jogo** – procure pelo nome"
        ),
        color=discord.Color.blurple(),
    )


def admin_embed():
    return discord.Embed(title="🛠 Painel de administração", description="Escolha uma ação:", color=discord.Color.orange())


def send_kwargs(emb, file, view):
    return dict(embed=emb, view=view, **({"file": file} if file else {}))


# ------------------------------------------------------------------ catálogo (navegação)


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
    def __init__(self, rows, mode):
        opts = [
            discord.SelectOption(
                label=r["nome"][:100], value=str(r["id"]),
                description=r["plataforma"], emoji=PLATAFORMAS.get(r["plataforma"], "🎮"),
            )
            for r in rows
        ]
        placeholder = {"ver": "Abrir um jogo...", "editar": "Escolha o jogo para editar...",
                       "remover": "Escolha o jogo para remover..."}[mode]
        super().__init__(placeholder=placeholder, options=opts, row=1)

    async def callback(self, interaction: discord.Interaction):
        v: CatalogView = self.view
        jogo = get_jogo(int(self.values[0]))
        if not jogo:
            await interaction.response.send_message("Esse jogo não existe mais.", ephemeral=True)
            return
        emb, file = jogo_embed(jogo)
        if v.mode == "ver":
            view = DetalheView(jogo, v.estado())
        elif v.mode == "editar":
            view = EditarView(jogo, v.estado())
        else:
            emb.title = f"🗑 Remover: {jogo['nome']}?"
            view = RemoverView(jogo, v.estado())
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


class PainelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Painel", emoji="🛠", style=discord.ButtonStyle.primary, row=2)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=admin_embed(), attachments=[], view=AdminView())


class CatalogView(discord.ui.View):
    def __init__(self, mode="ver", plataforma=None, query=None, page=0):
        super().__init__(timeout=900)
        self.mode, self.plataforma, self.query, self.page = mode, plataforma, query, page
        self.rows, self.total = [], 0
        self.build()

    def estado(self):
        return dict(mode=self.mode, plataforma=self.plataforma, query=self.query, page=self.page)

    def build(self):
        self.clear_items()
        self.rows, self.total = listar(self.plataforma, self.query, self.page)
        pages = max(1, math.ceil(self.total / PAGE_SIZE))
        self.add_item(PlataformaSelect(self.plataforma))
        if self.rows:
            self.add_item(JogoSelect(self.rows, self.mode))
        self.add_item(PageButton(-1, self.page <= 0))
        self.add_item(PageButton(+1, self.page >= pages - 1))
        if self.mode != "ver":
            self.add_item(PainelButton())

    def embed(self):
        pages = max(1, math.ceil(self.total / PAGE_SIZE))
        titulo = {"ver": "📚 Catálogo de jogos", "editar": "✏️ Editar jogo", "remover": "🗑 Remover jogo"}[self.mode]
        if self.query:
            titulo += f" • busca: {self.query}"
        if self.plataforma:
            titulo += f" • {self.plataforma}"
        if self.rows:
            desc = "\n".join(
                f"{PLATAFORMAS.get(r['plataforma'], '🎮')} **{r['nome']}** — {r['plataforma']}" for r in self.rows
            )
            if self.mode == "ver":
                desc += "\n\nEscolha um jogo no menu abaixo para ver a capa e o download."
        else:
            desc = "Nenhum jogo encontrado."
        emb = discord.Embed(title=titulo, description=desc, color=discord.Color.blurple())
        emb.set_footer(text=f"Página {self.page + 1}/{pages} • {self.total} jogo(s)")
        return emb

    async def atualizar(self, interaction: discord.Interaction):
        self.build()
        await interaction.response.edit_message(embed=self.embed(), attachments=[], view=self)


class DetalheView(discord.ui.View):
    def __init__(self, jogo, estado):
        super().__init__(timeout=900)
        self.estado = estado
        self.add_item(discord.ui.Button(label="Baixar", emoji="⬇️", style=discord.ButtonStyle.link, url=jogo["link"]))
        if jogo["tutorial"]:
            self.add_item(discord.ui.Button(label="Tutorial", emoji="📖", style=discord.ButtonStyle.link, url=jogo["tutorial"]))
        voltar = discord.ui.Button(label="Voltar", emoji="⬅️", style=discord.ButtonStyle.secondary)
        voltar.callback = self.voltar
        self.add_item(voltar)

    async def voltar(self, interaction: discord.Interaction):
        v = CatalogView(**self.estado)
        await interaction.response.edit_message(embed=v.embed(), attachments=[], view=v)


# ------------------------------------------------------------------ busca


class BuscaModal(discord.ui.Modal, title="Buscar jogo"):
    nome = discord.ui.TextInput(label="Nome do jogo", max_length=100, placeholder="Ex: Resident Evil")

    async def on_submit(self, interaction: discord.Interaction):
        termo = self.nome.value.strip()
        v = CatalogView(query=termo)
        if v.total == 1:
            jogo = get_jogo(v.rows[0]["id"])
            emb, file = jogo_embed(jogo)
            await interaction.response.send_message(
                ephemeral=True, **send_kwargs(emb, file, DetalheView(jogo, v.estado()))
            )
        else:
            await interaction.response.send_message(embed=v.embed(), view=v, ephemeral=True)


# ------------------------------------------------------------------ painel admin


class AdminView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)

    @discord.ui.button(label="Adicionar jogo", emoji="➕", style=discord.ButtonStyle.success)
    async def adicionar(self, interaction: discord.Interaction, button: discord.ui.Button):
        emb = discord.Embed(
            title="➕ Adicionar jogo",
            description="Primeiro escolha a **plataforma de origem** do jogo. Depois abrirá um formulário.",
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=emb, attachments=[], view=PlataformaPickView())

    @discord.ui.button(label="Editar jogo", emoji="✏️", style=discord.ButtonStyle.primary)
    async def editar(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = CatalogView(mode="editar")
        await interaction.response.edit_message(embed=v.embed(), attachments=[], view=v)

    @discord.ui.button(label="Remover jogo", emoji="🗑", style=discord.ButtonStyle.danger)
    async def remover(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = CatalogView(mode="remover")
        await interaction.response.edit_message(embed=v.embed(), attachments=[], view=v)


class PlataformaPickView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)
        opts = [discord.SelectOption(label=n, value=n, emoji=e) for n, e in PLATAFORMAS.items()]
        sel = discord.ui.Select(placeholder="Plataforma de origem do jogo", options=opts, row=0)
        sel.callback = self.escolheu
        self.select = sel
        self.add_item(sel)
        voltar = discord.ui.Button(label="Voltar", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
        voltar.callback = self.voltar
        self.add_item(voltar)

    async def escolheu(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddModal(self.select.values[0]))

    async def voltar(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=admin_embed(), view=AdminView())


class AddModal(discord.ui.Modal):
    def __init__(self, plataforma):
        super().__init__(title=f"Novo jogo • {plataforma}"[:45])
        self.plataforma = plataforma
        self.nome = discord.ui.TextInput(label="Nome do jogo", max_length=100)
        self.link = discord.ui.TextInput(label="Link de download", max_length=500, placeholder="https://...")
        self.capa = discord.ui.TextInput(label="Link da imagem de capa", max_length=500,
                                         placeholder="Link direto da imagem (.jpg, .png, .webp)")
        self.tutorial = discord.ui.TextInput(label="Link do tutorial (opcional)", required=False, max_length=500)
        self.descricao = discord.ui.TextInput(label="Descrição (opcional)", required=False,
                                              style=discord.TextStyle.paragraph, max_length=1500)
        for item in (self.nome, self.link, self.capa, self.tutorial, self.descricao):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Sem permissão.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        link, tutorial, capa = self.link.value.strip(), self.tutorial.value.strip(), self.capa.value.strip()
        if not link_valido(link) or (tutorial and not link_valido(tutorial)) or not link_valido(capa):
            await interaction.followup.send("❌ Todos os links precisam começar com http:// ou https://", ephemeral=True)
            return
        path, erro = await baixar_capa(capa)
        if erro:
            await interaction.followup.send(f"❌ Capa: {erro}.", ephemeral=True)
            return
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO jogos (nome, plataforma, descricao, link, tutorial, capa) VALUES (?, ?, ?, ?, ?, ?)",
                (self.nome.value.strip(), self.plataforma, self.descricao.value.strip() or None,
                 link, tutorial or None, path),
            )
        emb, file = jogo_embed(get_jogo(cur.lastrowid))
        await interaction.followup.send("✅ Jogo adicionado! Prévia:", ephemeral=True, **send_kwargs(emb, file, AdminView()))


class EditarView(discord.ui.View):
    def __init__(self, jogo, estado):
        super().__init__(timeout=900)
        self.jogo, self.estado = jogo, estado
        opts = [discord.SelectOption(label=n, value=n, emoji=e, default=(n == jogo["plataforma"]))
                for n, e in PLATAFORMAS.items()]
        sel = discord.ui.Select(placeholder="Mudar plataforma de origem", options=opts, row=0)
        sel.callback = self.mudar_plataforma
        self.select = sel
        self.add_item(sel)
        ed = discord.ui.Button(label="Editar dados", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
        ed.callback = self.abrir_modal
        self.add_item(ed)
        voltar = discord.ui.Button(label="Voltar", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
        voltar.callback = self.voltar
        self.add_item(voltar)

    async def mudar_plataforma(self, interaction: discord.Interaction):
        with db() as conn:
            conn.execute("UPDATE jogos SET plataforma = ? WHERE id = ?", (self.select.values[0], self.jogo["id"]))
        jogo = get_jogo(self.jogo["id"])
        emb, file = jogo_embed(jogo)
        await interaction.response.edit_message(
            embed=emb, attachments=[file] if file else [], view=EditarView(jogo, self.estado))

    async def abrir_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EditModal(get_jogo(self.jogo["id"])))

    async def voltar(self, interaction: discord.Interaction):
        v = CatalogView(**self.estado)
        await interaction.response.edit_message(embed=v.embed(), attachments=[], view=v)


class EditModal(discord.ui.Modal):
    def __init__(self, jogo):
        super().__init__(title=f"Editar • {jogo['nome']}"[:45])
        self.jogo = jogo
        self.nome = discord.ui.TextInput(label="Nome do jogo", max_length=100, default=jogo["nome"])
        self.link = discord.ui.TextInput(label="Link de download", max_length=500, default=jogo["link"])
        self.capa = discord.ui.TextInput(label="Nova capa (deixe vazio para manter)", required=False, max_length=500,
                                         placeholder="Link direto de uma nova imagem")
        self.tutorial = discord.ui.TextInput(label="Link do tutorial (vazio = sem tutorial)", required=False,
                                             max_length=500, default=jogo["tutorial"] or "")
        self.descricao = discord.ui.TextInput(label="Descrição", required=False, style=discord.TextStyle.paragraph,
                                              max_length=1500, default=(jogo["descricao"] or "")[:1500])
        for item in (self.nome, self.link, self.capa, self.tutorial, self.descricao):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Sem permissão.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        link, tutorial, capa = self.link.value.strip(), self.tutorial.value.strip(), self.capa.value.strip()
        if not link_valido(link) or (tutorial and not link_valido(tutorial)) or (capa and not link_valido(capa)):
            await interaction.followup.send("❌ Todos os links precisam começar com http:// ou https://", ephemeral=True)
            return
        nova_capa = self.jogo["capa"]
        if capa:
            path, erro = await baixar_capa(capa)
            if erro:
                await interaction.followup.send(f"❌ Capa: {erro}.", ephemeral=True)
                return
            apagar_arquivo(self.jogo["capa"])
            nova_capa = path
        with db() as conn:
            conn.execute(
                "UPDATE jogos SET nome = ?, link = ?, tutorial = ?, descricao = ?, capa = ? WHERE id = ?",
                (self.nome.value.strip(), link, tutorial or None, self.descricao.value.strip() or None,
                 nova_capa, self.jogo["id"]),
            )
        emb, file = jogo_embed(get_jogo(self.jogo["id"]))
        await interaction.followup.send("✅ Jogo atualizado! Prévia:", ephemeral=True, **send_kwargs(emb, file, AdminView()))


class RemoverView(discord.ui.View):
    def __init__(self, jogo, estado):
        super().__init__(timeout=900)
        self.jogo, self.estado = jogo, estado
        ok = discord.ui.Button(label="Confirmar remoção", emoji="🗑", style=discord.ButtonStyle.danger)
        ok.callback = self.confirmar
        self.add_item(ok)
        cancel = discord.ui.Button(label="Cancelar", emoji="⬅️", style=discord.ButtonStyle.secondary)
        cancel.callback = self.cancelar
        self.add_item(cancel)

    async def confirmar(self, interaction: discord.Interaction):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Sem permissão.", ephemeral=True)
            return
        with db() as conn:
            conn.execute("DELETE FROM jogos WHERE id = ?", (self.jogo["id"],))
        apagar_arquivo(self.jogo["capa"])
        emb = discord.Embed(title="🗑 Jogo removido", description=f"**{self.jogo['nome']}** saiu do catálogo.",
                            color=discord.Color.red())
        await interaction.response.edit_message(embed=emb, attachments=[], view=AdminView())

    async def cancelar(self, interaction: discord.Interaction):
        v = CatalogView(**self.estado)
        await interaction.response.edit_message(embed=v.embed(), attachments=[], view=v)


# ------------------------------------------------------------------ mensagem principal (persistente)


class MainView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # não expira; continua funcionando após reiniciar o bot

    @discord.ui.button(label="Ver catálogo", emoji="📚", style=discord.ButtonStyle.primary, custom_id="catalogo:ver")
    async def ver(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = CatalogView()
        await interaction.response.send_message(embed=v.embed(), view=v, ephemeral=True)

    @discord.ui.button(label="Buscar jogo", emoji="🔎", style=discord.ButtonStyle.secondary, custom_id="catalogo:buscar")
    async def buscar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BuscaModal())

    @discord.ui.button(label="Admin", emoji="🛠", style=discord.ButtonStyle.secondary, custom_id="catalogo:admin")
    async def admin(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Só administradores (permissão **Gerenciar Servidor**).", ephemeral=True)
            return
        await interaction.response.send_message(embed=admin_embed(), view=AdminView(), ephemeral=True)


# ------------------------------------------------------------------ bot


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(MainView())
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


@tree.command(name="catalogo", description="[Admin] Publica o catálogo de jogos neste canal")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def catalogo(interaction: discord.Interaction):
    await interaction.response.send_message(embed=main_embed(), view=MainView())


@bot.event
async def on_ready():
    print(f"Bot online como {bot.user}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Defina a variável de ambiente DISCORD_TOKEN.")
    init_db()
    bot.run(TOKEN)
