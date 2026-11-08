# Unreal MCP Project Export v1.5

Python-инструмент для инвентаризации Unreal Engine-проекта и выгрузки сведений об ассетах и Blueprint через локальный MCP-сервер редактора. Экспорт сохраняется в JSON и может продолжаться после прерывания. Консольный монитор показывает ход работы по журналу, а офлайн-скрипт собирает ошибки Blueprint в CSV.

Экспортёр читает файлы проекта и вызывает инструменты MCP; он не предназначен для изменения игровых ассетов. Результаты записываются в отдельный каталог --output. Работа с реальным сеансом Unreal Engine 5.8 в этой подготовленной копии не проверялась: локальные тесты используют заглушки MCP.

## Состав

| Файл | Назначение |
| --- | --- |
| gasp_project_export_v15.py | Инвентаризация файлов, реестр ассетов и экспорт Blueprint |
| gasp_export_status_v15.py | Запуск экспортёра с консольным монитором и журналом |
| gasp_blueprint_failures.py | Отчёт по ошибкам уже сохранённых Blueprint JSON |
| test_gasp_export_v15.py, test_v15_main_integration.py | Локальные тесты без Unreal |

## Требования

- Python 3.10+ (локальная проверка выполнена с Python 3.11.4).
- Пакет requests из requirements.txt (локальная проверка выполнена с 2.31.0).
- Для фаз registry и blueprints: открытый нужный Unreal-проект и доступный локальный MCP-сервер с инструментом call_tool для editor_toolset.toolsets.asset.AssetTools, editor_toolset.toolsets.blueprint.BlueprintTools, editor_toolset.toolsets.object.ObjectTools и editor_toolset.toolsets.programmatic.ProgrammaticToolset. Сервер и плагин Unreal в этот репозиторий не входят; совместимость зависит от их реализации.
- Фаза inventory работает без MCP и использует только файлы проекта.

На Windows в PowerShell из каталога репозитория:

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
~~~

Активация окружения не требуется, если вы вызываете его python.exe напрямую.

## Запуск

Укажите путь к папке с .uproject и отдельную папку для экспорта. Папка вывода должна находиться вне Unreal-проекта. Не добавляйте экспортированные данные в публичный репозиторий: они могут содержать Source, Config, дескрипторы плагинов и локальные пути.

~~~powershell
$project = 'C:\Path\To\UnrealProject'
$output = 'C:\Path\To\ExportOutput'
~~~

Проверка файловой инвентаризации без MCP:

~~~powershell
.\.venv\Scripts\python.exe .\gasp_project_export_v15.py --project $project --output $output --phase inventory
~~~

Полный экспорт через MCP по умолчанию http://127.0.0.1:8000/mcp:

~~~powershell
.\.venv\Scripts\python.exe .\gasp_export_status_v15.py --project $project --output $output --phase all
~~~

Для другого адреса сервера задайте --url. Монитор создаёт GASP_export_*.log рядом со скриптами, если не указан --log-file. Чтобы сохранить журнал в папке экспорта, добавьте --log-file (Join-Path $output 'export.log').

Повторить только ранее неудачные, частичные или прерванные Blueprint из существующей папки экспорта:

~~~powershell
.\.venv\Scripts\python.exe .\gasp_export_status_v15.py --project $project --output $output --phase blueprints --only-failed-blueprints --properties --dsl
~~~

Этот режим выбирает записи failed, partial или in_progress из blueprints/*.json, сохраняет исходные причины в blueprint_failures_before_v15.csv и не перезаписывает успешные Blueprint. Он всё равно опрашивает MCP для обнаружения ассетов. Используйте ту же папку --output, что и в предыдущем экспорте.

Отдельно составить CSV по уже сохранённым ошибкам без Unreal и MCP:

~~~powershell
.\.venv\Scripts\python.exe .\gasp_blueprint_failures.py --output $output
~~~

Дополнительные параметры доступны через --help у экспортёра и монитора. --include-plugins добавляет контент плагинов в реестр; --engine-root помогает сопоставлять ассеты плагинов движка с файлами для проверки SHA-256. --zip создаёт архив рядом с папкой вывода.

## Проверка без Unreal

~~~powershell
.\.venv\Scripts\python.exe -m unittest discover -v
~~~

Тесты используют временные проекты и заглушки MCP. Они проверяют восстановление графов и сохранность успешных записей, но не подтверждают совместимость с конкретным экземпляром редактора или MCP-сервера.

## Ограничения

- Совпадение путей /Game с файлами на диске служит эвристикой соответствия проекта; оно не доказывает, какой .uproject открыт в редакторе.
- Статус complete относится к запрошенным полям и не означает полную схему ассета Unreal.
- Внешние OFPA-пакеты каталогизируются отдельно; некоторые типы ассетов и операции BlueprintTools могут остаться частичными или неудачными.
- В проекте нет встроенной проверки PIE/runtime. Для больших AnimBP устойчивый режим может работать медленно.
- Публичная публикация результатов экспорта требует отдельной проверки данных; .gitignore защищает только новые неотслеживаемые файлы в этом репозитории.

## Происхождение версии

Версия 1.5 продолжает прежние результаты экспорта и ориентирована на адресное восстановление проблемных Blueprint. Исторические числа из конкретного проекта не являются ограничениями инструмента. Python-файлы в этой подготовленной копии сохранены без изменения логики.
