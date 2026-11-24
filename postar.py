"""Posta os Reels prontos no Instagram de cada canal, pela API oficial da Meta (Graph API). Grátis.

Dois caminhos, os dois oficiais (guia completo em postagem/LEIA-ME.md):

  instagram  (padrão)  Instagram Login: o GitHub Actions posta na hora certa com o PC desligado.
                       O vídeo e a capa ficam num endereço público (branch "midia" de um repositório
                       público) até irem ao ar. Um token por conta: IG_TOKEN_FM, IG_TOKEN_AI, ...
  facebook             Facebook Login: sobe o arquivo direto do PC, sem nada público, mas o PC precisa
                       estar ligado (Agendador do Windows). Cada conta ligada a uma Página do Facebook.
                       Um token só: META_TOKEN.

    python scripts/postar.py fila                     a fila e o estado de cada item (sem rede)
    python scripts/postar.py contas                   as contas que os tokens alcançam
    python scripts/postar.py publicar                 SIMULAÇÃO do que sairia agora (sem rede)
    python scripts/postar.py publicar --de-verdade    publica os itens vencidos da fila
        --canal fm --video 05                         só esse item, mesmo fora do horário
    python scripts/postar.py sincronizar --repo C:\\dev\\fila-reels [--enviar]
                                                      prepara o repositório do GitHub com a fila e
                                                      os vídeos pendentes; só dá push com --enviar

Fila, registro e contas (padrão em postagem/; --pasta muda):
    fila.csv        quando;canal;video[;capa_ms]   ex.: 2026-11-25 19:00;fm;05   (hora de Brasília)
    postados.json   o que já foi enviado ou postado; nunca posta duas vezes
    contas.json     só no caminho facebook: canal -> @ da conta

Os tokens só vêm de variáveis de ambiente (ou dos Secrets do GitHub). O script nunca os imprime e
nunca os põe em URL: vão no cabeçalho Authorization.
"""
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

AQUI = Path(__file__).resolve().parent
ROOT = AQUI.parent
PRONTOS = ROOT / "prontos"
VERSAO = "v27.0"
HOSTS = {"facebook": f"https://graph.facebook.com/{VERSAO}", "instagram": f"https://graph.instagram.com/{VERSAO}"}
RUPLOAD = f"https://rupload.facebook.com/ig-api-upload/{VERSAO}"

CANAIS = {"sd": "siga-o-dinheiro", "ai": "altamente-improvavel", "fm": "fio-da-meada", "fb": "fase-bonus"}
MARCA_LEGENDA = "LEGENDA (INSTAGRAM E TIKTOK)"
LEGENDA_MAX, HASHTAGS_MAX = 2200, 30
VIDEO_MAX = 300 * 1024 * 1024
ESPERA_STATUS, ESPERA_MAX = 30, 15 * 60
PERMISSOES_FB = {"instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement",
                 "pages_manage_posts"}

# Ajustado em main() pelos argumentos.
CFG = {"via": "instagram", "pasta": ROOT / "postagem", "midia": None, "midia_url": None}


class Erro(Exception):
    pass


def caminho(nome):
    return CFG["pasta"] / nome


def rel(p):
    try:
        return p.relative_to(ROOT)
    except ValueError:
        return p


# ---------------------------------------------------------------- fila e arquivos

def canal_de(texto):
    t = texto.strip().lower()
    if t in CANAIS:
        return CANAIS[t]
    if t in CANAIS.values():
        return t
    raise Erro(f"canal desconhecido: {texto!r} (use {', '.join(CANAIS)})")


def sigla(canal):
    return next(k for k, v in CANAIS.items() if v == canal)


def achar_stem(canal, video):
    """Número ou nome -> NN-slug. Na pasta de mídia (GitHub) a fila já traz o nome completo."""
    v = video.strip().removesuffix(".mp4")
    if CFG["midia"] and not v.isdigit():
        return v
    if CFG["midia"]:
        achados = sorted(p.name for p in (CFG["midia"] / sigla(canal)).glob(f"{int(v):02d}-*") if p.is_dir())
        onde = CFG["midia"] / sigla(canal)
    else:
        onde = PRONTOS / canal / "videos"
        achados = [p.stem for p in sorted(onde.glob(f"{int(v):02d}-*.mp4"))] if v.isdigit() else \
                  [v] if (onde / f"{v}.mp4").exists() else []
    if len(achados) != 1:
        raise Erro(f"{sigla(canal)} {video}: {'nenhum vídeo' if not achados else 'mais de um vídeo'} em {rel(onde)}")
    return achados[0]


