import os
import sys

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from scrapers.Loughborough_townhall.run_extractor import LoughboroughtownhallExtractor  # noqa: E402
from utils.logger import setup_logger  # noqa: E402

logger = setup_logger("test_loughborough_townhall", log_to_file=False)


def test_loughborough_townhal_pipeline():
    logger.info("Starting loughborough_townhall Pipeline Test")
    extractor = LoughboroughtownhallExtractor(
        local_test=False,
        show_count=2,
        save_csv_locally=True,
        csv_incremental_mode=False,
    )
    result = extractor.run()
    logger.info("Pipeline Result: %s", result)


if __name__ == "__main__":
    test_loughborough_townhal_pipeline()
