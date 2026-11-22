# PCB Design & EMC Review

[Русский](README.md) · **English**

A portable skill for **Codex and Claude Code** that helps review printed circuit boards, placement, routing, power delivery, SI/PI, parasitic coupling, EMI/EMC, and publicly documented measures for reducing compromising emanations.

The skill is named `pcb-design-review`. Its repository is `pcb-design-emc-skill`. The skill instructions, engineering references, and built-in rule descriptions are maintained in Russian.

## Capabilities

- Schematic and PCB review covers pin mapping, footprints, placement, return paths, stackup, constraints, and schematic-to-board consistency.
- Power review covers the power tree, sequencing, DC/DC converters, decoupling, PDN, transient response, back-powering, thermal conditions, and worst-case parameters.
- Interface review covers Ethernet, SFP/SerDes, DDR, clocks, FPGA I/O, and control buses.
- Interference analysis traces hypotheses from a source through a coupling path to a susceptible circuit, with A/B checks, alternative explanations, and verification of fixes.
- EMI/EMC review covers emissions, immunity, enclosure design, cables, filters, protection, and measurement planning.
- Compromising emanations call for publicly documented coupling reduction measures and a separate professional assessment.
- Verification covers ERC/DRC, engineering waivers, models, measurements, first power-up planning, and pending checks.

Base design constraints on the relevant component and project documentation. Record observations separately from hypotheses. Select values, clearances, and layer counts for the specific board.

## Repository structure

```text
pcb-design-emc-skill/
├── README.md
├── README.en.md
├── PROJECT_REVIEW.md
├── SKILL.md
├── references/
├── rules/
├── profiles/
├── schemas/
├── scripts/
├── examples/
└── tests/
```

Start with the skill instructions and consult the relevant engineering references and primary sources for the task. [Skill entry point](SKILL.md) · [PCB layout and power](references/layout-power.md) · [Interfaces](references/interfaces.md) · [Interference](references/interference-verification.md) · [Verification](references/engineering-validation.md) · [Source register](references/sources.md)

## Install in a project

These instructions are for local Codex and Claude Code installations. They copy the skill files into the project. CAD tools, MCP servers, models, test software, and permissions are managed separately.

### 1. Clone the repository

```sh
git clone https://github.com/1day0fmylife/pcb-design-emc-skill.git
cd pcb-design-emc-skill
```

Read `SKILL.md` and the relevant references. For a reproducible team setup, check out a reviewed commit or tag and record its SHA in your project documentation. Run the following commands from the root of the skill clone.

### 2. Install for Codex

Set the absolute path to your project.

```sh
project_dir=/absolute/path/to/your-project
skill_target="$project_dir/.agents/skills/pcb-design-review"
mkdir -p "$project_dir/.agents/skills"
# mkdir without -p stops installation when the target directory already exists.
if mkdir "$skill_target"; then
  cp SKILL.md "$skill_target/"
  cp -R references rules profiles schemas scripts "$skill_target/"
fi
```

### 3. Install for Claude Code

```sh
project_dir=/absolute/path/to/your-project
skill_target="$project_dir/.claude/skills/pcb-design-review"
mkdir -p "$project_dir/.claude/skills"
if mkdir "$skill_target"; then
  cp SKILL.md "$skill_target/"
  cp -R references rules profiles schemas scripts "$skill_target/"
fi
```

If you use both agents, install both copies from the same revision. When updating, compare the installed copy with the source and preserve local edits. The installed files are `SKILL.md`, `references/`, `rules/`, `profiles/`, `schemas/`, and `scripts/`.

Open a new agent session in the target project and verify that it discovers the skill. If it is missing, check the path, YAML frontmatter, workspace trust settings, and client policies. For team use, review the installed files before committing them to the project repository.

### Personal installation

Install the same files in the directory for your agent to make the skill available across projects.

| Agent | Directory |
|---|---|
| Codex | `~/.agents/skills/pcb-design-review/` |
| Claude Code | `~/.claude/skills/pcb-design-review/` |

Check the client's discovery rules before installing multiple versions with the same name. Claude.ai, Cowork, and cloud environments have their own installation mechanisms. [Codex documentation](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills) · [Claude Code documentation](https://code.claude.com/docs/en/skills#choose-where-skills-load)

