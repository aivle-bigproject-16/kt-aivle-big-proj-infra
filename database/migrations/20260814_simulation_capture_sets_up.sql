BEGIN;

SELECT pg_advisory_xact_lock(hashtext('20260814_simulation_capture_sets'));

ALTER TABLE public.battery_cell_image
    ADD COLUMN IF NOT EXISTS capture_set varchar(20);

UPDATE public.battery_cell_image
SET capture_set = 'INITIAL'
WHERE capture_set IS NULL;

ALTER TABLE public.battery_cell_image
    ALTER COLUMN capture_set SET DEFAULT 'INITIAL',
    ALTER COLUMN capture_set SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_battery_cell_image_capture_set'
          AND conrelid = 'public.battery_cell_image'::regclass
    ) THEN
        ALTER TABLE public.battery_cell_image
            ADD CONSTRAINT ck_battery_cell_image_capture_set
            CHECK (capture_set IN ('INITIAL', 'RECAPTURE', 'ARCHIVED'))
            NOT VALID;
    END IF;
END
$$;

ALTER TABLE public.battery_cell_image
    VALIDATE CONSTRAINT ck_battery_cell_image_capture_set;

CREATE INDEX IF NOT EXISTS ix_battery_cell_image_capture_source
    ON public.battery_cell_image (
        battery_cell_id,
        image_type,
        capture_set,
        id
    );

CREATE TABLE IF NOT EXISTS public.ops_backup_sim_image_20260814_v2 AS
SELECT image.*
FROM public.battery_cell_image AS image
JOIN public.battery_cell AS cell ON cell.id = image.battery_cell_id
WHERE cell.cell_serial_no LIKE 'SIM-%';

COMMENT ON TABLE public.ops_backup_sim_image_20260814_v2 IS
    'Immutable pre-v2 snapshot of SIM battery_cell_image rows';

CREATE TABLE IF NOT EXISTS public.ops_simulation_seed_migration (
    version varchar(80) PRIMARY KEY,
    manifest_sha256 varchar(64) NOT NULL,
    row_count integer NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

COMMIT;
