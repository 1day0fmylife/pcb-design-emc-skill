# Instructions for AI agents working on this repository

1. Keep `SKILL.md` frontmatter name `pcb-design-review` stable: preserve compatibility with existing Codex/Claude Code installations.
2. Work in Russian in user-facing engineering reports; IDs, paths, schema fields remain English.
3. `rules/catalog.yaml` is a draft policy registry, **not** an approved normative ruleset; refer to `references/rules-engine.md`, source documents, real project profiles and datasheets. Do not invent numeric limits.
4. Review existing files first. Prefer minimal non-destructive changes; do not rename existing references without updating all links and installation instructions.
5. For every rule provide a stable ID, applicability, physical reason, evidence / verification method and correction; do not declare PASS without a verified applicable criterion.
6. Treat a missing input as REVIEW_REQUIRED or NOT_EVALUATED, not PASS. Do not confuse P0 priority with confirmed failure.
7. Maintain separation between electrical safety, functional signal/power integrity, EMC emissions, EMC immunity and TEMPEST-like information security; none is certified by this skill.
8. Preserve approved schematics, footprints, net mappings, and mechanical constraints. No CAD writes, tool installation, external uploads or test execution on powered hardware without user authorization and required safety processes.
9. For hazardous voltages, isolation and pulse tests give only review criteria; competent engineers and test labs must validate. Do not publish restricted methods or confidential design information.
10. Update README/PLAN/TASKS and validate schema, links, and catalog IDs after changes. Distinguish code/test execution from documentation-only review.
