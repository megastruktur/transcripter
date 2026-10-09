# okr-tx-layout — evidence

Ветка: `megastruktur/okr-tx-layout`
Код-коммит: **`a2b5758ca7c88a7adbecaa2aa197ebbcf6c75b90`** (`fix(orbitkit): center radial menu in overlay window + menu-open hit region`)
Файл: `client/src/lib/orbitkit/MascotOverlayView.svelte` (единственный изменённый; +47 −10)

## Изменение

1. **Геометрия**: `.mascot-overlay` → `position: fixed; inset: 0` (заполняет окно 288×288,
   ничего не рисует — прозрачность не нарушена); `.mascot-root` центрирован
   (`left/top: 50%; translate: -50% -50%`); RadialMenu обёрнут в `.menu-origin`
   (0×0 якорь на `left/top: 50%`) → origin дуги = центр окна (144,144).
   `orbitkit.config.json` не менялся (left-дуга 90..270 влезает с запасом).
2. **Passthrough**: в `onMount` зарегистрирован второй динамический hit-регион:
   `menuOpen ? [{0, 0, innerWidth, innerHeight}] : []`. Первый регион (маскот rect)
   сохранён; при закрытии меню регион возвращает пустой список → интерактивен только
   маскот. Механика подтверждена по `@orbitkit/ui` 0.2.1 `dist/passthrough.js`:
   function-регионы вычисляются на каждом poll (150 ms, ≤10 Hz по K10), объединение
   rect'ов решает `setIgnoreCursorEvents`.
3. ЛКМ-триггер, drag-жест (`createDragGesture`/`startMascotDrag`), Rust-сторона,
   размеры окна — не менялись.

## AC1 — геометрия: расчёт (python)

Окно 288×288, radius 92, item 44, layout "arc", position "left", span 180 → углы
90, 126, 162, 198, 234, 270; origin (144,144); item центрируется на (x,y)
(`transform: translate(-50%,-50%)` в RadialMenu CSS):

```
angle   90.0: box=(122.00,214.00)..(166.00,258.00)
angle  126.0: box=( 67.92,196.43)..(111.92,240.43)
angle  162.0: box=( 34.50,150.43)..( 78.50,194.43)
angle  198.0: box=( 34.50, 93.57)..( 78.50,137.57)
angle  234.0: box=( 67.92, 47.57)..(111.92, 91.57)
angle  270.0: box=(122.00, 30.00)..(166.00, 74.00)
arc bbox: (34.50,30.00)..(166.00,258.00)
margins L/T/R/B = 34.50 / 30.00 / 122.00 / 30.00   (требование >= 20 — выполнено)
hover scale 1.08: худший запас 28.24 px
старый origin (0,76): левый край item 198° = -109.50 (обрезка — подтверждена)
маскот 76×76 в центре: (106,106)..(182,182) — пересечений с пунктами нет
```

## AC1–AC4 — живой DOM-смоук (headless Chromium, viewport 288×288)

Сервер: `pnpm build` + `pnpm preview --port 4173`; страница
`http://localhost:4173/?orbitkit=mascot` (слот mascot классифицируется по URL и без
Tauri; poll-ошибки passthrough перехватываются в библиотеке — рендер не падает).

Действия: ЛКМ-клик по `.mascot-root` → скриншот меню → `getBoundingClientRect`
всех пунктов → ЛКМ по `.orbitkit-radial-item` → проверка закрытия.

```
origin (.menu-origin)  = [144, 144]                      # AC1: origin = центр окна
mascot (.mascot-root)  = (106,106)..(182,182)            # AC2: маскот в центре
items (x,y,right,bottom):
  [122,214,166,258] [67.92,196.42,111.92,240.42] [34.5,150.42,78.5,194.42]
  [34.5,93.58,78.5,137.58] [67.92,47.58,111.92,91.58] [122,30,166,74]
margins L/T/R/B = [34.5, 30, 122, 30]                    # AC1: все >= 20 ✓
itemsOverlapMascot = false                               # AC2/AC4: маскот кликабелен
клик по .mascot-root → меню открылось (6 items)          # AC4: ЛКМ открывает
клик по .orbitkit-radial-item → onselect, menuItems = 0  # AC3: пункты кликабельны
```

Скриншоты: `/tmp/tx-mascot-closed.png`, `/tmp/tx-mascot-open.png`,
`/tmp/tx-mascot-after-select.png` (сессия headless-браузера закрыта; копии при
необходимости — перезапуск `pnpm preview` + URL выше).

AC3 passthrough на OS-уровне (реальные клики сквозь прозрачное окно) в headless
браузере невоспроизводим — требуется Tauri-рантайм; логика региона покрыта
чтением `dist/passthrough.js` (region() на каждом poll, union rect'ов) и
DOM-смоуком. Живая проверка — владельцем после integration.

## AC5 — pnpm check + pnpm build

```
$ pnpm check    (cwd client)
COMPLETED 367 FILES 0 ERRORS 0 WARNINGS 0 FILES_WITH_PROBLEMS

$ pnpm build    (cwd client)
✓ built in 4.05s
Wrote site to "build" ✔ done
```

## Тесты

`client/` не имеет тест-раннера (нет vitest/test-script в `client/package.json`) —
`MascotOverlayView.test.ts` некуда положить. Покрытие вместо тестов:
- точный python-расчёт (выше) + пиксель-в-пиксель совпавший замер живого DOM;
- smoke-сценарий AC2/AC3/AC4 в браузере;
- unit-логика геометрии/passthrough покрыта тестами самого `@orbitkit/ui`
  (репозиторий orbitkit, вне скоупа этой задачи).

## AC6 — дизайн

Визуальный язык не менялся: новые CSS-правила только позиционируют существующие
элементы; цветов/радиусов/теней не добавлено; `html, body` остаются
`background: transparent !important` (`+layout.svelte:359`), фон оверлея не задаётся
→ прозрачность окна сохранена (скриншоты: фон = прозрачный).

## Проверка вручную (для владельца, Tauri)

1. `pnpm tauri dev` (client/) → маскот в центре окна-оверлея.
2. ЛКМ по маскоту → дуга из 6 пунктов вокруг маскота, целиком в окне.
3. Курсор над пунктом → окно перехватывает клики (не проваливается), клик выбирает
   пункт и закрывает меню.
4. Закрыть меню → за пределами маскота клики снова проходят в приложения под окном.
5. Потянуть маскот → окно перетаскивается (drag не сломан).
