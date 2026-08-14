BEGIN;

SELECT pg_advisory_xact_lock(hashtext('20260814_simulation_capture_sets'));

DO $$
BEGIN
    IF to_regclass('public.ops_backup_sim_image_20260814_v2') IS NULL THEN
        RAISE EXCEPTION 'pre-migration backup table is missing';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.battery_cell_image AS image
        JOIN public.battery_cell AS cell ON cell.id = image.battery_cell_id
        JOIN public.inspection_image AS inspection_image
          ON inspection_image.battery_cell_image_id = image.id
        LEFT JOIN public.ops_backup_sim_image_20260814_v2 AS backup
          ON backup.id = image.id
        WHERE cell.cell_serial_no LIKE 'SIM-%'
          AND backup.id IS NULL
    ) THEN
        RAISE EXCEPTION
            'v2 seed rows are referenced by inspections; preserve the new backend and data';
    END IF;
END
$$;

DELETE FROM public.battery_cell_image AS image
USING public.battery_cell AS cell
WHERE cell.id = image.battery_cell_id
  AND cell.cell_serial_no LIKE 'SIM-%'
  AND NOT EXISTS (
      SELECT 1
      FROM public.ops_backup_sim_image_20260814_v2 AS backup
      WHERE backup.id = image.id
  );

UPDATE public.battery_cell_image AS image
SET capture_set = backup.capture_set
FROM public.ops_backup_sim_image_20260814_v2 AS backup
WHERE image.id = backup.id;

-- Match every seed version, not just the one that existed when this migration
-- was written. A pinned version leaves a newer ledger row behind after a
-- rollback, which then claims a seed is applied that is no longer in the table.
DELETE FROM public.ops_simulation_seed_migration
WHERE version LIKE 'simulation-runtime-%';

COMMIT;
