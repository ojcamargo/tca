#!/usr/bin/env python3
# tca.py — TXT/DOCX/PDF -> MP3 (com OCR) — Windows & Linux
# TTS backends: gTTS (online), edge-tts (online Microsoft), pyttsx3 (offline SAPI/espeak)
# Python 3.9+
# Observacao: todos os comentarios e mensagens de log estao sem acentos/cedilha para evitar problemas de encoding.

import argparse
import os
import shutil
import sys
import tempfile
import textwrap
import time
import asyncio
from pathlib import Path

MISSING_DEPS = []

# ===== TTS backends (importados opcionalmente) =====
try:
    from gtts import gTTS
    HAVE_GTTS = True
except Exception:
    HAVE_GTTS = False

try:
    import edge_tts
    HAVE_EDGE = True
except Exception:
    HAVE_EDGE = False

try:
    import pyttsx3
    HAVE_PYTTSX3 = True
except Exception:
    HAVE_PYTTSX3 = False

# ===== Audio (merge / conversao) =====
try:
    from pydub import AudioSegment
    HAVE_PYDUB = True
except Exception:
    HAVE_PYDUB = False
    MISSING_DEPS.append("pydub")

# ===== DOCX =====
try:
    import docx  # python-docx
    HAVE_DOCX = True
except Exception:
    HAVE_DOCX = False

# ===== PDF (texto) =====
try:
    from pypdf import PdfReader
    HAVE_PDF = True
except Exception:
    HAVE_PDF = False

# ===== OCR =====
try:
    import pytesseract
    from PIL import Image
    HAVE_TESS = True
except Exception:
    HAVE_TESS = False

# ===== Rasterizacao PDF sem Poppler (PyMuPDF) =====
try:
    import fitz  # PyMuPDF
    HAVE_FITZ = True
except Exception:
    HAVE_FITZ = False

SUPPORTED_EXTS = {".txt", ".docx", ".pdf"}


def die(msg: str, code: int = 1):
    """
    Encerra o programa com mensagem simples em stderr e codigo de saida informado.
    Usado para erros fatais e validacoes.
    """
    print(f"Erro: {msg}", file=sys.stderr)
    sys.exit(code)


def check_deps(args):
    """
    Verifica dependencias dinamicamente com base no que sera usado.
    - DOCX requer python-docx.
    - PDF requer pypdf.
    - OCR requer pymupdf (fitz) + pytesseract + Pillow.
    - Merge de audio requer pydub (e ffmpeg no sistema).
    - TTS selecionado determina modulos obrigatorios.
    """
    missing = []
    if any(p.suffix.lower() == ".docx" for p in args.inputs) and not HAVE_DOCX:
        missing.append("python-docx")
    if any(p.suffix.lower() == ".pdf" for p in args.inputs) and not HAVE_PDF:
        missing.append("pypdf")
    if args.ocr and (not HAVE_TESS or not HAVE_FITZ):
        if not HAVE_TESS:
            missing.append("pytesseract/Pillow (e binario do Tesseract no sistema)")
        if not HAVE_FITZ:
            missing.append("pymupdf")
    if not HAVE_PYDUB:
        missing.append("pydub")
    # backends TTS
    if args.tts == "gtts" and not HAVE_GTTS:
        missing.append("gTTS")
    if args.tts == "edge" and not HAVE_EDGE:
        missing.append("edge-tts")
    if args.tts == "pyttsx3" and not HAVE_PYTTSX3:
        missing.append("pyttsx3")
    if missing:
        die(
            "Dependencias em falta: "
            + ", ".join(sorted(set(missing)))
            + "\nInstale com: pip install -r requirements.txt\n"
            + "Tambem precisa de ffmpeg (pydub) e Tesseract (para OCR opcional).\n"
        )


def read_txt(path: Path) -> str:
    """
    Lê ficheiro TXT testando encodings comuns para evitar erros de decodificacao.
    Retorna string com o conteudo.
    """
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return Path(path).read_text(encoding=enc)
        except Exception:
            continue
    die(f"Nao foi possivel ler TXT: {path}")


def read_docx(path: Path) -> str:
    """
    Extrai texto de um DOCX usando python-docx.
    Retorna todo o texto concatenado por linhas.
    """
    doc = docx.Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    return "\n".join(parts)


