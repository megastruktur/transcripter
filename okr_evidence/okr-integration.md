# okr-integration — wiring right-click + rev-пины orbitkit

Дата: 2026-10-09 · Worktree: `okr-integration` (база `8c1bb9d` = squash okr-tx-layout) · ветка `megastruktur/okr-integration`

## Изменения

| Файл | Правка |
|---|---|
| `client/src/orbitkit.config.json` | `menu.trigger`: `"click"` → `"right-click"` |
| `client/package.json` | `"@orbitkit/ui": "github:megastruktur/orbitkit#9270ad18d131d5a974fb4d32bff99ee6b9463816&path:packages/orbitkit"` |
| `client/src-tauri/Cargo.toml` | `tauri-plugin-orbitkit = { git = "…", rev = "9270ad18d131d5a974fb4d32bff99ee6b9463816" }` (заменил `branch = "main"` — дрифт-контракт) |
| `client/src/lib/orbitkit/MascotOverlayView.svelte` | `createDragGesture({ openButton: "right", … })` — единственный вызов в client (grep). Подавление contextmenu покрыто библиотекой: хендлер `gesture.oncontextmenu` уже попадает в DOM через существующий spread `{...dragGesture}` на `.mascot-root` |
| `client/pnpm-lock.yaml`, `client/src-tauri/Cargo.lock` | следствие пинов: по одной замене resolved-источника (см. ниже) |
| `client/pnpm-workspace.yaml` | ключ `allowBuilds` для `@orbitkit/ui` обновлён: tarball-id старого ревизии `9306014` → репо-форма `"@orbitkit/ui@git+https://github.com/megastruktur/orbitkit.git": true` |

Геометрия/поведение okr-tx-layout не тронуты; прочие зависимости не обновлялись.

## Пин

- Полный SHA: `9270ad18d131d5a974fb4d32bff99ee6b9463816` = merge `--no-ff` кампании okr-trigger в orbitkit main (`9270ad1^1 = 9306014`, `9270ad1^2 = 8e85d3a`).

## Отклонения от буквы брифа (намерение сохранено)

1. **Прекомпозит был локальным**: `origin/main` на GitHub стоял в `9306014` — merge `9270ad1` не был запушен, из-за чего pnpm/cargo (и CI) физически не могли резолвить SHA. Выполнен fast-forward push (`9306014..9270ad1 main -> main`), после пуша `git ls-remote` отдаёт `refs/heads/main = 9270ad18…`.
2. **Синтаксис спецификатора**: бриф предписывал `#<sha>/path:packages/orbitkit` — pnpm 12.4.1 такую форму не резолвит (`Could not resolve … to a commit`). Рабочая форма pnpm для commit+subdir: `#<sha>&path:packages/orbitkit` (docs pnpm, release 11.7+). Зафиксировано в `package.json` именно она.
3. **pnpm-workspace.yaml**: pnpm 12 запрещает непод approved `prepare`-скрипты git-hosted пакетов (`ERR_PNPM_GIT_DEP_PREPARE_NOT_ALLOWED`); имя пакета ключом не одобряет git/tarball-зависимости (docs `allowBuilds`). Ключ заменён на репо-форму — одобряет любой коммит этого репо (и codeload-tarball `github:`-deps), переживает будущие rev-бампы. `packages`/`esbuild`-записи не тронуты.

## Гейты (pnpm 12.4.1, `COREPACK_ENABLE_DOWNLOAD_PROMPT=0 npx pnpm@12.4.1 …`)

| Гейт | Результат |
|---|---|
| `pnpm install --no-frozen-lockfile` (client/) | `Done in 5.4s`; git-hosted пакет подготовлен (`prepare` → devDeps + `svelte-package`, `Done in 3.6s`) |
| Резолюция ревизии | lockfile: `tarball: https://codeload.github.com/megastruktur/orbitkit/tar.gz/9270ad18d131d5a974fb4d32bff99ee6b9463816`, `integrity: sha512-e/ssYQ…`; `pnpm list @orbitkit/ui` → `@orbitkit/ui@0.2.1`; в `node_modules/@orbitkit/ui/dist/` присутствуют `openButton` (dragGesture.js) и литерал `"right-click"` (config.js/.d.ts) |
| `pnpm check` | `COMPLETED 367 FILES 0 ERRORS 0 WARNINGS 0 FILES_WITH_PROBLEMS` |
| `pnpm build` | vite build ✓, `Wrote site to "build"` (предупреждение про неиспользуемый `Renderer` из `marked` — ранее существовавшее) |
| `cargo check` (client/src-tauri) | `Finished dev profile` — green с rev-пином |
| `Cargo.lock` диф | 1 строка: `?branch=main#5cf41b0a…` → `?rev=9270ad18…#9270ad18…` |
| `pnpm-lock.yaml` диф | 5 строк: specifier + resolved tarball/integrity `9306014…` → `9270ad18…` |

## Приёмка

1. ✅ install проходит, резолюция ровно в `9270ad18` (см. таблицу).
2. ✅ `pnpm check` + `pnpm build` зелёные.
3. ✅ `cargo check` зелёный с rev-пином.
4. ✅ Rust-парсер принимает `"right-click"`: в запиненном дереве тест `test_menu_trigger_right_click_parses` (`crates/tauri-plugin-orbitkit/src/config.rs:702-726`: parse → `MenuTrigger::RightClick`, сериализация ровно `"right-click"`, `"click"`/`"hover"` не дрейфуют, неизвестная строка отвергается). Прогон самого теста на этом хосте невозможен (линковка tauri/gtk — известное ограничение хоста, `cargo check` — честный гейт); тест выполнялся в кампании okr-trigger (vitest 428/428 + cargo в orbitkit).
5. ✅ ЛКМ не тогглит, ПКМ тогглит — рантайм-проверка установленного артефакта (`node_modules/@orbitkit/ui/dist/dragGesture.js`, `openButton: "right"`): LMB click → 0 toggle; LMB drag (40px) → drag + click после drag заглушен; RMB press → `preventDefault` вызван + toggle; RMB drag → suppress без toggle. Итог счётчиков `{toggles:1, drags:2, prevented:true}`.
6. ✅ Дрифт-чек: grep по `client/` — `package.json` и `pnpm-lock.yaml` содержат только rev-форму; `Cargo.toml` — `rev = "9270ad18…"`, `branch` отсутствует; прочих orbitkit-ссылок нет.
7. Ограничение: живой смоук маскота требует Tauri-шелла (окно + passthrough), вне его `MascotOverlayView` не монтируется (`getCurrentWindow`). Живая проверка ЛКМ/ПКМ — за владельцем (как и stated в брифе).
