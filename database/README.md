# Simulation seed v2

Apply in this order:

1. Run `migrations/20260814_simulation_capture_sets_up.sql`.
2. Deploy the backend that filters `INITIAL` and `RECAPTURE` capture sets.
3. Validate the versioned CSV with `scripts/apply-simulation-seed.py`.
4. Run the same command with `--execute` and verify the four count groups.

The up migration snapshots all existing `SIM-%` image rows in
`ops_backup_sim_image_20260814_v2`. The loader archives those rows instead of
deleting them, so existing `inspection_image` foreign keys remain valid.

The down migration refuses to delete v2 rows after an inspection references
them. In that case keep the capture-set-aware backend and restore through a new
forward migration.