def ler_fila():
    fila = caminho("fila.csv")
    if not fila.exists():
        raise Erro(f"não achei {rel(fila)}")
    linhas = [l for l in fila.read_text(encoding="utf-8-sig").splitlines() if l.strip() and not l.lstrip().startswith("#")]
    itens = []
    for n, row in enumerate(csv.reader(linhas, delimiter=";"), 1):
        row = [c.strip() for c in row]
        if row[0].lower() == "quando":
            continue
        try:
            quando = datetime.strptime(row[0], "%Y-%m-%d %H:%M")
        except ValueError:
            raise Erro(f"fila: data {row[0]!r} fora do formato AAAA-MM-DD HH:MM") from None
        itens.append({"quando": quando, "canal": canal_de(row[1]), "video": row[2],
                      "capa_ms": int(row[3]) if len(row) > 3 and row[3] else 0})
    return sorted(itens, key=lambda i: i["quando"])


def arquivos(canal, stem):
    """(vídeo, descrição ou legenda, capa ou None) de um item."""
    if CFG["midia"]:
        d = CFG["midia"] / sigla(canal) / stem
        return d / "video.mp4", d / "legenda.txt", (d / "capa.jpg" if (d / "capa.jpg").exists() else None)
    pasta = PRONTOS / canal / "thumbs" / stem
    numero = stem.split("-", 1)[0]
    capa = None
    # capa-9x16.jpg é o nome que o make.py grava; "N.jpg" é a mesma capa renomeada para subir à mão
    for nome in ("capa-9x16.jpg", f"{int(numero)}.jpg" if numero.isdigit() else ""):
        if nome and (pasta / nome).exists():
            capa = pasta / nome
            break
    if capa is None and pasta.exists():
        try:
            from PIL import Image
            verticais = [p for p in sorted(pasta.glob("*.jpg")) if Image.open(p).size == (1080, 1920)]
            capa = verticais[0] if len(verticais) == 1 else None
        except ImportError:
            pass
    return (PRONTOS / canal / "videos" / f"{stem}.mp4", PRONTOS / canal / "descricoes" / f"{stem}.txt", capa)


def ler_legenda(arq):
    if not arq.exists():
        raise Erro(f"sem legenda: {rel(arq)}")
    texto = arq.read_text(encoding="utf-8")
    if arq.name == "legenda.txt":
        return texto.strip()
    if MARCA_LEGENDA not in texto:
        raise Erro(f"{rel(arq)} não tem o bloco {MARCA_LEGENDA}; rode scripts/legenda.py")
    bloco = re.split(r"\n={10,}", texto.split(MARCA_LEGENDA, 1)[1])[0].strip()
    if not bloco:
        raise Erro(f"{rel(arq)}: bloco da legenda vazio")
    return bloco


