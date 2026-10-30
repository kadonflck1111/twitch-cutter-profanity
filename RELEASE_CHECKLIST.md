# Чек-лист публикации

## Перед отправкой репозитория

- [ ] `python tools/check_release.py --source-only --tag v0.1.0`
- [ ] `python -X utf8 -m unittest discover -s tests -v`
- [ ] `node --check design/app.js`
- [ ] В `git status` нет сборок, архивов, моделей, медиа и ярлыков.
- [ ] В истории Git нет токенов, приватных ключей и пользовательских файлов.

## Настройки GitHub

- [ ] Основная ветка называется `main`.
- [ ] Включены Issues и Private vulnerability reporting.
- [ ] Для `main` включена защита ветки с обязательным успешным workflow `CI`.
- [ ] GitHub Actions разрешено создавать Releases с `contents: write`.
- [ ] Добавлены описание и темы репозитория: `whisper`, `ffmpeg`, `twitch`,
      `video-editing`, `profanity-filter`, `windows`.

## Релиз

- [ ] Тег совпадает с `app_meta.py`: `v0.1.0`.
- [ ] Workflow `Build Windows NVIDIA release` завершился успешно.
- [ ] Размер ZIP меньше 2 ГиБ.
- [ ] SHA-256 из `.sha256` совпадает со скачанным архивом.
- [ ] Архив проверен после распаковки в новую папку на чистой Windows-машине.
- [ ] Проверены запуск без NVIDIA, запуск с NVIDIA и первая загрузка модели.
- [ ] В архиве есть `LICENSE`, `THIRD_PARTY_NOTICES.md` и лицензии FFmpeg/NVIDIA.
- [ ] Черновик GitHub Release содержит понятные заметки и только после проверки
      переведён в состояние Published.

## После публикации

- [ ] Ссылка Releases в README открывает опубликованный архив.
- [ ] Создан тестовый issue через форму ошибки.
- [ ] Включены Dependabot alerts и security updates.
