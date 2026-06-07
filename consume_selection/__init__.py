"""consume-selection — local consume-index over the Readwise firehose.

Stage 1 (this slice):
  - db.py          : the consume.db schema (items + ratings tables)
  - ingest.py      : rw-tools-backed ingestion of Readwise documents into `items`
  - groundtruth.py : pull every `_rating/*` tag into `ratings`, keyed by rater

The index is a READ-MODEL. It never writes content into recall.it or Hopswiki.
"""

__version__ = "0.1.0"
