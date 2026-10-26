"""
Bot de catálogo de jogos para Discord, tudo por botões.

ÚNICO COMANDO:  /catalogo  (admin) -> publica o catálogo no canal onde for usado.

A mensagem publicada tem 4 botões:
  📚 Ver catálogo  -> filtro por plataforma, paginação e página do jogo (capa grande, download, ISO, tutorial)
  🔎 Buscar jogo   -> busca pelo nome
  📤 Enviar jogo   -> qualquer pessoa envia um jogo; fica PENDENTE até um moderador aprovar
  🛠 Admin         -> só para quem tem "Gerenciar Servidor": adicionar, editar, remover,
                      aprovar/rejeitar pendentes e escolher o canal de avisos

Variáveis de ambiente:
  DISCORD_TOKEN = token do bot
  GUILD_ID      = (opcional) ID do servidor, para o comando aparecer na hora

Instalação:  pip install -U "discord.py>=2.7"
Execução:    python bot.py
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
MAX_CAPA_BYTES = 8 * 1024 * 1024
MAX_PENDENTES_POR_USUARIO = 3

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
                capa TEXT,
                iso TEXT,
                status TEXT NOT NULL DEFAULT 'aprovado',
                autor_id INTEGER
            )"""
        )
        # migração: bancos criados por versões anteriores ganham as colunas novas
        existentes = {r[1] for r in conn.execute("PRAGMA table_info(jogos)")}
        for coluna, ddl in (("iso", "TEXT"), ("status", "TEXT NOT NULL DEFAULT 'aprovado'"), ("autor_id", "INTEGER")):
            if coluna not in existentes:
                conn.execute(f"ALTER TABLE jogos ADD COLUMN {coluna} {ddl}")
        conn.execute("CREATE TABLE IF NOT EXISTS config (chave TEXT PRIMARY KEY, valor TEXT)")


def get_config(chave):
    with db() as conn:
        r = conn.execute("SELECT valor FROM config WHERE chave = ?", (chave,)).fetchone()
    return r["valor"] if r else None


def set_config(chave, valor):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO config (chave, valor) VALUES (?, ?)", (chave, valor))


def listar(plataforma=None, query=None, page=0, status="aprovado"):
    where, params = ["status = ?"], [status]
    if plataforma:
        where.append("plataforma = ?")
        params.append(plataforma)
    if query:
        where.append("nome LIKE ?")
        params.append(f"%{query}%")
    clause = "WHERE " + " AND ".join(where)
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


def contar_pendentes(autor_id=None):
    with db() as conn:
        if autor_id:
            return conn.execute("SELECT COUNT(*) FROM jogos WHERE status = 'pendente' AND autor_id = ?",
                                (autor_id,)).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM jogos WHERE status = 'pendente'").fetchone()[0]


def link_valido(s):
    return s.strip().lower().startswith(("http://", "https://"))