def extract_text_pdf_pypdf(path: Path) -> str:
    """
    Extrai texto de PDF usando pypdf.
    Pode falhar em alguns PDFs; nesse caso faremos fallback com PyMuPDF.
    """
    texts = []
    try:
        reader = PdfReader(str(path))
        for i, page in enumerate(reader.pages):
            try:
                texts.append(page.extract_text() or "")
            except Exception as e:
                print(f"Aviso: pypdf falhou na pagina {i+1} de {path.name}: {e}", file=sys.stderr)
                texts.append("")
    except Exception as e:
        print(f"Aviso: pypdf nao conseguiu abrir {path.name}: {e}", file=sys.stderr)
    return "\n".join(texts).strip()


def extract_text_pdf_fitz(path: Path) -> str:
    """
    Fallback de extracao de texto via PyMuPDF (get_text) sem OCR.
    Funciona melhor em alguns PDFs quando pypdf falha.
    """
    if not HAVE_FITZ:
        return ""
    out = []
    try:
        with fitz.open(str(path)) as doc:
            for i, page in enumerate(doc, start=1):
                try:
                    out.append(page.get_text("text") or "")
                except Exception as e:
                    print(f"Aviso: fitz.get_text falhou na pagina {i}: {e}", file=sys.stderr)
                    out.append("")
    except Exception as e:
        print(f"Aviso: PyMuPDF nao conseguiu abrir {path.name}: {e}", file=sys.stderr)
    return "\n".join(out).strip()


def ocr_pdf_via_fitz(path: Path, dpi: int, ocr_lang: str) -> str:
    """
    Executa OCR: rasteriza cada pagina com PyMuPDF e aplica pytesseract.
    - dpi controla a resolucao da rasterizacao (300 recomendado).
    - ocr_lang e o codigo de idioma do tesseract, ex.: 'por', 'eng'.
    """
    if not HAVE_FITZ or not HAVE_TESS:
        die("OCR indisponivel: precisa de pymupdf + pytesseract + Tesseract instalado.")
    texts = []
    zoom = dpi / 72.0  # 72 dpi e base do PDF; zoom escala para o dpi alvo
    mat = fitz.Matrix(zoom, zoom)
    with fitz.open(str(path)) as doc:
        for i, page in enumerate(doc, start=1):
            try:
                # rasteriza pagina em pixmap (imagem)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                # aplica OCR
                txt = pytesseract.image_to_string(img, lang=ocr_lang)
            except Exception as e:
                print(f"Aviso: OCR falhou na pagina {i}: {e}", file=sys.stderr)
                txt = ""
            texts.append(txt.strip())
    return "\n\n".join(texts).strip()


def read_pdf(path: Path, use_ocr: bool, dpi: int, ocr_lang: str) -> str:
    """
    Extrai texto do PDF com a seguinte estrategia:
    1) Tenta pypdf.
    2) Se pouco texto, tenta PyMuPDF get_text (sem OCR).
    3) Se ainda pobre e OCR ativado, roda OCR pagina a pagina.
    """
    base = extract_text_pdf_pypdf(path)
    if len(base) < 50:
        alt = extract_text_pdf_fitz(path)
        if len(alt) > len(base):
            print(f"Info: extracao via PyMuPDF (sem OCR) foi melhor em {path.name}.", file=sys.stderr)
            base = alt
    if use_ocr and len(base) < 50:
        print(f"Nenhum texto util detectado em {path.name}; executando OCR...", file=sys.stderr)
        ocr_txt = ocr_pdf_via_fitz(path, dpi=dpi, ocr_lang=ocr_lang)
        if len(ocr_txt) > len(base):
            base = ocr_txt
    return base.strip()


def load_text_from_file(path: Path, args) -> str:
    """
    Roteia a leitura com base na extensao (.txt, .docx, .pdf).
    """
    ext = path.suffix.lower()
    if ext == ".txt":
        return read_txt(path)
    elif ext == ".docx":
        return read_docx(path)
    elif ext == ".pdf":
        return read_pdf(path, use_ocr=args.ocr, dpi=args.ocr_dpi, ocr_lang=args.ocr_lang)
    else:
        die(f"Extensao nao suportada: {ext}")


def normalize_text(s: str) -> str:
    """
    Normaliza espacos e linhas:
    - strip por linha
    - remove linhas vazias
    """
    lines = [line.strip() for line in s.splitlines()]
    return "\n".join(line for line in lines if line)


