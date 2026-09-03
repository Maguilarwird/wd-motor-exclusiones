# Motor de exclusiones — OTBB

Capa determinista que decide, mensaje por mensaje, si un correo aporta algo al
análisis o es ruido operativo. **Nunca borra nada:** agrega columnas con la
decisión, la regla que la tomó y el texto exacto que la disparó.

El motor no sabe nada del dominio. Todo el criterio vive en los catálogos.

---

## Estado: v1

Validado contra dos bases reales de producción y corregido con validación
humana regla por regla. **El criterio del catálogo lo fija la lingüista, no el
código.**

| | |
|---|---|
| Reglas del catálogo | 105, en `catalogos/exclusiones.csv`, 17 categorías |
| Condiciones implementadas | Frase contenida (92), Solo aislado (11), Remitente / dominio (2) |
| Bloques institucionales | 7, en `catalogos/banners.yaml` |
| Prioridad entre categorías | sí, declarada en `catalogos/prioridades.csv` |
| Deduplicación del historial citado | sí, `motor/deduplicacion.py` |
| Lematización | sí, spaCy `es_core_news_lg`, **opcional** (`motor/lemas.py`) |
| Límites de palabra | sí, todo término se compila con `` |
| Limpieza de bloques | sí, cabecera y pie tratados distinto, con prueba de seguridad |
| Alcance por regla | sí, columna `Alcance` consumida por el motor |
| Devuelve evidencia | sí, siempre: regla, condición, texto que disparó |
| Registra solapamientos | sí, columna `Perdedores` |
| Suite de regresión | 23 casos de catálogo + 12 de pipeline, 3 huecos conocidos |
| Auditoría del catálogo | `scripts/auditar_catalogo.py` |

Medido sobre dos bases reales, con el catálogo validado a mano:

| | base A | base B |
|---|---|---|
| correos | 80.340 | 12.078 |
| exclusiones | 631 | 407 |
| banners removidos | 84.534 | 8.149 |
| **ahorro de texto** | **59,3 %** | **49,2 %** |

Lo que **no** hace, y hay que saberlo antes de creerle un número:

- **No decide por especificidad.** Recoge todos los matches y desempata por
  condición, luego por la prioridad declarada de la categoría, luego por ID. Un
  match más específico no gana por ser más específico.
- **No implementa `Sin petición`.**
- **`No-reply` describe el mecanismo, no el contenido**, y por eso se solapa con
  todas las demás categorías. La prioridad lo mitiga —queda de último recurso—
  pero el catálogo sigue mezclando dos dimensiones. Ver `PENDIENTES.md`.
- **Sin el modelo de spaCy el motor funciona igual**, comparando superficie: las
  reglas escritas en infinitivo no disparan sobre las formas conjugadas. No se
  cae ni avisa distinto, sólo marca menos.

---

## Las dos ramas, y por qué son cuatro archivos

El flujo del OTBB se bifurca después del motor, y cada rama necesita algo
distinto del **mismo** procesamiento:

- el **maestro** quiere las exclusiones fuera, para no pagar tokens por ellas
- el **conversacional** las quiere adentro, porque un «gracias» al final dice
  cómo se cerró la conversación

Por eso **el motor nunca reemplaza el texto por la etiqueta**: devuelve el texto
y la marca por separado, y cada rama toma lo que necesita. Si el motor
sobreescribiera el cuerpo, la rama conversacional perdería el dato.

`scripts/salidas_otbb.py` produce las cuatro salidas de una sola corrida:

| archivo | para qué |
|---|---|
| `_maestro.csv` | lo que SÍ se analiza, sin exclusiones ni filas repetidas |
| `_conversacional.csv` | todas las filas en orden de hilo y turno, con texto limpio y la marca al lado |
| `_excluidos.csv` | qué se excluyó y por qué: hilo, turno, categoría, regla, evidencia |
| `_exclusiones_data_maestro.csv` | lo mismo agregado, en el formato exacto de la hoja `Data_maestro` |

---

## Instalación

```bash
pip install -r requirements.txt
python -m spacy download es_core_news_lg
```

El modelo **no** se instala con el `requirements.txt`: pesa ~600 MB. Tiene que
ser el grande. Medido con la suite de regresión, `es_core_news_sm` falla dos
casos —y son los de fuera de oficina: el modelo chico no reconoce «Estaré» ni
«Estaremos» como formas de «estar». Se elige con `MODELO_SPACY`.