def apagar_arquivo(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


async def salvar_capa(att: discord.Attachment):
    """Guarda uma cópia local da imagem enviada. Retorna (caminho, erro)."""
    if not (att.content_type or "").startswith("image/"):
        return None, "o arquivo enviado não é uma imagem (use png, jpg, webp...)"
    if att.size > MAX_CAPA_BYTES:
        return None, "a imagem é grande demais (máximo 8 MB)"
    ext = os.path.splitext(att.filename)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        ext = ".png"
    path = os.path.join(CAPAS_DIR, f"{uuid.uuid4().hex}{ext}")
    try:
        await att.save(path)
    except Exception:
        return None, "não consegui baixar a imagem enviada"
    return path, None


def eh_admin(interaction: discord.Interaction):
    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild


def validar_links(**links):
    """Recebe nome=valor; valores vazios são ignorados. Retorna mensagem de erro ou None."""
    for valor in links.values():
        if valor and not link_valido(valor):
            return "❌ Os links precisam começar com http:// ou https://"
    return None


# ------------------------------------------------------------------ embeds e botões de link


def jogo_embed(j):
    pendente = j["status"] == "pendente"
    emb = discord.Embed(
        title=j["nome"],
        description=(j["descricao"] or "")[:1500] or None,
        color=discord.Color.orange() if pendente else discord.Color.blurple(),
    )
    emoji = PLATAFORMAS.get(j["plataforma"], "🎮")
    emb.add_field(name="Plataforma de origem", value=f"{emoji} {j['plataforma']}", inline=True)
    if j["iso"]:
        emb.add_field(name="ISO original", value="✅ Disponível", inline=True)
    if j["tutorial"]:
        emb.add_field(name="Tutorial", value="✅ Disponível", inline=True)
    if pendente:
        if j["autor_id"]:
            emb.add_field(name="Enviado por", value=f"<@{j['autor_id']}>", inline=True)
        emb.set_footer(text="⏳ Aguardando aprovação")
    file = None
    if j["capa"] and os.path.exists(j["capa"]):
        fname = os.path.basename(j["capa"])
        file = discord.File(j["capa"], filename=fname)
        emb.set_image(url=f"attachment://{fname}")  # capa grande
    return emb, file


def link_buttons(j):
    btns = [discord.ui.Button(label="Baixar", emoji="⬇️", style=discord.ButtonStyle.link, url=j["link"])]
    if j["iso"]:
        btns.append(discord.ui.Button(label="ISO", emoji="💿", style=discord.ButtonStyle.link, url=j["iso"]))
    if j["tutorial"]:
        btns.append(discord.ui.Button(label="Tutorial", emoji="📖", style=discord.ButtonStyle.link, url=j["tutorial"]))
    return btns


def main_embed():
    return discord.Embed(
        title="🎮 Catálogo de Jogos",
        description=(
            "Use os botões abaixo:\n\n"
            "📚 **Ver catálogo** – navegue e filtre por plataforma\n"
            "🔎 **Buscar jogo** – procure pelo nome\n"
            "📤 **Enviar jogo** – sugira um jogo (passa por aprovação da moderação)"
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
    PLACEHOLDERS = {
        "ver": "Abrir um jogo...",
        "editar": "Escolha o jogo para editar...",
        "remover": "Escolha o jogo para remover...",
        "pendentes": "Escolha um envio para analisar...",
    }

    def __init__(self, rows, mode):
        opts = [
            discord.SelectOption(
                label=r["nome"][:100], value=str(r["id"]),
                description=r["plataforma"], emoji=PLATAFORMAS.get(r["plataforma"], "🎮"),
            )
            for r in rows
        ]
        super().__init__(placeholder=self.PLACEHOLDERS[mode], options=opts, row=1)

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
        elif v.mode == "remover":
            emb.title = f"🗑 Remover: {jogo['nome']}?"
            view = RemoverView(jogo, v.estado())
        else:
            view = PendenteView(jogo, v.estado())
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
    TITULOS = {"ver": "📚 Catálogo de jogos", "editar": "✏️ Editar jogo",
               "remover": "🗑 Remover jogo", "pendentes": "📥 Envios pendentes"}

    def __init__(self, mode="ver", plataforma=None, query=None, page=0):
        super().__init__(timeout=900)
        self.mode, self.plataforma, self.query, self.page = mode, plataforma, query, page
        self.status = "pendente" if mode == "pendentes" else "aprovado"
        self.rows, self.total = [], 0
        self.build()

    def estado(self):
        return dict(mode=self.mode, plataforma=self.plataforma, query=self.query, page=self.page)

    def build(self):
        self.clear_items()
        self.rows, self.total = listar(self.plataforma, self.query, self.page, self.status)
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
        titulo = self.TITULOS[self.mode]
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
        elif self.mode == "pendentes":
            desc = "Nenhum envio aguardando aprovação 🎉"
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
        for b in link_buttons(jogo):
            self.add_item(b)
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


# ------------------------------------------------------------------ enviar / adicionar jogo


class PlataformaPickView(discord.ui.View):
    """Escolha da plataforma. publico=True: envio de usuário (vai para moderação)."""

    def __init__(self, publico=False):
        super().__init__(timeout=900)
        self.publico = publico
        opts = [discord.SelectOption(label=n, value=n, emoji=e) for n, e in PLATAFORMAS.items()]
        sel = discord.ui.Select(placeholder="Plataforma de origem do jogo", options=opts, row=0)
        sel.callback = self.escolheu
        self.select = sel
        self.add_item(sel)
        if not publico:
            voltar = discord.ui.Button(label="Voltar", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
            voltar.callback = self.voltar
            self.add_item(voltar)

    async def escolheu(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddModal(self.select.values[0], self.publico))

    async def voltar(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=admin_embed(), view=AdminView())


class AddModal(discord.ui.Modal):
    def __init__(self, plataforma, publico=False):
        super().__init__(title=f"{'Enviar' if publico else 'Novo'} jogo • {plataforma}"[:45])
        self.plataforma, self.publico = plataforma, publico
        self.nome = discord.ui.TextInput(label="Nome do jogo", max_length=100)
        self.link = discord.ui.TextInput(label="Link de download do jogo", max_length=500, placeholder="https://...")
        self.iso = discord.ui.TextInput(label="Link da ISO original (opcional)", required=False, max_length=500)
        self.tutorial = discord.ui.TextInput(label="Link do tutorial (opcional)", required=False, max_length=500)
        self.capa = discord.ui.FileUpload(required=True, min_values=1, max_values=1)
        self.add_item(self.nome)
        self.add_item(self.link)
        self.add_item(self.iso)
        self.add_item(self.tutorial)
        self.add_item(discord.ui.Label(text="Imagem de capa (quanto maior, melhor)", component=self.capa))

    async def on_submit(self, interaction: discord.Interaction):
        if not self.publico and not eh_admin(interaction):
            await interaction.response.send_message("⛔ Sem permissão.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if self.publico and contar_pendentes(interaction.user.id) >= MAX_PENDENTES_POR_USUARIO:
            await interaction.followup.send(
                f"❌ Você já tem {MAX_PENDENTES_POR_USUARIO} envios aguardando aprovação. "
                "Espere a moderação analisar antes de enviar mais.", ephemeral=True)
            return
        link, iso, tutorial = self.link.value.strip(), self.iso.value.strip(), self.tutorial.value.strip()
        erro = validar_links(link=link, iso=iso, tutorial=tutorial)
        if erro:
            await interaction.followup.send(erro, ephemeral=True)
            return
        if not self.capa.values:
            await interaction.followup.send("❌ Envie a imagem de capa.", ephemeral=True)
            return
        path, erro = await salvar_capa(self.capa.values[0])
        if erro:
            await interaction.followup.send(f"❌ Capa: {erro}.", ephemeral=True)
            return
        status = "pendente" if self.publico else "aprovado"
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO jogos (nome, plataforma, link, iso, tutorial, capa, status, autor_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self.nome.value.strip(), self.plataforma, link, iso or None, tutorial or None,
                 path, status, interaction.user.id),
            )
        jogo = get_jogo(cur.lastrowid)
        emb, file = jogo_embed(jogo)
        if self.publico:
            await avisar_moderacao(jogo)
            await interaction.followup.send(
                "✅ Jogo enviado! Ele aparecerá no catálogo assim que a moderação aprovar. Prévia:",
                ephemeral=True, **send_kwargs(emb, file, discord.utils.MISSING))
        else:
            await interaction.followup.send("✅ Jogo adicionado! Prévia:", ephemeral=True,
                                            **send_kwargs(emb, file, AdminView()))


# ------------------------------------------------------------------ moderação


async def avisar_moderacao(jogo):
    canal_id = get_config("canal_avisos")
    if not canal_id:
        return
    try:
        canal = bot.get_channel(int(canal_id)) or await bot.fetch_channel(int(canal_id))
        emb, file = jogo_embed(jogo)
        kw = {"file": file} if file else {}
        await canal.send(
            content="📥 **Novo jogo aguardando aprovação!** Abra o botão 🛠 Admin → **Pendentes** para analisar.",
            embed=emb, **kw)
    except Exception as e:
        print("Não consegui avisar a moderação:", repr(e))


async def avisar_autor(jogo, texto):
    if not jogo["autor_id"]:
        return
    try:
        user = bot.get_user(jogo["autor_id"]) or await bot.fetch_user(jogo["autor_id"])
        await user.send(texto)
    except Exception:
        pass  # DMs fechadas, tudo bem


class VoltarPendentesView(discord.ui.View):
    def __init__(self, estado):
        super().__init__(timeout=900)
        self.estado = estado
        b = discord.ui.Button(label="Voltar aos pendentes", emoji="⬅️", style=discord.ButtonStyle.secondary)
        b.callback = self.voltar
        self.add_item(b)

    async def voltar(self, interaction: discord.Interaction):
        v = CatalogView(**self.estado)
        await interaction.response.edit_message(embed=v.embed(), attachments=[], view=v)


class PendenteView(discord.ui.View):
    def __init__(self, jogo, estado):
        super().__init__(timeout=900)
        self.jogo, self.estado = jogo, estado
        for b in link_buttons(jogo):  # para o moderador conferir os links
            b.row = 0
            self.add_item(b)
        ok = discord.ui.Button(label="Aprovar", emoji="✅", style=discord.ButtonStyle.success, row=1)
        ok.callback = self.aprovar
        self.add_item(ok)
        no = discord.ui.Button(label="Rejeitar", emoji="❌", style=discord.ButtonStyle.danger, row=1)
        no.callback = self.rejeitar
        self.add_item(no)
        voltar = discord.ui.Button(label="Voltar", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
        voltar.callback = self.voltar
        self.add_item(voltar)

    async def aprovar(self, interaction: discord.Interaction):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Sem permissão.", ephemeral=True)
            return
        with db() as conn:
            conn.execute("UPDATE jogos SET status = 'aprovado' WHERE id = ?", (self.jogo["id"],))
        await avisar_autor(self.jogo, f"✅ Seu jogo **{self.jogo['nome']}** foi aprovado e já está no catálogo!")
        emb = discord.Embed(title="✅ Jogo aprovado", description=f"**{self.jogo['nome']}** já está no catálogo.",
                            color=discord.Color.green())
        await interaction.response.edit_message(embed=emb, attachments=[], view=VoltarPendentesView(self.estado))

    async def rejeitar(self, interaction: discord.Interaction):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Sem permissão.", ephemeral=True)
            return
        with db() as conn:
            conn.execute("DELETE FROM jogos WHERE id = ?", (self.jogo["id"],))
        apagar_arquivo(self.jogo["capa"])
        await avisar_autor(self.jogo, f"❌ Seu envio **{self.jogo['nome']}** não foi aprovado pela moderação.")
        emb = discord.Embed(title="❌ Envio rejeitado", description=f"**{self.jogo['nome']}** foi removido.",
                            color=discord.Color.red())
        await interaction.response.edit_message(embed=emb, attachments=[], view=VoltarPendentesView(self.estado))

    async def voltar(self, interaction: discord.Interaction):
        v = CatalogView(**self.estado)
        await interaction.response.edit_message(embed=v.embed(), attachments=[], view=v)


class CanalView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)
        sel = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text],
                                       placeholder="Escolha o canal de avisos", row=0)
        sel.callback = self.escolheu
        self.sel = sel
        self.add_item(sel)
        voltar = discord.ui.Button(label="Voltar", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
        voltar.callback = self.voltar
        self.add_item(voltar)

    async def escolheu(self, interaction: discord.Interaction):
        canal = self.sel.values[0]
        set_config("canal_avisos", str(canal.id))
        emb = discord.Embed(title="📢 Canal de avisos definido",
                            description=f"Novos envios serão avisados em {canal.mention}.",
                            color=discord.Color.green())
        await interaction.response.edit_message(embed=emb, view=AdminView())

    async def voltar(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=admin_embed(), view=AdminView())


# ------------------------------------------------------------------ painel admin


class AdminView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)
        n = contar_pendentes()
        self.pendentes.label = f"Pendentes ({n})"
        if n:
            self.pendentes.style = discord.ButtonStyle.success

    @discord.ui.button(label="Adicionar jogo", emoji="➕", style=discord.ButtonStyle.success, row=0)
    async def adicionar(self, interaction: discord.Interaction, button: discord.ui.Button):
        emb = discord.Embed(
            title="➕ Adicionar jogo",
            description="Primeiro escolha a **plataforma de origem** do jogo. Depois abrirá um formulário.",
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=emb, attachments=[], view=PlataformaPickView())

    @discord.ui.button(label="Editar jogo", emoji="✏️", style=discord.ButtonStyle.primary, row=0)
    async def editar(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = CatalogView(mode="editar")
        await interaction.response.edit_message(embed=v.embed(), attachments=[], view=v)

    @discord.ui.button(label="Remover jogo", emoji="🗑", style=discord.ButtonStyle.danger, row=0)
    async def remover(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = CatalogView(mode="remover")
        await interaction.response.edit_message(embed=v.embed(), attachments=[], view=v)

    @discord.ui.button(label="Pendentes", emoji="📥", style=discord.ButtonStyle.secondary, row=1)
    async def pendentes(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = CatalogView(mode="pendentes")
        await interaction.response.edit_message(embed=v.embed(), attachments=[], view=v)

    @discord.ui.button(label="Canal de avisos", emoji="📢", style=discord.ButtonStyle.secondary, row=1)
    async def canal(self, interaction: discord.Interaction, button: discord.ui.Button):
        atual = get_config("canal_avisos")
        texto = (f"Canal atual: <#{atual}>\n\n" if atual else "Nenhum canal definido ainda.\n\n")
        texto += "Escolha onde o bot deve avisar quando alguém enviar um jogo para aprovação."
        emb = discord.Embed(title="📢 Canal de avisos", description=texto, color=discord.Color.orange())
        await interaction.response.edit_message(embed=emb, attachments=[], view=CanalView())


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
        ds = discord.ui.Button(label="Descrição", emoji="📝", style=discord.ButtonStyle.primary, row=1)
        ds.callback = self.abrir_descricao
        self.add_item(ds)
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

    async def abrir_descricao(self, interaction: discord.Interaction):
        await interaction.response.send_modal(DescricaoModal(get_jogo(self.jogo["id"])))

    async def voltar(self, interaction: discord.Interaction):
        v = CatalogView(**self.estado)
        await interaction.response.edit_message(embed=v.embed(), attachments=[], view=v)


class EditModal(discord.ui.Modal):
    def __init__(self, jogo):
        super().__init__(title=f"Editar • {jogo['nome']}"[:45])
        self.jogo = jogo
        self.nome = discord.ui.TextInput(label="Nome do jogo", max_length=100, default=jogo["nome"])
        self.link = discord.ui.TextInput(label="Link de download do jogo", max_length=500, default=jogo["link"])
        self.iso = discord.ui.TextInput(label="Link da ISO (vazio = sem ISO)", required=False, max_length=500,
                                        default=jogo["iso"] or "")
        self.tutorial = discord.ui.TextInput(label="Link do tutorial (vazio = sem tutorial)", required=False,
                                             max_length=500, default=jogo["tutorial"] or "")
        self.capa = discord.ui.FileUpload(required=False, min_values=0, max_values=1)
        self.add_item(self.nome)
        self.add_item(self.link)
        self.add_item(self.iso)
        self.add_item(self.tutorial)
        self.add_item(discord.ui.Label(text="Nova capa (opcional, vazio = manter)", component=self.capa))

    async def on_submit(self, interaction: discord.Interaction):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Sem permissão.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        link, iso, tutorial = self.link.value.strip(), self.iso.value.strip(), self.tutorial.value.strip()
        erro = validar_links(link=link, iso=iso, tutorial=tutorial)
        if erro:
            await interaction.followup.send(erro, ephemeral=True)
            return
        nova_capa = self.jogo["capa"]
        if self.capa.values:
            path, erro = await salvar_capa(self.capa.values[0])
            if erro:
                await interaction.followup.send(f"❌ Capa: {erro}.", ephemeral=True)
                return
            apagar_arquivo(self.jogo["capa"])
            nova_capa = path
        with db() as conn:
            conn.execute(
                "UPDATE jogos SET nome = ?, link = ?, iso = ?, tutorial = ?, capa = ? WHERE id = ?",
                (self.nome.value.strip(), link, iso or None, tutorial or None, nova_capa, self.jogo["id"]),
            )
        emb, file = jogo_embed(get_jogo(self.jogo["id"]))
        await interaction.followup.send("✅ Jogo atualizado! Prévia:", ephemeral=True, **send_kwargs(emb, file, AdminView()))


class DescricaoModal(discord.ui.Modal):
    def __init__(self, jogo):
        super().__init__(title=f"Descrição • {jogo['nome']}"[:45])
        self.jogo = jogo
        self.descricao = discord.ui.TextInput(label="Descrição", required=False, style=discord.TextStyle.paragraph,
                                              max_length=1500, default=(jogo["descricao"] or "")[:1500])
        self.add_item(self.descricao)

    async def on_submit(self, interaction: discord.Interaction):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Sem permissão.", ephemeral=True)
            return
        with db() as conn:
            conn.execute("UPDATE jogos SET descricao = ? WHERE id = ?",
                         (self.descricao.value.strip() or None, self.jogo["id"]))
        await interaction.response.send_message("✅ Descrição atualizada.", ephemeral=True)


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

    @discord.ui.button(label="Enviar jogo", emoji="📤", style=discord.ButtonStyle.success, custom_id="catalogo:enviar")
    async def enviar(self, interaction: discord.Interaction, button: discord.ui.Button):
        emb = discord.Embed(
            title="📤 Enviar um jogo",
            description=(
                "Escolha a **plataforma de origem** do jogo e preencha o formulário.\n"
                "Seu envio vai para a **moderação** e só aparece no catálogo depois de aprovado.\n\n"
                "⚠️ Links de jogos ou ISOs piratas serão rejeitados."
            ),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=emb, view=PlataformaPickView(publico=True), ephemeral=True)

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
    if not hasattr(discord.ui, "FileUpload"):
        raise SystemExit(f"discord.py {discord.__version__} é antigo demais. Instale a 2.7 ou mais nova: "
                         "coloque discord.py>=2.7 no requirements.txt.")
    init_db()
    bot.run(TOKEN)
