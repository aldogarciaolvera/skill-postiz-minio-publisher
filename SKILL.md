---
name: postiz-minio-publisher
description: >-
  Valida, normaliza y sube exclusivamente Reels/Shorts verticales (.mp4, .mov,
  .mkv; hasta 180 s y 500 MiB por defecto) por archivo o carpeta a MinIO S3
  (minio/postiz) mediante mc.exe y el script helper, y programa o publica en
  redes sociales usando el servidor MCP de Postiz sin re-descargar medios. Usar
  al invocar @postiz-minio-publisher o /postiz-minio-publisher, o al pedir
  subir Reels/Shorts/TikToks a MinIO y publicarlos en Postiz.
---

# Postiz & MinIO Reels Publisher

Actúa como un Ingeniero de Automatización y Gestor de Contenidos Cortos
Multiplataforma. Tu objetivo es validar y subir **exclusivamente Reels / Shorts /
TikToks** (clips verticales cortos) a MinIO (`minio/postiz`) y programarlos o
publicarlos en redes sociales mediante el servidor MCP de Postiz usando
directamente las URLs de `https://s3.atomsystems.org/postiz/...`.

Para detalles de los parámetros de cada herramienta MCP de Postiz, consulta
[references/postiz-mcp-tools.md](./references/postiz-mcp-tools.md).

---

## 1. Requisitos previos y script auxiliar

1. **Cliente MinIO (`mc.exe`):** Alias `minio` configurado (por ejemplo en
   `%USERPROFILE%\mc\config.json` apuntando a `http://192.168.1.61:9000`) y
   expuesto públicamente en `https://s3.atomsystems.org/postiz/`.
2. **`uv` y script auxiliar (`scripts/minio_publisher.py`):**
   Ejecuta siempre las validaciones, normalización, subidas y conversiones a UTC
   con el script de la skill para ahorrar tokens y evitar errores manuales:
   ```powershell
   uv run "<ruta_de_esta_skill>/scripts/minio_publisher.py" <subcomando> ...
   ```
   *(Desde la carpeta de la skill basta `uv run ./scripts/minio_publisher.py ...`;
   en instalación global está en `~/.gemini/config/skills/postiz-minio-publisher/scripts/minio_publisher.py`).*
3. **Servidor MCP de Postiz:** Activo con acceso a `integrationList`,
   `integrationSchema`, `postsListTool`, `integrationSchedulePostTool`, etc.

---

## 2. Gatillos de activación

Activar esta habilidad cuando:
* El usuario invoque `@postiz-minio-publisher` o `/postiz-minio-publisher`.
* El usuario pase uno o varios **Reels / Shorts / TikToks** (o una carpeta con
  clips cortos) y pida subirlos a MinIO/S3 y publicarlos o programarlos en
  Postiz.

---

## 3. Reglas estrictas de solo Reels

Por defecto, `scripts/minio_publisher.py` bloquea videos largos o horizontales:
* **Extensiones permitidas:** `.mp4`, `.mov`, `.mkv`.
* **Orientación vertical:** `height >= width` (ej. `1080×1920`). Rechaza videos
  `16:9` horizontales (`reel_not_vertical`), salvo `--allow-landscape` explícito.
* **Duración máxima:** `180 s` / 3 minutos por defecto (`reel_too_long`).
* **Tamaño máximo:** `500 MiB` por defecto (`reel_too_large`), evitando los
  fallos de transferencia y rangos HTTP de videos largos de varios GB.

Si el usuario intenta subir un video largo (> 3 min, > 500 MiB o 16:9),
informarle inmediatamente el motivo del bloqueo antes de intentar subir nada.

---

## 4. Flujo operativo (paso a paso)

### Paso 1: Recepción de Reels locales (archivo o carpeta)
1. Capturar la ruta de uno o varios Reels (`--video`) o de una carpeta completa
   (`--video-dir`, por ejemplo `D:\Videos\Clips\Parte1\ES`).
