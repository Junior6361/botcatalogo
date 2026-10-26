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
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageOps

import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = os.getenv("GUILD_ID")
DB_PATH = "jogos.db"
CAPAS_DIR = "capas"
MODS_DIR = "capas_mods"
PAGE_SIZE = 8  # 4 colunas x 2 linhas na grade visual
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
    os.makedirs(MODS_DIR, exist_ok=True)
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
        # Catálogo de mods separado do catálogo de jogos.
        conn.execute("""CREATE TABLE IF NOT EXISTS mods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            jogo_nome TEXT NOT NULL,
            mod_nome TEXT NOT NULL,
            descricao TEXT,
            imagem TEXT,
            link TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'aprovado',
            autor_id INTEGER,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        colunas_mods = {r[1] for r in conn.execute("PRAGMA table_info(mods)")}
        for coluna, ddl in (("status", "TEXT NOT NULL DEFAULT 'aprovado'"), ("autor_id", "INTEGER")):
            if coluna not in colunas_mods:
                conn.execute(f"ALTER TABLE mods ADD COLUMN {coluna} {ddl}")


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
            f"SELECT id, nome, plataforma, capa FROM jogos {clause} "
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
            "📤 **Enviar jogo** – sugira um jogo (passa por aprovação da moderação)\n"
            "🧩 **Catálogo de mods** – veja mods separados dos jogos"
        ),
        color=discord.Color.blurple(),
    )


def admin_embed():
    return discord.Embed(title="🛠 Painel de administração", description="Escolha uma ação:", color=discord.Color.orange())


def send_kwargs(emb, file, view):
    return dict(embed=emb, view=view, **({"file": file} if file else {}))


# ------------------------------------------------------------------ grade visual do catálogo


def criar_grade_capas(rows):
    """Monta uma grade 4x2 com capas e nomes para anexar à mensagem do Discord."""
    largura_capa, altura_capa = 180, 240
    margem, espacamento, faixa_titulo = 18, 14, 58
    colunas = 4
    linhas = max(1, math.ceil(len(rows) / colunas))
    largura = margem * 2 + colunas * largura_capa + (colunas - 1) * espacamento
    altura_cartao = altura_capa + faixa_titulo
    altura = margem * 2 + linhas * altura_cartao + (linhas - 1) * espacamento
    fundo = (32, 34, 39)
    cartao = (45, 48, 54)
    img = Image.new("RGB", (largura, altura), fundo)
    draw = ImageDraw.Draw(img)

    try:
        fonte_titulo = ImageFont.truetype("DejaVuSans.ttf", 15)
        fonte_plataforma = ImageFont.truetype("DejaVuSans.ttf", 11)
    except OSError:
        fonte_titulo = ImageFont.load_default()
        fonte_plataforma = ImageFont.load_default()

    for i, jogo in enumerate(rows):
        x = margem + (i % colunas) * (largura_capa + espacamento)
        y = margem + (i // colunas) * (altura_cartao + espacamento)
        draw.rounded_rectangle((x, y, x + largura_capa, y + altura_cartao), radius=8, fill=cartao)
        capa_path = jogo["capa"]
        try:
            if capa_path and os.path.isfile(capa_path):
                with Image.open(capa_path) as original:
                    capa = ImageOps.fit(original.convert("RGB"), (largura_capa, altura_capa), method=Image.Resampling.LANCZOS)
            else:
                capa = Image.new("RGB", (largura_capa, altura_capa), (24, 26, 31))
                placeholder = ImageDraw.Draw(capa)
                placeholder.text((largura_capa // 2, altura_capa // 2), "SEM CAPA", anchor="mm", fill=(190, 190, 190), font=fonte_titulo)
        except (OSError, ValueError):
            capa = Image.new("RGB", (largura_capa, altura_capa), (24, 26, 31))
            ImageDraw.Draw(capa).text((largura_capa // 2, altura_capa // 2), "CAPA INVÁLIDA", anchor="mm", fill=(190, 190, 190), font=fonte_plataforma)
        img.paste(capa, (x, y))

        nome = str(jogo["nome"])
        max_chars = 23
        if len(nome) > max_chars:
            nome = nome[:max_chars - 1].rstrip() + "…"
        draw.text((x + 8, y + altura_capa + 7), nome, fill=(255, 255, 255), font=fonte_titulo)
        plataforma = str(jogo["plataforma"])
        if len(plataforma) > 27:
            plataforma = plataforma[:26] + "…"
        draw.text((x + 8, y + altura_capa + 32), plataforma, fill=(185, 190, 198), font=fonte_plataforma)

    buffer = BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return discord.File(buffer, filename="catalogo.png")


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

    def render(self):
        pages = max(1, math.ceil(self.total / PAGE_SIZE))
        titulo = self.TITULOS[self.mode]
        if self.query:
            titulo += f" • busca: {self.query}"
        if self.plataforma:
            titulo += f" • {self.plataforma}"

        file = None
        if self.mode == "ver":
            if self.rows:
                desc = "Selecione um jogo no menu abaixo para abrir a ficha completa. Use o filtro de plataforma e os botões para navegar."
                file = criar_grade_capas(self.rows)
            else:
                desc = "Nenhum jogo encontrado. Tente outra busca ou plataforma."
        elif self.mode == "pendentes" and not self.rows:
            desc = "Nenhum envio aguardando aprovação."
        elif self.rows:
            desc = "\n".join(
                f"{PLATAFORMAS.get(r['plataforma'], '🎮')} **{r['nome']}** — {r['plataforma']}" for r in self.rows
            )
        else:
            desc = "Nenhum jogo encontrado."

        emb = discord.Embed(title=titulo, description=desc, color=discord.Color.blurple())
        if file:
            emb.set_image(url="attachment://catalogo.png")
        emb.set_footer(text=f"Página {self.page + 1}/{pages} • {self.total} jogo(s)")
        return emb, file

    def embed(self):
        # Compatibilidade para os modos administrativos, que não usam a grade anexada.
        emb, _ = self.render()
        return emb

    async def atualizar(self, interaction: discord.Interaction):
        self.build()
        emb, file = self.render()
        await interaction.response.edit_message(
            embed=emb, attachments=[file] if file else [], view=self
        )


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
        emb, file = v.render()
        await interaction.response.edit_message(embed=emb, attachments=[file] if file else [], view=v)


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
            emb, file = v.render()
            await interaction.response.send_message(
                embed=emb, file=file, view=v, ephemeral=True
            )


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
        emb, file = v.render()
        await interaction.response.edit_message(embed=emb, attachments=[file] if file else [], view=v)


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
        emb, file = v.render()
        await interaction.response.edit_message(embed=emb, attachments=[file] if file else [], view=v)


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
        emb, file = v.render()
        await interaction.response.edit_message(embed=emb, attachments=[file] if file else [], view=v)


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
        emb, file = v.render()
        await interaction.response.edit_message(embed=emb, attachments=[file] if file else [], view=v)


# ------------------------------------------------------------------ mensagem principal (persistente)


class MainView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # não expira; continua funcionando após reiniciar o bot

    @discord.ui.button(label="Ver catálogo", emoji="📚", style=discord.ButtonStyle.primary, custom_id="catalogo:ver")
    async def ver(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = CatalogView()
        emb, file = v.render()
        await interaction.response.send_message(embed=emb, file=file, view=v, ephemeral=True)

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

    @discord.ui.button(label="Enviar mod", emoji="📤", style=discord.ButtonStyle.success, custom_id="catalogo:enviarmod")
    async def enviar_mod(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdicionarModModal(publico=True))

    @discord.ui.button(label="Catálogo de mods", emoji="🧩", style=discord.ButtonStyle.primary, custom_id="catalogo:mods")
    async def abrir_mods(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = ModsCatalogView()
        emb, file = v.render()
        kwargs = {"embed": emb, "view": v}
        if file:
            kwargs["file"] = file
        await interaction.response.send_message(**kwargs, ephemeral=True)

    @discord.ui.button(label="Admin", emoji="🛠", style=discord.ButtonStyle.secondary, custom_id="catalogo:admin")
    async def admin(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Só administradores (permissão **Gerenciar Servidor**).", ephemeral=True)
            return
        await interaction.response.send_message(embed=admin_embed(), view=AdminView(), ephemeral=True)



# ------------------------------------------------------------------ catálogo separado de mods


def listar_mods(query=None, page=0):
    where, params = ["status = 'aprovado'"], []
    if query:
        where.append("(jogo_nome LIKE ? OR mod_nome LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM mods {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT id, jogo_nome, mod_nome, descricao, imagem, link FROM mods {clause} "
            "ORDER BY jogo_nome COLLATE NOCASE, mod_nome COLLATE NOCASE LIMIT ? OFFSET ?",
            params + [PAGE_SIZE, page * PAGE_SIZE],
        ).fetchall()
    return rows, total


def get_mod(mod_id):
    with db() as conn:
        return conn.execute("SELECT * FROM mods WHERE id = ?", (mod_id,)).fetchone()


def criar_grade_mods(rows):
    """Gera grade visual exclusiva para o catálogo de mods."""
    # Adaptamos os nomes dos campos apenas para reutilizar o renderizador de capas.
    adaptados = [dict(capa=r["imagem"], nome=r["mod_nome"], plataforma=r["jogo_nome"]) for r in rows]
    arquivo = criar_grade_capas(adaptados)
    arquivo.filename = "catalogo_mods.png"
    return arquivo


class ModSelect(discord.ui.Select):
    def __init__(self, rows):
        options = [discord.SelectOption(
            label=r["mod_nome"][:100], value=str(r["id"]),
            description=f"Jogo: {r['jogo_nome']}"[:100], emoji="🧩") for r in rows]
        super().__init__(placeholder="Escolha um mod para ver os detalhes", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        mod = get_mod(int(self.values[0]))
        if not mod:
            await interaction.response.send_message("Esse mod não existe mais.", ephemeral=True)
            return
        embed = discord.Embed(
            title=mod["mod_nome"],
            description=(f"**Jogo:** {mod['jogo_nome']}\n\n{mod['descricao'] or 'Sem descrição cadastrada.'}")[:4000],
            color=discord.Color.dark_teal(),
        )
        file = None
        if mod["imagem"] and os.path.isfile(mod["imagem"]):
            filename = os.path.basename(mod["imagem"])
            file = discord.File(mod["imagem"], filename=filename)
            embed.set_image(url=f"attachment://{filename}")
        view = discord.ui.View(timeout=900)
        view.add_item(discord.ui.Button(label="Abrir / baixar mod", emoji="🔗",
                                        style=discord.ButtonStyle.link, url=mod["link"]))
        back = discord.ui.Button(label="Voltar ao catálogo", emoji="⬅️", style=discord.ButtonStyle.secondary)
        async def voltar(i: discord.Interaction):
            v = ModsCatalogView(self.view.query, self.view.page)
            e, f = v.render()
            await i.response.edit_message(embed=e, attachments=[f] if f else [], view=v)
        back.callback = voltar
        view.add_item(back)
        await interaction.response.edit_message(embed=embed, attachments=[file] if file else [], view=view)


class ModsPageButton(discord.ui.Button):
    def __init__(self, delta, disabled):
        super().__init__(label="Anterior" if delta < 0 else "Próxima",
                         emoji="⬅️" if delta < 0 else "➡️",
                         style=discord.ButtonStyle.secondary, disabled=disabled, row=1)
        self.delta = delta

    async def callback(self, interaction: discord.Interaction):
        self.view.page += self.delta
        self.view.rebuild()
        emb, file = self.view.render()
        await interaction.response.edit_message(embed=emb, attachments=[file] if file else [], view=self.view)


class ModsCatalogView(discord.ui.View):
    def __init__(self, query=None, page=0):
        super().__init__(timeout=900)
        self.query, self.page = query, page
        self.rebuild()

    def rebuild(self):
        self.clear_items()
        self.rows, self.total = listar_mods(self.query, self.page)
        pages = max(1, math.ceil(self.total / PAGE_SIZE))
        if self.rows:
            self.add_item(ModSelect(self.rows))
        self.add_item(ModsPageButton(-1, self.page <= 0))
        self.add_item(ModsPageButton(1, self.page >= pages - 1))

    def render(self):
        pages = max(1, math.ceil(self.total / PAGE_SIZE))
        embed = discord.Embed(
            title="🧩 Catálogo de Mods",
            description=("Selecione um mod no menu para ver descrição, imagem e link.\n\n"
                         "**Mods nesta página:**\n" + "\n".join(
                             f"🧩 **{r['mod_nome']}** — *{r['jogo_nome']}*" for r in self.rows))
                        if self.rows else "Nenhum mod cadastrado ainda.",
            color=discord.Color.dark_teal(),
        )
        file = criar_grade_mods(self.rows) if self.rows else None
        if file:
            embed.set_image(url="attachment://catalogo_mods.png")
        embed.set_footer(text=f"Página {self.page + 1}/{pages} • {self.total} mod(s)")
        return embed, file


class AdicionarModModal(discord.ui.Modal):
    jogo_nome = discord.ui.TextInput(label="Nome do jogo", max_length=100, placeholder="Ex.: Skyrim")
    mod_nome = discord.ui.TextInput(label="Nome do mod", max_length=100, placeholder="Ex.: Texturas HD")
    descricao = discord.ui.TextInput(label="Descrição do mod", style=discord.TextStyle.paragraph,
                                     required=True, max_length=1500, placeholder="O que o mod altera/adiciona?")
    link = discord.ui.TextInput(label="Link do mod", max_length=500, placeholder="https://...")

    def __init__(self, publico=False):
        super().__init__(title="Enviar mod para aprovação" if publico else "Cadastrar mod")
        self.publico = publico
        self.imagem = discord.ui.FileUpload(required=True, min_values=1, max_values=1)
        self.add_item(discord.ui.Label(text="Imagem do mod", component=self.imagem))

    async def on_submit(self, interaction: discord.Interaction):
        if not self.publico and not eh_admin(interaction):
            await interaction.response.send_message("⛔ Apenas administradores podem cadastrar mods.", ephemeral=True)
            return
        link = self.link.value.strip()
        erro = validar_links(link=link)
        if erro:
            await interaction.response.send_message(erro, ephemeral=True)
            return
        if not self.imagem.values:
            await interaction.response.send_message("Envie a imagem do mod.", ephemeral=True)
            return
        att = self.imagem.values[0]
        if not (att.content_type or "").startswith("image/"):
            await interaction.response.send_message("O arquivo enviado precisa ser uma imagem.", ephemeral=True)
            return
        if att.size > MAX_CAPA_BYTES:
            await interaction.response.send_message("A imagem deve ter no máximo 8 MB.", ephemeral=True)
            return
        ext = os.path.splitext(att.filename)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            ext = ".png"
        path = os.path.join(MODS_DIR, f"{uuid.uuid4().hex}{ext}")
        try:
            await att.save(path)
        except Exception:
            await interaction.response.send_message("Não consegui salvar a imagem.", ephemeral=True)
            return
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO mods (jogo_nome, mod_nome, descricao, imagem, link, status, autor_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.jogo_nome.value.strip(), self.mod_nome.value.strip(), self.descricao.value.strip(), path, link,
                 "pendente" if self.publico else "aprovado", interaction.user.id),
            )
            mod_id = cur.lastrowid
        if self.publico:
            await interaction.response.send_message(
                f"✅ Seu mod **{self.mod_nome.value.strip()}** foi enviado para análise da moderação. Ele aparecerá no catálogo após aprovação.",
                ephemeral=True,
            )
            canal_id = get_config("canal_avisos")
            if canal_id:
                try:
                    canal = bot.get_channel(int(canal_id)) or await bot.fetch_channel(int(canal_id))
                    await canal.send(f"📥 Novo mod aguardando aprovação: **{self.mod_nome.value.strip()}** (jogo: {self.jogo_nome.value.strip()}). Use `/painelmods` → **Pendentes**.")
                except Exception as e:
                    print("Não consegui avisar moderação sobre mod:", repr(e))
        else:
            await interaction.response.send_message(
                f"✅ Mod **{self.mod_nome.value.strip()}** cadastrado no catálogo. ID: `{mod_id}`.", ephemeral=True,
            )


# ------------------------------------------------------------------ administração de mods

class ModAdminSelect(discord.ui.Select):
    def __init__(self, action, rows):
        self.action = action
        opts = [discord.SelectOption(label=f"{r['mod_nome']}"[:100], value=str(r['id']),
                                     description=f"Jogo: {r['jogo_nome']}"[:100], emoji="🧩") for r in rows]
        super().__init__(placeholder="Selecione o mod", options=opts, row=0)

    async def callback(self, interaction: discord.Interaction):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Apenas administradores podem gerenciar mods.", ephemeral=True)
            return
        mod = get_mod(int(self.values[0]))
        if not mod:
            await interaction.response.send_message("Esse mod não existe mais.", ephemeral=True)
            return
        if self.action == "editar":
            await interaction.response.send_modal(EditarModModal(mod))
        else:
            view = ConfirmarRemocaoModView(mod)
            emb = discord.Embed(title="Remover mod?", description=f"**{mod['mod_nome']}**\nJogo: {mod['jogo_nome']}\n\nEssa ação não pode ser desfeita.", color=discord.Color.red())
            await interaction.response.edit_message(embed=emb, view=view)


class ModAdminListView(discord.ui.View):
    def __init__(self, action, page=0):
        super().__init__(timeout=900)
        self.action, self.page = action, page
        with db() as conn:
            self.total = conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
            self.rows = conn.execute("SELECT id, jogo_nome, mod_nome FROM mods ORDER BY jogo_nome COLLATE NOCASE, mod_nome COLLATE NOCASE LIMIT ? OFFSET ?", (25, page * 25)).fetchall()
        if self.rows:
            self.add_item(ModAdminSelect(action, self.rows))
        if page > 0:
            b = discord.ui.Button(label="Anterior", emoji="⬅️", row=1)
            b.callback = self.anterior
            self.add_item(b)
        if (page + 1) * 25 < self.total:
            b = discord.ui.Button(label="Próxima", emoji="➡️", row=1)
            b.callback = self.proxima
            self.add_item(b)
        b = discord.ui.Button(label="Painel de mods", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
        b.callback = self.voltar
        self.add_item(b)

    def embed(self):
        verbo = "editar" if self.action == "editar" else "remover"
        desc = "Selecione o mod que deseja " + verbo + "." if self.rows else "Nenhum mod cadastrado."
        return discord.Embed(title=f"🧩 {verbo.capitalize()} mod", description=desc, color=discord.Color.dark_teal())

    async def anterior(self, interaction):
        v = ModAdminListView(self.action, self.page - 1)
        await interaction.response.edit_message(embed=v.embed(), view=v)

    async def proxima(self, interaction):
        v = ModAdminListView(self.action, self.page + 1)
        await interaction.response.edit_message(embed=v.embed(), view=v)

    async def voltar(self, interaction):
        await interaction.response.edit_message(embed=discord.Embed(title="🧩 Administração de mods", description="Escolha uma opção:", color=discord.Color.dark_teal()), view=ModAdminView())


class EditarModModal(discord.ui.Modal):
    def __init__(self, mod):
        super().__init__(title=f"Editar mod: {mod['mod_nome']}"[:45])
        self.mod_id = mod['id']
        self.mod = mod
        self.jogo_nome = discord.ui.TextInput(label="Nome do jogo", max_length=100, default=mod['jogo_nome'])
        self.mod_nome = discord.ui.TextInput(label="Nome do mod", max_length=100, default=mod['mod_nome'])
        self.descricao = discord.ui.TextInput(label="Descrição", style=discord.TextStyle.paragraph, required=True, max_length=1500, default=(mod['descricao'] or '')[:1500])
        self.link = discord.ui.TextInput(label="Link do mod", max_length=500, default=mod['link'])
        self.add_item(self.jogo_nome)
        self.add_item(self.mod_nome)
        self.add_item(self.descricao)
        self.add_item(self.link)

    async def on_submit(self, interaction: discord.Interaction):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Apenas administradores podem editar mods.", ephemeral=True)
            return
        link = self.link.value.strip()
        erro = validar_links(link=link)
        if erro:
            await interaction.response.send_message(erro, ephemeral=True)
            return
        with db() as conn:
            conn.execute("UPDATE mods SET jogo_nome=?, mod_nome=?, descricao=?, link=? WHERE id=?", (self.jogo_nome.value.strip(), self.mod_nome.value.strip(), self.descricao.value.strip(), link, self.mod_id))
        await interaction.response.send_message(f"✅ Mod **{self.mod_nome.value.strip()}** atualizado. Para trocar a imagem, remova e cadastre novamente com a nova capa.", ephemeral=True)


class ConfirmarRemocaoModView(discord.ui.View):
    def __init__(self, mod):
        super().__init__(timeout=120)
        self.mod = mod

    @discord.ui.button(label="Confirmar remoção", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirmar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Apenas administradores podem remover mods.", ephemeral=True)
            return
        with db() as conn:
            conn.execute("DELETE FROM mods WHERE id=?", (self.mod['id'],))
        apagar_arquivo(self.mod['imagem'])
        await interaction.response.edit_message(embed=discord.Embed(title="Mod removido", description=f"**{self.mod['mod_nome']}** foi removido do catálogo de mods.", color=discord.Color.red()), view=ModAdminView())

    @discord.ui.button(label="Cancelar", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=discord.Embed(title="🧩 Administração de mods", description="Escolha uma opção:", color=discord.Color.dark_teal()), view=ModAdminView())


class ModPendentesSelect(discord.ui.Select):
    def __init__(self, rows):
        options = [discord.SelectOption(label=r["mod_nome"][:100], value=str(r["id"]), description=f"Jogo: {r['jogo_nome']}"[:100]) for r in rows]
        super().__init__(placeholder="Escolha um mod pendente", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Apenas administradores podem moderar mods.", ephemeral=True)
            return
        mod = get_mod(int(self.values[0]))
        if not mod or mod["status"] != "pendente":
            await interaction.response.send_message("Esse envio não está mais pendente.", ephemeral=True)
            return
        emb = discord.Embed(title=mod["mod_nome"], description=f"**Jogo:** {mod['jogo_nome']}\n**Descrição:** {mod['descricao'] or 'Sem descrição'}\n**Link:** {mod['link']}\n**Enviado por:** <@{mod['autor_id']}>" if mod['autor_id'] else f"**Jogo:** {mod['jogo_nome']}\n**Descrição:** {mod['descricao'] or 'Sem descrição'}\n**Link:** {mod['link']}", color=discord.Color.orange())
        if mod["imagem"] and os.path.exists(mod["imagem"]):
            emb.set_image(url="attachment://mod_pendente.png")
            await interaction.response.edit_message(embed=emb, attachments=[discord.File(mod["imagem"], filename="mod_pendente.png")], view=ModDecisaoView(mod))
        else:
            await interaction.response.edit_message(embed=emb, view=ModDecisaoView(mod))


class ModPendentesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)
        with db() as conn:
            self.rows = conn.execute("SELECT id, jogo_nome, mod_nome FROM mods WHERE status='pendente' ORDER BY id DESC LIMIT 25").fetchall()
        if self.rows:
            self.add_item(ModPendentesSelect(self.rows))
        b = discord.ui.Button(label="Voltar", emoji="⬅️", row=1)
        b.callback = self.voltar
        self.add_item(b)

    def embed(self):
        return discord.Embed(title="📥 Mods pendentes", description="Selecione um envio para aprovar ou rejeitar." if self.rows else "Não há mods aguardando aprovação.", color=discord.Color.orange())

    async def voltar(self, interaction):
        await interaction.response.edit_message(embed=discord.Embed(title="🧩 Administração de mods", description="Escolha uma opção:", color=discord.Color.dark_teal()), view=ModAdminView())


class ModDecisaoView(discord.ui.View):
    def __init__(self, mod):
        super().__init__(timeout=900)
        self.mod = mod

    @discord.ui.button(label="Aprovar", emoji="✅", style=discord.ButtonStyle.success)
    async def aprovar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Apenas administradores podem moderar mods.", ephemeral=True); return
        with db() as conn:
            conn.execute("UPDATE mods SET status='aprovado' WHERE id=?", (self.mod["id"],))
        await interaction.response.edit_message(embed=discord.Embed(title="✅ Mod aprovado", description=f"**{self.mod['mod_nome']}** já aparece no catálogo de mods.", color=discord.Color.green()), attachments=[], view=ModPendentesView())

    @discord.ui.button(label="Rejeitar", emoji="❌", style=discord.ButtonStyle.danger)
    async def rejeitar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Apenas administradores podem moderar mods.", ephemeral=True); return
        with db() as conn:
            conn.execute("DELETE FROM mods WHERE id=?", (self.mod["id"],))
        apagar_arquivo(self.mod["imagem"])
        await interaction.response.edit_message(embed=discord.Embed(title="❌ Mod rejeitado", description=f"**{self.mod['mod_nome']}** foi removido dos envios.", color=discord.Color.red()), attachments=[], view=ModPendentesView())


class ModAdminView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)

    @discord.ui.button(label="Adicionar mod", emoji="➕", style=discord.ButtonStyle.success, row=0)
    async def adicionar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Apenas administradores podem gerenciar mods.", ephemeral=True)
            return
        await interaction.response.send_modal(AdicionarModModal(publico=False))

    @discord.ui.button(label="Editar mod", emoji="✏️", style=discord.ButtonStyle.primary, row=0)
    async def editar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Apenas administradores podem gerenciar mods.", ephemeral=True)
            return
        v = ModAdminListView("editar")
        await interaction.response.edit_message(embed=v.embed(), view=v)

    @discord.ui.button(label="Pendentes", emoji="📥", style=discord.ButtonStyle.secondary, row=1)
    async def pendentes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Apenas administradores podem moderar mods.", ephemeral=True)
            return
        v = ModPendentesView()
        await interaction.response.edit_message(embed=v.embed(), view=v)

    @discord.ui.button(label="Remover mod", emoji="🗑️", style=discord.ButtonStyle.danger, row=0)
    async def remover(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not eh_admin(interaction):
            await interaction.response.send_message("⛔ Apenas administradores podem gerenciar mods.", ephemeral=True)
            return
        v = ModAdminListView("remover")
        await interaction.response.edit_message(embed=v.embed(), view=v)


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


@tree.command(name="mods", description="Abre o catálogo separado de mods")
@app_commands.guild_only()
async def mods(interaction: discord.Interaction):
    view = ModsCatalogView()
    embed, file = view.render()
    kwargs = {"embed": embed, "view": view}
    if file:
        kwargs["file"] = file
    await interaction.response.send_message(**kwargs)


@tree.command(name="enviarmod", description="Envia um mod para aprovação da moderação")
@app_commands.guild_only()
async def enviarmod(interaction: discord.Interaction):
    await interaction.response.send_modal(AdicionarModModal(publico=True))


@tree.command(name="adicionarmod", description="[Admin] Cadastra um mod no catálogo separado")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def adicionarmod(interaction: discord.Interaction):
    await interaction.response.send_modal(AdicionarModModal(publico=False))


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


@tree.command(name="painelmods", description="[Admin] Abre as opções de administração dos mods")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def painelmods(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=discord.Embed(title="🧩 Administração de mods", description="Escolha uma opção:", color=discord.Color.dark_teal()),
        view=ModAdminView(), ephemeral=True,
    )


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
