# CAFF Tool

[badge-en]: https://img.shields.io/badge/lang-English%20%F0%9F%87%AC%F0%9F%87%A7-white
[badge-ru]: https://img.shields.io/badge/%D1%8F%D0%B7%D1%8B%D0%BA-%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9%20%F0%9F%87%B7%F0%9F%87%BA-white
[vot-readme-en]: README.md
[vot-readme-ru]: README_RU.md
[![en][badge-en]][vot-readme-en]
[![ru][badge-ru]][vot-readme-ru]

Редактор текстовых банков CAFF (`.bin`) для **Conker: Live & Reloaded** (Xbox, 2005):
распаковка в редактируемый TOML и обратная сборка с поддержкой строк **любой длины**.

> Основано на разборе формата из проекта **[conker-live-and-reloaded-decensor](https://github.com/nastys/conker-live-and-reloaded-decensor)**.
> В оригинальном репозитории — набор инструментов (decensor и др.), здесь же собран
> и доработан только `caff_tool`: точный структурный разбор, корректная привязка
> ключей, группировка UI-раскладки и rebuild для удлинённых строк.

## Возможности

- **Точная распаковка**: строки и ключи извлекаются структурно (таблица ключей +
  таблица смещений LSBL), не по эвристикам. Round-trip проверен на всех 467 `.bin`
  игры (EN/FR/DE/ES/IT/JA) — byte-for-byte.
- **Корректная привязка ключей**: ключ связан со строкой через номер строки
  исходного скрипта (`X.high16` ключа = `line` записи LSBL). Надписи UI больше
  не съезжают на чужие ключи.
- **UI-раскладка в одной записи**: `KEY|KEY_POSITION_X|KEY_POSITION_Y|KEY_SCALE`:

  ```toml
  [[entries]]
  key = "GAME_1|GAME_1_POSITION_X|GAME_1_POSITION_Y|GAME_1_SCALE"
  position_x = "0"
  position_y = "0"
  scale = "0"
  text = "Game 1"
  ```

- **UTF-8 без \u-escape**: тексты на любом языке читаются прямо в TOML.
- **Изменение длины строк**: равной длины патчатся байт-в-байт; любое изменение
  длины (короче или длиннее) запускает полную пересборку CAFF — таблица смещений LSBL №1, размерные поля (`0x34`,
  `0x50`, `0x64`, `0x194`, `B`/`C`) и контрольная сумма заголовка (`0x14`)
  пересчитываются автоматически.
- Поля `offset`/`max_bytes` в TOML не нужны — сборка резолвит строки по именам
  ключей (старые TOML с этими полями поддерживаются).

## Требования

Python **3.11+** (используется стандартный `tomllib`). Зависимостей нет.

## Использование

> ⚠️ В Windows для запуска можно использовать `python` или `py`, в зависимости от установленной версии Python.

> ⚠️ В Linux вместо `python` может потребоваться использовать `python3`.

```bat
:: .bin -> .toml (+ встроенная проверка lossless round-trip)
python caff_tool.py unpack input.bin [output.toml]

:: .toml -> .bin
python caff_tool.py pack input.toml [output.bin]

:: патчинг живого .bin (перезаписывает его!)
python caff_tool.py pack input.toml [target.bin] [output.bin] --safe
```

Пакетно по папке (рекурсивно все `default.bin` ↔ `default.toml`):
- Windows: `caff_unpack.bat <папка>` / `caff_pack.bat <папка>`
- Linux/macOS: `bash caff_unpack.sh <папка>` / `bash caff_pack.sh <папка>`

## Формат TOML

```toml
[meta]
build_path = 'D:\Database\UberRoot\aid\text\English\UIFrontend\XBOX_v1\default.bin'

[script]
audio_meta = "; ..."

[[entries]]
key = "CHAPTERS|CHAPTERS_POSITION_X|CHAPTERS_POSITION_Y|CHAPTERS_SCALE"
position_x = "0"
position_y = "0"
scale = "0"
text = "Chapters"

[binary]
template_hex = "43414646..."   # эталонный бинарник (fallback для pack)
```

Редактируются только значения `text` (и `position_*`, если нужно сдвинуть раскладку).
Составной `key` — единый идентификатор записи, разделитель `|`.

## Правила перевода

1. Не удалять управляющие теги движка: `{PMARKER}`, `{SWIRL}`, `{STAR}`,
   `{LIGHTNING}`, `{SKULL}` — они управляют паузами и иконками субтитров.
2. `position_x` / `position_y` / `scale` не переводить — это координаты/масштаб
   элементов меню (`"0"` = по умолчанию).
3. Ключи (`key`) не переименовывать: сборка находит строки по ним; опечатка
   выдаст ошибку `Key '...' not found in binary`.
4. Длина строки технически не ограничена, но время показа субтитра фиксировано
   (привязано к таймингам озвучки) и от длины текста не зависит: слишком длинная
   реплика не будет показана дольше, а просто не успеет прочитаться. Старайтесь
   укладываться в разумную длину оригинала.

## Структура CAFF (кратко)

```
0x000  заголовок CAFF (0x180 байт, контрольная сумма @ 0x14)
0x180  секция "text", магия 0x3E2EA091
0x1A8  count
0x1AC  таблица ключей: count × (key_off, X); X.high16 = номер строки скрипта
       ключи UTF-16LE
LSBL   "LSBL": таблица (u16 line, u32 wchar_offset)+ — смещения строк;
       line == X.high16 ключа связывает ключ со строкой
тексты UTF-16LE, NUL-терминированы
хвост  LSBL №2 (ASCII-ключи), отладочные комментарии, build_path
```

При удлинении текста сдвигается хвост файла — поэтому и нужен rebuild:
пересчёт LSBL №1, размеров секции и чексуммы.

---

Не аффилировано с Microsoft / Rare. Все торговые марки принадлежат их владельцам.
Инструмент предназначен для некоммерческого использования (переводы, моддинг,
исследование форматов).
