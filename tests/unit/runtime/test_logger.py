from __future__ import annotations

from runtime.logger import get_logger, setup_logging


def test_setup_logging_writes_info_to_console_and_debug_to_file(tmp_path, capsys):
    log_file = setup_logging(tmp_path)
    logger = get_logger("crawler.test")

    logger.debug("debug detail")
    logger.info("info summary")
    for handler in logger.parent.handlers:
        handler.flush()

    captured = capsys.readouterr()
    assert "info summary" in captured.err
    assert "debug detail" not in captured.err

    content = log_file.read_text(encoding="utf-8")
    assert "debug detail" in content
    assert "[DEBUG] [yanclaw.crawler.test]" in content