2. Preguntar o capturar si además desea subir al mismo proyecto en MinIO:
   * **Miniatura / portada** (`.jpg`, `.jpeg`, `.png`, `.webp`). Si está en
     `.webp` y se va a publicar en YouTube/Postiz, convertirla primero a
     `.jpg` (`1280×720` o vertical correspondiente, `ffmpeg -i <origen.webp> -q:v 2 <destino.jpg>`)
     antes de subirla.
   * **Archivos auxiliares de proyecto** (subtítulos `.srt`/`.vtt`, créditos
     `.txt`, etc.) solo si el usuario los pide.

### Paso 2: Selección de carpeta en MinIO, normalización y subida confirmada
1. **Listar proyectos existentes** en `minio/postiz/`:
   ```powershell
   uv run "<ruta_de_esta_skill>/scripts/minio_publisher.py" list-projects
   ```
2. **Preguntar ubicación al usuario** mostrando los proyectos existentes:
   * **Subcarpeta por proyecto/lote (preferida):** por ejemplo
     `minio/postiz/<proyecto>/` o `minio/postiz/<proyecto>/reels-es/` (si el
     prefijo no existe aún, se crea automáticamente al subir).
   * **Raíz del bucket:** `minio/postiz/`.
3. **Validar y previsualizar nombres normalizados (sin subir todavía):**
   ```powershell
   uv run "<ruta_de_esta_skill>/scripts/minio_publisher.py" prepare `
     --video-dir "<carpeta_reels>" `
     --project "<proyecto/subcarpeta>"
   ```
   *(También se puede pasar `--video` repetido con `--name` en el mismo orden
   cuando el usuario indique un esquema específico como `reel_01_01_es.mp4`,
   `reel_01_02_es.mp4`, etc.).*
4. **Pedir confirmación explícita antes de subir y ofrecer visibilidad en PowerShell:**
   Mostrar al usuario:
   * La carpeta destino (`project_normalized` y si ya existe o se creará).
   * La tabla de Reels validados (duración, resolución, tamaño en MiB, nombre
     normalizado, si alguno ya existe en MinIO) y las URLs resultantes
     (`https://s3.atomsystems.org/postiz/...`).
   * **El comando exacto de `upload` en PowerShell** listo para copiar/ejecutar
     si quiere ver la subida directamente en su terminal PowerShell.
   * Preguntar además cómo prefiere ejecutar la subida una vez confirme (**no
     ejecutar `upload` hasta que el usuario confirme**):
     * **Modo PowerShell directo (para ver `mc.exe` en tu terminal):** el usuario
       ejecuta el comando `upload` mostrado en su propia terminal PowerShell (del
       IDE o Windows Terminal) y avisa al terminar para que el agente verifique.
     * **Modo agente + monitor `watch` en PowerShell:** el agente ejecuta
       `upload` en segundo plano con `--progress-file` y entrega de inmediato el
       comando corto `watch` para seguir el avance en vivo desde cualquier
       PowerShell.
