#!/usr/bin/env python3
"""
prepare_data.py - turn the ToS;DR Zenodo CSVs into ClauseFinder retrieval files.

Usage:
    python prepare_data.py --data data/raw --map category_map.json --out data/processed

--data must contain documents.csv, points.csv and services.csv from
https://zenodo.org/records/15012282

Outputs (in --out):
    corpus/passages.jsonl  Pyserini JsonCollection input, one {"id", "contents"} per line.
                           Point pyserini.index.lucene --input at the corpus/ folder.
    passages_meta.csv      Where each passage sits: doc, service, character offsets, word count.
    qrels.txt              MP1 format, one "<qid> <passage_id> 1" per relevant passage.
                           qid = "<doc_id>:<category>" (one query per document and category).
    eval_pairs.csv         Every (document, category) pair: number of relevant passages and
                           a dev/test split (split by service, so no service is in both).
    queries.tsv            Starter query phrasings per category, "<category>\t<phrasing>".
                           Meant to be edited.
    stats.json             Counts for the progress report.

How passages and labels are built:
    The documents are stored as raw HTML, and ToS;DR quote positions (quote_start/quote_end)
    are character offsets into that raw HTML. So documents are split into blocks on the RAW
    text (keeping each block's offsets), labels are assigned by offset overlap, and tags are
    stripped from each block only afterwards. Stripping HTML first would shift every offset.
"""
import argparse
import bisect
import csv
import hashlib
import html
import json
import os
import re
import sys
from collections import Counter, defaultdict

import pandas as pd

csv.field_size_limit(sys.maxsize)

# Tags that start a new block of text. Inline tags (<a>, <strong>, <span>...) do not.
BLOCK_TAG = re.compile(
    r"<\s*/?\s*(?:p|div|li|ul|ol|h[1-6]|br|hr|tr|td|th|table|thead|tbody|section|article|"
    r"header|footer|nav|main|aside|blockquote|dd|dt|dl|pre|form)\b[^>]*>",
    re.I,
)
ANY_TAG = re.compile(r"<[a-zA-Z/!][^>]*>")
NEWLINES = re.compile(r"\n+")
# Places a long block may be cut: after sentence punctuation or at a line break.
LONG_CUT = re.compile(r"(?<=[.!?;])\s+|\n+")
WS = re.compile(r"\s+")

STARTER_QUERIES = {
    "arbitration": [
        "binding arbitration",
        "you agree to resolve disputes through arbitration instead of in court",
        "waive your right to participate in a class action lawsuit",
        "disputes will be resolved individually",
    ],
    "recurring_charges": [
        "we will automatically charge your payment method on a recurring basis",
        "subscription renews automatically until you cancel",
        "recurring billing",
        "you authorize us to charge your credit card",
    ],
    "data_sharing": [
        "we share your personal information with third parties",
        "we may sell your personal data",
        "disclose personal information to partners and advertisers",
        "third parties may receive information about you",
    ],
    "content_license": [
        "you grant us a worldwide royalty-free license to use your content",
        "license to use reproduce modify and distribute content you post",
        "perpetual irrevocable sublicensable license",
        "content you upload or submit to the service",
    ],
    "terms_changes": [
        "we may change these terms at any time",
        "we may modify or update this agreement",
        "your continued use means you accept the revised terms",
        "we will notify you of material changes to these terms",
    ],
}


def clean(raw):
    """Strip tags, decode HTML entities, collapse whitespace."""
    text = ANY_TAG.sub(" ", raw)
    text = html.unescape(text)
    return WS.sub(" ", text).strip()


def norm(s):
    return WS.sub(" ", str(s)).strip().lower()


def raw_blocks(text):
    """Split raw document text into (start, end) spans at block tags (or newlines for
    documents without HTML structure)."""
    boundaries = list(BLOCK_TAG.finditer(text))
    if len(boundaries) < 3:
        boundaries = list(NEWLINES.finditer(text))
    spans, prev = [], 0
    for m in boundaries:
        if m.start() > prev:
            spans.append((prev, m.start()))
        prev = m.end()
    if prev < len(text):
        spans.append((prev, len(text)))
    return spans


def split_long(text, start, end, max_words):
    """Cut a block longer than max_words at sentence ends / line breaks, packing pieces
    back together up to max_words. Yields (start, end) spans."""
    if len(clean(text[start:end]).split()) <= max_words:
        yield (start, end)
        return
    pieces, prev = [], start
    for m in LONG_CUT.finditer(text, start, end):
        if m.start() > prev:
            pieces.append((prev, m.start()))
        prev = m.end()
    if prev < end:
        pieces.append((prev, end))
    cur_s, cur_e, cur_n = None, None, 0
    for s, e in pieces:
        n = len(clean(text[s:e]).split())
        if cur_s is not None and cur_n + n > max_words:
            yield (cur_s, cur_e)
            cur_s, cur_n = None, 0
        if cur_s is None:
            cur_s = s
        cur_e, cur_n = e, cur_n + n
    if cur_s is not None:
        yield (cur_s, cur_e)