def split_sentences(text: str) -> list:
    """
    Divide texto em frases usando pontuacao comum.
    E simples, mas suficiente para cortes de chunks.
    """
    import re
    parts = re.split(r'(?<=[\.\!\?\:;])\s+', text)
    return [p.strip() for p in parts if p.strip()]


def chunk_text(s: str, max_chars: int = 4500) -> list:
    """
    Divide o texto em blocos com no maximo max_chars.
    Mantem o maximo possivel de paragrafo e frase.
    """
    if len(s) <= max_chars:
        return [s]
    paras = s.split("\n")
    chunks, buf = [], ""
    for para in paras:
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:
            # quebra por frases se o paragrafo for muito grande
            for sent in split_sentences(para):
                if len(buf) + len(sent) + 1 > max_chars:
                    if buf:
                        chunks.append(buf.strip()); buf = ""
                buf += sent + " "
        else:
            if len(buf) + len(para) + 1 > max_chars:
                if buf:
                    chunks.append(buf.strip()); buf = ""
            buf += para + "\n"
    if buf.strip():
        chunks.append(buf.strip())
    # garante que nenhum chunk estoure o limite
    final = []
    for c in chunks:
        if len(c) <= max_chars:
            final.append(c)
        else:
            final.extend([c[i:i+max_chars] for i in range(0, len(c), max_chars)])
    return final


# ===================== TTS backends =====================

def synth_gtts_to_mp3(text: str, lang: str, tld: str, slow: bool,
                      out: Path, retries: int, wait: float, backoff: float, throttle: float):
    """
    Sintese com gTTS (online).
    - Salva diretamente em MP3 no caminho 'out'.
    - Faz retentativas com backoff para lidar com erros 429/timeout.
    - Aplica pausa entre chunks (throttle) para evitar limitacao.
    - Implementa resume: se o arquivo ja existir e tiver tamanho > 0, nao refaz.
    """
    if out.exists() and out.stat().st_size > 0:
        print(f"[resume] ja existe: {out.name}")
        return
    if not HAVE_GTTS:
        die("Selecionado --tts gtts mas gTTS nao esta instalado.")
    attempt, delay, last_exc = 0, max(0.1, wait), None
    while attempt <= retries:
        try:
            tts = gTTS(text=text, lang=lang, tld=tld, slow=slow)
            tts.save(str(out))
            if throttle and throttle > 0:
                time.sleep(throttle)
            return
        except Exception as e:
            last_exc = e
            attempt += 1
            if attempt > retries:
                break
            print(f"[retry:gtts] erro {type(e).__name__} -> tentativa {attempt}/{retries} em {delay:.1f}s...")
            time.sleep(delay)
            delay *= max(1.0, backoff)
    try:
        if out.exists() and out.stat().st_size == 0:
            out.unlink(missing_ok=True)
    except Exception:
        pass
    die(f"Falha definitiva (gTTS): {last_exc}")


async def _edge_save_async(text: str, voice: str, outfile: Path):
    """
    edge-tts: executa a chamada assinc para salvar um MP3 com a voz informada.
    """
    communicate = edge_tts.Communicate(text, voice=voice)
    await communicate.save(str(outfile))


def synth_edge_to_mp3(text: str, voice: str, out: Path,
                      retries: int, wait: float, backoff: float, throttle: float):
    """
    Sintese com edge-tts (online Microsoft).
    - Salva MP3 direto.
    - Retentativas com backoff e pausa entre chunks.
    - Resume se arquivo ja existir.
    """
    if out.exists() and out.stat().st_size > 0:
        print(f"[resume] ja existe: {out.name}")
        return
    if not HAVE_EDGE:
        die("Selecionado --tts edge mas edge-tts nao esta instalado.")
    attempt, delay, last_exc = 0, max(0.1, wait), None
    while attempt <= retries:
        try:
            asyncio.run(_edge_save_async(text, voice, out))
            if throttle and throttle > 0:
                time.sleep(throttle)
            return
        except Exception as e:
            last_exc = e
            attempt += 1
            if attempt > retries:
                break
            print(f"[retry:edge] erro {type(e).__name__} -> tentativa {attempt}/{retries} em {delay:.1f}s...")
            time.sleep(delay)
            delay *= max(1.0, backoff)
    try:
        if out.exists() and out.stat().st_size == 0:
            out.unlink(missing_ok=True)
    except Exception:
        pass
    die(f"Falha definitiva (edge-tts): {last_exc}")