def sondar_video(arq):
    """Tamanho, duração, codecs e moov no começo, conferidos contra a especificação de Reels."""
    problemas, info = [], {}
    if not arq.exists():
        raise Erro(f"sem vídeo: {rel(arq)}")
    tam = arq.stat().st_size
    info["mb"] = tam / 1024 / 1024
    if tam > VIDEO_MAX:
        problemas.append(f"arquivo de {info['mb']:.0f} MB passa de 300 MB")
    try:
        import imageio_ffmpeg
        saida = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(arq)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", saida)
        if m:
            info["s"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
            if not 3 <= info["s"] <= 15 * 60:
                problemas.append(f"duração de {info['s']:.0f} s fora de 3 s a 15 min")
        v = re.search(r"Video: (\w+).*?, (\d{2,5})x(\d{2,5}).*?([\d.]+) fps", saida)
        if v:
            info["video"] = f"{v[1]} {v[2]}x{v[3]} {v[4]} fps"
            if v[1] not in ("h264", "hevc"):
                problemas.append(f"codec de vídeo {v[1]} (o Instagram aceita h264 ou hevc)")
            if int(v[2]) > 1920:
                problemas.append(f"largura {v[2]} passa de 1920")
            if not 23 <= float(v[4]) <= 60:
                problemas.append(f"{v[4]} fps fora de 23 a 60")
        a = re.search(r"Audio: (\w+).*?, (\d+) Hz", saida)
        if a:
            info["audio"] = f"{a[1]} {a[2]} Hz"
            if a[1] != "aac" or int(a[2]) > 48000:
                problemas.append(f"áudio {a[1]} {a[2]} Hz (precisa ser aac até 48 kHz)")
    except ImportError:
        pass  # no GitHub Actions não há ffmpeg; os vídeos já foram conferidos no PC
    with open(arq, "rb") as f:
        caixas = []
        while len(caixas) < 12:
            cab = f.read(8)
            if len(cab) < 8:
                break
            tamanho, tipo = int.from_bytes(cab[:4], "big"), cab[4:8].decode("latin-1")
            if tamanho == 1:
                tamanho = int.from_bytes(f.read(8), "big") - 8
            caixas.append(tipo)
            if tamanho < 8:
                break
            f.seek(tamanho - 8, 1)
    if "moov" in caixas and "mdat" in caixas and caixas.index("moov") > caixas.index("mdat"):
        problemas.append("o moov está no fim do arquivo; o Instagram pede no começo (ffmpeg -movflags +faststart)")
    return info, problemas


def chave_de(item):
    return f"{sigla(item['canal'])}/{achar_stem(item['canal'], item['video'])}"


def montar(item):
    stem = achar_stem(item["canal"], item["video"])
    video, leg, capa = arquivos(item["canal"], stem)
    texto = ler_legenda(leg)
    problemas = []
    if len(texto) > LEGENDA_MAX:
        problemas.append(f"legenda com {len(texto)} caracteres passa de {LEGENDA_MAX}")
    if len(re.findall(r"#\w+", texto)) > HASHTAGS_MAX:
        problemas.append(f"mais de {HASHTAGS_MAX} hashtags")
    info, p = sondar_video(video)
    return {**item, "stem": stem, "chave": f"{sigla(item['canal'])}/{stem}", "arquivo": video,
            "arq_legenda": leg, "legenda": texto, "capa": capa, "info": info, "problemas": problemas + p}


# ---------------------------------------------------------------- registro

def ler_registro():
    reg = caminho("postados.json")
    return json.loads(reg.read_text(encoding="utf-8")) if reg.exists() else {}


def anotar(chave, **campos):
    reg = ler_registro()
    reg.setdefault(chave, {}).update(campos)
    destino = caminho("postados.json")
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(destino)


# ---------------------------------------------------------------- API da Meta

def chamar(metodo, url, params=None, tok=None, corpo=None, cabecalhos=None, esquema="Bearer"):
    if not tok:
        raise Erro("chamada sem token")
    cab = {"Authorization": f"{esquema} {tok}", **(cabecalhos or {})}
    if params and metodo == "GET":
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    elif params:
        corpo = urllib.parse.urlencode(params).encode()
        cab["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=corpo, headers=cab, method=metodo)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()).get("error", {})
        except ValueError:
            raise Erro(f"Meta respondeu {e.code}") from None
        msg = err.get("error_user_msg") or err.get("message") or str(e)
        raise Erro(f"Meta respondeu {e.code}: {msg} (código {err.get('code')}/{err.get('error_subcode')})") from None
    except urllib.error.URLError as e:
        raise Erro(f"sem conexão com a Meta: {e.reason}") from None


def contas_disponiveis():
    """canal -> {usuario, ig, token, [pagina, token_pagina]} para os canais que dá para postar."""
    G = HOSTS[CFG["via"]]
    out = {}
    if CFG["via"] == "instagram":
        for s, canal in CANAIS.items():
            tok = os.environ.get(f"IG_TOKEN_{s.upper()}", "").strip()
            if tok:
                me = chamar("GET", f"{G}/me", {"fields": "user_id,username"}, tok=tok)
                out[canal] = {"usuario": me["username"].lower(), "ig": me.get("user_id") or me["id"], "token": tok}
        return out
    tok = os.environ.get("META_TOKEN", "").strip()
    if not tok:
        raise Erro("a variável META_TOKEN não está definida (postagem/LEIA-ME.md, caminho B)")
    alcance = {}
    r = chamar("GET", f"{G}/me/accounts", {"fields": "id,name,access_token,instagram_business_account{id,username}",
                                            "limit": 100}, tok=tok)
    for p in r.get("data", []):
        ig = p.get("instagram_business_account")
        if ig:
            alcance[ig["username"].lower()] = {"usuario": ig["username"].lower(), "ig": ig["id"], "token": tok,
                                               "pagina": p["id"], "nome_pagina": p["name"],
                                               "token_pagina": p.get("access_token")}
    arq = caminho("contas.json")
    if not arq.exists():
        raise Erro(f"não achei {rel(arq)}")
    for k, v in json.loads(arq.read_text(encoding="utf-8")).items():
        if v and not k.startswith("_") and v.strip().lstrip("@").lower() in alcance:
            out[canal_de(k)] = alcance[v.strip().lstrip("@").lower()]
    return out


def capa_pela_pagina(arquivo, conta):
    """Caminho facebook: foto temporária e não publicada na Página; devolve o endereço público dela."""
    G = HOSTS["facebook"]
    if not conta.get("token_pagina"):
        raise Erro("sem token da Página")
    limite = uuid.uuid4().hex
    partes = [f'--{limite}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n'.encode()
              for n, v in (("published", "false"), ("temporary", "true"))]
    partes.append(f'--{limite}\r\nContent-Disposition: form-data; name="source"; filename="capa.jpg"\r\n'
                  f"Content-Type: image/jpeg\r\n\r\n".encode() + arquivo.read_bytes() + b"\r\n")
    partes.append(f"--{limite}--\r\n".encode())
    r = chamar("POST", f"{G}/{conta['pagina']}/photos", tok=conta["token_pagina"], corpo=b"".join(partes),
               cabecalhos={"Content-Type": f"multipart/form-data; boundary={limite}"})
    foto = chamar("GET", f"{G}/{r['id']}", {"fields": "images"}, tok=conta["token_pagina"])
    imagens = sorted(foto.get("images", []), key=lambda i: i.get("height", 0), reverse=True)
    if not imagens:
        raise Erro("a Página não devolveu o endereço da foto")
    return imagens[0]["source"]


def url_publica(p, nome):
    return f"{CFG['midia_url'].rstrip('/')}/{sigla(p['canal'])}/{urllib.parse.quote(p['stem'])}/{nome}"


def publicar_item(p, conta):
    G, chave, tok = HOSTS[CFG["via"]], p["chave"], conta["token"]

    def api(metodo, rota, params=None):
        return chamar(metodo, f"{G}/{rota}", params, tok=tok)

    def esperar(container):
        inicio = time.time()
        while True:
            st = api("GET", container, {"fields": "status_code,status"})
            codigo = st.get("status_code")
            if codigo in ("FINISHED", "PUBLISHED", "ERROR", "EXPIRED"):
                return codigo, st.get("status", "")
            if time.time() - inicio > ESPERA_MAX:
                return "DEMOROU", st.get("status", "")
            print(f"    processando no Instagram ({codigo})…", flush=True)
            time.sleep(ESPERA_STATUS)

    container = ler_registro().get(chave, {}).get("container")
    if container:  # uma execução anterior parou no meio: retoma em vez de postar de novo
        codigo, _ = esperar(container)
        if codigo == "PUBLISHED":
            anotar(chave, estado="postado", obs="publicado numa execução anterior")
            print("    já tinha sido publicado; anotei no registro")
            return
        if codigo != "FINISHED":
            container = None

    if not container:
        d = (api("GET", f"{conta['ig']}/content_publishing_limit", {"fields": "quota_usage,config"}).get("data") or [{}])[0]
        usados, total = d.get("quota_usage", 0), (d.get("config") or {}).get("quota_total", 50)
        if usados >= total:
            raise Erro(f"cota de publicação da conta esgotada ({usados}/{total} em 24 h)")
        params = {"media_type": "REELS", "caption": p["legenda"]}
        if CFG["via"] == "instagram":
            params |= {"video_url": url_publica(p, "video.mp4"), "share_to_feed": "true"}
            if p["capa"]:
                params["cover_url"] = url_publica(p, "capa.jpg")
        else:
            params["upload_type"] = "resumable"
            if p["capa"]:
                try:
                    params["cover_url"] = capa_pela_pagina(p["capa"], conta)
                except Erro as e:
                    print(f"    capa pela Página falhou ({e}); vai o quadro de {p['capa_ms']} ms")
        if "cover_url" not in params:
            params["thumb_offset"] = p["capa_ms"]
        container = api("POST", f"{conta['ig']}/media", params)["id"]
        anotar(chave, estado="enviando", container=container, conta=conta["usuario"],
               inicio=datetime.now().isoformat(timespec="seconds"))
        if CFG["via"] == "facebook":
            tamanho = p["arquivo"].stat().st_size
            print(f"    enviando {tamanho / 1024 / 1024:.1f} MB…", flush=True)
            with open(p["arquivo"], "rb") as f:
                up = chamar("POST", f"{RUPLOAD}/{container}", tok=tok, corpo=f, esquema="OAuth",
                            cabecalhos={"offset": "0", "file_size": str(tamanho), "Content-Length": str(tamanho)})
            if not up.get("success"):
                raise Erro(f"o envio do vídeo não foi aceito: {up}")
        codigo, status = esperar(container)
        if codigo != "FINISHED":
            anotar(chave, estado="falhou", container=None, obs=f"{codigo}: {status}")
            raise Erro(f"o Instagram não aceitou o vídeo ({codigo}: {status})")

    media = api("POST", f"{conta['ig']}/media_publish", {"creation_id": container})["id"]
    link = api("GET", media, {"fields": "permalink"}).get("permalink", "")
    anotar(chave, estado="postado", container=container, media=media, link=link,
           postado_em=datetime.now().isoformat(timespec="seconds"))
    print(f"    POSTADO: {link or media}")


# ---------------------------------------------------------------- comandos

def cmd_fila(_):
    reg, agora = ler_registro(), datetime.now()
    for item in ler_fila():
        try:
            chave = chave_de(item)
            e = reg.get(chave, {}).get("estado")
            if e == "postado":
                print(f"{item['quando']:%d/%m %H:%M}  {chave:<46} postado    {reg[chave].get('link', '')}")
                continue
            p = montar(item)
        except Erro as e:
            print(f"{item['quando']:%d/%m %H:%M}  {sigla(item['canal'])} {item['video']:<42} ERRO: {e}")
            continue
        est = {"enviando": "a retomar", "falhou": "falhou"}.get(e) or ("vencido" if p["quando"] <= agora else "agendado")
        aviso = f"  !! {'; '.join(p['problemas'])}" if p["problemas"] else ""
        capa_txt = p["capa"].name if p["capa"] else f"quadro {p['capa_ms']} ms"
        print(f"{p['quando']:%d/%m %H:%M}  {p['chave']:<46} {est:<9}  capa: {capa_txt}{aviso}")


def cmd_contas(_):
    contas = contas_disponiveis()
    if CFG["via"] == "facebook":
        try:
            tok = os.environ["META_TOKEN"].strip()
            dbg = chamar("GET", f"{HOSTS['facebook']}/debug_token", {"input_token": tok}, tok=tok).get("data", {})
            exp = dbg.get("expires_at")
            validade = f"{datetime.fromtimestamp(exp):%d/%m/%Y %H:%M}" if exp else "sem data de vencimento"
            print(f"token válido: {'sim' if dbg.get('is_valid') else 'NÃO'}; vence em: {validade}")
            faltam = PERMISSOES_FB - set(dbg.get("scopes", []))
            if faltam:
                print(f"permissões que faltam no token: {', '.join(sorted(faltam))}")
        except Erro as e:
            print(f"(não consegui ler a validade do token: {e})")
    for s, canal in CANAIS.items():
        c = contas.get(canal)
        print(f"  {s} -> {'@' + c['usuario'] if c else '(sem conta configurada)'}")


def cmd_publicar(a):
    itens, reg, agora = ler_fila(), ler_registro(), datetime.now()
    if a.canal or a.video:
        if not (a.canal and a.video):
            raise Erro("use --canal e --video juntos")
        canal = canal_de(a.canal)
        alvo = achar_stem(canal, a.video)
        itens = [i for i in itens if i["canal"] == canal and achar_stem(canal, i["video"]) == alvo] or \
                [{"quando": agora, "canal": canal, "video": a.video, "capa_ms": 0}]
    else:
        itens = [i for i in itens if i["quando"] <= agora]
    fila, erros = [], 0
    for item in itens:
        try:
            if reg.get(chave_de(item), {}).get("estado") == "postado":
                continue
            fila.append(montar(item))
        except Erro as e:
            erros += 1
            print(f"ERRO em {sigla(item['canal'])} {item['video']}: {e}")
    if not fila:
        print("nada para publicar agora.")
        sys.exit(1 if erros else 0)

    modo = "PUBLICAÇÃO DE VERDADE" if a.de_verdade else "SIMULAÇÃO (nada sai daqui; para publicar, --de-verdade)"
    print(f"{modo}, caminho {CFG['via']}: {len(fila)} item(ns)\n")
    for p in fila:
        info = p["info"]
        print(f"■ {p['chave']}  (fila: {p['quando']:%d/%m %H:%M})")
        detalhes = [f"{info['mb']:.1f} MB"] + ([f"{info['s']:.1f} s"] if "s" in info else []) + \
                   [info[k] for k in ("video", "audio") if k in info]
        print(f"    vídeo: {p['arquivo'].name}  {'  '.join(detalhes)}")
        if CFG["via"] == "instagram" and CFG["midia_url"]:
            print(f"    endereço público: {url_publica(p, 'video.mp4')}")
        print(f"    capa:  {rel(p['capa']) if p['capa'] else 'quadro de %d ms' % p['capa_ms']}")
        gancho = p["legenda"].splitlines()[0]
        print(f"    legenda: {len(p['legenda'])} caracteres; gancho ({len(gancho)}): {gancho}")
        for prob in p["problemas"]:
            print(f"    !! {prob}")
        print()
    if not a.de_verdade:
        sys.exit(1 if erros else 0)
    if CFG["via"] == "instagram" and not CFG["midia_url"]:
        raise Erro("o caminho instagram precisa de --midia-url (o endereço público dos vídeos)")

    contas = contas_disponiveis()
    feitos = []
    for p in fila:
        print(f"■ {p['chave']}", flush=True)
        try:
            if p["problemas"]:
                raise Erro("o arquivo tem problemas (acima); corrija antes de postar")
            conta = contas.get(p["canal"])
            if not conta:
                raise Erro(f"o canal {sigla(p['canal'])} não tem token/conta configurada")
            if sys.stdin.isatty() and not a.sem_perguntar:
                if input(f"    publicar agora em @{conta['usuario']}? digite SIM: ").strip() != "SIM":
                    print("    pulado")
                    continue
            publicar_item(p, conta)
            feitos.append(p["chave"])
        except Erro as e:
            erros += 1
            print(f"    ERRO: {e}")
    if a.saida_postados:
        Path(a.saida_postados).write_text("".join(f"{k}\n" for k in feitos), encoding="utf-8", newline="\n")
    sys.exit(1 if erros else 0)


def git(repo, *args, capturar=False):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        raise Erro(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip() if capturar else None


def cmd_sincronizar(a):
    """Leva a fila e os vídeos pendentes para o repositório do GitHub (caminho instagram)."""
    repo = Path(a.repo).resolve()
    if not (repo / ".git").exists():
        raise Erro(f"{repo} não é um clone git (veja postagem/LEIA-ME.md, caminho A)")
    origem = git(repo, "remote", "get-url", "origin", capturar=True) if a.enviar else None
    if a.enviar:
        git(repo, "pull", "--ff-only", "--quiet")
    postados = json.loads((repo / "postados.json").read_text(encoding="utf-8")) if (repo / "postados.json").exists() else {}

    pendentes, linhas = [], ["quando;canal;video;capa_ms"]
    for item in ler_fila():
        p = montar(item)
        linhas.append(f"{item['quando']:%Y-%m-%d %H:%M};{sigla(item['canal'])};{p['stem']};{item['capa_ms']}")
        if postados.get(p["chave"], {}).get("estado") != "postado":
            if p["problemas"]:
                raise Erro(f"{p['chave']}: {'; '.join(p['problemas'])}")
            pendentes.append(p)

    # branch "midia": só os pendentes, num commit único sem histórico, para o repositório não crescer
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for p in pendentes:
            d = tmp / sigla(p["canal"]) / p["stem"]
            d.mkdir(parents=True)
            shutil.copy2(p["arquivo"], d / "video.mp4")
            (d / "legenda.txt").write_text(p["legenda"] + "\n", encoding="utf-8")
            if p["capa"]:
                shutil.copy2(p["capa"], d / "capa.jpg")
        (tmp / "LEIA-ME.txt").write_text("Vídeos na fila do Instagram. Cada pasta some depois de postada.\n",
                                          encoding="utf-8")
        git(tmp, "init", "--quiet", "--initial-branch=midia")
        git(tmp, "add", "-A")
        git(tmp, "-c", "user.name=postar.py", "-c", "user.email=postar@localhost", "commit", "--quiet", "-m",
            f"midia: {len(pendentes)} vídeo(s) na fila")
        mb = sum(p["arquivo"].stat().st_size for p in pendentes) / 1024 / 1024
        if a.enviar:
            git(tmp, "push", "--force", "--quiet", origem, "midia:midia")

    # branch principal: fila, script e workflow
    shutil.copy2(Path(__file__), repo / "postar.py")
    (repo / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "postagem" / "github" / "postar.yml", repo / ".github" / "workflows" / "postar.yml")
    shutil.copy2(ROOT / "postagem" / "github" / "README.md", repo / "README.md")
    (repo / "fila.csv").write_text("\n".join(linhas) + "\n", encoding="utf-8")
    git(repo, "add", "-A")
    if git(repo, "status", "--porcelain", capturar=True):
        git(repo, "-c", "user.name=postar.py", "-c", "user.email=postar@localhost", "commit", "--quiet", "-m",
            f"fila: {len(pendentes)} pendente(s)")
    if a.enviar:
        git(repo, "push", "--quiet", "origin", "HEAD")

    print(f"{len(pendentes)} vídeo(s) pendente(s), {mb:.0f} MB no branch midia:")
    for p in pendentes:
        print(f"  {p['quando']:%d/%m %H:%M}  {p['chave']}")
    print("enviado ao GitHub." if a.enviar else
          "\nnada foi enviado ao GitHub (commit só local). Para enviar, repita com --enviar.")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Posta os Reels da fila no Instagram (Graph API, grátis).")
    ap.add_argument("--via", choices=["instagram", "facebook"], default=os.environ.get("POSTAR_VIA", "instagram"),
                    help="instagram (GitHub Actions, padrão) ou facebook (PC ligado)")
    ap.add_argument("--pasta", help="onde ficam fila.csv e postados.json (padrão: postagem/)")
    ap.add_argument("--midia", help="pasta com <canal>/<NN-slug>/video.mp4, capa.jpg e legenda.txt (GitHub)")
    ap.add_argument("--midia-url", default=os.environ.get("MIDIA_URL"), help="endereço público dessa pasta")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fila", help="mostra a fila e o estado de cada item")
    sub.add_parser("contas", help="mostra as contas que os tokens alcançam")
    pub = sub.add_parser("publicar", help="publica os itens vencidos (simulação, sem --de-verdade)")
    pub.add_argument("--de-verdade", action="store_true", help="publica de fato; sem isso é simulação")
    pub.add_argument("--canal", help="só este canal (sd, ai, fm, fb), junto com --video")
    pub.add_argument("--video", help="só este vídeo (número ou nome), mesmo fora do horário")
    pub.add_argument("--sem-perguntar", action="store_true", help="não pede SIM item a item (agendadores)")
    pub.add_argument("--saida-postados", help="arquivo onde listar o que foi postado nesta execução")
    sin = sub.add_parser("sincronizar", help="prepara o repositório do GitHub com a fila e os vídeos pendentes")
    sin.add_argument("--repo", required=True, help="pasta do clone local do repositório")
    sin.add_argument("--enviar", action="store_true", help="dá push no GitHub; sem isso, só commit local")
    a = ap.parse_args()
    CFG["via"] = a.via
    CFG["pasta"] = Path(a.pasta).resolve() if a.pasta else ROOT / "postagem"
    CFG["midia"] = Path(a.midia).resolve() if a.midia else None
    CFG["midia_url"] = a.midia_url
    try:
        {"fila": cmd_fila, "contas": cmd_contas, "publicar": cmd_publicar, "sincronizar": cmd_sincronizar}[a.cmd](a)
    except Erro as e:
        print(f"erro: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
