# `postiz-minio-publisher`

Skill para **Antigravity / Gemini** enfocada **exclusivamente en Reels / Shorts /
TikToks** (`.mp4`, `.mov`, `.mkv`; verticales y cortos por defecto) que valida,
normaliza y sube lotes o clips individuales a **MinIO S3** (`minio/postiz`)
mediante `mc.exe`, para luego programarlos o publicarlos en redes sociales con el
**servidor MCP de Postiz** sin re-descargar medios.

---

## Características principales

- **Validación estricta solo Reels:** bloquea automáticamente videos largos
  (`> 180 s`), pesados (`> 500 MiB`) u horizontales (`width > height`, `16:9`)
  usando `ffprobe`, evitando problemas de carga en archivos largos.
- **Soporte por carpeta o lote (`--video-dir` / `--video` + `--name`):** permite
  pasar una carpeta completa de Reels o rutas individuales con nombres
  normalizados personalizados (ej. `reel_01_01_es.mp4`, `reel_01_02_es.mp4`).
- **Organización en MinIO por proyecto o raíz:** lista carpetas existentes en
  `minio/postiz/`, pregunta si subir a la raíz o a una subcarpeta por proyecto
  (creándola automáticamente si no existe) y exige confirmación explícita antes
  de iniciar la subida.
- **Visibilidad en terminal PowerShell (2 modos):** permite al usuario correr el
  comando `upload` directamente en su terminal PowerShell (y luego validar con
  `verify`), o bien dejar que el agente suba en segundo plano mientras el usuario
  observa el tablero por archivo en vivo con `watch --progress-file ...`.
- **Cero duplicación en Postiz:** envía directamente las URLs
  `https://s3.atomsystems.org/postiz/...` a `integrationSchedulePostTool` sin
  pasar por `uploadFromUrlTool`.
- **Programación controlada:** pregunta siempre modo (`schedule`, `draft` o
  `now`), visibilidad y horarios locales (ej. `06:00` + cada `4` horas),
  convierte automáticamente a **UTC** y publica sin comentarios adicionales.

---

## Estructura del repositorio

```text
postiz-minio-publisher/
├── SKILL.md                        # Instrucciones principales de la skill (solo Reels)
├── README.md                       # Documentación, instalación y uso
├── VERSION                         # Versión actual (Semantic Versioning)
├── CHANGELOG.md                    # Historial de cambios
├── LICENSE
├── scripts/
│   └── minio_publisher.py          # CLI de validación Reels, normalización, subida, UTC y descripciones
├── references/
│   └── postiz-mcp-tools.md         # Esquemas verificados del servidor MCP de Postiz
└── tests/
    └── test_minio_publisher.py     # Pruebas unitarias de la CLI
```

---

## Requisitos previos

1. **MinIO Client (`mc.exe` / `mc`)** en `PATH` con el alias `minio`
   configurado y el bucket `postiz` accesible públicamente en
   `https://s3.atomsystems.org/postiz/`.
2. **[`uv`](https://docs.astral.sh/uv/)** (o Python 3.10+) para ejecutar
   `scripts/minio_publisher.py`.
3. **`ffprobe`** (y **`ffmpeg`** si conviertes portadas `.webp` a `.jpg`) en
   `PATH` para validar duración y orientación vertical de cada Reel.
4. **Servidor MCP de Postiz** configurado en `~/.gemini/config/mcp_config.json`
   apuntando a tu instancia (`https://postiz.atomsystems.org`) con
   `STORAGE_PROVIDER="cloudflare"` y
   `CLOUDFLARE_BUCKET_URL="https://s3.atomsystems.org/postiz"`.

---

## Instalación

### Instalación global en Antigravity (Windows)

Copia la skill al directorio global de skills de Antigravity (excluyendo `.git`
y cachés temporales):

```powershell
$dst = "$HOME\.gemini\config\skills\postiz-minio-publisher"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item -Force "SKILL.md", "README.md", "VERSION", "CHANGELOG.md", "LICENSE" $dst
Copy-Item -Recurse -Force "scripts", "references" $dst
```

Antigravity descubre automáticamente cualquier carpeta con `SKILL.md` dentro de
`~/.gemini/config/skills/`.

---

## Uso

### Dentro de Antigravity

Invoca la skill con `@postiz-minio-publisher` o `/postiz-minio-publisher`
indicando un Reel o una carpeta de Reels:

> "Usa `@postiz-minio-publisher` para subir estos Reels `D:\Videos\Clips\Parte1\ES`
> a `gta-vi/reels-es` y programarlos en YouTube Shorts desde las 6 am cada 4 horas."

El agente ejecutará el flujo paso a paso:
1. Validará extensión (`.mp4`, `.mov`, `.mkv`), duración (`≤ 180 s`), tamaño
   (`≤ 500 MiB`) y orientación vertical (`height >= width`).
2. Listará proyectos existentes en `minio/postiz/` y te preguntará la ruta
   destino y nombres normalizados.
3. Mostrará la vista previa y pedirá confirmación antes de ejecutar `mc.exe cp`.
4. Consultará canales y horarios en Postiz, confirmará modo (`schedule`, `draft`
   o `now`) y visibilidad, y convertirá tu hora local a UTC antes de agendar.

### Uso directo del script (`scripts/minio_publisher.py`)

```powershell
# Listar carpetas de proyecto existentes en minio/postiz
uv run ./scripts/minio_publisher.py list-projects

# Validar y previsualizar una carpeta completa de Reels (sin subir aún)
uv run ./scripts/minio_publisher.py prepare `
  --video-dir "D:\Videos\Clips\Parte1\ES" `
  --project "gta-vi/reels-es"

# Subir lote de Reels con nombres normalizados personalizados y progreso JSON
uv run ./scripts/minio_publisher.py upload `
  --video "D:\Videos\Clips\Parte1\ES\Reel_01_ES.mp4" --name "reel_01_01_es.mp4" `
  --video "D:\Videos\Clips\Parte1\ES\Reel_02_ES.mp4" --name "reel_01_02_es.mp4" `
  --project "gta-vi/reels-es" `
  --progress-file "$env:TEMP\postiz_upload_progress.json"

# Ver en vivo la subida desde otra terminal PowerShell
uv run ./scripts/minio_publisher.py watch --progress-file "$env:TEMP\postiz_upload_progress.json"

# Verificar activos subidos manualmente en PowerShell (mc stat + HTTP HEAD)
uv run ./scripts/minio_publisher.py verify `
  --video-dir "D:\Videos\Clips\Parte1\ES" `
  --project "gta-vi/reels-es"

# Convertir hora local del sistema a UTC para Postiz
uv run ./scripts/minio_publisher.py to-utc --local-datetime "2026-11-22 06:00"

# Construir descripción HTML para Postiz desde archivos de texto
uv run ./scripts/minio_publisher.py build-description `
  --chapters "D:\Videos\chapters1.txt" `
  --credits "D:\Videos\drift_3y4_out\youtube_credits.txt"
```

### Pruebas unitarias

```powershell
uv run --no-project python -m unittest discover -s tests -v
```
