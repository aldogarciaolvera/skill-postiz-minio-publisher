# Referencia verificada: herramientas MCP de Postiz

Resumen de las herramientas disponibles en el servidor MCP `postiz` y cómo se
usan dentro del flujo `postiz-minio-publisher`.

> **Regla clave de almacenamiento:** cuando `Postiz` está configurado con
> `STORAGE_PROVIDER="cloudflare"` y
> `CLOUDFLARE_BUCKET_URL="https://s3.atomsystems.org/postiz"`, las URLs
> `https://s3.atomsystems.org/postiz/...` pasan directamente la validación de
> dominio en `integrationSchedulePostTool`. **No** llames a `uploadFromUrlTool`
> para archivos ya subidos a `minio/postiz`, así evitas duplicar o re-descargar
> videos pesados (ej. 6 GB).

## 1. Canales y grupos

* **`groupList`**
  * Sin parámetros requeridos. Lista los grupos/clientes disponibles para
    filtrar canales.
* **`integrationList`**
  * Parámetros: `group` (opcional, ID obtenido de `groupList`).
  * Devuelve las integraciones/canales conectados con su `integrationId` y
    `platform`.
* **`integrationSchema`**
  * Requeridos: `platform` (`x`, `linkedin`, `linkedin-page`, `reddit`,
    `instagram`, `instagram-standalone`, `facebook`, `threads`, `youtube`,
    `gmb`, `tiktok`, `tiktok-business`, `pinterest`, `dribbble`, `discord`,
    `slack`, `kick`, `twitch`, `mastodon`, `bluesky`, `lemmy`, `wrapcast`,
    `telegram`, `nostr`, `vk`, `medium`, `devto`, `hashnode`, `wordpress`,
    `listmonk`, `moltbook`, `whop`, `skool`, `mewe`, `tumblr`) y `isPremium`
    (`boolean`).
  * Devuelve el esquema de `settings` de la plataforma y, cuando aplica,
    funciones auxiliares (`tools`) invocables mediante `triggerTool`.
* **`triggerTool`**
  * Requeridos: `integrationId`, `methodName` (de `integrationSchema`),
    `dataSchema` (arreglo de `{ key, value }`).
  * Sirve para obtener IDs reales requeridos por `settings` (categorías,
    tableros, playlists, menciones, etc.).

## 2. Gestión de medios

* **`mediaListTool`**
  * Opcionales: `page` (entero `>= 1`), `search` (filtro por nombre original).
  * Lista archivos registrados en la tabla interna de medios de Postiz. *(Nota:
    los archivos subidos directamente vía `mc.exe cp` a `minio/postiz` no se
    indexan automáticamente aquí; usa `scripts/minio_publisher.py prepare` o
    `mc.exe stat` para verificar existencia en MinIO).*
* **`uploadFromUrlTool`** *(solo excepción / enlaces externos)*
  * Requerido: `url` (URL pública `http(s)`). Opcional: `adjust` (`boolean`).
  * Usar únicamente si el usuario pide explícitamente re-codificar/escalar un
    video mediante Postiz (`adjust: true`) o si el archivo proviene de una URL
    externa ajena a `s3.atomsystems.org/postiz`.
* **`mediaStatusTool`**
  * Requerido: `id`. Solo necesario si se usó `uploadFromUrlTool` con
    `adjust: true`, esperando hasta que `status` sea `"ready"`.

## 3. Consulta, programación y mantenimiento de posts

* **`postsListTool`**
  * Requeridos: `startDate` y `endDate` en **UTC** (ej. `2026-11-22T00:00:00`).
  * Opcional: `customer` (ID de grupo).
  * Lista publicaciones existentes (`scheduled`, `draft`, `published`, `errored`).
* **`integrationSchedulePostTool`**
  * Requerido: `socialPost` (arreglo de publicaciones). Cada elemento requiere:
    * `integrationId` (ID de la integración, no el ID interno).
    * `isPremium` (`boolean`, relevante sobre todo para X).
    * `date` (fecha/hora en **UTC**; si `type` es `"now"`, pasar la fecha
      actual en UTC).
    * `shortLink` (`boolean`).
    * `type`: `"draft" | "schedule" | "now"`.
    * `postsAndComments`: en esta skill, pasar **siempre 1 único elemento**
      (solo el post principal, sin comentarios). Requiere:
      * `content`: HTML válido donde cada línea va envuelta en `<p>`. Etiquetas
        permitidas: `h1`, `h2`, `h3`, `u`, `strong`, `li`, `ul`, `p` (sin
        mezclar `u` y `strong`), además de `img`/`span` solo si las reglas de la
        integración lo exigen.
      * `attachments`: arreglo de URLs (`string`) o de objetos
        `{ path, alt?, thumbnail?, thumbnailTimestamp? }`. Solo admite medios
        compatibles (video y portada/miniatura `.jpg`/`.png`/`.webp`); nunca
        adjuntar archivos de licencia o líneas del tiempo aquí.
    * `settings`: arreglo de `{ key, value }` acorde a `integrationSchema`
      (pasar siempre el `id` cuando el esquema lo pida, no la etiqueta visible).
      *(En YouTube, `title` y `type` (`public`/`private`/`unlisted`) son
      obligatorios; además el esquema expone `thumbnail` opcional).*
  * Respuesta: `previewUrl` por elemento en éxito, u `output.errors` si falla la
    validación.
* **`postSettingsTool`**
  * Requeridos: `id` y `settings`. Actualiza solo `settings` en posts aún no
    publicados (`scheduled` o `draft`).
* **`deletePostTool`**
  * Requerido: `id`. Elimina de forma irreversible un post (si estaba programado
    evita su publicación; si ya estaba publicado solo se elimina de Postiz).