## Use the skill

In Codex:

```text
$pcb-design-review Review the schematic and PCB in hardware/controller.
Check power, placement, return paths, SI/PI, EMI, and publicly documented
measures for reducing compromising emanations. Start with the project
requirements. Include RefDes, nets, layers, evidence, and a verification plan.
Perform a read-only review and leave the CAD files untouched.
```

In Claude Code:

```text
/pcb-design-review Review hardware/controller for power, SI/PI, EMI, and
publicly documented measures for reducing compromising emanations.
Perform a read-only review and leave the CAD files untouched.
```

Other examples:

```text
Review the power tree and prepare a first power-up plan.

Investigate PHY errors during a DC/DC load step. Verify the suspected
coupling path before choosing a fix.

Review DRC coverage and waivers. List board properties that still need
verification.
```

## PCB profile configurator

The local CLI requires Python 3.10 or later, PyYAML 6, and jsonschema 4. [Dependency list](scripts/requirements.txt)

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r scripts/requirements.txt
```

For commands that read native PCB files, use a Python interpreter that can import `pcbnew` from your KiCad installation. Use `probe-kicad` to check availability. The profile configurator and import of an existing DRC JSON report can run without `pcbnew`.

```sh
python3 scripts/pcb_profile.py list --fields
python3 scripts/pcb_profile.py analyze relay-controller
python3 scripts/pcb_profile.py init my-controller --from industrial-24v \
  --description "Relay controller" --feature relay --feature external-ports \
  --output my-controller.yaml
```

In the generated YAML, enter the board revision, features, artifact references, and parameters supported by a document with a known revision and scope. Validate the profile and generate a review plan.

```sh
python3 scripts/pcb_profile.py validate my-controller.yaml
python3 scripts/pcb_profile.py analyze my-controller.yaml --output review-plan.md
python3 scripts/pcb_profile.py analyze my-controller.yaml --format json --output review-plan.json
```

The plan lists applicable rules, rationales, verification methods, recommendations, missing inputs, and the basis for exclusions. It is generated from the profile. Supply project-specific numerical criteria and record actual inspection results separately. [Inheritance, rule selection, and statuses](references/rules-engine.md)

### Relay controller example

The sample profile declares two domain pairs. The plan requests separate `clearance` and `creepage` criteria for each pair and identifies missing inputs. The sample DRC JSON contains an active finding and an exclusion. [Sample profile](examples/relay-review/profile.yaml)

```sh
python3 scripts/pcb_profile.py analyze examples/relay-review/profile.yaml
python3 scripts/pcb_profile.py import-drc examples/relay-review/profile.yaml \
  examples/relay-review/drc.json --context examples/relay-review/drc-context.yaml
python3 scripts/pcb_profile.py validate-report examples/relay-review/profile.yaml \
  examples/relay-review/review.json
python3 scripts/pcb_profile.py export-report examples/relay-review/profile.yaml \
  examples/relay-review/review.json --output relay-review.md
