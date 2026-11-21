# План развития pcb-design-review

## Цель

Превратить существующую инженерную методику в проверяемый переносимый skill с машинным каталогом, профильными ограничениями, KiCad adapter и отчётами; сохранять human sign-off для safety/EMC.

## Этапы

1. **v0.2 / правила**: формализовать полный rule registry, согласовать критичность, описать applicability и отсутствие данных, проверить ссылки и структуру. Текущий набор — draft, ещё не нормативно утверждённый.
2. **v0.3 / схема**: источник требований, численные thresholds только из project/fab/datasheet, JSON Schema + unit tests, исключения/waivers с владельцем и обоснованием.
3. **v0.4 / KiCad read-only**: извлекать board nets, tracks, zones, stackup и механические объекты, запускать native ERC/DRC и объединять reports. Ни один automatic parser не заменяет проверки путей возврата.
4. **v0.5 / power/EMC**: модель связности coil-contact-driver, DC/DC loops, TVS placement, GND continuity, список гипотез и A/B проверок. Требуется измерительная верификация.
5. **v0.6 / high speed**: FPGA/SFP+/10G, модели канала и constraints по datasheet, summary по SI и границам моделирования.
6. **v1.0 / quality gate**: CI tests, fixture boards с намеренными нарушениями и expected findings, воспроизводимые версии, отчёты JSON/Markdown/SARIF, sign-off и changelog.

## Риски

- Ложное ощущение подтверждённого EMC или electrical safety из чистого DRC.
- Копирование произвольных численных параметров на другой stackup/фабрику.
- Несовместимость pcbnew API и версий KiCad.
- Неправильное толкование AI геометрии без схемы/PCB/контекстных моделей.
- Загрузка закрытых спецификаций в публичный GitHub.
