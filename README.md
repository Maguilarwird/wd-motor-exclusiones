# Motor de exclusiones — OTBB

Capa determinista que decide, mensaje por mensaje, si un correo aporta algo al
análisis o es ruido operativo. **Nunca borra nada:** agrega columnas con la
decisión, la regla que la tomó y el texto exacto que la disparó.

El motor no sabe nada del dominio. Todo el criterio vive en los catálogos.

---

## Estado: v0

Esto es el prototipo funcionando, con la higiene mínima para vivir en un repo.
**No es todavía la versión descrita en el documento de hallazgos.** Lo que falta
está en [PENDIENTES.md](PENDIENTES.md), medido y priorizado.

Lo que v0 sí hace:

| | |
|---|---|
| Reglas del catálogo | 141, en `catalogos/exclusiones.csv` |
| Condiciones implementadas | Frase contenida, Solo aislado, Remitente / dominio |
| Límites de palabra | sí, todo término se compila con `\b` |
| Limpieza de bloques | sí, cabecera y pie tratados distinto, con prueba de seguridad |
| Alcance por regla | sí, columna `Alcance` consumida por el motor |
| Devuelve evidencia | sí, siempre: regla, condición, texto que disparó |
| Registra solapamientos | sí, columna `Perdedores` |
| Suite de regresión | 13 casos, 3 marcados como hueco conocido |
| Auditoría del catálogo | `scripts/auditar_catalogo.py` |

Lo que v0 **no** hace, y hay que saberlo antes de creerle un número:

- **No deduplica el historial citado.** Es el arreglo de mayor retorno (~15,6%
  del ahorro de tokens) y no está implementado.
- **No usa lematización.** Matchea superficie. Las reglas del catálogo escritas
  en infinitivo (`estar ausente`, `encontrar de vacaciones`) no disparan sobre
  las formas conjugadas reales. La auditoría las lista.
- **No decide por especificidad.** Recoge todos los matches y se queda con el
  primero según el orden de condición, no con el más específico.
- **No implementa `Sin petición`.**

---

## Instalación

```bash
pip install -r requirements.txt
```

## Uso

```bash
# procesar una base
python scripts/procesar_exclusiones.py ruta/a/base.xlsx --contexto santander,cl

# probar textos a mano
python scripts/probar_exclusiones.py --contexto santander,cl

# antes de cualquier commit que toque el catálogo
python scripts/auditar_catalogo.py
python tests/test_regresion.py
```

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

---

## Alcance: cómo se agrega un cliente

Un cliente nuevo es **filas nuevas**, no un archivo nuevo ni un `if` en el
motor. Cada regla declara dónde aplica:

- `todos` — aplica siempre
- `cl`, `mx`, … — solo si ese token viene en el contexto de la llamada
- `santander`, `bch`, … — solo para ese cliente
- `cl,mx` — varios, separados por coma

El motor recibe el contexto desde afuera (`--contexto`) y activa solo lo que
calza. En v0 **todas las reglas están en `todos`**, para que el comportamiento
sea idéntico al del prototipo. Marcar las que son locales es trabajo de
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
```

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