def _pyttsx3_pick_voice(engine, prefer_voice: str | None, lang_hint: str) -> str | None:
    """
    Seleciona uma voz para pyttsx3:
    - se prefer_voice foi informada, tenta casar pelo id ou name
    - senao tenta PT-BR, depois PT, senao a primeira voz disponivel
    """
    voices = engine.getProperty("voices") or []
    if prefer_voice:
        for v in voices:
            if prefer_voice.lower() in (v.id or "").lower() or prefer_voice.lower() in (v.name or "").lower():
                return v.id
    cand = None
    lang_hint = (lang_hint or "pt").lower()
    for v in voices:
        lang = ",".join(getattr(v, "languages", []) or [])
        name = (getattr(v, "name", "") or "").lower()
        vid = (getattr(v, "id", "") or "").lower()
        blob = f"{lang}|{name}|{vid}"
        if "pt" in blob:
            if "br" in blob or "braz" in blob:
                return v.id
            cand = cand or v.id
    return cand or (voices[0].id if voices else None)


def synth_pyttsx3_to_mp3(text: str, voice_pref: str, out_mp3: Path):
    """
    Sintese offline com pyttsx3.
    - Gera WAV via SAPI (Windows) ou espeak (Linux) e converte para MP3 com pydub/ffmpeg.
    - Implementa resume se o MP3 final ja existir.
    """
    if out_mp3.exists() and out_mp3.stat().st_size > 0:
        print(f"[resume] ja existe: {out_mp3.name}")
        return
    if not HAVE_PYTTSX3:
        die("Selecionado --tts pyttsx3 mas pyttsx3 nao esta instalado.")
    if not HAVE_PYDUB:
        die("pyttsx3 requer pydub/ffmpeg para converter WAV->MP3.")

    with tempfile.TemporaryDirectory(prefix="tca_wav_") as td:
        wav_path = Path(td) / (out_mp3.stem + ".wav")
        try:
            engine = pyttsx3.init()
            # seleciona a voz mais adequada
            vid = _pyttsx3_pick_voice(engine, voice_pref, "pt")
            if vid:
                engine.setProperty("voice", vid)
            # gera WAV em disco
            engine.save_to_file(text, str(wav_path))
            engine.runAndWait()
        except Exception as e:
            die(f"TTS offline (pyttsx3) falhou: {e}")

        # converte WAV -> MP3 via pydub/ffmpeg
        try:
            seg = AudioSegment.from_file(str(wav_path), format="wav")
            seg.export(str(out_mp3), format="mp3", bitrate="192k")
        except Exception as e:
            die(f"Falha ao converter WAV->MP3 (ffmpeg disponivel?): {e}")


def synth_chunk_to_mp3(tts_backend: str, text: str,
                       lang: str, tld: str, slow: bool,
                       voice: str,
                       tmpdir: Path, index: int,
                       retries: int, wait: float, backoff: float, throttle: float) -> Path:
    """
    Orquestra a sintese por chunk com o backend escolhido.
    Gera um ficheiro MP3 do chunk no diretorio temporario e retorna o caminho.
    """
    out = tmpdir / f"chunk_{index:04d}.mp3"
    if tts_backend == "gtts":
        synth_gtts_to_mp3(text, lang, tld, slow, out, retries, wait, backoff, throttle)
    elif tts_backend == "edge":
        synth_edge_to_mp3(text, voice or "pt-BR-AntonioNeural", out, retries, wait, backoff, throttle)
    elif tts_backend == "pyttsx3":
        synth_pyttsx3_to_mp3(text, voice, out)
    else:
        die(f"TTS backend desconhecido: {tts_backend}")
    return out


