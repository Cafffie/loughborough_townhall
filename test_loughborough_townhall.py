import os
import sys

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from scrapers.elgiva.run_extractor import ElgivaExtractor  # noqa: E402
from utils.logger import setup_logger  # noqa: E402

logger = setup_logger("test_elgiva", log_to_file=False)


def test_elgiva_pipeline():
    logger.info("Starting Elgiva Pipeline Test")
    extractor = ElgivaExtractor(
        local_test=False,
        show_count=None,
        save_csv_locally=True,
        csv_incremental_mode=False,
    )
    result = extractor.run()
    logger.info("Pipeline Result: %s", result)


if __name__ == "__main__":
    test_elgiva_pipeline()
