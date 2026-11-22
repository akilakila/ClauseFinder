# ClauseFinder data prep

`prepare_data.py` turns the raw ToS;DR dataset into the files our BM25 pipeline (adapted from MP1) needs.

**The processed files are already committed in `data/processed/`**, so you can use them without running anything. You only need the steps below if you change `category_map.json` or the script's settings.

## Requirements

- Python 3.9+ (tested on 3.13)
- pandas 2.2+ (tested on 2.2.3 and 3.0.5, identical output): `pip install "pandas>=2.2"`

## Reproduce the processed data

1. Download `documents.csv`, `points.csv` and `services.csv` from https://zenodo.org/records/15012282 into `data/raw/`. This folder is gitignored; never commit it (`documents.csv` is ~268 MB).

2. Run:
   ```
   python prepare_data.py --data data/raw --map category_map.json --out data/processed --english-only
   ```

3. Check you got the same files:
   ```
   python -c "import hashlib,sys;[print(hashlib.md5(open(f,'rb').read()).hexdigest(),f) for f in sys.argv[1:]]" data/processed/qrels.txt data/processed/eval_pairs.csv data/processed/corpus/passages.jsonl data/processed/stats.json
   ```
   Expected:
   ```
   50102521b8cdbe094a144ec4ae6afeef  qrels.txt
   041476ff24f875383096b0f46094e0ad  eval_pairs.csv
   13468f15bc89afe18d1bfed61f1d3665  corpus/passages.jsonl
   e46308478694aa9c0b4f1276168b5056  stats.json
   ```
   If they differ, check you used `--english-only`, the committed `category_map.json`, and default settings. (On Windows, Git line-ending conversion can change the committed files' checksums without changing their content.)

If the team changes the data, commit the new `data/processed/` along with the change, but avoid unnecessary recommits: Git keeps every version (~37 MB each).

## Output files (`data/processed/`)

| File | MP1 equivalent | Contents |
|---|---|---|
| `corpus/passages.jsonl` | `processed_corpus/` | One passage per line: `{"id": "<doc_id>_<n>", "contents": "..."}`. Index the `corpus/` folder with Pyserini. |
| `qrels.txt` | `*-qrels.txt` | `<qid> <passage_id> 1`, where qid = `<doc_id>:<category>` |
| `queries.tsv` | `*-queries.txt` | Starter phrasings: `<category>\t<phrasing>` |
| `eval_pairs.csv` | | Every (document, category) pair, its number of relevant passages, and dev/test split |
| `passages_meta.csv` | | Each passage's document, service, raw character offsets, and word count |
| `stats.json` | | Counts for the report: 428 services, 1,214 documents, 101,234 passages, 833 labelled pairs |

## How it works

1. Keeps non-deleted, comprehensively reviewed services and their non-empty documents.
2. Keeps approved reviewer quotes whose case is in `category_map.json`.
3. Splits each document's raw HTML into passages at block tags (`<p>`, `<li>`, headings...), merging blocks under 8 words into the next and cutting blocks over 250 words at sentence ends. Tags are stripped from the stored text, but each passage keeps its raw HTML offsets.
4. Skips non-English documents (113, about 8%).
5. Marks a passage relevant if it overlaps a quote by 30+ characters (or half of a short quote). Quote offsets point into the raw HTML, which is why step 3 keeps raw offsets. Misaligned quotes are found by text search; 34 of 1,210 couldn't be found.
6. Splits services 25% dev / 75% test using a hash of the service ID, so the split is the same everywhere.

## Limitations

- Labels are incomplete; some "wrong" results will actually be correct.
- ToS;DR has no auto-renewal case, so `recurring_charges` uses case 384 (recurring card charges). It's the smallest category (53 documents).
- The regex baseline needs `docbot_regex` from `cases.csv`, and the interface may need company names from `services.csv`. Neither is in the processed files yet.
- July 2023 snapshot; some policies have changed since.

## Source and license

Derived from the ToS;DR policies dataset (21/07/2023) by Terms of Service; Didn't Read, https://doi.org/10.5281/zenodo.15012282, licensed GPL-3.0 or later. The processed files are shared under the same license.
