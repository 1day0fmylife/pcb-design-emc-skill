# Проверка импорта на QA-плате KiCad

Для прогона использована плата `multinetclasses_drc` из набора QA KiCad, тег `10.0.8`. Плата, файл проекта и правила загружаются во временный каталог. Профиль ссылается на верхнюю границу из `rule_3` этого тестового проекта. [Исходная плата](https://github.com/KiCad/kicad-source-mirror/blob/10.0.8/qa/data/pcbnew/multinetclasses_drc.kicad_pcb) · [Исходные правила](https://github.com/KiCad/kicad-source-mirror/blob/10.0.8/qa/data/pcbnew/multinetclasses_drc.kicad_dru)

## Воспроизведение

Команды выполняются из корня репозитория. Прогон проверен с `kicad-cli` 10.0.8.

```sh
qa_dir=$(mktemp -d)
qa_base=https://raw.githubusercontent.com/KiCad/kicad-source-mirror/10.0.8/qa/data/pcbnew
for extension in pcb pro dru; do
  file="multinetclasses_drc.kicad_$extension"
  curl -fsSL "$qa_base/$file" -o "$qa_dir/$file" || exit 1
done

(
  cd "$qa_dir" || exit 1
  LC_ALL=C XDG_CONFIG_HOME="$qa_dir/config" XDG_CACHE_HOME="$qa_dir/cache" \
    kicad-cli pcb drc --format json --units mm --severity-all \
    --output drc.json multinetclasses_drc.kicad_pcb
)

python3 scripts/pcb_profile.py import-drc examples/kicad-multinetclasses/profile.yaml \
  "$qa_dir/drc.json" --context examples/kicad-multinetclasses/context.yaml \
  --output "$qa_dir/drc-import.json"
```

При наличии модуля `pcbnew` добавьте `--board "$qa_dir/multinetclasses_drc.kicad_pcb"`, чтобы сверить SHA-256 и извлечь поля объектов по UUID.

## Результат

KiCad выдал две ошибки `track_width` на `F.Cu`. Ширина сегментов `Net-(R1-Pad2)` и `Net-(R3-Pad1)` составила соответственно 1,016 и 0,508 мм при верхней границе 0,127 мм в `rule_3`. Прогон в отдельной конфигурации также выдал четыре предупреждения об отсутствии библиотеки `Resistor_SMD` в таблице библиотек. Повтор с `--exit-code-violations` вернул код 5 и те же замечания. Первый прогон вернул 0. Контрольные суммы PCB, PRO и DRU совпали до и после обоих прогонов.

Импорт сохранил шесть замечаний. Для обеих ошибок он указал правило `PCB-DFM-001` и кандидат `RULE-3-MAX`. После сверки UUID, ширины сегментов и `rule_3` численные результаты `FAIL` прошли `validate-report` в охвате этого критерия.

В режиме `--board` сопоставились все шесть объектов DRC. Для двух дорожек извлечены сеть и слой, для четырёх посадочных мест — RefDes.

Перед использованием [файла контекста](context.yaml) с другим экспортом сверьте позиции `/violations/0` и `/violations/1` с UUID и верхней границей из правила. В `track_width_bounds` записывается подтверждённое направление `min` или `max` для каждой позиции. [Использованный профиль](profile.yaml)
