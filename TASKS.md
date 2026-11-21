# Backlog — PCB Engineering Rules

## P0: модель проверки и safety

- [x] Сохранить имеющийся skill и добавить реестр инженерных правил с уникальными PCB-* ID.
- [x] Зафиксировать категории геометрии, GND, питания, трассировки, компонентов, реле, EMC, механики, DFM и тестируемости.
- [x] Добавить draft профили 2/4 слоя, relay, industrial-24V, isolated и FPGA/10G.
- [x] Формализовать PASS/FAIL/REVIEW_REQUIRED/NOT_EVALUATED/N_A и независимый sign-off.
- [ ] Утвердить список обязательных нормативов под конкретные типы изделий и редакции.
- [ ] Добавить реальный project-specific electrical safety constraint matrix, с traceable source.
- [ ] Определить алгоритм оценки creepage, включая отверстия/пазы/материал и применимый product standard.

## P1: машинная валидация

- [ ] Реализовать schema validator YAML/JSON с unit tests, выявлением duplicate rule IDs и broken references.
- [ ] Добавить source-of-truth metadata: ревизия стандарта/datasheet/фабрики, owner и дату подтверждения порогов.
- [ ] Реализовать applicability predicates для класса напряжения, интерфейса и конструкции, без фиктивных чисел.
- [ ] Добавить typed waivers (owner, reason, expiry, revision), запрещая waivers для обязательной electrical safety.
- [ ] Реализовать summary Markdown+JSON+SARIF; отличать confidence от priority.
- [ ] Тестировать фрагменты нормативных утверждений и обязательных ограничений безопасности.

## P2: KiCad adapter

- [ ] Определить поддерживаемые версии KiCad и CLI/pcbnew API матрицу.
- [ ] Реализовать read-only inventory schematic/netlist/PCB/stackup.
- [ ] Реализовать запуск ERC/DRC с исключениями и анализом exit code.
- [ ] Реализовать copper-edge, trace/space, NPTH/PTH, mount keepout и 3D geometry проверки.
- [ ] Добавить блокировки против несанкционированной правки CAD агентом.

## P3: EMI / SI / PI

- [ ] Прототип rule engine для реле: тип катушки, suppression, contact load и isolation.
- [ ] Проверять decoupling loop, return plane, DC/DC switching loops и sensitive net vicinity.
- [ ] Ввести полную линию source → coupling path → victim, с альтернативными гипотезами.
- [ ] Ввести FPGA/SFP+/Ethernet differential constraints по конкретному datasheet.
- [ ] Добавить fixture платы: correct/incorrect relays, power, mounting holes, SI/ESD.
- [ ] Подтверждать эффективность гипотез measured data; без false EMC compliance.

## P4: выпуск

- [ ] Добавить GitHub Actions (проверка схем/тестов, без доступа к конфиденциальным CAD).
- [ ] Определить license и release/versioning policy.
- [ ] Провести профильный hardware/EMC/safety review и опубликовать limitations.
