"""Shared numeric constants used across ingestion, storage, and web layers.

Anything imported here is a knob you might want to turn without hunting
through multiple files. Storage layer, run.py, and app.py all read from
the same value to keep behaviour coherent.
"""

# Articles at or above this score get two things:
#   1. Bilingual translation on ingest (run.py _should_translate + storage
#      articles_needing_translation).
#   2. The 「抓取到的正文」 block rendered on the detail page (templates/
#      article.html).
# Lower-scored articles are still fully indexed (FTS + matched_keywords) so
# search hits them normally — the detail page just omits the body and points
# readers at the original URL, and translation is skipped to save API cost.
SCORE_THRESHOLD_FULL_BODY = 4
