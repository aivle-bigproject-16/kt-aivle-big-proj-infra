#!/usr/bin/env python3
"""Clone the approved SIM cell group so a run can request hundreds of cells.

The simulation seed provisions SIM-0001..SIM-0020 only, so
`POST /sim` rejects any `batteryCellCount` above the group size before the AI
gateway is ever called. This script appends further SIM cells that reuse the
image rows of the recorded group, cycling through the group without
replacement and restarting at the first template whenever it is exhausted.
The recorded cells themselves are never modified.

Clones inherit the template timestamps instead of the current time. The battery
list API pages by `created_at` descending, so clones stamped with `now()` would
fill the first page and hide every inspected cell. The script also realigns
clones that an earlier run stamped incorrectly.

The REPLAY service maps the cloned cells back onto the recorded results with
the same group-and-reset rule (see `replay/README.md`).
"""

import argparse
import os
import re


DEFAULT_DB_URL = (
    "postgresql://aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"
    "?sslmode=require"
)
DEFAULT_DB_USERNAME = "postgres.raybvdyfljopcfnhxiaq"
CELL_PREFIX = "SIM-"
SERIAL_PATTERN = re.compile(r"^SIM-(\d+)$")
ADVISORY_LOCK_KEY = "20260818_simulation_cell_pool"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target-cells",
        type=int,
        required=True,
        help="total number of SIM cells the database should hold",
    )
    parser.add_argument(
        "--group-size",
        type=int,
        default=None,
        help=(
            "how many recorded cells act as templates; "
            "defaults to every SIM cell that already exists"
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="commit the transaction; the default is a rollback-only dry run",
    )
    return parser.parse_args()


def serial_number(serial):
    match = SERIAL_PATTERN.match(serial)
    if match is None:
        raise ValueError(f"unexpected simulation cell serial: {serial}")
    return int(match.group(1))


def format_serial(number, width=4):
    return f"{CELL_PREFIX}{number:0{width}d}"


def template_serials(existing_serials, group_size=None):
    """Return (all serials in order, the template group)."""
    ordered = sorted(set(existing_serials), key=serial_number)
    if not ordered:
        raise ValueError("no SIM cells exist; run the simulation seed first")

    templates = ordered[: group_size or len(ordered)]
    if not templates:
        raise ValueError("template group is empty")
    return ordered, templates


def build_clone_plan(existing_serials, target_cells, group_size=None):
    """Return [(new_serial, template_serial)] needed to reach target_cells.

    Templates are consumed in order and the rotation resets once the group is
    used up, so cell 21 clones the first recorded cell again when the group
    holds 20 cells.
    """
    if target_cells < 1:
        raise ValueError("target cell count must be positive")

    ordered, templates = template_serials(existing_serials, group_size)

    width = max(len(serial) - len(CELL_PREFIX) for serial in ordered)
    taken = set(ordered)
    plan = []
    number = 1
    while len(taken) + len(plan) < target_cells:
        serial = format_serial(number, width)
        if serial not in taken:
            plan.append((serial, templates[(number - 1) % len(templates)]))
        number += 1
    return plan


def connection_string():
    url = os.getenv("DB_URL", DEFAULT_DB_URL)
    if url.startswith("jdbc:"):
        url = url.removeprefix("jdbc:")
    return url


def apply(target_cells, group_size, execute):
    # Imported lazily so the planning logic stays testable without a driver.
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(
        connection_string(),
        user=os.getenv("DB_USERNAME", DEFAULT_DB_USERNAME),
        password=os.environ["DB_PASSWORD"],
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (ADVISORY_LOCK_KEY,),
            )
            cursor.execute(
                """
                SELECT cell_serial_no
                FROM public.battery_cell
                WHERE cell_serial_no LIKE %s
                ORDER BY cell_serial_no
                """,
                (CELL_PREFIX + "%",),
            )
            existing = [row["cell_serial_no"] for row in cursor.fetchall()]
            plan = build_clone_plan(existing, target_cells, group_size)
            _, templates = template_serials(existing, group_size)

            created_cells = 0
            copied_images = 0
            for new_serial, template_serial in plan:
                cursor.execute(
                    """
                    INSERT INTO public.battery_cell (
                        cell_serial_no,
                        purchase_id,
                        product_id,
                        model_name,
                        cell_type,
                        manufactured_date,
                        cell_size,
                        created_at,
                        updated_at
                    )
                    SELECT
                        %(new_serial)s,
                        template.purchase_id,
                        template.product_id,
                        template.model_name,
                        template.cell_type,
                        template.manufactured_date,
                        template.cell_size,
                        template.created_at,
                        template.updated_at
                    FROM public.battery_cell AS template
                    WHERE template.cell_serial_no = %(template_serial)s
                    ON CONFLICT (cell_serial_no) DO NOTHING
                    """,
                    {
                        "new_serial": new_serial,
                        "template_serial": template_serial,
                    },
                )
                created_cells += cursor.rowcount

                cursor.execute(
                    """
                    INSERT INTO public.battery_cell_image (
                        battery_cell_id,
                        image_type,
                        capture_set,
                        bucket_name,
                        object_key,
                        storage_type,
                        file_name,
                        file_size,
                        content_type,
                        width,
                        height,
                        volume,
                        "index",
                        axis,
                        created_at
                    )
                    SELECT
                        clone.id,
                        image.image_type,
                        image.capture_set,
                        image.bucket_name,
                        image.object_key,
                        image.storage_type,
                        image.file_name,
                        image.file_size,
                        image.content_type,
                        image.width,
                        image.height,
                        image.volume,
                        image."index",
                        image.axis,
                        now()
                    FROM public.battery_cell_image AS image
                    JOIN public.battery_cell AS template
                      ON template.id = image.battery_cell_id
                    JOIN public.battery_cell AS clone
                      ON clone.cell_serial_no = %(new_serial)s
                    WHERE template.cell_serial_no = %(template_serial)s
                      AND image.capture_set IN ('INITIAL', 'RECAPTURE')
                    ON CONFLICT (battery_cell_id, bucket_name, object_key)
                    DO NOTHING
                    """,
                    {
                        "new_serial": new_serial,
                        "template_serial": template_serial,
                    },
                )
                copied_images += cursor.rowcount

            # Clones must not look newer than the cells they were copied from.
            # The battery list API pages by created_at DESC, so freshly stamped
            # clones would fill the first page and hide every inspected cell.
            cursor.execute(
                """
                UPDATE public.battery_cell AS clone
                SET created_at = baseline.created_at,
                    updated_at = baseline.updated_at
                FROM (
                    SELECT min(created_at) AS created_at,
                           min(updated_at) AS updated_at
                    FROM public.battery_cell
                    WHERE cell_serial_no = ANY(%(templates)s)
                ) AS baseline
                WHERE clone.cell_serial_no LIKE %(prefix)s
                  AND clone.cell_serial_no <> ALL(%(templates)s)
                  AND clone.created_at > baseline.created_at
                """,
                {"templates": templates, "prefix": CELL_PREFIX + "%"},
            )
            realigned_cells = cursor.rowcount

            cursor.execute(
                """
                SELECT count(*) AS cells
                FROM public.battery_cell
                WHERE cell_serial_no LIKE %s
                """,
                (CELL_PREFIX + "%",),
            )
            total_cells = cursor.fetchone()["cells"]

            cursor.execute(
                """
                SELECT count(*) AS images
                FROM public.battery_cell_image AS image
                JOIN public.battery_cell AS cell
                  ON cell.id = image.battery_cell_id
                WHERE cell.cell_serial_no LIKE %s
                  AND image.capture_set IN ('INITIAL', 'RECAPTURE')
                """,
                (CELL_PREFIX + "%",),
            )
            total_images = cursor.fetchone()["images"]

            if total_cells < target_cells:
                raise RuntimeError(
                    f"expected {target_cells} SIM cells but found {total_cells}"
                )

        if execute:
            connection.commit()
        else:
            connection.rollback()

        return {
            "planned": len(plan),
            "createdCells": created_cells,
            "copiedImages": copied_images,
            "realignedCells": realigned_cells,
            "totalCells": total_cells,
            "totalImages": total_images,
        }


def main():
    args = parse_args()
    summary = apply(args.target_cells, args.group_size, args.execute)
    mode = "executed" if args.execute else "dry-run"
    print(
        f"{mode}: planned={summary['planned']} "
        f"createdCells={summary['createdCells']} "
        f"copiedImages={summary['copiedImages']} "
        f"realignedCells={summary['realignedCells']} "
        f"totalCells={summary['totalCells']} "
        f"totalImages={summary['totalImages']}"
    )


if __name__ == "__main__":
    main()
