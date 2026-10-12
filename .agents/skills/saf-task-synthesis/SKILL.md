---
name: saf-task-synthesis
description: Derive new Harbor scientific tasks from an existing task and reference solution or genuine successful trajectory. Prepare SAF seed bundles and run Builder plus preflight Auditor without Distiller or Solver.
---

# SAF task synthesis

## Capability

Choose SAF when the new task should inherit a concrete scientific method and reference experience. Raw materials may be a Harbor task, loose task instructions, code and data, or a genuine successful trajectory. Organize supplied evidence into the native input; a Harbor package is not required. Paper-only material without a reference method may fit Sci or Omega instead. Abstract workflow transfer to an independent problem fits TB Science.

## Prepare inputs

Create a seed directory with `task_card.json` describing the original objective, public inputs/outputs and environment, plus `seed_bundle.json`:

```json
{"schema_version":"saf.seed.v2","seed_id":"scientific-seed","task_card":"task_card.json","solution_path":"solution","successful_trajectory":null,"environment_dir":null,"input_dir":null,"software_dir":null}
```

Paths are relative to the seed root and must reference actual files/directories; do not use symlinks. Supply at least a reference solution or genuine successful trajectory. `strategy_seed` requires `solution_path`. Optional directory fields can be null. Copy original data and dependencies without inventing reference solutions or success evidence. A supplied Harbor package should retain its file relationships.

Create YAML configuration using `pipeline.workflow_mode: audited`, a bounded `pipeline.task_count`, and `allow_host_full_access: true` only within authorized host access. Keep native audit/revision budgets. `reuse_seed` environment strategy requires a complete seed environment directory. TTF uses the host redundancy target for `pipeline.task_count`. Model settings and credentials belong to the caller's environment, not seed files. See [native defaults](../../../science-agent-factory/src/science_agent_factory/resources/default.yaml) for optional settings and [seed example](../../../science-agent-factory/examples/seed_bundle/task_card.json) if existing material needs clarification.

## End-to-end entrypoint

In TTF hosted mode, prepare files under the assigned run and submit `saf.run` with `seed` and `config` paths, followed by `tasks.collect` with `pipeline: saf`. The host owns execution; do not run commands or substitute full `saf run`/Solver workflows in the planner.

The host calls the native API in the SAF interpreter:

```python
import asyncio
from pathlib import Path
from science_agent_factory.contracts.settings import FactorySettings
from science_agent_factory.workflow.factory import ScienceAgentFactory

settings = FactorySettings.load(Path(config))
settings.storage_root = Path(output_root)
factory = ScienceAgentFactory(settings)
state = asyncio.run(factory.generate_tasks(Path(seed), job_id="generation"))
```

A Harbor JobConfig must be available; the TTF adapter supplies a default when absent. `generate_tasks` runs Builder, preflight Auditor and native feedback revisions; it skips Distiller and independent Solver. This entrypoint creates no solver training trajectory.

## Results

Read the job's `state.json`, candidate `task_dir` paths and corresponding preflight audit reports. Only `preflight_accepted` with actual accepted audit evidence is eligible. Preserve rejection reports and failures; exit success alone is insufficient. Solver `attempts` must remain empty. Preflight acceptance is not an independent Oracle proof. In TTF, native evidence stays under `native/saf/`; the host collects eligible tasks into `tasks/` and writes `task_manifest.json`.
