# Motor de exclusiones — OTBB

Capa determinista que decide, mensaje por mensaje, si un correo aporta algo al
análisis o es ruido operativo. **No usa IA generativa** y **nunca borra nada:**
agrega columnas con la decisión, la regla que la tomó y el texto exacto que la
disparó.

El motor no sabe nada del dominio. Todo el criterio vive en los catálogos.

---

## Índice

1. [Qué hace](#qué-hace)
2. [Arquitectura](#arquitectura)
3. [Las cuatro salidas OTBB](#las-cuatro-salidas-otbb)
4. [Servicio HTTP](#servicio-http)
5. [Scripts de terminal](#scripts-de-terminal)
6. [Instalación](#instalación)
7. [Despliegue](#despliegue)
8. [Catálogos](#los-catálogos)
9. [Contrato de salida del motor](#contrato-de-salida-del-motor)
10. [Estado y métricas](#estado-v1)
11. [Limitaciones conocidas](#limitaciones-conocidas)
12. [Mantenimiento del catálogo](#antes-de-tocar-el-catálogo)
13. [Datos sensibles](#lo-que-no-puede-entrar-a-este-repo)

---

## Qué hace

En una base de correos hay mucho texto que no sirve para entender la
conversación:

- respuestas automáticas (fuera de oficina, no-reply)
- banners institucionales repetidos en cada mail
- historial citado que ya apareció antes en el hilo
- filas duplicadas por la exportación
- insistencias (cliente que escribe de nuevo sin respuesta)

El motor limpia, deduplica, clasifica hilos y aplica ~105 reglas de exclusión
con trazabilidad completa. Va **antes** del análisis con LLM para ahorrar tokens
y entregar conversaciones más limpias.

Medido sobre dos bases reales, con el catálogo validado a mano:

| | base A | base B |
|---|---|---|
| correos | 80.340 | 12.078 |
| exclusiones | 631 | 407 |
| banners removidos | 84.534 | 8.149 |
| **ahorro de texto** | **59,3 %** | **49,2 %** |

---

## Arquitectura

Hay **una sola implementación** del procesamiento. Todo converge en
`motor/pipeline.py::procesar()`:

```
Correos
  → 1. agrupar por hilo y ordenar
  → 2. quitar banners institucionales
  → 3. clasificar filas (duplicado vs insistencia)
  → 4. deduplicar historial citado
  → 5. aplicar catálogo de exclusiones
  → salidas enriquecidas
```

Las cuatro tablas OTBB se arman en `motor/salidas.py`, compartido por el
servicio HTTP y los scripts de terminal.

| Módulo | Rol |
|---|---|
| `motor/pipeline.py` | Orquestación de las 5 etapas |
| `motor/exclusion_engine.py` | Motor de reglas, banners, prioridades |
| `motor/deduplicacion.py` | Quita historial citado repetido |
| `motor/hilos.py` | Duplicados de archivo vs insistencias reales |
| `motor/lemas.py` | Lematización opcional con spaCy |
| `motor/salidas.py` | Las cuatro salidas OTBB |
| `motor/columnas.py` | Detección de columnas en bases CSV/XLSX |
| `api.py` | Servicio HTTP FastAPI |

**Principio de diseño:** el motor nunca reemplaza el texto por la etiqueta.
Devuelve texto y marca por separado; cada rama del OTBB toma lo que necesita.

---

## Las cuatro salidas OTBB

El flujo del OTBB se bifurca después del motor:

| Rama | Qué necesita |
|---|---|
| **Maestro** | Exclusiones **fuera** — no pagar tokens por ruido |
| **Conversacional** | Exclusiones **adentro** — un «gracias» al final dice cómo se cerró la conversación |

Por eso son **cuatro archivos** y no uno con banderas:

| Salida | Contenido |
|---|---|
| `_maestro.csv` | Lo que SÍ se analiza: sin exclusiones ni filas repetidas del archivo |
| `_conversacional.csv` | Todas las filas en orden de hilo y turno, con texto limpio y la marca al lado |
| `_excluidos.csv` | Qué se excluyó y por qué: hilo, turno, categoría, regla, evidencia |
| `_exclusiones_data_maestro.csv` | Agregado en el formato exacto de la hoja `Data_maestro` (Tipificación, N, %, ejemplos) |

La etiqueta `[Categoría] texto` se agrega al armar el conversacional, no en el
motor. Si el motor reemplazara el cuerpo, la rama conversacional perdería el
dato.

---

## Servicio HTTP

Forma principal de consumo en producción. Expone el mismo `procesar()` que
usan los scripts, más la generación de las cuatro salidas OTBB.

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

Documentación interactiva: `http://localhost:8000/docs`

### Rutas

| Ruta | Auth | Para qué |
|---|---|---|
| `GET /health` | no | Vivo, catálogo cargado, estado de spaCy. 503 si el catálogo está malo |
| `GET /catalogo` | sí | Resumen de reglas por categoría y alcance |
| `POST /procesar` | sí | Un mensaje, campos del motor |
| `POST /procesar-lote` | sí | Un hilo o base completa, campos del motor |
| `POST /procesar-otbb` | sí | **Las cuatro salidas OTBB** (`?formato=json` o `zip`) |
| `POST /validar-catalogo` | sí | Corre la suite de regresión contra el catálogo en disco |
| `POST /recargar-catalogo` | sí | Recarga en memoria **solo si la suite pasa** |

Autenticación: header `x-api-key`. Si no hay `API_KEY` ni `API_KEY_FILE`
configurados, el servicio queda sin autenticación (solo desarrollo local).

### Entrada de mensajes

`texto` es lo único obligatorio. El resto habilita etapas de mayor retorno:

| Campo | Obligatorio | Para qué |
|---|---|---|
| `texto` | sí | Cuerpo del correo |
| `hilo` | no | Thread Id. Sin esto no hay dedup ni insistencia |
| `fecha` | no | Ordenar dentro del hilo |
| `remitente` | no | Detectar insistencia |
| `msg_id` | no | Distinguir fila duplicada de insistencia real |
| `asunto` | no | Se antepone al texto antes de evaluar reglas |
| `id` | no | Identificador de fila; se devuelve igual |

`contexto` es una lista de tokens de alcance, ej. `["santander", "cl"]`.

### `POST /procesar-otbb` — endpoint principal

Procesa un lote y devuelve las cuatro salidas en una sola llamada.

**Request:**

```json
{
  "mensajes": [
    {
      "id": "1",
      "texto": "Buenos días, necesito ampliar mi línea.",
      "asunto": "Consulta de cupo",
      "hilo": "thread-abc",
      "fecha": "2026-06-15 10:00:00",
      "remitente": "cliente@correo.cl",
      "msg_id": "msg-001"
    }
  ],
  "contexto": ["santander", "cl"],
  "nombre_base": "mi_base"
}
```

**Respuesta JSON** (`?formato=json`, default):

```json
{
  "resumen": {
    "correos_en_base": 100,
    "exclusiones_marcadas": 12,
    "van_al_maestro": 85,
    "ahorro_pct": 52.3,
    "hilos": 40,
    "banners_removidos": 230
  },
  "avisos": [],
  "sin_codigo": [],
  "columnas": { "texto": "texto", "hilo": "hilo" },
  "maestro": [ "...filas..." ],
  "conversacional": [ "...filas..." ],
  "excluidos": [ "...filas..." ],
  "exclusiones_data_maestro": [ "...filas agregadas..." ]
}
```

**Respuesta ZIP** (`?formato=zip`): descarga un archivo con los cuatro CSV más
un `{nombre_base}_resumen.json`. El campo `nombre_base` del body define el
prefijo de los archivos (default: `salida`).

**Ejemplos con curl:**

```bash
# JSON — consumo programático
curl -X POST "http://localhost:8000/procesar-otbb" \
  -H "Content-Type: application/json" \
  -H "x-api-key: tu-clave" \
  -d '{"mensajes":[{"texto":"Estoy fuera de oficina.","hilo":"H1","fecha":"2026-01-01","remitente":"a@b.cl","msg_id":"m1"}],"contexto":["cl"]}'

# ZIP — equivalente a salidas_otbb.py
curl -X POST "http://localhost:8000/procesar-otbb?formato=zip" \
  -H "Content-Type: application/json" \
  -H "x-api-key: tu-clave" \
  -d '{"mensajes":[...],"contexto":["santander","cl"],"nombre_base":"base_junio"}' \
  -o base_junio_otbb.zip
```

**Errores:**

| Código | Cuándo |
|---|---|
| 401 | API key inválida o ausente |
| 413 | Más mensajes que `MAX_MENSAJES_POR_LOTE` (default 5000) |
| 422 | Limpieza de banner dejó mensajes casi vacíos (banner mal clasificado) |
| 503 | Catálogo no cargado o inválido |

### Restricción de lotes

**Un hilo no puede partirse entre dos llamadas.** La deduplicación del
historial y la insistencia comparan mensajes del mismo hilo: si la mitad quedó
en otro lote, no hay contra qué comparar.

Al armar lotes se corta **entre hilos**, nunca dentro de uno. Para bases
grandes, `scripts/cliente_api.py` parte automáticamente y reensambla las
salidas.

### Variables de entorno

| Variable | Para qué | Si falta |
|---|---|---|
| `CATALOGO_EXCLUSIONES` | Ruta del CSV de reglas | Usa el del repo |
| `BANNERS_EXCLUSIONES` | Ruta del YAML de bloques | Usa el del repo |
| `PRIORIDADES_EXCLUSIONES` | Ruta del CSV de prioridades | Todas las categorías empatan; desempata el ID |
| `TIPIFICACIONES_EXCLUSIONES` | Ruta del CSV de tipificaciones WM | Columna Tipificación queda vacía |
| `MODELO_SPACY` | Qué modelo cargar | Prueba `lg` y cae a `sm` |
| `SIN_LEMAS` | Apaga lematización (A/B) | Queda encendida |
| `API_KEY_FILE` | Archivo con la clave | Se lee `API_KEY` |
| `API_KEY` | La clave | Servicio **sin autenticación** |
| `MAX_MENSAJES_POR_LOTE` | Tope por llamada | 5000 |
| `PORT` | Puerto de escucha | 8000 |

Dos decisiones de despliegue no negociables:

1. **El catálogo vive fuera de la imagen**, montado en `/catalogos`. Cambiar una
   regla no requiere redeploy.
2. **La recarga corre la suite antes de aceptar.** Si el catálogo nuevo no
   pasa, devuelve 400 y sigue activo el anterior.

---

## Scripts de terminal

Alternativa local o para bases en disco. Ninguna ruta de cliente vive dentro de
los scripts: la base se pasa por argumento.

```bash
# Cuatro salidas OTBB, procesamiento local
python scripts/salidas_otbb.py ruta/a/base.csv --contexto santander,cl

# Cuatro salidas OTBB vía servicio desplegado
python scripts/cliente_api.py ruta/a/base.csv --url http://localhost:8095 --key-file clave.txt

# Probar textos a mano contra el catálogo
python scripts/probar_exclusiones.py --contexto cliente,cl

# Buscar huecos del catálogo con evidencia
python scripts/huecos_catalogo.py salida_conversacional.csv

# Medir un catálogo candidato contra un set validado a mano
python scripts/medir_catalogo.py base.xlsx --validado validado.xlsx \
    --catalogo catalogos/exclusiones.csv --contexto cliente,cl

# Auditoría y tests (obligatorio antes de tocar el catálogo)
python scripts/auditar_catalogo.py
python tests/test_regresion.py
python tests/test_pipeline.py
python tests/test_salidas.py
```

**La clave de la API nunca va como argumento** — queda en el historial del shell.
Usar `--key-file` o la variable `API_KEY`.

El catálogo se resuelve por `--catalogo`, por variable de entorno
(`CATALOGO_EXCLUSIONES`), o por el default del repo.

| Script | Qué hace |
|---|---|
| `salidas_otbb.py` | Cuatro salidas OTBB, local |
| `cliente_api.py` | Cuatro salidas OTBB vía HTTP |
| `procesar_exclusiones.py` | Procesamiento parcial legacy (sin salidas OTBB) |
| `auditar_catalogo.py` | Valida catálogo: campos declarados vs implementados |
| `medir_catalogo.py` | Precisión contra set validado a mano |
| `medir.py` | Volumetría de ahorro sobre una base |
| `probar_exclusiones.py` | REPL interactivo de reglas |
| `huecos_catalogo.py` | Candidatos de reglas nuevas desde conversacional |
| `revisar.py` | Comparación contra etiquetado de referencia |

---

## Instalación

```bash
pip install -r requirements.txt
python -m spacy download es_core_news_lg
```

El modelo **no** se instala con `requirements.txt`: pesa ~600 MB. Tiene que ser
el grande (`es_core_news_lg`). Medido con la suite de regresión, `es_core_news_sm`
falla dos casos de fuera de oficina — el modelo chico no reconoce «Estaré» ni
«Estaremos» como formas de «estar». Se elige con `MODELO_SPACY`.

Sin el modelo el motor arranca igual y marca menos. `GET /health` reporta cuál
está cargado.

---

## Despliegue

### Docker

```bash
docker build -t motor-exclusiones .
docker run -p 8000:8000 \
  -e API_KEY=tu-clave \
  -e CATALOGO_EXCLUSIONES=/catalogos/exclusiones.csv \
  -e BANNERS_EXCLUSIONES=/catalogos/banners.yaml \
  -e PRIORIDADES_EXCLUSIONES=/catalogos/prioridades.csv \
  -e TIPIFICACIONES_EXCLUSIONES=/catalogos/tipificaciones.csv \
  -v /ruta/a/catalogos:/catalogos:ro \
  motor-exclusiones
```

### Docker Compose

```bash
# Configurar rutas locales (opcional)
export CATALOGOS_HOST_PATH=./catalogos
export API_KEY_SECRET_PATH=./secrets/api_key.txt

docker compose up -d --build
```

Por defecto el servicio escucha en **puerto 8095** del host (8000 dentro del
contenedor), para no chocar con otros servicios locales.

### Requisitos de infraestructura

- **Imagen ~1,7 GB**, proceso ~900 MB RAM (casi todo spaCy). Un plan de 512 MB
  no lo levanta.
- **Un worker** (`uvicorn --workers 1`): el modelo se carga una vez en memoria.
- **Catálogo montado**, no dentro de la imagen. Si queda adentro, cambiar una
  regla pasa a ser un despliegue.

---

## Los catálogos

Todo el criterio de negocio vive acá. Lo edita la lingüista, no el código.

| Archivo | Rol |
|---|---|
| `catalogos/exclusiones.csv` | ~105 reglas, 17 categorías |
| `catalogos/prioridades.csv` | Desempate entre categorías |
| `catalogos/banners.yaml` | 7 bloques institucionales a quitar |
| `catalogos/tipificaciones.csv` | Mapeo (Categoría, Origen) → código WM |
| `catalogos/valores_validos.csv` | Valores permitidos al editar columnas |

En producción se montan juntos en `/catalogos` y se apuntan con las variables de
entorno correspondientes.

### `catalogos/exclusiones.csv`

Se lee **por nombre de columna**, nunca por posición. Está en CSV a propósito:
un `.xlsx` en git es un binario sin diff legible. El motor también acepta
`.xlsx`, pero la fuente de verdad del repo es el CSV.

**Cómo editarlo sin Office:** Google Sheets (Importar → editar → Descargar CSV)
o LibreOffice Calc. Correr auditoría y suite antes de commitear.

| Columna | Para qué |
|---|---|
| `ID` | Estable. No se genera por posición |
| `Categoría` | Categoría de exclusión |
| `Texto Contenido` | Términos disparadores (sintaxis abajo) |
| `Condición de match` | Frase contenida · Solo aislado · Remitente / dominio |
| `Tratamiento` | Qué hacer con el mensaje |
| `Alcance` | `todos`, o el país / cliente donde aplica |

**Sintaxis de `Texto Contenido`:**

| Forma | Significa | Ejemplo |
|---|---|---|
| `a \| b` | Co-ocurrencia (AND) | `fuera de oficina \| respuesta automática` |
| `a / b` (con espacios) | Frases alternativas (OR) | `no se pudo entregar / no se puede entregar` |
| `a/b/c` (sin espacios) | Alternancia de palabra | `mi/el/su correo ahora/nuevo es` |
| `fecha` | Meta-token de fechas | `estaré ausente \| desde/hasta fecha` |

**Condiciones implementadas:** Frase contenida (92), Solo aislado (11),
Remitente / dominio (2). `Sin petición` está declarada pero **no implementada**
— ver `PENDIENTES.md`.

### `catalogos/banners.yaml`

Bloques institucionales removidos antes de evaluar reglas.

- `tipo: cabecera` — se borra donde esté; el resto se conserva
- `tipo: pie` — corta desde el match hasta el final

Tratar una cabecera como pie borra el cuerpo del mensaje. Por eso hay red de
seguridad: si un mensaje tenía >90 caracteres y tras limpiar queda con <30, se
descarta la limpieza y el proceso termina con error.

### `catalogos/prioridades.csv`

Cuando dos reglas de categorías distintas matchean, gana la de **número más
bajo**:

```
10  Rebote / fallo de entrega
20  Fuera de oficina / OOO
30  Confirmación / OKG
40  Evento de calendario
50  Marketing / promo
90  Notificación
99  No-reply                          último recurso
```

Una categoría no listada queda en 60. **Si el archivo no está, todas empatan**
y el desempate vuelve a ser el ID.

### Alcance por cliente

Un cliente nuevo es **filas nuevas**, no un `if` en el motor:

- `todos` — aplica siempre
- `cl`, `mx`, … — solo si ese token viene en el contexto
- `santander`, `bch`, … — solo para ese cliente
- `cl,mx` — varios, separados por coma

Hoy todas las reglas están en `todos`. Marcar las locales es trabajo de
catálogo; la auditoría lista candidatas.

---

## Contrato de salida del motor

Por cada mensaje, `procesar()` agrega campos sin borrar los de entrada.

**Exclusión:**

| Campo | Descripción |
|---|---|
| `excluido` | `true` / `false` |
| `categoria` | Categoría de exclusión |
| `regla_id` | EXC-nnnn |
| `texto_regla` | Texto de la regla tal como está en el catálogo |
| `condicion` | Condición que se aplicó |
| `evidencia` | Texto exacto que disparó |
| `accionable` | Si trae señal accionable |
| `tratamiento` | Qué hacer con el mensaje |
| `definicion` | Definición de la regla |
| `origen` | Sistema, proveedor o persona |
| `via` | `superficie` o `lema` |
| `continua` | Si el mensaje marcado espera algo más |
| `motivo_continuidad` | Por qué |
| `perdedores` | Otras reglas que también matchearon |

**Limpieza y prompt:**

| Campo | Descripción |
|---|---|
| `texto_para_prompt` | Texto limpio para mandar al LLM |
| `chars_original` / `chars_para_prompt` | Medición de ahorro |
| `bloques_removidos` | Banners que se quitaron |
| `alerta_limpieza` | Limpieza dejó el mensaje casi vacío |

**Hilo** (requiere `hilo`, `fecha`, `msg_id`):

| Campo | Descripción |
|---|---|
| `fila_estado` | `unico` · `duplicado_archivo` · `duplicado_sospechoso` |
| `repite_a` | A qué mensaje del hilo repite |
| `insistencia_n` | N-ésimo intento sin respuesta |
| `horas_sin_respuesta` | Tiempo de espera |
| `reenvio_identico` | No aportó texto nuevo |
| `motivo_fila` | Por qué se clasificó así |
| `posicion_en_hilo` / `mensajes_en_hilo` | Turno dentro de la conversación |

**Filas repetidas e insistencia no son lo mismo.** Una fila repetida es defecto
de ingesta; una insistencia sí pasó en la realidad y es fricción operativa. En
ambos casos el texto deja de mandarse al maestro; la insistencia deja rastro.

`Via` permite medir lematización sin adivinar. Con `SIN_LEMAS=1` se obtiene el
A/B contra la misma base.

---

## Estado: v1

Validado contra dos bases reales de producción y corregido con validación humana
regla por regla. **El criterio del catálogo lo fija la lingüista, no el código.**

| Capacidad | Estado |
|---|---|
| Reglas del catálogo | 105 reglas, 17 categorías |
| Bloques institucionales | 7 en `banners.yaml` |
| Prioridad entre categorías | Sí, `prioridades.csv` |
| Deduplicación del historial | Sí |
| Lematización spaCy | Opcional, `es_core_news_lg` |
| Límites de palabra | Sí, `\b` en todos los términos |
| Alcance por regla | Sí, columna `Alcance` |
| Cuatro salidas OTBB | Sí, local y vía `POST /procesar-otbb` |
| Suite de regresión | 23 casos catálogo + 12 pipeline + 8 salidas |
| Auditoría del catálogo | `scripts/auditar_catalogo.py` |

Versión del servicio HTTP: **0.2.0** (expone salidas OTBB completas).

---

## Limitaciones conocidas

Detalle y decisiones pendientes en `PENDIENTES.md`.

- **No decide por especificidad.** Desempata por condición → prioridad de
  categoría → ID.
- **No implementa `Sin petición`.**
- **`No-reply` mezcla mecanismo y contenido** — la prioridad 99 lo mitiga.
- **Sin spaCy** el motor funciona igual comparando superficie; las reglas en
  infinitivo no disparan sobre conjugaciones.
- **Capa de insistencias** medida pero no validada a mano fila por fila.
- **9 tipificaciones WM** pendientes de definir en `tipificaciones.csv`.

---

## Antes de tocar el catálogo

```bash
python scripts/auditar_catalogo.py && python tests/test_regresion.py
```

La auditoría falla cuando algo está **declarado y no consumido**.

Si la regla es de criterio y no de sintaxis:

```bash
python scripts/medir_catalogo.py base.xlsx --validado validado.xlsx
```

Reporta cuántos errores arregla, cuántos aciertos pierde y qué marcas nuevas
aparecen — las tres cifras juntas.

El archivo de validación **no está en el repo** (trae texto real de correos).
Vive fuera y se pasa por `--validado`.

---

## Lo que no puede entrar a este repo

Ninguna base de correos. Ni recortada, ni anonimizada. Traen RUT, correos y
nombres completos de clientes y ejecutivos.

Las bases van en `datos/`, que está en el `.gitignore`.

Los casos de `tests/casos_regresion.yaml` son texto **inventado a mano**. Por
eso la suite corre en cualquier máquina.

---

## Referencias

| Recurso | Ubicación |
|---|---|
| Pendientes y decisiones abiertas | `PENDIENTES.md` |
| Explicación del servicio (resumen) | `docs/explicacion-servicio.md` |
| Orquestación del pipeline | `motor/pipeline.py` |
| Cuatro salidas OTBB | `motor/salidas.py` |
| Motor de reglas | `motor/exclusion_engine.py` |
| API HTTP | `api.py` |
| Suite de regresión | `tests/casos_regresion.yaml` |
