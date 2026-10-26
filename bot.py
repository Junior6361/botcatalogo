return
    mensagem = None
    atual_id = get_config(chave)
    if atual_id:
        try:
            mensagem = await canal.fetch_message(int(atual_id))
            if mensagem.author.id != bot.user.id:
                mensagem = None
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            mensagem = None
    # Na primeira execução após esta atualização, tenta reutilizar o painel antigo já publicado.
    if mensagem is None and hasattr(canal, "history"):
        alvo = "🎮 Catálogo de Jogos" if chave.startswith("catalogo_msg_") else None
        try:
            async for candidata in canal.history(limit=100):
                if candidata.author.id != bot.user.id:
                    continue
                titulos = [e.title or "" for e in candidata.embeds]
                reconhecida = (
                    (alvo and (alvo in (candidata.content or "") or alvo in titulos))
                    or (chave.startswith("mods_msg_") and "🧩 Catálogo de Mods" in titulos)
                )
                if reconhecida:
                    mensagem = candidata
                    break
        except (discord.Forbidden, discord.HTTPException):
            # Sem permissão para ler o histórico? Publica normalmente e registra o ID.
            mensagem = None
    arquivos = [file] if file else []
    if mensagem:
        await mensagem.edit(content=content, embed=embed, attachments=arquivos, view=view)
        set_config(chave, str(mensagem.id))
        acao = "atualizado"
    else:
        mensagem = await canal.send(content=content, embed=embed, view=view, files=arquivos)
        set_config(chave, str(mensagem.id))
        acao = "publicado"
    await interaction.edit_original_response(content=f"✅ Painel {acao} em {canal.mention}.")


@tree.command(name="mods", description="Abre o catálogo separado de mods")
@app_commands.guild_only()
async def mods(interaction: discord.Interaction):
    view = ModsCatalogView()
    embed, file = view.render()
    kwargs = {"embed": embed, "view": view, "ephemeral": True}
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
        content="## 🧩 Administração de mods\nEscolha uma ação no menu abaixo.",
        view=ModAdminView(), ephemeral=True,
    )


@tree.command(name="catalogo", description="[Admin] Publica ou atualiza o catálogo de jogos neste canal")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def catalogo(interaction: discord.Interaction):
    await publicar_ou_atualizar_painel(
        interaction, f"catalogo_msg_{interaction.channel_id}", content=main_content(), view=MainView())


@bot.event
async def on_ready():
    print(f"Bot online como {bot.user}")
    # A lista de guilds fica disponível após a conexão. Aqui limpamos comandos
    # de escopo de servidor que tenham sobrado de versões anteriores do bot.
    await bot.cleanup_legacy_guild_commands()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Defina a variável de ambiente DISCORD_TOKEN.")
    if not hasattr(discord.ui, "FileUpload"):
        raise SystemExit(f"discord.py {discord.__version__} é antigo demais. Instale a 2.7 ou mais nova: "
                         "coloque discord.py>=2.7 no requirements.txt.")
    init_db()
    bot.run(TOKEN)
