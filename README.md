# Fila de Reels

Fila de postagem dos Reels dos canais no Instagram, publicada pela API oficial da Meta a cada 15 minutos pelo GitHub Actions.

- `fila.csv`: o que posta e quando (hora de Brasília).
- `postados.json`: o que já foi ao ar, com o link. O robô escreve aqui, não edite à mão.
- branch `midia`: os vídeos que ainda vão ao ar. Cada um sai daqui logo depois de postado.

Gerado por `scripts/postar.py sincronizar` no projeto dos canais. Não edite os arquivos aqui; edite lá e sincronize.