Sin el modelo el motor arranca igual y marca menos. `GET /health` dice cuál está
cargado.

## Uso

```bash
# las cuatro salidas del OTBB, de una corrida
python scripts/salidas_otbb.py ruta/a/base.csv --contexto cliente,cl

# mandar una base al servicio en vez de procesarla acá
python scripts/cliente_api.py ruta/a/base.csv --url https://... --key-file clave.txt

# probar textos a mano
python scripts/probar_exclusiones.py --contexto cliente,cl

# buscar huecos del catálogo con evidencia, sin decidir nada
python scripts/huecos_catalogo.py salida_conversacional.csv

# antes de cualquier commit que toque el catálogo
python scripts/auditar_catalogo.py
python tests/test_regresion.py
python tests/test_pipeline.py
```

**La clave de la API nunca va como argumento**: queda en el historial del shell.
Se pasa con `--key-file` o por la variable `API_KEY`.

Ninguna ruta de cliente vive dentro de los scripts. La base se pasa por
argumento; el catálogo se resuelve por `--catalogo`, por la variable de entorno
`CATALOGO_EXCLUSIONES`, o por el default del repo.

---

## El servicio

Todo lo anterior es una librería. `motor/pipeline.py` la orquesta en una sola
función, `procesar(mensajes, contexto)`, y `api.py` la expone por HTTP. Los
scripts de terminal y el servicio llaman a la misma función: no hay dos
implementaciones que puedan divergir.

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
# o
docker build -t motor-exclusiones . && \
docker run -p 8000:8000 -v /ruta/a/catalogos:/catalogos motor-exclusiones
```

| Ruta | Para qué |
|---|---|
| `GET /health` | vivo, y con qué catálogo. 503 si el catálogo está malo |
| `GET /catalogo` | qué reglas hay cargadas ahora mismo |
| `POST /procesar` | un mensaje, síncrono |
| `POST /procesar-lote` | un hilo o una base completa |
| `POST /validar-catalogo` | corre la suite contra el catálogo en disco |
| `POST /recargar-catalogo` | lo carga en memoria, **solo si la suite pasa** |

Lo que entra por cada mensaje: `texto` es lo único obligatorio. `hilo`, `fecha`,
`remitente` y `msg_id` no lo son, pero sin ellos se pierden las dos etapas de
mayor retorno — la respuesta lo dice en `avisos` en vez de callarlo.

Lo que sale: el mensaje con `texto_para_prompt` y los campos del contrato de
salida. **Nunca se borra ni se reemplaza un campo de entrada, solo se agregan.**
Eso es lo que permite que quien consuma decida después qué usar sin coordinarse
antes.

Dos decisiones del despliegue que no son negociables:

1. **El catálogo vive fuera de la imagen**, en el volumen `/catalogos`. Cambiar
   una regla no puede requerir un despliegue.
2. **La recarga corre la suite antes de aceptar.** Si el catálogo nuevo no pasa,
   devuelve 400 y sigue activo el anterior. Autoservicio sin ese test es cómo una
   regla mala llega a producción en silencio.

### Variables de entorno

| variable | para qué | si falta |
|---|---|---|
| `CATALOGO_EXCLUSIONES` | ruta del CSV de reglas | usa el del repo |
| `BANNERS_EXCLUSIONES` | ruta del YAML de bloques | usa el del repo |
| `PRIORIDADES_EXCLUSIONES` | ruta del CSV de prioridades | **todas las categorías empatan** y desempata el ID |
| `MODELO_SPACY` | qué modelo cargar | prueba `lg` y cae a `sm` |
| `SIN_LEMAS` | apaga la lematización, para medir A/B | queda encendida |
| `API_KEY_FILE` | archivo con la clave (secret montado) | se lee `API_KEY` |
| `API_KEY` | la clave, si no hay archivo | el servicio queda **sin autenticación** |
| `MAX_MENSAJES_POR_LOTE` | tope por llamada | 5000 |
| `PORT` | puerto de escucha | 8000 |

### Lo que hay que saber antes de desplegarlo en un PaaS

- **La imagen pesa ~1,7 GB y el proceso usa ~900 MB de RAM**, casi todo el
  modelo de spaCy (~600 MB). Un plan de 512 MB no lo levanta.
- **Un hilo no se puede partir entre dos llamadas.** La deduplicación del
  historial y la insistencia se calculan comparando mensajes del mismo hilo: si
  la mitad quedó en otro lote, no hay contra qué comparar. Al armar los lotes se
  corta entre hilos, nunca dentro de uno.
- **El catálogo va montado, no dentro de la imagen.** Si queda adentro, cambiar
  una regla pasa a ser un despliegue.

---

## Los catálogos

### `catalogos/exclusiones.csv`

Se lee **por nombre de columna**, nunca por posición: insertar una columna no
cambia lo que el motor lee.

Está en CSV a propósito. Un `.xlsx` dentro de git es un binario: no se puede ver
qué regla cambió entre dos versiones ni resolver un conflicto. Con CSV el diff
se lee línea por línea. El motor también acepta `.xlsx` si hace falta, pero la
fuente de verdad del repo es el CSV.

**Cómo editarlo sin licencia de Office:** subir el CSV a Google Sheets (Archivo
→ Importar), editar, y bajar con Archivo → Descargar → Valores separados por
comas. Reemplazar el archivo, correr la auditoría y la suite, y recién ahí
commitear. También sirve LibreOffice Calc, que es gratis y abre CSV y xlsx.
`catalogos/valores_validos.csv` lista los valores aceptados en cada columna.

| Columna | Para qué |
|---|---|
| `ID` | estable. No se genera por posición: insertar una fila no debe mover los IDs de abajo |
| `Categoría` | la categoría de exclusión |
| `Texto Contenido` | los términos disparadores (sintaxis abajo) |
| `Condición de match` | Frase contenida · Solo aislado · Remitente / dominio |
| `Tratamiento` | qué hacer con el mensaje |
| `Alcance` | `todos`, o el país / cliente donde aplica |

Sintaxis de `Texto Contenido`:

| Forma | Significa | Ejemplo |
|---|---|---|
| `a \| b` | co-ocurrencia: los dos deben estar | `fuera de oficina \| respuesta automática` |
| `a / b` (con espacios) | frases alternativas completas | `no se pudo entregar / no se puede entregar` |
| `a/b/c` (sin espacios) | alternancia de una palabra | `mi/el/su correo ahora/nuevo es` |
| `fecha` | meta-token que matchea fechas reales | `estaré ausente \| desde/hasta fecha` |

### `catalogos/banners.yaml`

Bloques institucionales que se remueven antes de evaluar cualquier regla. Si no
se limpian, una frase del banner activa una regla y el mensaje se excluye por
algo que el cliente nunca escribió.

`tipo: cabecera` se borra donde esté. `tipo: pie` corta desde el match hasta el
final. **Tratar una cabecera como pie borra el cuerpo del mensaje** — en la
muestra de INVEX eso destruyó 39 de 116 cuerpos y produjo un "ahorro" del 45%
que era información perdida. Por eso hay una prueba de seguridad: si un mensaje
tenía más de 90 caracteres y tras limpiar queda con menos de 30, se descarta la
limpieza, se usa el crudo, y el proceso termina con error.

### `catalogos/prioridades.csv`

Cuando dos reglas de categorías distintas matchean el mismo mensaje, gana la de
**número más bajo**. Vive en un archivo aparte y no como una columna del
catálogo porque es una decisión **por categoría**, no por regla: declararla 105
veces invita a que dos filas de la misma categoría queden con prioridades
distintas.

```
10  Rebote / fallo de entrega     el servidor dice que no llegó: es un hecho, no una lectura
20  Fuera de oficina / OOO        dice QUÉ pasa; además es un auto-reply, y tiene que ganarle
30  Confirmación / OKG
40  Evento de calendario
50  Marketing / promo
90  Notificación                  genérica: cede ante cualquier categoría más precisa
99  No-reply                      describe el MECANISMO, no el contenido. Último recurso
```

Una categoría que no está en el archivo queda en 60: cede ante las declaradas
por encima, pero no se va al fondo en silencio. **Si el archivo no está, todas
empatan y el desempate vuelve a ser el ID** — el motor no se cae, sólo pierde la
precedencia. Por eso el despliegue tiene que apuntarle con
`PRIORIDADES_EXCLUSIONES`.

---

## Alcance: cómo se agrega un cliente

Un cliente nuevo es **filas nuevas**, no un archivo nuevo ni un `if` en el
motor. Cada regla declara dónde aplica:

- `todos` — aplica siempre
- `cl`, `mx`, … — solo si ese token viene en el contexto de la llamada
- `santander`, `bch`, … — solo para ese cliente
- `cl,mx` — varios, separados por coma

El motor recibe el contexto desde afuera (`--contexto`) y activa solo lo que
calza. Hoy **todas las reglas están en `todos`**: no hay ninguna regla específica de un
cliente, y esa es la intención —el motor sirve a cualquier cliente con correos. Marcar las que son locales es trabajo de
catálogo: la auditoría lista las candidatas.

---

## Lo que no puede entrar a este repo

Ninguna base de correos. Ni recortada, ni anonimizada. Traen RUT, correos y
nombres completos de clientes y de ejecutivos, y una vez en la historia de git
no salen borrando el archivo.

Las bases van en `datos/`, que está en el `.gitignore`.

Los casos de `tests/casos_regresion.yaml` son texto **inventado a mano**. No son
una muestra enmascarada. Por eso pueden estar acá y por eso la suite corre en
cualquier máquina.

---

## Contrato de salida

Por cada mensaje el motor agrega:

```
Tipo                Exclusión | Gestionable
Exclusión           la categoría
Regla_ID            EXC-nnnn
Regla               el texto de la regla, tal como está escrito
Condición           la condición que se aplicó
Evidencia           el texto exacto que disparó
Accionable          si la exclusión trae señal accionable
Tratamiento         qué hacer con el mensaje
Perdedores          las otras reglas que también matchearon
Bloques_removidos   qué banners se limpiaron
Alerta_limpieza     True si la limpieza dejó el mensaje casi vacío
Origen              quién lo generó: sistema, proveedor, persona
Definicion          la definición de la regla, tal como está en el catálogo
Via                 con qué comparación se encontró: superficie | lema
Continua            si el mensaje marcado espera algo más de la conversación
Motivo_continuidad  por qué
```

`Via` existe para poder medir la lematización sin adivinar: dice si la regla
disparó comparando el texto tal cual o su lema. Corriendo con `SIN_LEMAS=1` se
obtiene el A/B contra la misma base.

`Continua` es lo que permite que un «gracias» aislado se **marque sin
excluirse**: la rama conversacional necesita ver cómo se cerró el hilo. Coincide
con el criterio humano en el 95 % de los casos medidos.

Y cuando la base trae hilos (`--hilo`, `--orden`, `--msgid`), además:

```
Texto_para_prompt    el mensaje sin el historial que ya se citó antes
Fila_estado          unico | duplicado_archivo | duplicado_sospechoso
Repite_a             a qué mensaje del hilo repite
Insistencia_n        2, 3, 4... veces seguidas que ese remitente escribió
                     sin que nadie le contestara
