# Printer to-do lists in Home Assistant

What each printer needs — calibrations, due maintenance, repairs of open faults, manual tasks —
comes from 3d-api ([3d-cli #281](https://github.com/alex-mextner/3d-cli/issues/281),
`3d printer todo`); [#285](https://github.com/alex-mextner/3d-cli/issues/285) shows it in Home
Assistant. Source: `packages/printops_tasks.yaml`.

## Entities

| Entity | What |
|---|---|
| `sensor.printops_k1se_tasks`, `sensor.printops_u1_tasks` | state = open tasks (snoozed ones not counted); attributes `summary` (`open`, `blocking`, `waiting`, `snoozed`, `needs_human`, `next_idle`) and `tasks` (each with `id`, `title`, `why`, `type`, `head`, `slot`, `blocks`, `when`, `duration_min`, `needs_human`, `waiting_on`, `source`, `done_hint`, …). REST, every 60 s, from `GET /v1/printers/{p}/tasks`. |
| `todo.printops_k1se`, `todo.printops_u1` | the open tasks as a to-do list (Local To-do). Tick an item: the task is done in 3d-api. Add an item: it becomes a manual task. |
| `rest_command.printops_task_done` | `task` (id or uid), `note?` → `POST /v1/tasks/{task}/done` |
| `rest_command.printops_task_snooze` | `task`, `until?` (ISO date/time; none = until reopened), `reason?` |
| `rest_command.printops_task_add` | `printer`, `title`, `why?` → a manual task |
| `script.printops_todo_sync` | `printer`: makes the to-do list equal to 3d-api's open tasks |

## Why a Local To-do list plus a sync script

Home Assistant has no YAML-configured to-do platform (no `template` to-do entity), and a custom
integration would need an HA restart, which `3d ha deploy` never does. So the to-do entity is a
**Local To-do** list per printer, and `automation.printops_todo_sync` keeps it equal to 3d-api:

- on every change of the sensors (a 3d-api read) or of the lists (an item ticked or added), at HA
  start and every 10 minutes it runs `script.printops_todo_sync` for the printer;
- each synced item carries its task id first in the description (`#12 · BLOCKS head T2 · …`, then
  why and, for derived tasks, the command that closes it);
- a ticked item → `rest_command.printops_task_done`; on 200 the item is removed. A derived task
  (fault repair, due rule, calibration) answers 409: the item is unticked and a persistent
  notification shows the command that records the work (that record closes the task);
- an item without an id (typed in HA) → `rest_command.printops_task_add`, then the item is replaced
  by the synced one;
- an item whose task is no longer open in 3d-api is removed; an open task without an item is added.

Updates follow 3d-api within the sensor's 60 s poll (a tick in HA is sent at once). Push within
seconds of a `printer_task.*` event needs the WebSocket client of the planned `printops` custom
integration (plan §12 phase 4).

## One-time setup (before the first deploy of this package)

1. A 3d-api token with scopes `read,write`:
   `3d printops token create ha-tasks --scopes read,write --ssh ultra@192.168.0.39`.
2. In the HA repo's `secrets.yaml`: `printops_api_auth: "Bearer <token>"`. Without it the HA
   configuration check fails and `3d ha deploy` rolls back.
3. Settings → Devices & services → Add integration → **Local To-do**, list name `printops_k1se`;
   again with `printops_u1`. The entity ids must be `todo.printops_k1se` and `todo.printops_u1`.
   Until a list exists the sync skips that printer.

## Checking it

- Developer tools → States: `sensor.printops_u1_tasks` shows the open count; its `tasks`
  attribute lists the U1 T2 repair task (blocks head T2).
- Developer tools → Actions: `rest_command.printops_task_snooze` with `task: <id>` → the task
  leaves the open count at the next poll; `3d printer todo reopen <id>` brings it back.
- To-do lists → `printops_u1`: tick a manual item → `3d printer todo list u1 --all` shows it done;
  tick the T2 repair item → it unticks and a notification names
  `3d printops maint fault close 1 --fixed-by MAINT_ID`.
