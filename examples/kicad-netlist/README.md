# Сверка нетлиста на демонстрационных проектах KiCad

Прогон выполнен с KiCad 10.0.8 и модулем `pcbnew` той же установки. Для контрольной пары использован `pic_programmer` из комплекта демонстрационных проектов KiCad. [Демонстрационные проекты KiCad](https://github.com/KiCad/kicad-source-mirror/tree/10.0.8/demos)

Команды выполняются из корня этого репозитория. При необходимости замените `demo_dir` путём к локальной копии примера.

```sh
demo_dir=/usr/share/kicad/demos/pic_programmer
netlist_dir=$(mktemp -d)
kicad-cli sch export netlist --format kicadxml --output "$netlist_dir/pic.xml" \
  "$demo_dir/pic_programmer.kicad_sch"
python3 scripts/pcb_profile.py compare-netlist --board "$demo_dir/pic_programmer.kicad_pcb" \
  --netlist "$netlist_dir/pic.xml" --design-revision KiCad-10.0.8-demo \
  --output "$netlist_dir/parity.json"
```

В контрольном прогоне сопоставились 63 компонента и 236 выводов, статус сверки — `PASS`. Для проверки расхождения в отдельной копии экспортированного XML перенесён узел `P2.1` из `/VPP/MCLR` в `GND`. Эта копия дала один `net_mismatch` со статусом `FAIL`, сохранила UUID площадки и обе сети. Исходные файлы схемы и платы сохранили контрольные суммы. При пропуске `--design-revision` отчёт получает `REVIEW_REQUIRED`.

На той же версии проверены `complex_hierarchy` с 68 компонентами и 165 выводами и `multichannel_mixer` со 114 компонентами и 280 выводами. Обе пары получили `PASS` в охвате сравнения назначений сетей.

## Назначения посадочных мест

```sh
python3 scripts/pcb_profile.py compare-netlist --board "$demo_dir/pic_programmer.kicad_pcb" \
  --netlist "$netlist_dir/pic.xml" --design-revision KiCad-10.0.8-demo \
  --check-footprints --output "$netlist_dir/footprints.json"
```

Для `pic_programmer` совпали 63 полных идентификатора посадочных мест. Когда в отдельной копии XML заменено назначение `C1`, отчёт содержит один `footprint_assignment_mismatch` и итоговый `FAIL`. Число сверенных выводов осталось 236. [Критерий и охват](../../references/rules-engine.md#сверка-назначений-посадочных-мест)