def merge_mp3s(files: list[Path], out_path: Path, ffmpeg_hint: str | None, mode: str = "pydub"):
    """
    Junta todos os MP3 de chunks num unico MP3 final.
    - mode='pydub' usa pydub (re-encode), requer ffmpeg instalado.
    - mode='ffmpegcopy' usa ffmpeg concat com -c copy (sem re-encode).
    """
    if not files:
        die("Nada para juntar.")
    if mode == "ffmpegcopy":
        ff = ffmpeg_hint or shutil.which("ffmpeg")
        if not ff:
            die("ffmpeg nao encontrado (use --ffmpeg PATH ou instale no PATH).")
        # cria arquivo de lista temporario para o concat
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8") as lst:
            for f in files:
                lst.write(f"file '{str(Path(f).resolve()).replace('\\', '\\\\')}'\n")
            list_path = lst.name
        try:
            cmd = [ff, "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", str(out_path)]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                die(f"ffmpeg concat falhou: {proc.stderr.strip()}")
        finally:
            try:
                Path(list_path).unlink(missing_ok=True)
            except Exception:
                pass
        return

    # modo pydub (default)
    if not HAVE_PYDUB:
        die("pydub nao esta disponivel para merge; use --merge-mode ffmpegcopy")
    if ffmpeg_hint:
        # forca caminho do ffmpeg no pydub se informado
        AudioSegment.converter = ffmpeg_hint
        AudioSegment.ffmpeg = ffmpeg_hint
        from shutil import which as _which
        AudioSegment.ffprobe = _which("ffprobe") or ffmpeg_hint.replace("ffmpeg", "ffprobe")
    audio = AudioSegment.empty()
    try:
        for f in files:
            audio += AudioSegment.from_file(str(f), format="mp3")
        audio.export(str(out_path), format="mp3", bitrate="192k")
    except Exception as e:
        die(f"Merge pydub falhou: {e}")


