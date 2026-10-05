"""
Cliente del servicio HTTP: manda una base al motor y guarda las cuatro salidas OTBB.

    python scripts/cliente_api.py base.csv  --contexto santander,cl
    python scripts/cliente_api.py base.xlsx --url http://192.168.1.50:8000

Existe para consumir el mismo flujo que salidas_otbb.py contra el servicio
desplegado: mismo catálogo, misma versión y misma API key que va a usar el OTBB.

Si la base cabe en un solo lote, llama POST /procesar-otbb?formato=zip.
Si no, parte por hilo con POST /procesar-lote y arma las cuatro salidas acá con
motor.salidas — un hilo nunca debe quedar partido entre dos llamadas.

La API key NUNCA se pasa por argumento: sale de --key-file o de la variable
API_KEY. Un argumento queda en el historial del shell.
"""

import argparse
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motor import columnas  # noqa: E402
from motor.salidas import armar_salidas, escribir_salidas  # noqa: E402


def leer_key(key_file):
    if key_file:
        return Path(key_file).read_text(encoding="utf-8").strip()
    key = os.environ.get("API_KEY", "").strip()
    if not key:
        sys.exit("Sin API key. Pasá --key-file RUTA o exportá API_KEY. "
                 "No la pases por argumento: queda en el historial del shell.")
    return key


def lotes_por_hilo(mensajes, maximo):
    """Corta en lotes de a lo más `maximo` mensajes, sin partir un hilo."""
    grupos = {}
    for i, m in enumerate(mensajes):
        grupos.setdefault(m["hilo"] or f"__solo_{i}__", []).append((i, m))

    lotes, actual = [], []
    for pares in grupos.values():
        if actual and len(actual) + len(pares) > maximo:
            lotes.append(actual)
            actual = []
        actual.extend(pares)
    if actual:
        lotes.append(actual)
    return lotes


def _headers(key):
    return {"x-api-key": key}


def _health(url, timeout):
    try:
        h = requests.get(f"{url}/health", timeout=timeout)
    except requests.RequestException as e:
        sys.exit(f"No se pudo hablar con {url}: {e}")
    if h.status_code != 200:
        sys.exit(f"El servicio no está sano: {h.status_code} {h.text}")
    return h.json()


def _procesar_otbb_zip(url, key, mensajes, contexto, stem, timeout):
    r = requests.post(
        f"{url}/procesar-otbb",
        params={"formato": "zip"},
        headers=_headers(key),
        json={"mensajes": mensajes, "contexto": contexto, "nombre_base": stem},
        timeout=timeout,
    )
    if r.status_code != 200:
        sys.exit(f"HTTP {r.status_code} {r.text[:400]}")
    return r.content


def _procesar_lote(url, key, mensajes, contexto, timeout):
    r = requests.post(
        f"{url}/procesar-lote",
        headers=_headers(key),
        json={"mensajes": mensajes, "contexto": contexto},
        timeout=timeout,
    )
    if r.status_code != 200:
        sys.exit(f"HTTP {r.status_code} {r.text[:400]}")
    return r.json()


def _extraer_zip(zip_bytes, destino):
    destino = Path(destino)
    destino.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(destino)
    return sorted(destino.glob("*"))


