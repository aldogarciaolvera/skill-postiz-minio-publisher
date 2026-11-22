# Changelog

Todos los cambios relevantes de este proyecto se documentaran en este archivo.

El formato esta basado en Keep a Changelog y el proyecto utiliza Semantic Versioning.

## [Unreleased]

### Added

- `SKILL.md` enfocada exclusivamente en Reels/Shorts verticales (`.mp4`/`.mov`/`.mkv`, `≤ 180 s`, `≤ 500 MiB`, `height >= width`), con soporte de carpeta/lotes, selección raíz vs. subcarpeta con confirmación previa, dos modos visibles en PowerShell (ejecución directa + `verify`, o subida del agente + `watch` en vivo), entrega directa sin re-descarga a Postiz y conversión local → UTC.
- `references/postiz-mcp-tools.md` con los esquemas verificados del servidor MCP de Postiz.
- `scripts/minio_publisher.py` (`list-projects`, `prepare`, `upload`, `verify`, `watch`, `to-utc`, `build-description`) con validación estricta solo-Reels (`--max-duration`, `--max-size-mib`, `--allow-landscape`), soporte `--video-dir`, `--video` repetible + `--name`, seguimiento por lote en `--progress-file`, monitor interactivo `watch` para PowerShell, verificación `verify` (`mc.exe stat` + `HEAD` HTTPS) y generador de descripciones HTML sin enlaces completos.
- `tests/test_minio_publisher.py` con pruebas unitarias de la CLI.
- `README.md` con estructura, requisitos, instalación global y ejemplos de uso.

## [0.0.1] - 2026-11-22

### Added

- Inicializacion del proyecto.
