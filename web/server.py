"""Web server for O'Reilly Ingest."""

import json
import logging
import re
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core import Kernel, create_default_kernel
from plugins import ChunkConfig
from plugins.downloader import DownloadProgress
import config

logger = logging.getLogger("oreilly_ingest")


class DownloaderHandler(SimpleHTTPRequestHandler):
    """HTTP request handler for the downloader web interface."""

    kernel: Kernel = None
    download_progress: dict = {}
    _progress_lock = threading.Lock()
    _cancel_requested: bool = False
    _last_logged_progress: tuple | None = None

    @classmethod
    def _set_progress(cls, data: dict):
        """Thread-safe progress replacement."""
        with cls._progress_lock:
            cls.download_progress = data

    @classmethod
    def _update_progress(cls, **kwargs):
        """Thread-safe progress update."""
        with cls._progress_lock:
            cls.download_progress.update(kwargs)

    def __init__(self, *args, **kwargs):
        self.static_dir = Path(__file__).parent / "static"
        super().__init__(*args, directory=str(self.static_dir), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/status":
            self._handle_status()
        elif path == "/api/search":
            params = parse_qs(parsed.query)
            query = params.get("q", params.get("query", [""]))[0]
            self._handle_search(query)
        elif match := re.match(r"/api/book/([^/]+)/chapters$", path):
            self._handle_chapters_list(match.group(1))
        elif match := re.match(r"/api/book/([^/]+)$", path):
            self._handle_book_info(match.group(1))
        elif path == "/api/progress":
            self._handle_progress()
        elif path == "/api/settings":
            self._handle_get_settings()
        elif path == "/api/formats":
            self._handle_formats()
        else:
            super().do_GET()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        data = json.loads(body) if body else {}

        if self.path == "/api/download":
            self._handle_download(data)
        elif self.path == "/api/cookies":
            self._handle_cookies(data)
        elif self.path == "/api/cancel":
            self._handle_cancel()
        elif self.path == "/api/reveal":
            self._handle_reveal(data)
        elif self.path == "/api/settings/output-dir":
            self._handle_set_output_dir(data)
        else:
            self._send_json({"error": "Not found"}, 404)

    def _handle_status(self):
        auth = self.kernel["auth"]
        status = auth.get_status()
        logger.info("Auth status checked: valid=%s reason=%s", status.get("valid"), status.get("reason"))
        self._send_json(status)

    def _handle_search(self, query: str):
        if not query:
            self._send_json({"results": []})
            return

        book = self.kernel["book"]
        results = book.search(query)
        logger.info("Search completed for query=%r with %d result(s)", query, len(results))
        self._send_json({"results": results})

    def _handle_book_info(self, book_id: str):
        book = self.kernel["book"]
        try:
            info = book.fetch(book_id)
            logger.info("Loaded book metadata for book_id=%s", book_id)
            self._send_json(info)
        except Exception as e:
            logger.error("Failed to load book metadata for book_id=%s: %s", book_id, e)
            self._send_json({"error": str(e)}, 400)

    def _handle_chapters_list(self, book_id: str):
        """Return list of chapters for chapter selection UI."""
        chapters_plugin = self.kernel["chapters"]
        try:
            chapters = chapters_plugin.fetch_list(book_id)
            logger.info("Loaded %d chapter(s) for book_id=%s", len(chapters), book_id)
            result = {
                "chapters": [
                    {
                        "index": i,
                        "title": ch.get("title", f"Chapter {i + 1}"),
                        "pages": ch.get("virtual_pages"),
                        "minutes": ch.get("minutes_required"),
                    }
                    for i, ch in enumerate(chapters)
                ],
                "total": len(chapters),
            }
            self._send_json(result)
        except Exception as e:
            logger.error("Failed to load chapters for book_id=%s: %s", book_id, e)
            self._send_json({"error": str(e)}, 400)

    def _handle_progress(self):
        with self._progress_lock:
            self._send_json(dict(self.download_progress))

    def _handle_get_settings(self):
        """Return current settings."""
        self._send_json(
            {
                "output_dir": str(config.OUTPUT_DIR),
            }
        )

    def _handle_formats(self):
        """Return available output formats for discovery.

        This endpoint allows any client (web, CLI, etc.) to discover
        supported formats, aliases, and which formats support chapter selection.
        """
        from plugins.downloader import DownloaderPlugin
        self._send_json(DownloaderPlugin.get_formats_info())

    def _handle_set_output_dir(self, data: dict):
        """Handle output directory selection - browse or direct path."""
        system_plugin = self.kernel["system"]
        output_plugin = self.kernel["output"]

        if data.get("browse"):
            # Open native folder picker dialog
            initial_dir = config.OUTPUT_DIR
            selected = system_plugin.show_folder_picker(initial_dir)
            if selected:
                self._send_json({"success": True, "path": str(selected)})
            else:
                self._send_json({"cancelled": True})
            return

        path_str = data.get("path", "").strip()

        if not path_str:
            self._send_json({"error": "path required"}, 400)
            return

        success, message, path = output_plugin.validate_dir(path_str)
        if not success:
            self._send_json({"error": message}, 400)
            return

        self._send_json({"success": True, "path": str(path)})

    def _handle_cookies(self, data: dict):
        """Save cookies from user input."""
        if not isinstance(data, dict) or not data:
            logger.warning("Rejected invalid cookie payload")
            self._send_json({"error": "Invalid cookie data"}, 400)
            return

        try:
            config.COOKIES_FILE.write_text(json.dumps(data, indent=2))
            self.kernel.http.reload_cookies()
            logger.info("Saved %d cookie(s) to %s", len(data), config.COOKIES_FILE)
            self._send_json({"success": True})
        except Exception as e:
            logger.error("Failed to save cookies: %s", e)
            self._send_json({"error": str(e)}, 500)

    def _handle_cancel(self):
        """Request cancellation of the current download."""
        with self._progress_lock:
            status = self.download_progress.get("status")
            if status and status not in ("completed", "error", "cancelled"):
                DownloaderHandler._cancel_requested = True
                logger.warning("Cancellation requested for active download")
                self._send_json({"success": True, "message": "Cancel requested"})
            else:
                logger.info("Cancellation requested but no active download was running")
                self._send_json({"success": False, "message": "No active download"})

    def _handle_reveal(self, data: dict):
        """Open file manager and select the specified file."""
        path_str = data.get("path", "")
        if not path_str:
            logger.warning("Reveal request rejected: missing path")
            self._send_json({"error": "path required"}, 400)
            return

        path = Path(path_str).resolve()

        if not path.exists():
            logger.warning("Reveal request rejected: path does not exist: %s", path)
            self._send_json({"error": "Path does not exist"}, 404)
            return

        system_plugin = self.kernel["system"]
        success = system_plugin.reveal_in_file_manager(path)

        if success:
            logger.info("Revealed path in file manager: %s", path)
            self._send_json({"success": True})
        else:
            logger.error("Failed to reveal path in file manager: %s", path)
            self._send_json({"error": "Failed to reveal file"}, 500)

    def _handle_download(self, data: dict):
        """Start a book download."""
        book_id = data.get("book_id")
        output_format = data.get("format", "epub")
        selected_chapters = data.get("chapters")
        output_dir_str = data.get("output_dir")
        chunking_opts = data.get("chunking", {})
        skip_images = data.get("skip_images", False)

        if not book_id:
            logger.warning("Download request rejected: missing book_id")
            self._send_json({"error": "book_id required"}, 400)
            return

        # Parse chunking config
        chunk_config = None
        if chunking_opts:
            chunk_size = chunking_opts.get("chunk_size", 4000)
            overlap = chunking_opts.get("overlap", 200)
            chunk_config = ChunkConfig(
                chunk_size=chunk_size,
                overlap=overlap,
                respect_boundaries=True,
            )

        # Validate output directory
        output_plugin = self.kernel["output"]
        if output_dir_str:
            success, message, output_dir = output_plugin.validate_dir(output_dir_str)
            if not success:
                logger.error("Download request rejected for book_id=%s: invalid output dir: %s", book_id, message)
                self._send_json({"error": message}, 400)
                return
        else:
            output_dir = output_plugin.get_default_dir()

        # Check if already downloading
        with self._progress_lock:
            status = self.download_progress.get("status")
            if status and status not in ("completed", "error", "cancelled"):
                logger.warning("Download request rejected for book_id=%s: another download is already in progress", book_id)
                self._send_json({"error": "Download already in progress"}, 409)
                return

        # Parse formats using plugin (single source of truth)
        from plugins.downloader import DownloaderPlugin
        formats = DownloaderPlugin.parse_formats(output_format)
        chapter_count = len(selected_chapters) if isinstance(selected_chapters, list) else "all"
        logger.info(
            "Starting download for book_id=%s formats=%s chapters=%s output_dir=%s skip_images=%s",
            book_id,
            ",".join(formats),
            chapter_count,
            output_dir,
            skip_images,
        )

        # Start download in background thread
        thread = threading.Thread(
            target=self._download_book_async,
            args=(book_id, output_dir, formats, selected_chapters, skip_images, chunk_config),
            daemon=True,
        )
        thread.start()

        # Return immediately
        self._send_json({"status": "started", "book_id": book_id})

    def _download_book_async(
        self,
        book_id: str,
        output_dir: Path,
        formats: list[str],
        selected_chapters: list | None,
        skip_images: bool,
        chunk_config: ChunkConfig | None,
    ):
        """Background download wrapper with error handling."""
        # Reset cancel flag
        DownloaderHandler._cancel_requested = False

        try:
            downloader = self.kernel["downloader"]
            result = downloader.download(
                book_id=book_id,
                output_dir=output_dir,
                formats=formats,
                selected_chapters=selected_chapters,
                skip_images=skip_images,
                chunk_config=chunk_config,
                progress_callback=self._on_progress,
                cancel_check=lambda: DownloaderHandler._cancel_requested,
            )

            self._set_progress(
                {
                    "status": "completed",
                    "book_id": result.book_id,
                    "title": result.title,
                    "percentage": 100,
                    **result.files,
                }
            )
            logger.info(
                "Download completed for book_id=%s title=%r files=%s",
                result.book_id,
                result.title,
                ",".join(sorted(result.files.keys())),
            )
        except Exception as e:
            error_msg = str(e)
            if "cancelled" in error_msg.lower():
                self._set_progress({"status": "cancelled", "error": error_msg})
                logger.warning("Download cancelled for book_id=%s", book_id)
            else:
                self._set_progress({"status": "error", "error": error_msg})
                logger.error("Download failed for book_id=%s: %s", book_id, error_msg)

    def _on_progress(self, progress: DownloadProgress):
        """Handle progress updates from the downloader plugin."""
        self._set_progress(
            {
                "status": progress.status,
                "book_id": progress.book_id,
                "percentage": progress.percentage,
                "message": progress.message,
                "eta_seconds": progress.eta_seconds,
                "current_chapter": progress.current_chapter,
                "total_chapters": progress.total_chapters,
                "chapter_title": progress.chapter_title,
            }
        )
        progress_key = (
            progress.status,
            progress.percentage,
            progress.current_chapter,
            progress.total_chapters,
            progress.chapter_title,
            progress.message,
        )
        if progress_key != self._last_logged_progress:
            self._last_logged_progress = progress_key
            suffix = ""
            if progress.total_chapters:
                suffix = f" ({progress.current_chapter}/{progress.total_chapters})"
            extras = []
            if progress.chapter_title:
                extras.append(progress.chapter_title)
            if progress.message:
                extras.append(progress.message)
            extra_text = f" - {' | '.join(extras)}" if extras else ""
            logger.info(
                "Download progress: %s%% %s%s%s",
                progress.percentage,
                progress.status,
                suffix,
                extra_text,
            )

    def _send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        request_line = args[0] if args else ""
        if "/api/progress" in request_line:
            return
        logger.info("HTTP %s", request_line)


def create_server(host: str = "localhost", port: int = 8000) -> HTTPServer:
    """Create and configure the HTTP server."""
    kernel = create_default_kernel()
    DownloaderHandler.kernel = kernel

    server = HTTPServer((host, port), DownloaderHandler)
    return server


def validate_startup_dependencies():
    """Validate required runtime dependencies before starting the server."""
    kernel = create_default_kernel()
    kernel["pdf"].check_dependencies()


def run_server(host: str = "localhost", port: int = 8000):
    """Start the HTTP server."""
    server = create_server(host, port)
    logger.info("Server running at http://%s:%s", host, port)
    server.serve_forever()