def _imprimir_resumen(salidas, destino):
    res = salidas["resumen"]
    total = res["correos_en_base"]
    print("\n=== RESUMEN ===")
    print(f"correos en la base            {total}")
    print(f"  exclusiones marcadas        {res['exclusiones_marcadas']}  "
          f"({res['pct_exclusiones']}%)")
    print(f"  filas repetidas del archivo {res['filas_repetidas_archivo']}  "
          f"({res['pct_duplicados']}%)")
    print(f"  van al maestro              {res['van_al_maestro']}  "
          f"({res['pct_maestro']}%)")
    print(f"hilos                         {res['hilos']}")
    print(f"banners removidos             {res['banners_removidos']}")
    print(f"insistencias                  {res['insistencias']} en "
          f"{res['hilos_con_insistencia']} hilos")
    print(f"texto: {res['chars_original']:,} -> {res['chars_para_prompt']:,} caracteres "
          f"({res['ahorro_pct']}% menos)")
    if salidas["sin_codigo"]:
        print(f"\nAVISO: {len(salidas['sin_codigo'])} tipificación(es) sin código WM")
    if salidas["avisos"]:
        print("\n=== AVISOS ===")
        for a in dict.fromkeys(salidas["avisos"]):
            print(" -", a)
    for p in destino.values():
        print(f"\n{p}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base", help="csv o xlsx con los correos")
    ap.add_argument("--url", default=os.environ.get("MOTOR_URL", "http://localhost:8000"))
    ap.add_argument("--key-file", default=None, help="archivo con la API key")
    ap.add_argument("--contexto", default="", help="tokens de alcance, ej: santander,cl")
    ap.add_argument("--salida", default=None, help="carpeta (default: junto a la base)")
    ap.add_argument("--texto", default=None)
    ap.add_argument("--asunto", default=None)
    ap.add_argument("--hilo", default=None)
    ap.add_argument("--orden", dest="fecha", default=None)
    ap.add_argument("--remitente", default=None)
    ap.add_argument("--msgid", dest="msg_id", default=None)
    ap.add_argument("--sin-asunto", action="store_true")
    ap.add_argument("--max-lote", type=int, default=5000)
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    base = Path(args.base)
    if not base.exists():
        sys.exit(f"No existe: {base}")
    destino = Path(args.salida) if args.salida else base.parent
    destino.mkdir(parents=True, exist_ok=True)
    key = leer_key(args.key_file)
    contexto = [t for t in args.contexto.split(",") if t.strip()]

    salud = _health(args.url, min(args.timeout, 30))
    print(f"{args.url} | {salud['reglas_cargadas']} reglas | "
          f"catálogo {salud['catalogo']} | v{salud['version']}")

    df = columnas.leer_base(base)
    forzadas = {k: getattr(args, k) for k in
                ("texto", "asunto", "hilo", "fecha", "remitente", "msg_id")}
    if args.sin_asunto:
        forzadas["asunto"] = None
    col = columnas.detectar(df, forzadas)
    print(f"{len(df)} filas leídas de {base.name}")
    print("columnas -> " + "  ".join(f"{k}={v!r}" for k, v in col.items()
                                     if not k.startswith("ref_")))
    if not col["hilo"]:
        print("AVISO: sin columna de hilo no hay dedup de historial ni insistencia.")

    filas = columnas.mensajes_desde(df, col)
    lotes = lotes_por_hilo(filas, args.max_lote)
    print(f"{len(lotes)} llamada(s), sin partir hilos")

    if len(lotes) == 1:
        zip_bytes = _procesar_otbb_zip(
            args.url, key, filas, contexto, base.stem, args.timeout)
        zip_path = destino / f"{base.stem}_otbb.zip"
        zip_path.write_bytes(zip_bytes)
        print(f"ZIP recibido: {zip_path}")
        extraidos = _extraer_zip(zip_bytes, destino)
        for p in extraidos:
            if p.suffix == ".json":
                meta = json.loads(p.read_text(encoding="utf-8"))
                print("\n=== RESUMEN ===")
                for k, v in meta["resumen"].items():
                    print(f"{k:30} {v}")
                if meta.get("sin_codigo"):
                    print(f"\nAVISO: {len(meta['sin_codigo'])} tipificación(es) sin código WM")
                if meta.get("avisos"):
                    print("\n=== AVISOS ===")
                    for a in dict.fromkeys(meta["avisos"]):
                        print(" -", a)
            else:
                print(f"\n{p}")
        return 0

    # Varios lotes: procesar-lote + armar salidas localmente (misma lógica que el servicio).
    mensajes_proc = [None] * len(filas)
    avisos, resumenes = [], []
    for n, lote in enumerate(lotes, 1):
        posiciones = [p for p, _ in lote]
        payload = [m for _, m in lote]
        d = _procesar_lote(args.url, key, payload, contexto, args.timeout)
        if len(d["mensajes"]) != len(posiciones):
            sys.exit(f"lote {n}: el servicio devolvió {len(d['mensajes'])} mensajes "
                     f"para {len(posiciones)} enviados")
        for p, m in zip(posiciones, d["mensajes"]):
            mensajes_proc[p] = m
        avisos.extend(d["avisos"])
        resumenes.append(d["resumen"])
        print(f"  lote {n}/{len(lotes)}: {len(lote)} mensajes ok")

    resultado = {
        "mensajes": mensajes_proc,
        "avisos": list(dict.fromkeys(avisos)),
        "resumen": _sumar_resumenes(resumenes),
    }
    salidas = armar_salidas(filas, resultado, col=col)
    rutas = escribir_salidas(salidas, destino, base.stem)
    _imprimir_resumen(salidas, rutas)

    if salidas["resumen"].get("alertas_limpieza"):
        print(f"\nFALLA: en {salidas['resumen']['alertas_limpieza']} filas la limpieza "
              "dejó el mensaje casi vacío.")
        return 1
    return 0


def _sumar_resumenes(resumenes):
    tot = {k: sum(x.get(k) or 0 for x in resumenes) for k in (
        "mensajes", "hilos", "chars_original", "chars_para_prompt",
        "tokens_original", "tokens_para_prompt", "excluidos",
        "duplicados_archivo", "insistencias", "reenvios_identicos",
        "hilos_con_insistencia", "solapamientos", "banners_removidos",
        "alertas_limpieza")}
    if tot["chars_original"]:
        tot["ahorro_pct"] = round(
            (tot["chars_original"] - tot["chars_para_prompt"])
            / tot["chars_original"] * 100, 1)
    else:
        tot["ahorro_pct"] = 0.0
    return tot


if __name__ == "__main__":
    sys.exit(main())
