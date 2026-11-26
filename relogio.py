"""Relógio da fila: dispara o postar.yml no minuto de cada horário do fila.csv.

O cron do GitHub atrasa horas neste repositório, e o disparo pelo Windows só funciona com o PC ligado.
Este script roda dentro do próprio GitHub Actions (workflow relogio.yml): fica acordado por até ~5h40,
olhando a fila a cada 20 s, dispara a postagem no minuto marcado e, antes de acabar, inicia o próximo
relógio. Disparo a mais não faz mal: o postar.py só publica o que venceu e nunca publica duas vezes.
"""
import datetime as dt
import subprocess
import time
from zoneinfo import ZoneInfo

BR = ZoneInfo("America/Sao_Paulo")
LIMITE = 5 * 3600 + 40 * 60          # o GitHub encerra um job em 6 h
TOLERANCIA = dt.timedelta(minutes=10)


def agora():
    return dt.datetime.now(BR)


def horarios():
    """Os horários da fila, relidos do repositório (a fila muda ao longo do dia)."""
    subprocess.run(["git", "pull", "--quiet", "--ff-only"], check=False)
    out = set()
    with open("fila.csv", encoding="utf-8") as f:
        for linha in f:
            if linha.startswith(("#", "quando")) or not linha.strip():
                continue
            try:
                out.add(dt.datetime.strptime(linha.split(";")[0].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=BR))
            except ValueError:
                pass
    return out


def gh(*args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    return "ok" if r.returncode == 0 else (r.stderr or r.stdout).strip()


def disparar(motivo):
    print(f"{agora():%d/%m %H:%M:%S} postagem disparada ({motivo}): {gh('workflow', 'run', 'postar.yml', '-f', 'de_verdade=true')}",
          flush=True)


def main():
    inicio = time.monotonic()
    disparar("início do relógio: publica o que venceu enquanto nenhum relógio rodava")
    feitos, lidos_em, hs = set(), -1e9, set()
    while time.monotonic() - inicio < LIMITE:
        if time.monotonic() - lidos_em > 600:
            hs, lidos_em = horarios(), time.monotonic()
            proximos = sorted(h for h in hs if h > agora())[:3]
            print(f"{agora():%d/%m %H:%M} próximos horários: {', '.join(f'{h:%d/%m %H:%M}' for h in proximos) or 'nenhum'}",
                  flush=True)
        t = agora()
        for h in sorted(hs):
            if h <= t < h + TOLERANCIA and h not in feitos:
                feitos.add(h)
                disparar(f"horário {h:%d/%m %H:%M}")
        time.sleep(20)
    print(f"{agora():%d/%m %H:%M} próximo relógio: {gh('workflow', 'run', 'relogio.yml')}", flush=True)


if __name__ == "__main__":
    main()
