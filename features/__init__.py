import logging
from . import url_download
from . import telegram
from . import google_drive
from . import local_files
from . import proxy_fetcher
from . import subtitle_finder
from . import video_extractor
from . import video_clipper
from . import torrent_search
from . import face_swap
from . import ocr

logger = logging.getLogger(__name__)

def register_all_features(app):
    """Register all feature blueprints/routes."""
    logger.info("Registering features...")
    url_download.register_routes(app)
    telegram.register_routes(app)
    google_drive.register_routes(app)
    local_files.register_routes(app)
    proxy_fetcher.register_routes(app)
    subtitle_finder.register_routes(app)
    video_extractor.register_routes(app)
    video_clipper.register_routes(app)
    torrent_search.register_routes(app)
    face_swap.register_routes(app)
    ocr.register_rountes(app)
    logger.info("All features registered")
