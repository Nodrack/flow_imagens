"""Validate task 5 against local moderation, without changing the production DB/files."""
import json
import gc
import sqlite3
import sys
import tempfile
from pathlib import Path
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flowapp.config import load_config
from flowapp.database import Database
from flowapp.similarity import SimilarityChecker, matches_for_asset


def main():
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config.json")
    results = []
    # Run migrations on a temporary snapshot, never on the live database.
    with tempfile.TemporaryDirectory(prefix="flow-similarity-") as temporary:
        snapshot = Path(temporary) / "flow.sqlite3"
        with closing(sqlite3.connect((root / "data" / "flow.sqlite3").as_uri()+"?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(snapshot)) as destination:
                source.backup(destination)
        database = Database(snapshot)
        checker = SimilarityChecker(database, root / "data", config.similarity_rules)
        records = database.list_moderation()
        for title in ("The Filament Dandelion", "The Transistor Mycelium"):
            record = next((row for row in records if row["title"] == title and row.get("concept_id")), None)
            if record is None:
                raise SystemExit(f"Referência de moderação ausente: {title}")
            assets = [a for a in database.list_assets(record["concept_id"]) if a["role"] == "upscaled"]
            if not assets:
                raise SystemExit(f"Arquivo final não registrado: {title}")
            asset = assets[-1]
            report = checker.report(record["collection_id"], asset=asset, title=title)
            related = matches_for_asset(report, asset["id"])
            approved_ids = {row["id"] for row in records if row["status"] == "approved"
                            and row["collection_id"] == record["collection_id"]}
            accepted_matches = [match for match in related if match["right"].get("record_id") in approved_ids]
            results.append({"title": title, "passed": bool(accepted_matches),
                            "approved_matches": accepted_matches,
                            "missing_history": report["missing_history"], "unreadable": report["unreadable"]})
            print(f"{title}: {'PASS' if accepted_matches else 'FAIL'}")
            for match in accepted_matches:
                print(f"  {match['right']['title']}: " + " ".join(match["reasons"]))
        # Database's transaction contexts are GC-managed; release SQLite handles before Windows cleanup.
        del database, checker
        gc.collect()
    output = root / "data" / "similarity-validation.json"
    output.write_text(json.dumps({"rules": config.similarity_rules.model_dump(), "results": results},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Relatório local: {output}")
    if not all(result["passed"] for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
