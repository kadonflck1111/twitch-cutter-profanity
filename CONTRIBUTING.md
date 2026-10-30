# Участие в разработке

Спасибо за интерес к Twitch Cutter Profanity.

## Локальная настройка

Требуются Windows 10/11 x64, Python 3.10, FFmpeg и ffprobe в `PATH`.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python web_app.py
```

## Проверки перед pull request

```powershell
python -X utf8 -m unittest discover -s tests -v
python tools/check_release.py --source-only
node --check design/app.js
```

Добавляйте регрессионный тест для исправлений логики. Не коммитьте модели
Whisper, медиафайлы пользователей, папки сборки, архивы релизов или локальные
ярлыки. Изменения интерфейса желательно сопровождать снимком экрана.

## Pull request

Опишите проблему, принятое решение и выполненные проверки. Один pull request
должен решать одну связанную задачу. Отдельно укажите изменения формата
выходных файлов, зависимостей или лицензионных условий.