Horas_sin_respuesta  cuánto esperó antes de volver a escribir
Reenvio_identico     True si además no aportó nada nuevo
Motivo_fila          por qué se clasificó así
```

**Filas repetidas e insistencia no son lo mismo, aunque en el texto se vean
igual.** Una fila repetida es un defecto de la ingesta: no pasó nada en la
realidad. Una insistencia sí pasó — alguien tuvo que volver a escribir porque no
le respondieron — y es fricción operativa, uno de los hallazgos que el OTBB
existe para entregar. En los dos casos el texto se deja de mandar; la diferencia
es que la insistencia deja rastro.

La insistencia se decide **por turnos, no por texto**: el mismo remitente escribe
otra vez y nadie contestó en medio. Medirla por similitud de texto no funciona,
porque una respuesta corta que cita la cadena entera se ve idéntica a un reenvío.

Nunca se descarta en silencio. El costo del error es asimétrico: una exclusión
falsa descarta una solicitud real de un cliente y nadie se entera.

---

## Antes de tocar el catálogo

```bash
python scripts/auditar_catalogo.py && python tests/test_regresion.py
```

La auditoría falla cuando algo está **declarado y no consumido**. Existe porque
declarar no es implementar: en el proyecto de entidades un campo declarado y
nunca leído generó 37 falsos positivos que se investigaron durante días como un
problema de precisión del motor.