def make_passages(text, min_words, max_words):
    """Return a list of [start, end, cleaned_text, n_words] passages for one document.
    Short blocks (headings, menu items) are merged forward into the next block."""
    blocks = []
    for s, e in raw_blocks(text):
        for s2, e2 in split_long(text, s, e, max_words):
            t = clean(text[s2:e2])
            if t:
                blocks.append([s2, e2, t, len(t.split())])
    out, buf = [], None
    for b in blocks:
        if buf is None:
            buf = list(b)
        else:
            buf = [buf[0], b[1], buf[2] + " " + b[2], buf[3] + b[3]]
        if buf[3] >= min_words:
            out.append(buf)
            buf = None
    if buf is not None:
        if out and buf[3] < min_words:
            last = out[-1]
            out[-1] = [last[0], buf[1], last[2] + " " + buf[2], last[3] + buf[3]]
        else:
            out.append(buf)
    # Drop passages with no letters at all (pure numbers/punctuation).
    return [p for p in out if re.search(r"[A-Za-z]", p[2])]


def locate_quote(text, qs, qe, quote):
    """Return (start, end, method) for a quote, or None if it can't be found.
    Trusts the stored offsets when they match the quote (ignoring whitespace),
    otherwise searches the document for the quote text."""
    if 0 <= qs < qe <= len(text):
        span, q = norm(text[qs:qe]), norm(quote)
        if span == q or ((q in span or span in q) and abs(len(span) - len(q)) < 5):
            return qs, qe, "offsets"
    idx = text.find(quote)
    if idx >= 0:
        return idx, idx + len(quote), "exact_search"
    prefix = quote.strip()[:60]
    if len(prefix) >= 20:
        idx = text.find(prefix)
        if idx >= 0:
            return idx, idx + len(quote.strip()), "prefix_search"
    return None


EN_WORDS = set("the and of to you your we our or in for is that with by this any are be not may".split())


def looks_english(passages, threshold=0.12):
    """Rough check: share of very common English words. English legal text is usually
    well above 0.2; other languages are near 0."""
    words = [w for p in passages for w in re.findall(r"[a-z]+", p[2].lower())]
    if not words:
        return False
    return sum(w in EN_WORDS for w in words) / len(words) >= threshold


