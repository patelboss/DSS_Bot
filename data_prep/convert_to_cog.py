"""
data_prep/convert_to_cog.py — One-time utility to convert raw GeoTIFFs
(FCM, FTM, DEM) into Cloud-Optimized GeoTIFFs and upload them to Supabase.

Patched Version: Memory-safe block-by-block streaming for heavy raster layers.
"""

import logging
import os
import tempfile
from pathlib import Path

import click
import rasterio
from rasterio.enums import Resampling
from rasterio.shutil import copy as rio_copy
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

BUCKET  = os.getenv("SUPABASE_BUCKET", "raster-layers")
SB_URL  = os.getenv("SUPABASE_URL")
SB_KEY  = os.getenv("SUPABASE_SERVICE_KEY")

LAYER_MAP = {
    "FCM": os.getenv("COG_FCM",  "fcm.tif"),
    "FTM": os.getenv("COG_FTM",  "ftm.tif"),
    "DEM": os.getenv("COG_DEM",  "dem.tif"),
}


@click.command()
@click.option("--input",  "-i", required=True,  help="Path to source GeoTIFF")
@click.option("--layer",  "-l", required=True,  type=click.Choice(["FCM", "FTM", "DEM"]),
              help="Raster layer type")
@click.option("--upload", "-u", is_flag=True,   default=True,
              help="Upload to Supabase after conversion (default: True)")
def main(input: str, layer: str, upload: bool) -> None:
    """Convert a GeoTIFF to COG and optionally upload it to Supabase Storage."""
    src_path  = Path(input)
    dest_name = LAYER_MAP[layer]

    if not src_path.exists():
        raise click.ClickException(f"Input file not found: {src_path}")

    with tempfile.TemporaryDirectory() as tmp:
        cog_path = Path(tmp) / dest_name
        log.info("Converting '%s' → COG …", src_path.name)

        _convert_to_cog(src_path, cog_path, layer)

        size_mb = cog_path.stat().st_size / (1024 * 1024)
        log.info("COG created: %s (%.1f MB)", cog_path.name, size_mb)

        if upload:
            if not SB_URL or not SB_KEY:
                raise click.ClickException(
                    "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env"
                )
            _upload_to_supabase(cog_path, dest_name)


def _convert_to_cog(src: Path, dest: Path, layer: str) -> None:
    """
    Write a Cloud-Optimized GeoTIFF using block window iterations to prevent
    local machine Out-of-Memory crashes on large datasets.
    """
    with rasterio.open(src) as dataset:
        profile = dataset.profile.copy()

        # Choose compression — lossless DEFLATE for categorical, LZW for DEM
        compress = "DEFLATE" if layer in ("FCM", "FTM") else "LZW"
        predictor = 2 if layer == "DEM" else 1   # horizontal differencing

        profile.update(
            driver="GTiff",
            compress=compress,
            predictor=predictor,
            tiled=True,
            blockxsize=512,
            blockysize=512,
            interleave="band",
            copy_src_overviews=True,
        )

        # Build internal overviews on a temp file first
        overview_path = dest.parent / f"_ov_{dest.name}"
        try:
            # Memory-Safe Write: Stream blocks iteratively instead of doing a full read()
            with rasterio.open(overview_path, "w", **profile) as tmp_ds:
                for ji, window in dataset.block_windows(1):
                    # Loop over all internal raster blocks sequentially
                    data_block = dataset.read(window=window)
                    tmp_ds.write(data_block, window=window)

            # Build structural zoom overviews (2, 4, 8, 16, 32)
            with rasterio.open(overview_path, "r+") as tmp_ds:
                [span_10](start_span)overview_levels = [2, 4, 8]
                tmp_ds.build_overviews(
                    overview_levels,
                    Resampling.nearest if layer in ("FCM", "FTM") else Resampling.average,
                )
                tmp_ds.update_tags(ns="rio_overview", resampling="nearest")

            # Finalize layout structural order
            rio_copy(
                str(overview_path),
                str(dest),
                copy_src_overviews=True,
                driver="GTiff",
                compress=compress,
                predictor=predictor,
                tiled=True,
                blockxsize=512,
                blockysize=512,
            )
        finally:
            if overview_path.exists():
                overview_path.unlink()

    log.info("COG written with overviews (%s compression)", compress)


def _upload_to_supabase(cog_path: Path, object_name: str) -> None:
    """Upload the COG to the Supabase Storage bucket."""
    sb = create_client(SB_URL, SB_KEY)

    log.info("Uploading '%s' to Supabase bucket '%s' …", object_name, BUCKET)
    with open(cog_path, "rb") as f:
        sb.storage.from_(BUCKET).upload(
            path=object_name,
            file=f,
            file_options={
                "content-type": "image/tiff",
                "upsert":       "true",        # overwrite if exists
            },
        )

    log.info("✅  Upload complete → %s", object_name)


if __name__ == "__main__":
    main()
    