```

The importer reads an existing KiCad DRC v1 JSON report and links objects by UUID using the supplied context. Use `validate-report` to check an engineering report against its profile. [Criteria, context, and evidence formats](references/rules-engine.md)

When an engineering report declares `evidence_artifacts`, `validate-report` verifies the SHA-256 of each local JSON artifact and resolves the records referenced by findings. [Sample report](examples/relay-review/review.json) · [Evidence artifact format](references/rules-engine.md#целостность-артефактов)

`export-report` validates an engineering report and exports it as Markdown, JSON, or SARIF 2.1.0. The exports retain the original statuses, priorities, and confidence levels when present. [Command and export formats](references/rules-engine.md#экспорт-инженерного-отчёта)

An engineering waiver records an ID, owner, rationale, revision, and expiry date against a confirmed `FAIL`. The report retains the `FAIL` status, and validation rejects electrical-safety waivers. [Waiver format](references/rules-engine.md#инженерные-исключения)

A profile can define the minimum copper gap for a net-class pair in a specified operating mode. When importing a KiCad `clearance` finding, the importer can suggest that criterion if both nets are known and both UUIDs are mapped to the corresponding classes. [Net-class pair example](references/rules-engine.md#пары-классов-сетей)

A criterion can record its owner, confirmation date, and values from alternative sources. For conflicting values, the plan asks for a source selection and rationale tied to the current board revision. [Criterion confirmation and source conflicts](references/rules-engine.md#подтверждение-и-расхождения-источников)

### KiCad commands

The DRC importer maps geometry findings to candidate rules. The board inventory records through-hole plating, drill shape, drill dimensions, and drill center. For slots, it also records the long-axis orientation when available. [Mapping table](references/rules-engine.md#сопоставление-геометрии-drc)

`compare-trace-width` reads straight and arc track widths from the PCB and checks them against the profile minimum or maximum. The user selects tracks by UUID. JSON output records the width, criterion, and source. [Example and scope](references/rules-engine.md#сверка-ширины-дорожек)

`compare-copper-edge` measures nominal clearance from selected straight tracks to a closed `Edge.Cuts` outline. It supports segments, rectangles, and board-level circles, including round boards and round cutouts. [Inputs and coverage](references/rules-engine.md#сверка-зазора-меди-до-края)

`compare-copper-gap` measures nominal clearance between straight tracks on different nets on the same copper layer. Use explicit UUID pairs or `--all-pairs` to select pairs from the tracks listed in the context. The profile provides the minimum for the net class or class pair and operating mode. [Example and scope](references/rules-engine.md#сверка-зазора-между-дорожками)

The official KiCad QA board was used to verify DRC export with `kicad-cli` 10.0.8. The importer found two track-width maximum violations and preserved them across different exit-code settings. [Reproducible KiCad run](examples/kicad-multinetclasses/README.md)

`inventory-board path/to/file.kicad_pcb` reads board objects by UUID. The `import-drc --board path/to/file.kicad_pcb` option adds native net names, layer names, and reference designators to findings. Both operations require `pcbnew` in the selected Python environment. [Inventory fields and scope](references/rules-engine.md#чтение-платы-по-uuid)

`compare-netlist` matches a native PCB against a KiCad XML netlist by reference designator, pin number, and net name. Its report shows missing components or pads, unexpected connected pads, and net mismatches. Use a common revision for both files. [Export and comparison example](examples/kicad-netlist/README.md)

`compare-netlist --check-footprints` also compares `Library:Footprint` identifiers for unambiguously matched reference designators. A mismatch records the footprint UUID and both identifiers. Incomplete assignments require review. [Footprint assignment checks](references/rules-engine.md#сверка-назначений-посадочных-мест)

If the installed `pcbnew` cannot determine whether there is copper around an NPTH pad, hole geometry remains available. Netlist comparison marks affected pins `REVIEW_REQUIRED` and retains their pad UUIDs. [Board inventory](references/rules-engine.md#чтение-платы-по-uuid)

`inventory-stackup` reads the physical stackup from the native PCB using local `pcbnew`. JSON records the layer and sublayer order and explicitly stored thicknesses, materials, and dielectric properties. A missing or incomplete stackup is marked `REVIEW_REQUIRED`. [Format and usage](references/rules-engine.md#физический-стек-платы)

`compare-stackup` checks PCB data against the tolerances, layer order, and stated properties in an agreed fabrication stackup sheet. The report records the revision and input file hashes. [Stackup sheet template](examples/fabrication-stackup.example.yaml) · [Comparison procedure](references/rules-engine.md#сверка-стека-с-изготовителем)

`compare-mounting` compares mounting holes selected by UUID against a drawing. Checks cover the center, shape, dimensions, plating, and slot orientation. The report records the revision and input file hashes. [Mechanical criteria template](examples/mechanical-holes.example.yaml) · [Comparison procedure](references/rules-engine.md#сверка-монтажных-отверстий-с-чертежом)

`run-checks` runs local ERC and DRC against a temporary project copy. The output directory contains the original KiCad JSON reports and `summary.json` with version, exit codes, coverage, and the SHA-256 of inputs. [Commands and statuses](references/rules-engine.md#локальный-запуск-erc-и-drc)

`probe-kicad` reports the versions and paths of installed `kicad-cli` and `pcbnew` and checks CLI flags and Python methods used to read boards. Run it with `python3 scripts/pcb_profile.py probe-kicad`. [Compatibility matrix](references/rules-engine.md#совместимость-kicad)

## Inputs and outputs

Provide native schematic and PCB files, a BOM with exact part numbers, the fabrication stackup, datasheets and errata, mechanical and enclosure information, supply and load data, interface requirements, operating conditions, and test criteria. Troubleshooting also requires reproducible conditions and measurements. Keep confidential material out of external services.

With incomplete inputs, continue the checks possible with available evidence and mark unresolved items as TBD. A screenshot can support the review. Native files are needed for checks that depend on CAD geometry and connectivity.

A review records its scope, evidence-backed findings with priorities, confidence in the defect and proposed fix, trade-offs, verification steps, checks already performed, and remaining risks. Apply the project's own ADR, ICD, and sign-off processes.

## Verification and limitations

The repository has undergone checks of the skill structure and local Markdown links, static review of instructions, and independent text-only walkthroughs for three scenarios. These cover incomplete PHY/DC/DC input, a project without an architecture decision record, and DRC violations with exit code 0.

Validate the configurator with `python3 scripts/pcb_profile.py validate` and run `python3 -B -m unittest discover -s tests -v`. Native ERC and DRC were verified on demonstration and QA boards using KiCad 10.0.8. [Repository review summary](PROJECT_REVIEW.md)

GitHub Actions runs schema, catalog, profile, sample report, and test checks on Python 3.10 and 3.14 for pull requests and updates to `main`. Tests that require local KiCad or its demo files skip when those prerequisites are unavailable. [CI workflow](.github/workflows/validate.yml)

The native runs validate the KiCad adapter for the documented scenarios. Client discovery in the target versions of Codex and Claude Code, full engineering review of a product, and bench measurements remain separate tasks.

**Formal EMC and TEMPEST compliance requires an approved program run by an authorized organization.** AI review, ERC/DRC, simulations, and preliminary laboratory checks can provide evidence within their individual scopes. Evaluate information leakage independently from emissions. Keep restricted TEMPEST requirements, keys, confidential parameters, and confidential results out of this repository.

## Roadmap

1. Verify installation in target versions of Codex and Claude Code and record the tested versions.
2. Build public KiCad examples with known defects and expected review results.
3. Verify the ERC/DRC adapter and geometry reader in KiCad 8.0 and 9.0. Extend edge clearance checks to outlines that include arcs.
4. Prepare templates for a power budget, a constraint matrix, and an engineering review report.
5. Obtain specialist SI/PI and EMC review, then refine the rules using measurements.
6. Select and add a license before release under its terms. A license has yet to be selected.

[Development plan](PLAN.md) · [Backlog](TASKS.md)

## Formalized rules and profiles

The machine-readable catalog contains 182 rules in 20 families. Each has a unique PCB-* ID, severity, verification method, and engineering rationale. The catalog is a draft for engineering review. [Rule catalog](rules/catalog.yaml)

- [Geometry, clearance and creepage, routing, mounting, and DFM](references/geometry-mechanical-dfm.md)
- [Capacitors, resistors, diodes, oscillators, inductors, MOSFETs, and thermal design](references/component-placement.md)
- [Relays, coil and contact-load transient suppression, and EMC failure analysis](references/relay-switching-emc.md)
- [Standards and applicability](references/standards-applicability.md)
- [AI and EDA review process, automation scope, and statuses](references/rules-engine.md)
- [Catalog guide](rules/README.md), [profiles](profiles/), [JSON Schema](schemas/), and [example report](examples/review-result.example.json)
- [Development plan](PLAN.md), [backlog](TASKS.md), and [agent instructions](AGENTS.md)

In `profiles/`, a value of `null` marks a parameter as TBD. Obtain mains-voltage clearances, target impedance, clearance and creepage, and current limits from applicable project sources. Record the fabrication, component, and standard revisions and approvals for the board.

Statuses are `PASS`, `FAIL`, `REVIEW_REQUIRED`, `NOT_EVALUATED`, and `N_A`, with the basis for each result. `PASS` requires evidence against a verified, applicable criterion. Formal EMC, electrical-safety, and TEMPEST compliance require separate evaluations.
