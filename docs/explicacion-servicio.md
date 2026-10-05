# Motor de exclusiones — qué es y cómo funciona

Es una **capa de preprocesamiento determinista** para el flujo OTBB (Onboarding / análisis de correos). Recibe correos y responde, mensaje por mensaje: **¿esto aporta al análisis o es ruido operativo?** (fuera de oficina, no-reply, avisos de phishing, acuses de recibo, etc.).

**No usa IA generativa.** Todo el criterio vive en catálogos editables; el código solo aplica reglas, limpia texto y enriquece metadatos.

---

## Problema que resuelve

En una base de correos hay mucho texto que no sirve para entender la conversación:

- respuestas automáticas ("fuera de oficina")
- banners del banco repetidos en cada mail
- historial citado que ya apareció antes en el hilo
- filas duplicadas por la exportación
- insistencias (cliente que escribe de nuevo sin respuesta)

El motor **no borra nada de la base original**: agrega columnas con la decisión, la regla que la tomó y la evidencia. Cada consumidor downstream decide qué hacer con eso.

---

## Arquitectura en una frase

```
Correos → pipeline (limpieza + dedup + clasificación) → motor de reglas (catálogo) → salidas enriquecidas
```

Hay **una sola implementación** (`procesar()` en `motor/pipeline.py`). La usan igual el script de terminal, Docker y la API HTTP.

---

## Pipeline: las 5 etapas (en orden)

1. **Agrupar por hilo y ordenar por fecha.**  
   Sin orden no hay "mensaje anterior", y sin eso no hay deduplicación.

2. **Quitar los bloques institucionales** del texto que se va a mandar.  
   El banner de phishing del banco no aporta nada al análisis, se repite en cada correo y se paga en tokens igual que el resto. Se quita acá, antes de deduplicar, para que tampoco ensucie la comparación entre mensajes.

3. **Clasificar las filas del hilo.**  
   Distingue la fila que el archivo trae dos veces (defecto de datos) de la insistencia real (alguien volvió a escribir porque no le contestaron).

4. **Deduplicar el historial citado.**  
   Quita de cada mensaje los tramos que ya aparecieron antes en el hilo.

5. **Aplicar el catálogo sobre el TEXTO ORIGINAL**, no sobre el deduplicado ni sobre el limpio.  
   Una regla tiene que poder ver el mensaje como lo escribió la persona.

| Etapa | Qué hace |
|---|---|
| **1. Hilos** | Agrupa por `hilo`, ordena por `fecha` |
| **2. Banners** | Quita bloques institucionales (avisos CMF, phishing, etc.) del texto que viaja al prompt |
| **3. Clasificación** | Detecta filas duplicadas vs. insistencias reales (por turnos, no por similitud de texto) |
| **4. Dedup** | Elimina del cuerpo lo que ya se citó en mensajes anteriores del hilo |
| **5. Exclusiones** | Evalúa el catálogo contra el **texto original** (con asunto), no contra el texto ya recortado |

Detalle importante: las reglas se evalúan sobre el correo tal como lo escribió la persona, pero el texto que va al LLM (`texto_para_prompt`) sí va limpio y deduplicado — ahí está el ahorro de tokens (~50–60% medido en bases reales).

---

## Cómo decide si excluir

El criterio está en **`catalogos/exclusiones.csv`** (~105 reglas, 17 categorías). El motor no "entiende" el dominio; compara patrones.

### Condiciones implementadas

- **Frase contenida** — el patrón aparece en el texto
- **Solo aislado** — el mensaje es esencialmente esa frase (≤3 tokens extra)
- **Remitente / dominio** — match contra el remitente

### Sintaxis de patrones (campo `Texto Contenido`)

- `|` → co-ocurrencia (AND): `fuera de oficina | respuesta automática`
- ` / ` → frases alternativas (OR)
- `a/b/c` → alternancia de palabra: `mi/el/su correo ahora/nuevo es`

### Desempate

Cuando varias reglas matchean:

1. Condición (prioridad entre tipos de match)
2. Prioridad de categoría (`catalogos/prioridades.csv`)
3. ID de regla

### Lematización opcional (spaCy)

Para que reglas en infinitivo ("encontrar fuera de oficina") matcheen conjugaciones ("me encuentro fuera de la oficina"). La columna `Via` indica si fue por `superficie` o `lema`.

spaCy actúa como **preprocesador**, nunca como clasificador. Sin el modelo el motor sigue funcionando, comparando solo superficie — marca menos, pero no se cae.

---

## Las dos ramas OTBB (por eso hay 4 salidas)

