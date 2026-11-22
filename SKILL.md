---
name: postiz-minio-publisher
description: >-
  Valida, normaliza y sube videos (.mp4, .mov, .mkv), miniaturas y archivos
  auxiliares de proyecto a MinIO S3 (minio/postiz) mediante mc.exe y el script
  helper, y programa o publica en redes sociales usando el servidor MCP de
  Postiz sin re-descargar medios. Usar al invocar @postiz-minio-publisher o
  /postiz-minio-publisher, o al pedir subir un video a MinIO y publicarlo en
  Postiz.
---

# Postiz & MinIO Social Video Publisher

Actúa como un Ingeniero de Automatización y Gestor de Contenidos Multiplataforma.
Tu objetivo es validar y subir videos y activos de proyecto a MinIO
(`minio/postiz`) y programar o publicar en redes sociales mediante el servidor
MCP de Postiz usando directamente las URLs de `https://s3.atomsystems.org/postiz/...`.

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
* El usuario pase la ruta de un video local y pida subirlo a MinIO/S3 y
  publicarlo o programarlo en Postiz.

---

## 3. Flujo operativo (paso a paso)

### Paso 1: Recepción de archivos locales
1. Capturar la ruta local del **video principal** (solo se admiten
   `.mp4`, `.mov`, `.mkv`).
2. Preguntar o capturar si además desea subir al mismo proyecto en MinIO:
   * **Miniatura / portada** (`.jpg`, `.jpeg`, `.png`, `.webp`).
   * **Archivos auxiliares de proyecto** (líneas del tiempo `.fcpxml`/`.edl`/`.drp`,
     subtítulos `.srt`/`.vtt`, licencias `.txt`/`.pdf`/`.md`, etc.).

### Paso 2: Selección de carpeta en MinIO, normalización y subida confirmada
1. **Listar proyectos existentes** en `minio/postiz/`:
   ```powershell
   uv run "<ruta_de_esta_skill>/scripts/minio_publisher.py" list-projects
   ```
2. **Preguntar ubicación al usuario** mostrando los proyectos existentes:
   * **Subcarpeta por proyecto (preferida):** `minio/postiz/<proyecto>/` (si el
     proyecto no existe aún, el prefijo se crea automáticamente al subir).
   * **Raíz del bucket:** `minio/postiz/`.
3. **Validar y previsualizar nombres normalizados (sin subir todavía):**
   ```powershell
   uv run "<ruta_de_esta_skill>/scripts/minio_publisher.py" prepare `
     --video "<ruta_video>" `
     --thumbnail "<ruta_miniatura>" `
     --extra "<ruta_auxiliar_1>" `
     --project "<nombre_proyecto>"
   ```
   *(Omitir `--project` si eligió raíz; omitir `--thumbnail` / `--extra` si no
   aplican).*
4. **Pedir confirmación explícita antes de subir:**
   Mostrar al usuario la carpeta destino (`project_normalized` y si ya existe o
   se creará), los nombres normalizados de cada archivo (minúsculas ASCII sin
   espacios ni acentos), su tamaño, si alguno ya existe en MinIO y las URLs
   resultantes (`https://s3.atomsystems.org/postiz/...`). **No ejecutar `upload`
   hasta que el usuario confirme.**
5. **Ejecutar la subida verificada:**
   ```powershell
   uv run "<ruta_de_esta_skill>/scripts/minio_publisher.py" upload `
     --video "<ruta_video>" `
     --project "<nombre_proyecto>"
   ```
   *(Agregar `--thumbnail`, `--extra`, `--skip-existing` o `--overwrite` según
   lo confirmado por el usuario).*
   Confirmar que el JSON devuelva `upload_status` exitoso y `http_check.accessible: true`.

### Paso 3: Consulta de canales, horarios disponibles y esquemas en Postiz
1. Llamar a `groupList` (si aplica) e `integrationList` para mostrar los canales
   conectados (`integrationId`, nombre, `platform`).
2. Si el usuario quiere revisar huecos o programar fecha, convertir la ventana
   local a UTC con:
   ```powershell
   uv run "<ruta_de_esta_skill>/scripts/minio_publisher.py" to-utc --local-datetime "<YYYY-MM-DD HH:MM>"
   ```
   y consultar `postsListTool` en esa ventana UTC.
3. Para cada plataforma elegida, llamar a `integrationSchema` (`platform`,
   `isPremium`) y, si devuelve `tools` necesarios (playlists, categorías, etc.),
   invocar `triggerTool` para obtener los IDs reales requeridos por `settings`.

### Paso 4: Confirmación de publicación (sin comentarios, hora local → UTC)
Antes de llamar a `integrationSchedulePostTool`, confirmar siempre con el usuario:
1. **Tipo de envío (preguntar siempre):** `"schedule"` (programado), `"draft"`
   (borrador) o `"now"` (publicar ahora).
2. **Fecha y hora:** aceptar la hora en la **zona horaria local del usuario**
   (del sistema) y convertirla a UTC con el subcomando `to-utc` (o `to-utc --local-datetime now` si eligió `"now"`).
3. **Canales destino y texto HTML:**
   * Formatear cada línea de `content` dentro de `<p>...</p>` (etiquetas válidas:
     `h1`, `h2`, `h3`, `u`, `strong`, `li`, `ul`, `p`; nunca combinar `u` +
     `strong`).
   * **Sin comentarios:** `postsAndComments` debe llevar **un único elemento**
     (solo el post principal).
4. **Uso de miniaturas y archivos auxiliares:**
   * **Video:** pasar directamente `https://s3.atomsystems.org/postiz/<ruta_video>`
     en `attachments`. **No usar `uploadFromUrlTool`** para archivos ya subidos
     a `minio/postiz` (Postiz comparte el bucket y toma el video directo sin
     re-descargar archivos pesados).
   * **Miniatura (`.jpg`/`.png`/`.webp`):** vincular su URL S3 en
     `attachments[0].thumbnail` (y/o en `settings.thumbnail` si el esquema de la
     plataforma lo solicita; verificar `integrationSchema`).
   * **Archivos auxiliares (líneas del tiempo, licencias, subtítulos, etc.):**
     quedan almacenados en `minio/postiz/<proyecto>/` con su URL pública; **no**
     se colocan en `attachments` de Postiz. Solo si el usuario lo pide, puede
     incluirse el enlace en el texto HTML (`<p>...</p>`) del post.
5. **Ajustes por plataforma (`settings`):** título, visibilidad (`public` /
   `private` / `unlisted` en YouTube, etc.), `shortLink`, etc.

### Paso 5: Programación y verificación en Postiz
1. Invocar `integrationSchedulePostTool` con `socialPost`.
2. Si tiene éxito, entregar al usuario:
   * Los `previewUrl` de cada publicación creada.
   * El resumen de todas las URLs guardadas en MinIO (video, miniatura y
     archivos auxiliares del proyecto).
3. Si devuelve `output.errors`, corregir los parámetros observados y reintentar
   tras confirmar cualquier cambio relevante.
