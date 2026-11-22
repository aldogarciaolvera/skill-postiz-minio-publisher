# `postiz-minio-publisher`

Skill para **Antigravity / Gemini** que automatiza la validación, normalización y
subida de videos (`.mp4`, `.mov`, `.mkv`), miniaturas y archivos auxiliares de
proyecto hacia un bucket **MinIO S3** (`minio/postiz`) mediante `mc.exe`, para
luego consultar canales y programar o publicar en redes sociales mediante el
**servidor MCP de Postiz** sin re-descargar archivos pesados.

---

## Características principales

- **Organización en MinIO por proyecto o raíz:** al recibir la ruta del video,
  lista carpetas existentes en `minio/postiz/`, pregunta si subir a la raíz o a
  una subcarpeta por proyecto (creándola automáticamente si no existe) y exige
  confirmación explícita antes de iniciar la transferencia.
- **Normalización y validación local previa (`scripts/minio_publisher.py`):**
  - Acepta estrictamente videos `.mp4`, `.mov` y `.mkv`.
  - Normaliza nombres de carpeta y archivo (minúsculas, ASCII sin acentos ni
    espacios) para generar URLs S3 limpias.
  - Detecta si el objeto ya existe en MinIO (`mc.exe stat`) y extrae metadatos
    básicos con `ffprobe` cuando está disponible.
- **Activos adicionales del proyecto:** permite subir miniaturas (`.jpg`,
  `.jpeg`, `.png`, `.webp`) para vincularlas en Postiz, y archivos auxiliares
  (líneas del tiempo, subtítulos, licencias, etc.) para resguardarlos en la
  misma carpeta del proyecto en MinIO.
- **Cero duplicación en Postiz:** envía directamente la URL
  `https://s3.atomsystems.org/postiz/...` a `integrationSchedulePostTool` sin
  pasar por `uploadFromUrlTool` (ideal para archivos de varios GB).
- **Control de publicación:** pregunta siempre entre `schedule`, `draft` o
  `now`, convierte la hora local del sistema a **UTC** y publica solo el post
  principal (sin comentarios adicionales).

---

## Estructura del repositorio

```text
postiz-minio-publisher/
├── SKILL.md                        # Instrucciones principales de la skill
├── README.md                       # Documentación, instalación y uso
├── VERSION                         # Versión actual (Semantic Versioning)
├── CHANGELOG.md                    # Historial de cambios
├── LICENSE
├── scripts/
│   └── minio_publisher.py          # CLI de validación, normalización, subida y UTC (PEP 723 + uv)
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
3. **`ffprobe`** opcional en `PATH` para inspeccionar duración, resolución y
   códecs del video antes de subirlo.
4. **Servidor MCP de Postiz** configurado en `~/.gemini/config/mcp_config.json`
   apuntando a tu instancia (`https://postiz.atomsystems.org`), idealmente con
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
indicando la ruta del video:

> "Usa `@postiz-minio-publisher` para subir `C:\Videos\Mi Video.mp4` con su
> miniatura `C:\Videos\portada.png` y programarlo en YouTube."

El agente ejecutará el flujo paso a paso:
1. Validará extensión (`.mp4`, `.mov`, `.mkv`) y archivos auxiliares.
2. Listará proyectos existentes en `minio/postiz/` y te preguntará si va en
   raíz o en una subcarpeta por proyecto.
3. Mostrará los nombres normalizados y pedirá confirmación antes de ejecutar
   `mc.exe cp`.
4. Consultará canales y horarios en Postiz, preguntará el modo (`schedule`,
   `draft` o `now`) y convertirá tu hora local a UTC antes de programar.

### Uso directo del script (`scripts/minio_publisher.py`)

```powershell
# Listar carpetas de proyecto existentes en minio/postiz
uv run ./scripts/minio_publisher.py list-projects

# Validar y previsualizar rutas normalizadas sin subir aún
uv run ./scripts/minio_publisher.py prepare `
  --video "C:\Ruta\Mi Video.mp4" `
  --thumbnail "C:\Ruta\Portada.png" `
  --extra "C:\Ruta\Licencia.txt" `
  --project "Mi Proyecto"

# Subir archivos confirmados y verificar acceso HTTPS
uv run ./scripts/minio_publisher.py upload `
  --video "C:\Ruta\Mi Video.mp4" `
  --thumbnail "C:\Ruta\Portada.png" `
  --extra "C:\Ruta\Licencia.txt" `
  --project "Mi Proyecto"

# Convertir hora local del sistema a UTC para Postiz
uv run ./scripts/minio_publisher.py to-utc --local-datetime "2026-11-22 18:30"
```

### Pruebas unitarias

```powershell
uv run --no-project python -m unittest discover -s tests -v
```