def build_cli():
    """
    Define e retorna o parser de argumentos de linha de comando.
    Mantem nomes simples e sem acentuacao.
    """
    p = argparse.ArgumentParser(
        prog="tca.py",
        description="Converte TXT/DOCX/PDF em um unico MP3. OCR automatico em PDFs digitalizados.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Exemplos:
              python tca.py doc.pdf -o narracao.mp3
              python tca.py cap1.docx cap2.docx notas.txt -o livro.mp3
              python tca.py *.pdf -o reuniao.mp3 --slow
              # escolher TTS:
              python tca.py doc.pdf -o out.mp3 --tts edge --voice pt-BR-AntonioNeural
              python tca.py doc.pdf -o out.mp3 --tts pyttsx3 --voice "Microsoft Maria"
        """),
    )
    p.add_argument("inputs", nargs="+", type=Path, help="Ficheiros de entrada (TXT, DOCX, PDF).")
    # output so exigido quando vamos juntar
    p.add_argument("-o", "--output", required=False, type=Path, help="Ficheiro MP3 de saida (obrigatorio exceto com --no-merge).")

    # TTS
    p.add_argument("--tts", choices=["gtts", "edge", "pyttsx3"], default="gtts",
                   help="Backend TTS: gtts (online), edge (online), pyttsx3 (offline). Default: gtts.")
    p.add_argument("--voice", default=None,
                   help="Nome/ID da voz (edge/pyttsx3). Ex.: pt-BR-AntonioNeural no edge; nome da voz no SAPI/espeak para pyttsx3.")
    p.add_argument("--lang", default="pt", help="Codigo de lingua gTTS (default: pt).")
    p.add_argument("--tld", default="com.br", help="TLD gTTS (com.br=BR, pt=PT). Default: com.br.")
    p.add_argument("--slow", action="store_true", help="Fala mais pausada (gTTS).")

    # Retentativas e throttle (para backends online)
    p.add_argument("--retry", type=int, default=8, help="Retentativas por chunk em falha (default: 8).")
    p.add_argument("--retry-wait", type=float, default=3.0, help="Espera inicial entre retentativas (s). Default: 3.0.")
    p.add_argument("--retry-backoff", type=float, default=1.7, help="Fator de backoff exponencial. Default: 1.7.")
    p.add_argument("--throttle", type=float, default=1.2, help="Pausa entre chunks (s) para evitar 429. Default: 1.2.")

    p.add_argument("--max-chars", type=int, default=4500, help="Tamanho maximo por segmento. Default: 4500.")

    # OCR
    p.add_argument("--ocr", action="store_true", default=True, help="Ativa OCR automatico (Default: ativo)")
    p.add_argument("--no-ocr", dest="ocr", action="store_false", help="Desativa OCR.")
    p.add_argument("--ocr-lang", default="por", help="Idioma do Tesseract (ex.: 'por', 'eng'). Default: por.")
    p.add_argument("--ocr-dpi", type=int, default=300, help="DPI de rasterizacao para OCR. Default: 300.")

    # Merge e ajustes
    p.add_argument("--chunks-dir", type=Path, default=None, help="Guarda os MP3 de cada segmento neste diretorio (permite retomar).")
    p.add_argument("--no-merge", action="store_true", help="Nao junta; apenas cria segmentos.")
    p.add_argument("--merge-mode", choices=["pydub","ffmpegcopy"], default="pydub",
                   help="Metodo de juncao: pydub (default) ou ffmpegcopy (concat copy).")
    p.add_argument("--ffmpeg", dest="ffmpeg_path", default=None, help="Caminho do ffmpeg (opcional).")
    return p


def main():
    """
    Ponto de entrada principal:
    - Valida argumentos, dependencias e entradas
    - Le e normaliza textos
    - Divide em chunks
    - Sintetiza audio por chunk usando backend selecionado
    - Faz merge (se aplicavel) para MP3 final
    """
    args = build_cli().parse_args()

    # exige -o quando vamos juntar
    if not args.no_merge and not args.output:
        die("Falta o argumento -o/--output (obrigatorio quando nao usa --no-merge).")

    # valida inputs
    if not args.inputs:
        die("Indique pelo menos um ficheiro.")
    for pth in args.inputs:
        if not pth.exists():
            die(f"Ficheiro nao encontrado: {pth}")
        if pth.suffix.lower() not in SUPPORTED_EXTS:
            die(f"Extensao nao suportada em '{pth.name}'. Suportado: {', '.join(sorted(SUPPORTED_EXTS))}")

    check_deps(args)

    # le todos os ficheiros, mantem ordem recebida
    all_texts = []
    for p in args.inputs:
        print(f"Lendo: {p}")
        txt = load_text_from_file(p, args)
        all_texts.append(txt)

    combined = normalize_text("\n\n".join(all_texts))
    print(f"[diag] caracteres extraidos: {len(combined)}")
    if not combined.strip():
        die("Nao foi extraido texto util dos ficheiros fornecidos.")

    chunks = chunk_text(combined, max_chars=args.max_chars)
    print(f"[diag] segmentos (chunks): {len(chunks)}")
    if not chunks:
        die("Falha inesperada: nenhum segmento gerado.")

    # escolhe diretorio para chunks: persistente (--chunks-dir) ou temporario
    if args.chunks_dir:
        args.chunks_dir.mkdir(parents=True, exist_ok=True)
        tmpdir = args.chunks_dir
        cleanup_tmp = False
    else:
        tmpdir_obj = tempfile.TemporaryDirectory(prefix="tca_")
        tmpdir = Path(tmpdir_obj.name)
        cleanup_tmp = True

    mp3_parts: list[Path] = []

    try:
        # sintetiza chunk a chunk conforme backend
        for i, chunk in enumerate(chunks, start=1):
            print(f"Sintetizando segmento {i}/{len(chunks)}... (tts={args.tts})")
            part = synth_chunk_to_mp3(
                tts_backend=args.tts,
                text=chunk, lang=args.lang, tld=args.tld, slow=args.slow,
                voice=args.voice,
                tmpdir=tmpdir, index=i,
                retries=args.retry, wait=args.retry_wait, backoff=args.retry_backoff, throttle=args.throttle
            )
            mp3_parts.append(part)

        # se nao for juntar, termina aqui (modo --no-merge)
        if args.no_merge:
            print(f"Segmentos criados em: {tmpdir}")
            print("Finalizando sem juntar (--no-merge).")
            return

        # realiza juncao num unico MP3
        out_path = args.output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"A juntar {len(mp3_parts)} segmentos em: {out_path}")

        merge_mp3s(mp3_parts, out_path, ffmpeg_hint=args.ffmpeg_path, mode=args.merge_mode)

        # checagem pos-exportacao
        out_abs = out_path.resolve()
        if not out_abs.exists():
            die(f"Falha: exportacao nao criou o ficheiro: {out_abs}")
        size = out_abs.stat().st_size
        if size == 0:
            die(f"Falha: ficheiro criado mas com 0 bytes: {out_abs}")
        print(f"Feito: {out_abs} ({size} bytes)")
    finally:
        # limpa diretorio temporario se usado
        if not args.chunks_dir and cleanup_tmp:
            try:
                tmpdir_obj.cleanup()
            except Exception:
                pass


if __name__ == "__main__":
    main()
