BEGIN;

SELECT pg_advisory_xact_lock(hashtext('20260817_simulation_image_3d_metadata'));

ALTER TABLE public.battery_cell_image
    ADD COLUMN IF NOT EXISTS volume bigint,
    ADD COLUMN IF NOT EXISTS "index" bigint,
    ADD COLUMN IF NOT EXISTS axis varchar(20);

ALTER TABLE public.battery_cell_image
    DROP CONSTRAINT IF EXISTS ck_battery_cell_image_axis;

ALTER TABLE public.battery_cell_image
    ADD CONSTRAINT ck_battery_cell_image_axis
    CHECK (axis IS NULL OR axis IN ('x', 'y', 'z')) NOT VALID;

ALTER TABLE public.battery_cell_image
    VALIDATE CONSTRAINT ck_battery_cell_image_axis;

UPDATE public.battery_cell
SET cell_size = '[100, 254, 871]'::jsonb
WHERE cell_serial_no LIKE 'SIM-%'
  AND (cell_size IS NULL OR cell_size = '{}'::jsonb);

COMMIT;
