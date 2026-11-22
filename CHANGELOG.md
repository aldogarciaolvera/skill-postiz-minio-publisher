# Changelog

Todos los cambios relevantes de este proyecto se documentaran en este archivo.

El formato esta basado en Keep a Changelog y el proyecto utiliza Semantic Versioning.

## [Unreleased]

### Added

- `SKILL.md` con el flujo operativo completo (validación local, selección raíz vs. subcarpeta por proyecto con confirmación previa, subida vía `mc.exe`, entrega directa sin re-descarga a Postiz y conversión de hora local a UTC).
- `references/postiz-mcp-tools.md` con los esquemas verificados del servidor MCP de Postiz.
- `scripts/minio_publisher.py` (`list-projects`, `prepare`, `upload`, `to-utc`) para validación de `.mp4`/`.mov`/`.mkv`, normalización de nombres, verificación `mc.exe stat` + `HEAD` HTTPS y conversión a UTC.
- `tests/test_minio_publisher.py` con pruebas unitarias de la CLI.
- `README.md` con estructura, requisitos, instalación global y ejemplos de uso.

## [0.0.1] - 2026-11-22

### Added

- Inicializacion del proyecto.
