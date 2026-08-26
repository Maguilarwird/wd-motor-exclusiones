"""
Vista de revisión humana: el hilo completo en orden, y por cada mensaje, lado a
lado, cómo llegó y qué es lo que efectivamente se manda.

    python scripts/revisar.py base.csv --contexto santander,cl

    # sin datos personales, para mostrar a terceros
    python scripts/revisar.py base.csv --contexto santander,cl --enmascarar

Escribe salidas/revision_<base>.html y salidas/revision_<base>.csv.
salidas/ está en el .gitignore: estos archivos traen texto real de correos.
"""

import argparse
import csv
import html
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motor.exclusion_engine import ExclusionEngine, load_catalog  # noqa: E402
from motor.deduplicacion import tramos_repetidos_en_hilo  # noqa: E402

RX_RUT = re.compile(r"\b\d{1,2}\.?\d{3}\.?\d{3}-[\dkK]\b")
RX_MAIL = re.compile(r"[\w.\-]+@[\w.\-]+\.\w+")


def enmascarar(texto):
    return RX_MAIL.sub("[MAIL]", RX_RUT.sub("[RUT]", texto))


def con_marcas(texto, tramos):
    """Texto escapado con los tramos repetidos tachados y etiquetados."""
    if not tramos:
        return html.escape(texto)
    partes, cursor = [], 0
    for a, b, origen in tramos:
        partes.append(html.escape(texto[cursor:a]))
        partes.append(
            f'<span class="cita"><span class="cita-tag">ya dicho en el mensaje {origen}</span>'
            f'{html.escape(texto[a:b])}</span>'
        )
        cursor = b
    partes.append(html.escape(texto[cursor:]))
    return "".join(partes)


def lo_que_queda(texto, tramos):
    if not tramos:
        return texto.strip()
    partes, cursor = [], 0
    for a, b, _ in tramos:
        partes.append(texto[cursor:a])
        cursor = b
    partes.append(texto[cursor:])
    return re.sub(r"\s+", " ", " ".join(partes)).strip()