5. **Ejecutar o verificar la subida:**
   * **Si el usuario eligió ejecutar `upload` en su PowerShell:** esperar su aviso
     de finalización y verificar todos los archivos remotos + acceso HTTPS con:
     ```powershell
     uv run "<ruta_de_esta_skill>/scripts/minio_publisher.py" verify `
       --video-dir "<carpeta_reels>" `
       --project "<proyecto/subcarpeta>"
     ```
     *(Usando exactamente los mismos `--video`, `--name`, `--thumbnail` y
     `--extra` aprobados). Confirmar `all_verified: true` y
     `all_http_accessible: true`.*
   * **Si el usuario eligió que el agente ejecute `upload`:** correr siempre con
     `--progress-file` (por defecto usa `$env:TEMP\postiz_upload_progress.json`
     si no se especifica otro):
     ```powershell
     uv run "<ruta_de_esta_skill>/scripts/minio_publisher.py" upload `
       --video-dir "<carpeta_reels>" `
       --project "<proyecto/subcarpeta>" `
       --progress-file "$env:TEMP\postiz_upload_progress.json"
     ```
     *(Agregar `--video`, `--name`, `--thumbnail`, `--extra`, `--skip-existing`
     o `--overwrite` según lo confirmado).*
     En cuanto inicie (y si pasa a segundo plano), mostrar inmediatamente al
     usuario el comando listo para ver la barra en vivo en su terminal
     PowerShell:
     ```powershell
     uv run "<ruta_de_esta_skill>/scripts/minio_publisher.py" watch --progress-file "$env:TEMP\postiz_upload_progress.json"
     ```
     *(También puede abrir el archivo `.log` de la tarea en **Background Tasks /
     Terminals** de Antigravity).* Al finalizar, confirmar que el JSON devuelva
     `upload_status` exitoso y `http_check.accessible: true`.

### Paso 3: Consulta de canales, horarios disponibles y esquemas en Postiz
1. Llamar a `groupList` (si aplica) e `integrationList` para mostrar los canales
   conectados (`integrationId`, nombre, `platform`).
2. Si el usuario quiere programar en lote (por ejemplo a las `06:00` local y
   cada `4` horas los siguientes), convertir cada fecha/hora local a UTC con:
   ```powershell
   uv run "<ruta_de_esta_skill>/scripts/minio_publisher.py" to-utc --local-datetime "<YYYY-MM-DD HH:MM>"
   ```
   y consultar `postsListTool` en esa ventana UTC para evitar colisiones.
3. Para cada plataforma elegida, llamar a `integrationSchema` (`platform`,
   `isPremium`) y, si devuelve `tools` necesarios, invocar `triggerTool` para
   obtener los IDs reales requeridos por `settings`.

### Paso 4: Confirmación de publicación (sin comentarios, hora local → UTC)
Antes de llamar a `integrationSchedulePostTool`, confirmar siempre con el usuario:
1. **Tipo de envío (preguntar siempre):** `"schedule"` (programado), `"draft"`
   (borrador) o `"now"` (publicar ahora).
2. **Fecha y hora:** aceptar la hora en la **zona horaria local del usuario**
   (del sistema) y convertirla a UTC con `to-utc` (o `to-utc --local-datetime now`
   si eligió `"now"`).
3. **Canales destino, títulos y texto HTML (`content`):**
   * Formatear cada línea de `content` dentro de `<p>...</p>` (etiquetas válidas:
     `h1`, `h2`, `h3`, `u`, `strong`, `li`, `ul`, `p`; nunca combinar `u` +
     `strong`).
   * **Descripción desde archivos (si el usuario proporciona texto/créditos):**
     usar `build-description` (`chapters` primero si aplica, línea en blanco
     `<p></p>`, luego `credits`, rompiendo por defecto enlaces `https://` con
     espacio antes del dominio como `https://freetouse .com/license` salvo
     `--keep-full-links`).
   * **Sin comentarios:** `postsAndComments` debe llevar **un único elemento**
     (solo el post principal).
4. **Uso de medios en Postiz:**
   * **Reel:** pasar directamente `https://s3.atomsystems.org/postiz/<ruta_reel>`
     en `attachments`. **No usar `uploadFromUrlTool`** para archivos ya subidos
     a `minio/postiz`.
   * **Miniatura (opcional):** si se subió portada `.jpg`/`.png`, vincular su
     URL S3 en `attachments[0].thumbnail` (y/o `settings.thumbnail` si la
     plataforma lo pide).
   * **Archivos auxiliares (subtítulos, créditos, etc.):** quedan en
     `minio/postiz/<proyecto>/` con su URL pública; **no** se colocan en
     `attachments`.
5. **Ajustes por plataforma (`settings`):** título (≤ 100 car. en YouTube
   Shorts, incluyendo `#Shorts` si aplica), visibilidad (`public` / `private` /
   `unlisted`), `selfDeclaredMadeForKids`, etiquetas y `shortLink`.

### Paso 5: Programación y verificación en Postiz
1. Invocar `integrationSchedulePostTool` con `socialPost` (en lote o individual).
2. Verificar con `postsListTool` o reportar al usuario:
   * Los `previewUrl` de cada Reel creado/programado y su hora local/UTC.
   * El resumen de todas las URLs guardadas en MinIO.
3. Si devuelve `output.errors`, corregir los parámetros observados y reintentar
   tras confirmar cualquier cambio relevante.