def split_for(service_id, dev_fraction):
    h = int(hashlib.md5(str(service_id).encode()).hexdigest(), 16)
    return "dev" if (h % 1000) < dev_fraction * 1000 else "test"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="folder with documents.csv, points.csv, services.csv")
    ap.add_argument("--map", required=True, help="category_map.json")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--scope", choices=["reviewed", "all"], default="reviewed",
                    help="reviewed = only comprehensively reviewed services (default); all = every service")
    ap.add_argument("--min-words", type=int, default=8, help="merge blocks shorter than this into the next one")
    ap.add_argument("--max-words", type=int, default=250, help="cut blocks longer than this")
    ap.add_argument("--dev-fraction", type=float, default=0.25)
    ap.add_argument("--english-only", action="store_true",
                    help="skip documents that don't look English (about 8%% of reviewed documents)")
    args = ap.parse_args()

    with open(args.map) as f:
        cmap = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    case_to_cat = {cid: cat for cat, v in cmap.items() for cid in v["case_ids"]}
    categories = list(cmap)

    print("Loading services and documents...")
    services = pd.read_csv(os.path.join(args.data, "services.csv"), low_memory=False)
    services = services[services["status"].isna()]
    if args.scope == "reviewed":
        services = services[services["is_comprehensively_reviewed"] == True]  # noqa: E712
    service_ids = set(services["id"])

    docs = pd.read_csv(os.path.join(args.data, "documents.csv"),
                       usecols=["id", "name", "service_id", "status", "text"], low_memory=False)
    docs = docs[docs["status"].isna() & docs["text"].notna() & docs["service_id"].isin(service_ids)]
    docs = docs[docs["text"].str.strip().str.len() > 0]

    print("Loading points...")
    points = pd.read_csv(os.path.join(args.data, "points.csv"), low_memory=False)
    points = points[(points["status"] == "approved") & points["case_id"].isin(case_to_cat)
                    & points["quote_text"].notna() & points["document_id"].notna()]
    points = points.assign(document_id=points["document_id"].astype(int))
    points = points[points["document_id"].isin(set(docs["id"]))]
    points_by_doc = defaultdict(list)
    for r in points.itertuples():
        points_by_doc[r.document_id].append(r)

    os.makedirs(os.path.join(args.out, "corpus"), exist_ok=True)
    stats = Counter()
    align = Counter()
    rel = defaultdict(set)  # (doc_id, category) -> set of passage ids
    meta_rows = []
    skipped_docs = set()

    print(f"Splitting {len(docs)} documents into passages...")
    with open(os.path.join(args.out, "corpus", "passages.jsonl"), "w") as fout:
        for d in docs.itertuples():
            text = d.text
            passages = make_passages(text, args.min_words, args.max_words)
            if args.english_only and not looks_english(passages):
                stats["skipped_non_english"] += 1
                skipped_docs.add(d.id)
                continue
            starts = [p[0] for p in passages]
            for i, (s, e, t, n) in enumerate(passages):
                pid = f"{d.id}_{i}"
                fout.write(json.dumps({"id": pid, "contents": t}) + "\n")
                meta_rows.append((pid, d.id, d.service_id, d.name, s, e, n))
            stats["documents"] += 1
            stats["passages"] += len(passages)

            for pt in points_by_doc.get(d.id, []):
                cat = case_to_cat[pt.case_id]
                qs = int(pt.quote_start) if pd.notna(pt.quote_start) else -1
                qe = int(pt.quote_end) if pd.notna(pt.quote_end) else -1
                loc = locate_quote(text, qs, qe, pt.quote_text)
                if loc is None:
                    align["not_found"] += 1
                    continue
                qs, qe, method = loc
                align[method] += 1
                need = min(30, 0.5 * (qe - qs))
                # passages are sorted by start; check those that could overlap [qs, qe)
                j = max(0, bisect.bisect_right(starts, qs) - 1)
                hit = False
                while j < len(passages) and passages[j][0] < qe:
                    ps, pe = passages[j][0], passages[j][1]
                    overlap = min(pe, qe) - max(ps, qs)
                    if overlap > 0 and overlap >= need:
                        rel[(d.id, cat)].add(f"{d.id}_{j}")
                        hit = True
                    j += 1
                if not hit:
                    align["no_passage_overlap"] += 1

    meta = pd.DataFrame(meta_rows, columns=["passage_id", "doc_id", "service_id", "doc_name",
                                            "start_char", "end_char", "n_words"])
    meta.to_csv(os.path.join(args.out, "passages_meta.csv"), index=False)

    # qrels.txt in MP1 format: "<qid> <docid> <rel>"
    with open(os.path.join(args.out, "qrels.txt"), "w") as f:
        for (doc_id, cat), pids in sorted(rel.items()):
            for pid in sorted(pids, key=lambda x: int(x.split("_")[1])):
                f.write(f"{doc_id}:{cat} {pid} 1\n")

    # Every (document, category) pair, labelled or not, with a dev/test split by service.
    pairs = []
    for d in docs.itertuples():
        if d.id in skipped_docs:
            continue
        split = split_for(d.service_id, args.dev_fraction)
        for cat in categories:
            pairs.append((f"{d.id}:{cat}", d.id, d.service_id, cat,
                          len(rel.get((d.id, cat), ())), split))
    pairs = pd.DataFrame(pairs, columns=["qid", "doc_id", "service_id", "category", "n_relevant", "split"])
    pairs.to_csv(os.path.join(args.out, "eval_pairs.csv"), index=False)

    with open(os.path.join(args.out, "queries.tsv"), "w") as f:
        for cat in categories:
            for q in STARTER_QUERIES.get(cat, [cat.replace("_", " ")]):
                f.write(f"{cat}\t{q}\n")

    labelled = pairs[pairs["n_relevant"] > 0]
    report = {
        "scope": args.scope,
        "services": int(docs.loc[~docs["id"].isin(skipped_docs), "service_id"].nunique()),
        "documents": stats["documents"],
        "skipped_non_english": stats["skipped_non_english"],
        "passages": stats["passages"],
        "passage_words": {k: round(float(v), 1) for k, v in meta["n_words"].describe().items()},
        "approved_points_for_categories": int((~points["document_id"].isin(skipped_docs)).sum()),
        "quote_alignment": dict(align),
        "per_category": {
            cat: {
                "points": int(((points["case_id"].map(case_to_cat) == cat)
                               & ~points["document_id"].isin(skipped_docs)).sum()),
                "documents_with_label": int((labelled["category"] == cat).sum()),
                "relevant_passages": int(labelled.loc[labelled["category"] == cat, "n_relevant"].sum()),
                "dev_pairs": int(((labelled["category"] == cat) & (labelled["split"] == "dev")).sum()),
                "test_pairs": int(((labelled["category"] == cat) & (labelled["split"] == "test")).sum()),
            }
            for cat in categories
        },
        "labelled_pairs": int(len(labelled)),
    }
    with open(os.path.join(args.out, "stats.json"), "w") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