CSS = """
:root{--bg:#FAFAFA;--card:#FFF;--ink:#06122A;--grey:#5D5D66;--line:#DBDBE0;
--blue:#111184;--green:#22EDA3;--pumpkin:#FE881E;
--cita-bg:#FFF1DF;--cita-ink:#8A5A12;--cita-line:#E9B978;--keep-bg:#EAFBF3}
@media(prefers-color-scheme:dark){:root{--bg:#06122A;--card:#0C1B36;--ink:#FAFAFA;
--grey:#9C9BAB;--line:#23304C;--blue:#6F70E0;
--cita-bg:#33240F;--cita-ink:#EBB877;--cita-line:#6B4C1C;--keep-bg:#0E2A22}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:'Mona Sans',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
font-size:15px;line-height:1.55}
.wrap{max-width:1180px;margin:0 auto;padding:26px 20px 90px}
h1{font-size:26px;font-weight:800;letter-spacing:-.02em;margin:0 0 6px}
.sub{color:var(--grey);margin:0 0 20px;font-size:14px}
.aviso{background:var(--pumpkin);color:#06122A;padding:10px 14px;border-radius:4px;
font-weight:600;font-size:14px;margin-bottom:20px}
.guia{background:var(--card);border:1px solid var(--line);border-radius:5px;
padding:18px 20px;margin-bottom:20px}
.guia h2{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--grey);
margin:0 0 14px;font-weight:700}
.guia dl{margin:0;display:grid;grid-template-columns:auto 1fr;gap:10px 18px;align-items:baseline}
.guia dt{font-weight:700;font-size:13px;white-space:nowrap}
.guia dd{margin:0;font-size:14px;color:var(--grey)}
.barra{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);
padding:12px 0;margin-bottom:20px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;z-index:9}
button{font:inherit;font-size:13px;font-weight:600;padding:6px 12px;border-radius:3px;
border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button.on{background:var(--blue);color:#FAFAFA;border-color:var(--blue)}
button:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.hilo{margin-bottom:32px;border:1px solid var(--line);border-radius:5px;overflow:hidden}
.hilo-cab{background:var(--card);padding:13px 16px;border-bottom:1px solid var(--line);
display:flex;gap:16px;align-items:baseline;flex-wrap:wrap;position:sticky;top:57px;z-index:4}
.hilo-cab b{font-size:15px}
.hilo-cab span{color:var(--grey);font-size:13px;font-variant-numeric:tabular-nums}
.msg{padding:16px;border-bottom:1px solid var(--line);background:var(--card);transition:opacity .15s}
.msg:last-child{border-bottom:none}
.msg.apagado{opacity:.28}
.msg.marcado{box-shadow:inset 4px 0 0 var(--pumpkin)}
.meta{display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:12px;
color:var(--grey);margin-bottom:6px}
.n{background:var(--blue);color:#FAFAFA;border-radius:3px;padding:1px 8px;font-weight:700;
font-variant-numeric:tabular-nums}
.tag{border:1px solid var(--line);border-radius:3px;padding:1px 7px;font-weight:600}
.tag.sent{border-color:var(--blue);color:var(--blue)}
.asunto{font-weight:600;font-size:14px;margin-bottom:10px}
.panels{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:860px){.panels{grid-template-columns:1fr}}
.panel h4{margin:0 0 6px;font-size:11px;letter-spacing:.1em;text-transform:uppercase;
color:var(--grey);font-weight:700;display:flex;justify-content:space-between;gap:8px}
.panel h4 b{font-weight:700;color:var(--ink);font-variant-numeric:tabular-nums}
.cuerpo{white-space:pre-wrap;font-size:13.5px;line-height:1.6;padding:11px 12px;
border:1px solid var(--line);border-radius:4px;max-height:300px;overflow:auto}
.cuerpo.queda{background:var(--keep-bg);border-color:var(--green)}
.cuerpo.vacio{color:var(--grey);font-style:italic}
.nota-banner{margin:7px 0 0;font-size:12px;color:var(--grey);line-height:1.45}
.cita{background:var(--cita-bg);color:var(--cita-ink);border-radius:2px;
text-decoration:line-through;text-decoration-thickness:1px;position:relative;
padding:0 2px;box-shadow:inset 0 0 0 1px var(--cita-line)}
.cita-tag{display:block;text-decoration:none;font-size:10px;font-weight:700;
letter-spacing:.06em;text-transform:uppercase;opacity:.85;margin-bottom:1px}
.decision{margin-top:12px;display:flex;gap:8px;align-items:flex-start;flex-wrap:wrap;font-size:13px}
.badge{border-radius:3px;padding:2px 9px;font-weight:700;font-size:12px;white-space:nowrap}
.badge.ex{background:var(--pumpkin);color:#06122A}
.badge.ok{background:var(--green);color:#06122A}
.detalle{color:var(--grey)}
.detalle code{background:var(--bg);padding:1px 5px;border-radius:2px;font-size:12px}
.flags{margin-top:12px;display:flex;gap:16px;flex-wrap:wrap;padding-top:10px;
border-top:1px dashed var(--line)}
label.flag{display:inline-flex;gap:7px;align-items:center;font-size:13px;
color:var(--grey);cursor:pointer;user-select:none}
label.flag:hover{color:var(--ink)}
.msg.marcado label.flag{color:var(--cita-ink);font-weight:600}
#salida{width:100%;min-height:110px;margin-top:10px;font-family:ui-monospace,Menlo,monospace;
font-size:12px;padding:10px;border:1px solid var(--line);border-radius:4px;
background:var(--card);color:var(--ink);display:none}
"""

