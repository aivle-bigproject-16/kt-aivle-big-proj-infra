BEGIN;

SELECT pg_advisory_xact_lock(hashtext('20260817_simulation_image_3d_metadata'));

ALTER TABLE public.battery_cell_image
    DROP CONSTRAINT IF EXISTS ck_battery_cell_image_axis,
    DROP COLUMN IF EXISTS axis,
    DROP COLUMN IF EXISTS "index",
    DROP COLUMN IF EXISTS volume;

COMMIT;
