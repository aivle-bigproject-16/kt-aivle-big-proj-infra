# Simulation seed v2

## The manifest

The loader takes a CSV that maps S3 objects onto cells and capture sets. It is
versioned next to the images it references:

```
s3://kt-aivle-big-proj-kks/simulations/server-simulation-v1.5/manifests/runtime-representative-images-v2.csv
s3://kt-aivle-big-proj-kks/simulations/server-simulation-v1.5/manifests/runtime-representative-images-v2.csv.sha256
```

Download both and confirm the digest before running anything against the
database. The `.sha256` sidecar is in `sha256sum` format, so it verifies in
place:

```bash
aws s3 cp s3://kt-aivle-big-proj-kks/simulations/server-simulation-v1.5/manifests/runtime-representative-images-v2.csv .
aws s3 cp s3://kt-aivle-big-proj-kks/simulations/server-simulation-v1.5/manifests/runtime-representative-images-v2.csv.sha256 .
sha256sum --check runtime-representative-images-v2.csv.sha256
```

The same digest is recorded in `ops_simulation_seed_migration.manifest_sha256`
after a successful load, so the applied manifest can always be identified later.
The digest of the manifest currently in S3 is:

```
a7011a855690643cfbbe20c06fb62de28d71e1116f298b3e0e765440a2d7d1b5
```

The manifest holds 880 rows built from only 82 distinct objects, because all
twenty cells share one representative image set. The unique constraint is
`(battery_cell_id, bucket_name, object_key)`, so reuse across cells does not
collide.

## Apply in this order

1. Run `migrations/20260814_simulation_capture_sets_up.sql`.
2. Deploy the backend that filters `INITIAL` and `RECAPTURE` capture sets.
3. Validate the manifest with `scripts/apply-simulation-seed.py <manifest>`.
   Without `--execute` the loader performs the whole load and rolls it back, so
   a clean dry run proves the real run will also pass.
4. Run the same command with `--execute` and verify the four count groups:
   `INITIAL/CT 400`, `INITIAL/RGB 400`, `RECAPTURE/CT 40`, `RECAPTURE/RGB 40`.

The loader needs `DB_PASSWORD`, and takes `DB_URL` and `DB_USERNAME` from the
environment when the defaults in the script do not apply.

The count check runs inside the transaction, so a load that does not reach
exactly those four groups is rolled back rather than half-applied.

## What the migrations do

The up migration snapshots all existing `SIM-%` image rows in
`ops_backup_sim_image_20260814_v2`. The loader archives those rows instead of
deleting them, so existing `inspection_image` foreign keys remain valid.

The down migration refuses to delete v2 rows after an inspection references
them. In that case keep the capture-set-aware backend and restore through a new
forward migration.

Note that the down migration also deletes the `ops_simulation_seed_migration`
row, and with it the `manifest_sha256` of the applied manifest. Record that
digest before rolling back if it is the only copy you have.

## Verifying a live database

The seed is correct when every simulation cell carries twenty `INITIAL` images
per modality. Row totals alone do not prove this, because a cell can be missing
entirely while the totals still match:

```sql
SELECT c.cell_serial_no,
       count(*) FILTER (WHERE i.image_type = 'CT')  AS ct,
       count(*) FILTER (WHERE i.image_type = 'RGB') AS rgb
FROM public.battery_cell c
JOIN public.battery_cell_image i
  ON i.battery_cell_id = c.id AND i.capture_set = 'INITIAL'
WHERE c.cell_serial_no LIKE 'SIM-%'
GROUP BY 1
HAVING count(*) FILTER (WHERE i.image_type = 'CT')  <> 20
    OR count(*) FILTER (WHERE i.image_type = 'RGB') <> 20;
```

An empty result means every cell is covered. A cell that appears here cannot
complete capture: `SimulationCaptureService` rejects the batch with
`400 BAD_REQUEST "<TYPE> <SET> 원본 이미지가 없습니다"`, the orchestrator
exhausts its three retries, and the inspections stay in `PENDING` instead of
moving to `ANALYZING`.