JS = r"""
const marcados=new Map();
function filtrar(modo,btn){
  document.querySelectorAll('.barra button[data-f]').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
  document.querySelectorAll('.msg').forEach(m=>{
    const hit = modo==='todos'
      || (modo==='excl' && m.dataset.excl==='1')
      || (modo==='solape' && m.dataset.solape==='1')
      || (modo==='recorte' && Number(m.dataset.recorte)>=40)
      || (modo==='marcado' && m.classList.contains('marcado'));
    m.classList.toggle('apagado', !hit && modo!=='todos');
  });
  document.querySelectorAll('.hilo').forEach(h=>{
    const alguno=[...h.querySelectorAll('.msg')].some(m=>!m.classList.contains('apagado'));
    h.style.display = (modo==='todos'||alguno) ? '' : 'none';
  });
}
function flag(cb,id,tipo){
  const card=cb.closest('.msg');
  const previo=marcados.get(id)||new Set();
  cb.checked ? previo.add(tipo) : previo.delete(tipo);
  previo.size ? marcados.set(id,previo) : marcados.delete(id);
  card.classList.toggle('marcado', previo.size>0);
  document.getElementById('n').textContent = marcados.size;
}
function exportar(){
  const t=document.getElementById('salida');
  t.style.display='block';
  t.value = marcados.size
    ? 'ID,que_esta_mal\n' + [...marcados].map(([id,s])=>id+','+[...s].join(' + ')).join('\n')
    : '(no marcaste ninguno)';
  t.select();
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--texto", default="combined_msg")
    ap.add_argument("--hilo", default="Thread Id")
    ap.add_argument("--orden", default="Date")
    ap.add_argument("--remitente", default="From")
    ap.add_argument("--asunto", default="Subject")
    ap.add_argument("--carpeta", default="Folder")
    ap.add_argument("--contexto", default="")
    ap.add_argument("--catalogo", default=None)
    ap.add_argument("--enmascarar", action="store_true")
    ap.add_argument("--max-hilos", type=int, default=0)
    args = ap.parse_args()

    import pandas as pd
    ruta = Path(args.base)
    if ruta.suffix.lower() in (".csv", ".tsv"):
        df = pd.read_csv(ruta, engine="python", dtype=str, keep_default_na=False)
    else:
        df = pd.read_excel(ruta, dtype=str).fillna("")

    for col in (args.texto, args.hilo):
        if col not in df.columns:
            sys.exit(f"Falta la columna {col!r}. Hay: {list(df.columns)}")

    contexto = [t for t in args.contexto.split(",") if t.strip()]
    engine = ExclusionEngine(load_catalog(args.catalogo))

    def campo(i, col):
        return str(df[col].iloc[i]) if col in df.columns else ""

    idx = sorted(range(len(df)), key=lambda i: (campo(i, args.hilo), campo(i, args.orden)))
    hilos = defaultdict(list)
    for i in idx:
        hilos[campo(i, args.hilo)].append(i)

    claves = list(hilos)
    if args.max_hilos:
        claves = claves[:args.max_hilos]

    partes, filas_csv = [], []
    n_msg = n_excl = n_solape = 0

    for h_n, clave in enumerate(claves, 1):
        ids = hilos[clave]
        textos = [campo(i, args.texto) for i in ids]
        if args.enmascarar:
            textos = [enmascarar(t) for t in textos]
        tramos_hilo = tramos_repetidos_en_hilo(textos)
        total = len(ids)

        msgs, quitado_hilo, total_hilo = [], 0, 0
        for pos, (i, texto, tramos) in enumerate(zip(ids, textos, tramos_hilo), 1):
            r = engine.analizar(texto, campo(i, args.remitente), contexto)
            queda = lo_que_queda(texto, tramos)
            quitado = len(texto) - len(queda)
            pct = quitado / len(texto) * 100 if texto else 0
            total_hilo += len(texto)
            quitado_hilo += quitado
            n_msg += 1
            n_excl += bool(r["excluido"])
            n_solape += bool(r["perdedores"])

            mid = f"H{h_n:02d}-M{pos}"
            remit = campo(i, args.remitente)
            carpeta = campo(i, args.carpeta)
            asunto = campo(i, args.asunto)
            if args.enmascarar:
                remit, asunto = enmascarar(remit), enmascarar(asunto)

            if r["excluido"]:
                dec = (f'<span class="badge ex">EXCLUIDO · {html.escape(r["categoria"])}</span>'
                       f'<span class="detalle">regla <code>{html.escape(r["regla_id"])}</code> '
                       f'«{html.escape(r["texto_regla"])}» ({html.escape(r["condicion"])}) · '
                       f'evidencia: <code>{html.escape(r["evidencia"])}</code>'
                       + (f' · también matchearon: {html.escape(", ".join(r["perdedores"]))}'
                          if r["perdedores"] else "") + "</span>")
            else:
                dec = '<span class="badge ok">GESTIONABLE</span>'
                if r["perdedores"]:
                    dec += (f'<span class="detalle">matchearon sin ganar: '
                            f'{html.escape(", ".join(r["perdedores"]))}</span>')
            if r["bloques_removidos"]:
                dec += (f'<span class="detalle">banners removidos: '
                        f'{html.escape(", ".join(r["bloques_removidos"]))}</span>')

            nota_banner = ""
            if r["bloques_removidos"]:
                nota_banner = ('<p class="nota-banner">Además, antes de mandarlo se le quita '
                               'el bloque institucional ' + html.escape(", ".join(r["bloques_removidos"]))
                               + ' (el banner de seguridad). Eso pasa en otro paso y por eso '
                                 'todavía se ve arriba.</p>')
            cuerpo_queda = (html.escape(queda) if queda.strip()
                            else "El mensaje entero ya se había dicho antes en el hilo. "
                                 "No queda aporte nuevo.")
            clase_queda = "cuerpo queda" + ("" if queda.strip() else " vacio")

            msgs.append(f'''
    <div class="msg" data-excl="{int(bool(r["excluido"]))}" data-solape="{int(bool(r["perdedores"]))}" data-recorte="{pct:.0f}">
      <div class="meta">
        <span class="n">Mensaje {pos} de {total}</span>
        <span class="tag {'sent' if carpeta.upper()=='SENT' else ''}">{html.escape(carpeta or '—')}</span>
        <span>{html.escape(remit)}</span>
        <span>{html.escape(campo(i, args.orden))}</span>
        <span>{html.escape(mid)}</span>
      </div>
      <div class="asunto">{html.escape(asunto)}</div>
      <div class="panels">
        <div class="panel">
          <h4><span>Como llegó</span><b>{len(texto):,} car.</b></h4>
          <div class="cuerpo">{con_marcas(texto, tramos)}</div>
        </div>
        <div class="panel">
          <h4><span>Lo que sí se manda</span><b>{len(queda):,} car. · −{pct:.0f}%</b></h4>
          <div class="{clase_queda}">{cuerpo_queda}</div>{nota_banner}
        </div>
      </div>
      <div class="decision">{dec}</div>
      <div class="flags">
        <label class="flag"><input type="checkbox" onchange="flag(this,'{mid}','decision')"> la decisión está mal</label>
        <label class="flag"><input type="checkbox" onchange="flag(this,'{mid}','recorte')"> el recorte está mal</label>
      </div>
    </div>''')

            filas_csv.append([mid, clave, pos, total, carpeta, remit, asunto,
                              "Exclusión" if r["excluido"] else "Gestionable",
                              r["categoria"], r["regla_id"], r["texto_regla"],
                              r["condicion"], r["evidencia"], ", ".join(r["perdedores"]),
                              len(texto), len(queda), f"{pct:.0f}%", queda, "", ""])

        pct_hilo = quitado_hilo / total_hilo * 100 if total_hilo else 0
        partes.append(f'''
  <div class="hilo">
    <div class="hilo-cab">
      <b>Hilo {h_n:02d}</b>
      <span>{len(ids)} mensajes</span>
      <span>{total_hilo:,} caracteres</span>
      <span>{pct_hilo:.0f}% es repetición de lo ya dicho</span>
    </div>{"".join(msgs)}
  </div>''')

    aviso = ("" if args.enmascarar else
             '<div class="aviso">Contiene texto real de correos, con RUT, nombres y '
             'direcciones. No compartir este archivo. Para mostrarlo a terceros, '
             'regenerar con --enmascarar.</div>')

    doc = f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Revisión — {html.escape(ruta.name)}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Mona+Sans:wght@200..900&display=swap">
<style>{CSS}</style></head><body><div class="wrap">
<h1>Revisión humana</h1>
<p class="sub">{html.escape(ruta.name)} · {len(claves)} hilos · {n_msg:,} mensajes ·
{n_excl} excluidos · {n_solape} con más de una regla matcheando.</p>
{aviso}
<div class="guia">
  <h2>Cómo leer esto</h2>
  <dl>
    <dt>Los hilos van completos</dt>
    <dd>En orden cronológico, «Mensaje 3 de 8» es el tercero de ese hilo. Los filtros
        no esconden mensajes: apagan los que no aplican para que el hilo se siga
        leyendo entero.</dd>
    <dt>Columna izquierda</dt>
    <dd>El correo como llegó. Lo <span class="cita">tachado</span> ya se había dicho
        antes, y cada tramo dice en qué mensaje del hilo apareció por primera vez.</dd>
    <dt>Columna derecha</dt>
    <dd>Lo que efectivamente se le manda a la generativa: el aporte nuevo de ese correo.
        Esto es lo que hay que validar — si ahí falta algo que el cliente escribió por
        primera vez, el recorte está mal.</dd>
    <dt>Y aparte</dt>
    <dd><b>GESTIONABLE</b> o <b>EXCLUIDO</b> es una decisión distinta, sobre el mensaje
        entero. Un mensaje puede ser gestionable y estar 60% tachado: se conserva, pero
        no se repite lo ya dicho.</dd>
  </dl>
</div>
<div class="barra">
  <button data-f class="on" onclick="filtrar('todos',this)">Todos</button>
  <button data-f onclick="filtrar('excl',this)">Los que el motor excluyó</button>
  <button data-f onclick="filtrar('recorte',this)">Los que perdieron 40% o más</button>
  <button data-f onclick="filtrar('solape',this)">Con dos reglas compitiendo</button>
  <button data-f onclick="filtrar('marcado',this)">Los que marqué</button>
  <button onclick="exportar()">Copiar lo marcado (<span id="n">0</span>)</button>
</div>
<textarea id="salida" readonly aria-label="IDs marcados"></textarea>
{"".join(partes)}
</div><script>{JS}</script></body></html>"""

    salidas = Path(__file__).resolve().parents[1] / "salidas"
    salidas.mkdir(exist_ok=True)
    suf = "_enmascarado" if args.enmascarar else ""
    fh = salidas / f"revision_{ruta.stem}{suf}.html"
    fh.write_text(doc, encoding="utf-8")

    fc = salidas / f"revision_{ruta.stem}{suf}.csv"
    with open(fc, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["ID", "Thread Id", "Posición", "Mensajes del hilo", "Carpeta",
                    "Remitente", "Asunto", "Tipo", "Categoría", "Regla_ID", "Regla",
                    "Condición", "Evidencia", "Perdedores", "Chars originales",
                    "Chars que se mandan", "% recortado", "LO QUE SE MANDA",
                    "VEREDICTO HUMANO", "COMENTARIO"])
        w.writerows(filas_csv)

    print(f"{len(claves)} hilos · {n_msg:,} mensajes · {n_excl} excluidos · {n_solape} con solapamiento")
    print(f"HTML: {fh}")
    print(f"CSV:  {fc}")


if __name__ == "__main__":
    main()
