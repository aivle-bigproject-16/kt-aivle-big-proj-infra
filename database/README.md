# Simulation seed v3

## The manifest

The loader takes a CSV that maps S3 objects onto cells and capture sets. It is
versioned next to the images it references:

```
s3://kt-aivle-big-proj-kks/simulations/server-simulation-v1.5/manifests/runtime-case-balanced-images-v3.csv
s3://kt-aivle-big-proj-kks/simulations/server-simulation-v1.5/manifests/runtime-case-balanced-images-v3.csv.sha256
```

Download both and confirm the digest before running anything against the
database. The `.sha256` sidecar is in `sha256sum` format, so it verifies in
place:

```bash
BASE=s3://kt-aivle-big-proj-kks/simulations/server-simulation-v1.5/manifests
aws s3 cp $BASE/runtime-case-balanced-images-v3.csv .
aws s3 cp $BASE/runtime-case-balanced-images-v3.csv.sha256 .
sha256sum --check runtime-case-balanced-images-v3.csv.sha256
```

The same digest is recorded in `ops_simulation_seed_migration.manifest_sha256`
after a successful load, so the applied manifest can always be identified later.
The digest of the manifest currently in S3 is:

```
455182a445cf2a8b8821c4164e174093435c383ca79819a9374a1e2add211374
```

The superseded `runtime-representative-images-v2.csv` is still in the same
prefix. Do not load it: it gives every cell the same defective RGB set, so all
twenty cells reject identically and the run tells you nothing.

## Rebuilding the manifest

`scripts/build-simulation-manifest.py` regenerates it from the defect labels
published alongside the images, and is deterministic — the same labels produce
a byte-identical CSV:

```bash
python scripts/build-simulation-manifest.py --download -o runtime-case-balanced-images-v3.csv
```

`--download` fetches the four label archives it needs from
`s3://kt-aivle-big-proj-kks/simulations/server-simulation-v1.5/metadata-zips/`
into `.cache/simulation-labels`. It prints the case distribution it produced,
which is the part the loader cannot check for you.

## What "case balanced" means

The v1.5 source concentrates every defect in two cells. All 500 defective RGB
frames belong to SIM-0001 and SIM-0002, and the only three defective CT frames
belong to SIM-0001; the other 34,000 images are clean. Assigning each cell only
its own images therefore caps a run at two rejecting cells, so the generator
assigns images across cells deliberately. That is allowed because the unique
constraint is `(battery_cell_id, bucket_name, object_key)`, which scopes an
object to a cell rather than to the table.

The resulting spread is five cells per quadrant, with the defect count stepped
inside each group so a run crosses more than one decision point:

|Cells|CT|RGB|Expected|
|---|---|---|---|
|SIM-0001 – SIM-0005|clean|clean|PASS / PASS|
|SIM-0006 – SIM-0010|clean|1, 2, 3, 5, 10 defects|PASS / REJECT|
|SIM-0011 – SIM-0015|1, 2, 3, 1, 2 defects|clean|REJECT / PASS|
|SIM-0016 – SIM-0020|1, 2, 3, 1, 2 defects|1, 2, 3, 5, 10 defects|REJECT / REJECT|

All three defect types the source contains are covered: `porosity` on CT,
`Pollution` and `Damaged` on RGB. CT density stops at three because the source
holds exactly three defective CT frames, and `Damaged` is placed on three named
cells because only three such frames exist and filename ordering never reaches
them.

Note that `quality_label` is validated but never inserted — it is not one of the
columns the loader writes. Whether a recapture actually triggers depends on the
runtime quality model rejecting a frame, not on the label in this file.

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

The down migration refuses to delete seeded rows after an inspection references
them. In that case keep the capture-set-aware backend and restore through a new
forward migration.

Note that the down migration also deletes every `ops_simulation_seed_migration`
row, and with it the `manifest_sha256` of the applied manifest. The digest is
also in the `.sha256` sidecar in S3, so this is recoverable, but record it
first if the manifest you applied was never published there.

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
