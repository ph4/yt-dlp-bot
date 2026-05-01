#!/usr/bin/env python3.12
"""Telegram bot for downloading YouTube audio (single tracks & playlists)."""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import FSInputFile, MediaUnion

BOT_TOKEN = os.environ.get("BOT_TOKEN")
YT_DLP = shutil.which("yt-dlp") or ""
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB Telegram limit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("aiogram").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

if not BOT_TOKEN:
    msg = "BOT_TOKEN environment variable is not set"
    logger.critical(msg)
    raise SystemExit(1)

if not YT_DLP:
    msg = "yt-dlp not found in PATH"
    logger.critical(msg)
    raise SystemExit(1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

URL_RE = re.compile(
    r"(?:https?://)?"
    r"(?:www\.|music\.|m\.)?"
    r"(?:youtube\.com/(?:watch\?v=|playlist\?list=|shorts/|live/|embed/|e/)"
    r"|youtu\.be/)"
    r"[\w\-_?&=./+%#]+"
)


def is_playlist_url(url: str) -> bool:
    return bool(re.search(r"youtube\.com/playlist\?list=", url))


async def _run_subprocess(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    logger.debug("Running: %s", " ".join(cmd))
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=timeout),
    )


async def download_track(url: str, out_dir: str) -> str | None:
    cmd = [
        YT_DLP,
        "-f",
        "140",
        "--embed-metadata",
        "--embed-thumbnail",
        "-o",
        f"{out_dir}/%(title)s.%(ext)s",
        "--print",
        "after_move:filepath",
        url,
    ]
    try:
        proc = await _run_subprocess(cmd)
    except subprocess.TimeoutExpired:
        logger.warning("Timeout downloading %s", url)
        return None

    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        logger.warning("Download failed for %s: %s", url, stderr[-500:])
        return None

    filepath = proc.stdout.strip().split("\n")[-1].strip()
    if filepath and os.path.exists(filepath):
        return filepath

    logger.warning("File not found after download: %s", filepath)
    return None


async def get_playlist_info(url: str) -> tuple[str, list[str]]:
    cmd = [
        YT_DLP,
        "--flat-playlist",
        "--print",
        "playlist_title",
        "--print",
        "webpage_url",
        url,
    ]
    try:
        proc = await _run_subprocess(cmd, timeout=60)
    except subprocess.TimeoutExpired:
        logger.warning("Timeout fetching playlist %s", url)
        return ("Unknown Playlist", [])

    lines = [line for line in proc.stdout.strip().split("\n") if line]
    if not lines:
        return ("Unknown Playlist", [])
    return (lines[0], lines[1:])


@dp.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    await message.answer(
        "Send me a YouTube link (single track or playlist) "
        "and I'll download the audio for you.\n\n"
        "Supported: m4a format, with embedded metadata + album art."
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message) -> None:
    await message.answer(
        "Just send a YouTube URL — single video or playlist. "
        "I'll download the best m4a audio (AAC 129k) with metadata and cover art."
    )


@dp.message()
async def handle_url(message: types.Message) -> None:
    text = message.text or ""
    match = URL_RE.search(text)
    if not match:
        return

    url = match.group(0)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        if is_playlist_url(url):
            await handle_playlist(message, url)
        else:
            await handle_single(message, url)
    except Exception:
        logger.exception("Unhandled error processing %s", url)
        await message.answer("An unexpected error occurred. Please try again later.")


async def handle_single(message: types.Message, url: str) -> None:
    status = await message.answer("Downloading...")

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = await download_track(url, tmpdir)
        if not filepath:
            await status.edit_text("Failed to download. Check the URL and try again.")
            return

        file_size = os.path.getsize(filepath)
        if file_size > MAX_FILE_SIZE:
            await status.edit_text(
                f"File too large ({file_size / 1024 / 1024:.1f} MB > 50 MB limit)"
            )
            return

        await status.edit_text("Uploading...")
        doc = FSInputFile(filepath)
        await message.reply_document(doc)

    await status.delete()


async def handle_playlist(message: types.Message, url: str) -> None:
    status = await message.answer("Fetching playlist info...")
    title, urls = await get_playlist_info(url)

    if not urls:
        await status.edit_text("Could not parse playlist. Check the URL.")
        return

    success_count = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        media_group: list[MediaUnion] = []
        for i, track_url in enumerate(urls, 1):
            await status.edit_text(f"**{title}** — {i}/{len(urls)}\nDownloading...")
            filepath = await download_track(track_url, tmpdir)
            if not filepath:
                await message.answer(f"Failed: track {i}")
                continue

            file_size = os.path.getsize(filepath)
            if file_size > MAX_FILE_SIZE:
                await message.answer(f"Track {i} too large, skipping")
                os.unlink(filepath)
                continue

            success_count += 1
            media_group.append(
                types.InputMediaDocument(
                    media=FSInputFile(filepath),
                    caption=os.path.basename(filepath),
                )
            )

        if media_group:
            chunks = [media_group[j : j + 10] for j in range(0, len(media_group), 10)]
            for j, chunk in enumerate(chunks, 1):
                await status.edit_text(f"**{title}** — uploading batch {j}/{len(chunks)}...")
                await message.answer_media_group(chunk)

        await status.edit_text(f"**{title}** — done! ({success_count}/{len(urls)} tracks)")


async def main() -> None:
    logger.info("Starting bot polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
