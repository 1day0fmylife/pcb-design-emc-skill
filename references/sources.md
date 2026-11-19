# Источники и применимость

Отправные открытые материалы; проверены как доступные источники 2026-11-19. Это не утверждённый нормативный профиль изделия.

- Analog Devices, [MT-031: Grounding Data Converters and Solving the Mystery of AGND and DGND](https://www.analog.com/media/en/training-seminars/tutorials/MT-031.pdf), Rev. A, 10/2008. Для анализа возвратных токов и mixed-signal grounding; рекомендации нужно сопоставлять с конкретным преобразователем и системой.
- Analog Devices, [MT-101: Decoupling Techniques](https://www.analog.com/media/en/training-seminars/tutorials/MT-101.pdf), Rev. 0, 03/2009. Для развязки и влияния паразитных параметров; не заменяет PDN guide современного SoC/FPGA.
- Texas Instruments, [SSZT090, Board Layout Considerations](https://www.ti.com/document-viewer/lit/html/SSZT090/GUID-D62264EC-5FF1-497C-9311-E3E043EE6B4F), March 2022. Для контуров переключения и switch node в buck-конвертерах; рекомендации не переносить автоматически на другую топологию.

- Texas Instruments, [SNLA387: Ethernet PHY PCB Design Layout Checklist](https://www.ti.com/lit/an/snla387/snla387.pdf), June 2021. Дополнение к datasheet для Ethernet PHY, magnetics, clock и размещения; не универсальный набор ограничений для всех PHY и SFP+.
- Analog Devices, [AN-1368: Ferrite Bead Demystified](https://www.analog.com/en/resources/app-notes/an-1368.html), разделы DC Bias Current, LC Resonance Effect и Damping Methods. Для оценки частотной зависимости, DC bias и резонанса фильтра; применимость проверять для выбранного феррита и нагрузки. Редакцию используемого PDF фиксировать отдельно.
- KiCad, [Command-Line Interface 9.0](https://docs.kicad.org/9.0/en/cli/cli.html), документация на основе 9.0.9, revision `152cd19e`, разделы PCB DRC и Schematic ERC. Только пример документированной версии; опции и поведение сверять с установленной версией.

При работе с реальной платой дополни реестр datasheet, errata, hardware design guide и reference layout выбранных ИС, IBIS/S-parameter моделями и данными изготовителя stackup. Для каждой использованной версии зафиксируй документ, ревизию/дату, раздел, URL, применимый корпус/вариант и ограничение доступа. Недоступные NDA-материалы обозначай TBD, не восстанавливай их по догадкам.

При выборе нормативов проверь у официального издателя действующую редакцию и применимость к рынку, изделию, портам и условиям эксплуатации. Название стандарта без согласованной программы не является критерием приёмки. Закрытые нормы ПЭМИН здесь не хранятся.