Después del motor, el flujo se bifurca:

| Rama | Qué necesita |
|---|---|
| **Maestro** | Exclusiones **fuera** — no pagar tokens por ruido |
| **Conversacional** | Exclusiones **adentro** — un "gracias" al final dice cómo cerró la conversación |

Por eso el motor **nunca reemplaza el texto por la etiqueta**: devuelve texto y marca por separado.

`POST /procesar-otbb` devuelve las cuatro tablas:

| Archivo | Contenido |
|---|---|
| `_maestro.csv` | Solo lo analizable (sin exclusiones ni duplicados) |
| `_conversacional.csv` | Todo el hilo en orden, con texto limpio y marca al lado |
| `_excluidos.csv` | Qué se excluyó y por qué (regla, evidencia, categoría) |
| `_exclusiones_data_maestro.csv` | Formato exacto de la hoja Data_maestro |

---

## API HTTP

Servicio FastAPI (`api.py`), autenticado con `API_KEY` o `API_KEY_FILE`.

| Ruta | Uso |
|---|---|
| `GET /health` | Estado, catálogo cargado, si spaCy está activo |
| `GET /catalogo` | Resumen de reglas por categoría |
| `POST /procesar` | Un mensaje |
| `POST /procesar-lote` | Un hilo o base completa |
| `POST /procesar-otbb` | Las 4 salidas OTBB (JSON o ZIP) |
| `POST /validar-catalogo` | Corre suite de regresión |
| `POST /recargar-catalogo` | Recarga catálogo **solo si pasa la suite** |

### Entrada

- **Mínimo:** `texto` (obligatorio)
- **Recomendado:** `hilo`, `fecha`, `remitente`, `msg_id` — sin ellos se pierden deduplicación e insistencia (la respuesta lo avisa en `avisos`)

### Restricción

Un hilo **no puede partirse** entre dos llamadas: la deduplicación necesita el historial completo.

---

## Catálogos (fuera de la imagen Docker)

| Archivo | Rol |
|---|---|
| `exclusiones.csv` | Reglas de exclusión |
| `prioridades.csv` | Orden entre categorías |
| `banners.yaml` | Bloques institucionales a quitar |
| `tipificaciones.csv` | Mapeo a columnas WM |
| `valores_validos.csv` | Valores permitidos al editar |

El catálogo se monta como volumen (`/catalogos`). Cambiar una regla **no requiere redeploy** — solo `POST /recargar-catalogo`, que valida antes de activar.

---

## Qué devuelve por mensaje (campos clave)

Además de los campos de entrada intactos, agrega:

- **`Exclusión` / `Regla_ID` / `Evidencia`** — qué, cuál regla, qué texto disparó
- **`Texto_para_prompt`** — texto limpio para mandar al LLM
- **`Bloques_removidos`** — banners que se quitaron
- **`Continua`** — un "gracias" puede marcarse sin excluirse si cierra la conversación
- **`Insistencia_n`** — cuántas veces el mismo remitente escribió sin respuesta
- **`Fila_estado`** — `unico`, `duplicado_archivo`, `duplicado_sospechoso`
- **`Via`** — `superficie` o `lema` (cómo se encontró el match)
- **`Perdedores`** — otras reglas que también matchearon pero perdieron el desempate

---

## Principios de diseño

1. **Determinista y auditable** — misma entrada → misma salida; siempre hay evidencia.
2. **Catálogo = criterio de negocio** — lo edita la lingüista, no el código.
3. **No destructivo** — agrega columnas, no borra filas ni reemplaza texto.
4. **Seguro al cambiar reglas** — auditoría + suite de regresión antes de recargar.
5. **spaCy es ayuda, no cerebro** — solo lematiza; la clasificación es regex + catálogo.

---

## En resumen

Es un **filtro inteligente de ruido operativo** para correos bancarios: limpia banners, deduplica historial, detecta insistencias y aplica ~105 reglas de exclusión con trazabilidad completa. Va **antes** del análisis con LLM para ahorrar tokens y entregar conversaciones más limpias, sin delegar el criterio a un modelo generativo.

---

## Referencias en el repo

| Recurso | Ubicación |
|---|---|
| README técnico completo | `README.md` |
| Orquestación del pipeline | `motor/pipeline.py` |
| Motor de reglas | `motor/exclusion_engine.py` |
| Lematización spaCy | `motor/lemas.py` |
| API HTTP | `api.py` |
| Suite de regresión | `tests/casos_regresion.yaml` |
| Pendientes y decisiones | `PENDIENTES.md` |
