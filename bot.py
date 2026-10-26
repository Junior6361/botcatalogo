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
